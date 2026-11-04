"""Weekly job that merges the RYO and SAS ACH draft files into one balance sheet and emails it."""

# Standard library imports
from csv import reader, writer
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from logging import getLogger
from pathlib import PurePosixPath
from re import compile
from typing import TYPE_CHECKING, TypedDict, override

# Third party imports
from dateutil.relativedelta import SA, SU, relativedelta
from dateutil.rrule import DAILY, rrule
from pandas import Series, concat, isna, read_csv, to_numeric

# First party imports
from aeth_ext.errors.err_handling import alert
from aeth_ext.types import EmailMessageParts
from aeth_ext.utils import batch_send_emails, prepare_email_message, today
from scheduled_report_aggregator.environment_init_vars import SETTINGS
from scheduled_report_aggregator.jobs.base import CanRescheduleJobError, FTPHandlerKey, JobBase, JobError

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterable, Mapping
  from pathlib import Path
  from re import Pattern
  from typing import ClassVar, Literal

logger = getLogger(__name__)


__all__ = ["BalanceSheetJob"]


@dataclass
class FileVars:
  pickup_folder: PurePosixPath
  filename_pattern_factory: Callable[[datetime | None], Pattern[str]]
  local_holding_folder: Path
  dt_pattern: str
  # How long after the week starts (Sunday 00:00 local) the file should be in. Past it, a missing
  # file sends one alert for the week; the job keeps retrying either way.
  deadline: relativedelta

  def __post_init__(self) -> None:
    self.local_holding_folder.mkdir(parents=True, exist_ok=True)


def assemble_ryo_filename_pattern(now: datetime | None = None) -> Pattern[str]:
  now = today() if now is None else now
  dates = list(
    rrule(
      DAILY,
      dtstart=(now - relativedelta(weekday=SU(-1), hour=0, minute=0, second=0, microsecond=0)),
      until=(now + relativedelta(weekday=SA(+1), hour=23, minute=59, second=59, microsecond=999999)),
    )
  )

  years = {str(dt.year) for dt in dates}
  months = {f"{dt.month:02d}" for dt in dates}
  days = {f"{dt.day:02d}" for dt in dates}

  years_part = "|".join(years)
  months_part = "|".join(months)
  days_part = "|".join(days)

  pattern = (
    rf"^RYO_ACH_Drafts_"
    r"(?P<timestamp>"
    rf"(?P<year>{years_part})"
    rf"(?P<month>{months_part})"
    rf"(?P<day>{days_part})"
    r"(?P<hour>\d{2})"
    r"(?P<minute>\d{2})"
    # The name is typed by hand and padded with a varying run of zeros after the minute (8, then
    # 6 so far), so the timestamp stops at the minute and any padding is accepted.
    r")\d*\.csv$"
  )
  return compile(pattern)


def _assemble_range_pattern(
  end_val: str | int,
  start_val: str | int = 0,
  d2_max: int = 9,
) -> str:
  """Convert a two-digit number (e.g. 26) into a regex range pattern (e.g. 2[0-6]|1[0-9]|0[0-9]).

  Expects at most 2 digits; d2_max is inclusive.
  """
  end_val = str(end_val)
  start_val = int(start_val)
  assert 0 <= start_val <= 9, "total_min must be between 0 and 9"  # noqa: PLR2004
  assert 0 <= d2_max <= 9, "d2_max must be between 0 and 9"  # noqa: PLR2004

  second_digit = None

  first_digit = int(end_val[0])
  if len(end_val) == 2:  # noqa: PLR2004
    second_digit = int(end_val[1])

  if len(end_val) == 2:  # noqa: PLR2004
    patterns = []

    for d1 in range(first_digit + 1):
      if d1 == 0:
        patt = rf"{d1}[{start_val}-{d2_max}]"
      elif d1 == first_digit:
        patt = rf"{d1}[0-{second_digit}]"
      elif d1 > first_digit:
        raise ValueError("HOW?!")
      else:
        patt = rf"{d1}[0-9]"
      patterns.append(patt)

    return "|".join(patterns)

  elif len(end_val) == 1:
    return rf"0[{start_val}-{end_val}]"
  else:
    raise ValueError("HOW?!")


_cached_hmid = rf"({_assemble_range_pattern(end_val=23)})"

_cached_mstrt = rf"({_assemble_range_pattern(end_val=59, start_val=0)})"
_cached_mmid = rf"({_assemble_range_pattern(end_val=59)})"
_cached_mend = rf"({_assemble_range_pattern(end_val=59)})"

_cached_sstrt = rf"({_assemble_range_pattern(end_val=59, start_val=0)})"
_cached_smid = rf"({_assemble_range_pattern(end_val=59)})"
_cached_send = rf"({_assemble_range_pattern(end_val=59)})"


def assemble_sas_filename_pattern(now: datetime | None = None) -> Pattern[str]:
  now = today(tzinfo=SETTINGS.tz) if now is None else now
  start_est = now - relativedelta(weekday=SU(-1), hour=0, minute=0, second=0, microsecond=0)
  end_est = now + relativedelta(weekday=SA(+1), hour=23, minute=59, second=59, microsecond=999999)
  rrule_end_est = end_est + relativedelta(weekday=SU(+1), hour=0, minute=0, second=0, microsecond=0)
  # convert from local tz (SETTINGS.tz) to UTC
  start = start_est.astimezone(UTC)
  end = end_est.astimezone(UTC)
  rrule_end = rrule_end_est.astimezone(UTC)

  dates = list(rrule(DAILY, dtstart=start, until=rrule_end))

  days = {f"{dt.day:02d}" for dt in dates[1:-1]}

  dmid = "|".join(days)

  hstrt = rf"({_assemble_range_pattern(end_val=23, start_val=start.hour)})"
  hmid = _cached_hmid
  hend = rf"({_assemble_range_pattern(end_val=end.hour)})"

  assembled_year = r"(?P<year>{syear}|{eyear})".format(syear=start.year, eyear=end.year)  # noqa: UP032
  assembled_month = r"(?P<month>{smonth:02d}|{emonth:02d})".format(smonth=start.month, emonth=end.month)  # noqa: UP032
  assembled_day = r"(?P<day>(?P<dstrt>{sday})|(?P<dmid>{dmid})|(?P<dend>{eday}))".format(sday=start.day, dmid=dmid, eday=end.day)  # noqa: UP032
  assembled_hour = r"(?P<hour>(?(dstrt){hstrt}|(?(dmid){hmid}|{hend})))".format(hstrt=hstrt, hmid=hmid, hend=hend)  # noqa: UP032
  assembled_minute = r"(?P<minute>(?(dstrt){mstrt}|(?(dmid){mmid}|{mend})))".format(  # noqa: UP032
    mstrt=_cached_mstrt, mmid=_cached_mmid, mend=_cached_mend
  )
  assembled_second = r"(?P<second>(?(dstrt){sstrt}|(?(dmid){smid}|{send})))".format(  # noqa: UP032
    sstrt=_cached_sstrt, smid=_cached_smid, send=_cached_send
  )
  assembled_microsecond = r"(?P<microsecond>\d{1,6})"

  timestamp = r"(?P<timestamp>{year}-{month}-{day}T{hour}_{minute}_{second}\.{microsecond})".format(  # noqa: UP032
    year=assembled_year,
    month=assembled_month,
    day=assembled_day,
    hour=assembled_hour,
    minute=assembled_minute,
    second=assembled_second,
    microsecond=assembled_microsecond,
  )

  pattern = r"^Sweet_Fire_{timestamp}\.csv$".format(timestamp=timestamp)  # noqa: UP032

  return compile(pattern)


SAS_STORE_NAME_OVERRIDES: dict[str, int] = {
  # The warehouse itself, drafted by SAS as a customer; RYO's equivalent is "SFT-WHOLESALE" -> 999.
  "SWEET FIRE TOB WHSE": 999,
}


def extract_storenums(names: Series, overrides: Mapping[str, int] | None = None) -> Series:
  """Pull the store number out of each store name, as a nullable Int64.

  A name in *overrides* gets its mapped number; a name with no digits in it gets <NA> rather than
  failing the whole report, so the caller can keep that row under its original name.
  """
  storenums = to_numeric(names.str.extract(r"^.*?(\d+).*?$", expand=False), errors="coerce").astype("Int64")
  if overrides:
    mapped = names.map(overrides).astype("Int64")
    storenums = mapped.fillna(storenums)
  return storenums


def read_text_lenient(path: Path) -> str:
  """Read a CSV as UTF-8, falling back to Windows-1252 (Excel's plain "CSV" save) when it isn't."""
  raw = path.read_bytes()
  try:
    return raw.decode("utf-8-sig")
  except UnicodeDecodeError:
    logger.info("%s is not UTF-8; reading it as Windows-1252", path.name)
    return raw.decode("cp1252", errors="replace")


def parse_amount(raw: object) -> Decimal | str | None:
  """Parse a money cell: a Decimal, None for a blank cell, or the raw text when it can't be read.

  Handles "$1,234.56", "-$1,234.56", the accounting negative "$(1,234.56)" and the accounting zero
  "$-". Returning unreadable text unchanged lets the row still reach the report, with a note.
  """
  if not isinstance(raw, str) or not raw.strip():
    return None
  text = "".join(raw.split()).replace("$", "").replace(",", "")
  if text == "-":
    return Decimal("0.00")
  negative = text.startswith("(") and text.endswith(")")
  if negative:
    text = text[1:-1]
  try:
    value = Decimal(text)
  except InvalidOperation:
    return raw.strip()
  if not value.is_finite():
    return raw.strip()
  return -value if negative else value


def sum_amounts(amounts: Iterable[object]) -> Decimal | None:
  """Sum the readable amounts in a group, or None when none of them could be read."""
  readable = [amount for amount in amounts if isinstance(amount, Decimal)]
  return sum(readable, Decimal(0)) if readable else None


def join_notes(*notes: object) -> str | None:
  """Join the non-empty notes once each, in order, or None when there are none."""
  kept = dict.fromkeys(note for note in notes if isinstance(note, str) and note)
  return "; ".join(kept) or None


HEADER_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")


def format_header_date(raw: str | None, label: str) -> str:
  """Reformat a RYO header date as YYYY/MM/DD, or keep the raw text, flagged, when it can't be read."""
  text = (raw or "").strip()
  for fmt in HEADER_DATE_FORMATS:
    try:
      return date.strptime(text, fmt).strftime("%Y/%m/%d")
    except ValueError:
      continue
  logger.warning("Could not read the RYO file's %s %r; printing it as is", label, text)
  return f"{text} (could not read this date)" if text else "(missing from the RYO file)"


class DownloadedFiles(TypedDict):
  ryo: Path
  sas: Path


class BalanceSheetJob(JobBase):
  """Downloads this week's RYO and SAS draft files, merges them, and emails the result."""

  reschedule_delay_minutes: ClassVar[int] = 10
  email_recipients = (
    "jacob.ogden@sweetfiretobacco.com",
    "office@sweetfiretobacco.com",
  )

  def __post_init__(self) -> None:
    """Register each source file's remote location/pattern and prepare local folders."""
    self.file_details: dict[FTPHandlerKey, FileVars] = {
      "ryo": FileVars(
        pickup_folder=PurePosixPath("/Accounting"),
        filename_pattern_factory=assemble_ryo_filename_pattern,
        local_holding_folder=self.job_holding_folder / "ryo",
        # RYO_ACH_Drafts_202611021157000000.csv; only the minute-precision part is the timestamp
        dt_pattern="%Y%m%d%H%M",
        # Ron usually uploads it on Monday: due by 15:00 Monday.
        deadline=relativedelta(days=1, hours=15),
      ),
      "sas": FileVars(
        pickup_folder=PurePosixPath("/Outgoing/ach_detail"),
        filename_pattern_factory=assemble_sas_filename_pattern,
        local_holding_folder=self.job_holding_folder / "sas",
        # Sweet_Fire_2026-06-17T03_31_24.476.csv
        dt_pattern="%Y-%m-%dT%H_%M_%S.%f",
        # SAS's automated upload lands around 07:00 Wednesday: due by 08:00, an hour of grace.
        deadline=relativedelta(days=3, hours=8),
      ),
    }
    self.job_output_folder = self.job_holding_folder / "output"
    self.job_output_folder.mkdir(parents=True, exist_ok=True)
    # (source, week start) pairs already alerted as late, so each late file alerts once a week.
    self.late_alerts_sent: set[tuple[FTPHandlerKey, date]] = set()

  @override
  def main_job(self) -> None:
    downloaded_files = self.download_all_files()

    try:
      report_path = self.assemble_report(downloaded_files)
    except Exception as e:
      # Assembly only reads the files just downloaded, so a retry would fail the same way on the
      # same files: alert and stop now rather than retrying up to the error threshold.
      logger.exception("%s: Error assembling report:", self.__class__.__name__, exc_info=e)
      raise JobError(
        f"Could not assemble the balance sheet from {downloaded_files['ryo'].name} and {downloaded_files['sas'].name}: "
        f"{type(e).__name__}: {e}",
        reason="error in report assembly",
      ) from e

    try:
      self.email_report(report_path)
    except Exception as e:
      logger.exception("%s: Error emailing report:", self.__class__.__name__, exc_info=e)
      raise CanRescheduleJobError("error in emailing report", count_error=True) from e

  def download_file(self, ftp_key: FTPHandlerKey) -> Path:
    """Fetch the newest matching file for one source, rescheduling if none is from this week."""
    file_vars = self.file_details[ftp_key]
    with self.ftp_session(ftp_key) as conn:
      files = list(conn.listdir(file_vars.pickup_folder.as_posix()))
      pattern = file_vars.filename_pattern_factory(today())

      filtered_files = filter(lambda f: pattern.match(f.filename), files)
      # Uploaded this week but not matching the expected name: a misnamed file would be among these.
      unmatched = [f.filename for f in files if not pattern.match(f.filename) and self.check_if_this_week(f.modified_time)]

      # check that filtered_files is not empty before calling max, otherwise it will raise a ValueError
      try:
        youngest_file = max(filtered_files, key=lambda f: f.modified_time)
      except ValueError:
        logger.warning("No matching files found in %s for FTP %s", file_vars, ftp_key)
        self.alert_if_past_deadline(ftp_key, "no file with the expected name was found", unmatched_names=unmatched)
        raise CanRescheduleJobError(
          f"Error in downloading file: missing {ftp_key} file",
          reason=f"missing {ftp_key} file",
          count_error=False,
        ) from None

      matched = pattern.match(youngest_file.filename)
      assert matched is not None

      timestamp_str = matched.group("timestamp")
      timestamp = datetime.strptime(timestamp_str, file_vars.dt_pattern).replace(tzinfo=SETTINGS.tz)

      if self.check_if_this_week(timestamp):
        remote_file = file_vars.pickup_folder / youngest_file.filename
        local_file = file_vars.local_holding_folder / youngest_file.filename
        logger.debug("Downloading report: %s", youngest_file.filename)
        with local_file.open("wb") as file:
          conn.download_file(remote_path=remote_file.as_posix(), callback=file.write)
        logger.debug("Report downloaded to %s", local_file)
        return local_file

      else:
        self.alert_if_past_deadline(
          ftp_key,
          f"the newest file with the expected name, {youngest_file.filename}, is not from this week",
          unmatched_names=unmatched,
        )
        raise CanRescheduleJobError(
          f"Balance sheet file {ftp_key} is not from this week. Youngest file timestamp: {timestamp.isoformat()}",
          reason=f"{ftp_key} file not from this week",
          count_error=False,
        ) from None

  def alert_if_past_deadline(self, ftp_key: FTPHandlerKey, problem: str, unmatched_names: Iterable[str] = ()) -> None:
    """Alert once per week when a source's file is still missing after its deadline.

    The job keeps retrying either way; this only makes sure someone hears about a late, missing or
    misnamed file instead of the job retrying silently.
    """
    file_vars = self.file_details[ftp_key]
    now = datetime.now(tz=SETTINGS.tz)
    week_start = now + relativedelta(weekday=SU(-1), hour=0, minute=0, second=0, microsecond=0)
    deadline = week_start + file_vars.deadline
    alert_key = (ftp_key, week_start.date())
    if now < deadline or alert_key in self.late_alerts_sent:
      return
    self.late_alerts_sent.add(alert_key)

    details = (
      f"The {ftp_key.upper()} file for the week of {week_start:%Y-%m-%d} was due by {deadline:%a %Y-%m-%d %H:%M}, "
      f"but {problem} in {file_vars.pickup_folder}.\n\n"
      f"The job keeps checking every {self.reschedule_delay_minutes} minutes and sends the balance sheet once it arrives."
    )
    if unmatched_names := sorted(unmatched_names):
      details += "\n\nFiles uploaded this week that did not match the expected name (a misnamed file may be among them):\n"
      details += "\n".join(f"  {name}" for name in unmatched_names)
    logger.warning("%s: %s file is late. %s", self.__class__.__name__, ftp_key, details)
    alert(f"{self.__class__.__name__}: {ftp_key.upper()} file is late", details, in_except_block=False)

  def download_all_files(self) -> DownloadedFiles:
    """Download every source file; if any is missing, delete the rest and reschedule.

    Every source is checked on each run, even after one turns up missing, so each file's deadline
    is checked independently of the others.
    """
    downloaded_files: dict[str, Path] = {}
    missing: list[CanRescheduleJobError] = []
    with self.jobname_cvar.set(self.__class__.__name__):
      try:
        for ftp_key in self.file_details:
          try:
            downloaded_files[ftp_key] = self.download_file(ftp_key)
          except CanRescheduleJobError as e:
            missing.append(e)
      except Exception:
        for file in downloaded_files.values():
          file.unlink(missing_ok=True)
        raise

    if missing:
      for file in downloaded_files.values():
        file.unlink(missing_ok=True)
      if len(missing) == 1:
        raise missing[0]
      raise CanRescheduleJobError(
        "; ".join(str(e) for e in missing),
        reason=" and ".join(e.reason for e in missing),
        count_error=False,
      )

    return DownloadedFiles(**downloaded_files)

  def assemble_report(self, downloaded_files: DownloadedFiles) -> Path:
    """Merge the RYO and SAS CSVs per store into the combined balance sheet CSV."""
    ryo_file = StringIO(read_text_lenient(downloaded_files["ryo"]))
    # The first line is "DATE:,<date>,Draft Date:,<date>,,"; the table starts on the line after it.
    first_line_parts = [*next(reader([ryo_file.readline()]), []), "", "", "", ""]
    ryo_df = read_csv(
      ryo_file,
      header=0,
      names=["store", "ryo", "ryo_notes", "store2", "ryo2", "notes2"],
      dtype=str,
      # Only an empty cell is missing; text like "N/A" stays text, so it gets an error note.
      keep_default_na=False,
      na_values=[""],
    )
    ryo_date = format_header_date(first_line_parts[1], "date")
    ryo_draft_date = format_header_date(first_line_parts[3], "draft date")

    ryo_df_one = ryo_df[["store", "ryo", "ryo_notes"]]
    ryo_df_two = ryo_df[["store2", "ryo2", "notes2"]]
    ryo_df_two = ryo_df_two.rename(columns={"store2": "store", "ryo2": "ryo", "notes2": "ryo_notes"})
    ryo_df = concat(
      [ryo_df_one, ryo_df_two],
      ignore_index=True,
    )

    # drop rows where store is nan
    ryo_df = ryo_df[~isna(ryo_df["store"])]

    ryo_df = ryo_df.apply(lambda col: col.str.strip())

    ryo_df.loc[:, "store"] = ryo_df["store"].str.replace(r"SFT-WHOLESALE", "SFT-WHOLESALE 999")
    ryo_df["storenum"] = extract_storenums(ryo_df["store"])
    # Only rows with no store number keep a name, so they merge and print under it instead.
    ryo_df["store_name"] = ryo_df["store"].where(ryo_df["storenum"].isna())

    # An unreadable amount keeps its raw text in the cell, and the row says so in its error notes.
    ryo_df["ryo_total"] = Series([parse_amount(raw) for raw in ryo_df["ryo"]], index=ryo_df.index, dtype=object)
    ryo_df["ryo_error_notes"] = [
      f"RYO amount {total!r} could not be read" if isinstance(total, str) else None for total in ryo_df["ryo_total"]
    ]

    # ryo_df.to_csv("test_ryo.csv", header=True, index=False)

    sas_df = read_csv(
      StringIO(read_text_lenient(downloaded_files["sas"])),
      header=0,
      names=[
        "custnum",
        "store",
        "type",
        "invoice",
        "invoice_date",
        "draft_date",
        "amount",
        "customer_total",
      ],
      usecols=[
        "custnum",
        "store",
        "invoice",
        "invoice_date",
        "draft_date",
        "amount",
      ],
      dtype=str,
      keep_default_na=False,
      na_values=[""],
    )

    sas_df.loc[:, "store"] = sas_df["store"].str.strip()
    sas_df["storenum"] = extract_storenums(sas_df["store"], SAS_STORE_NAME_OVERRIDES)
    sas_df["store_name"] = sas_df["store"].where(sas_df["storenum"].isna())

    sas_df["sas_amount"] = Series([parse_amount(raw) for raw in sas_df["amount"]], index=sas_df.index, dtype=object)
    sas_df["sas_error_notes"] = [
      f"SAS amount {amount!r} on invoice {invoice} could not be read and is left out of the SAS total"
      if isinstance(amount, str)
      else f"SAS invoice {invoice} has no amount"
      if amount is None
      else None
      for amount, invoice in zip(sas_df["sas_amount"], sas_df["invoice"], strict=True)
    ]
    # sas_df.to_csv("test_sas.csv", header=True, index=False)

    # group by store and total the readable amounts; dropna=False keeps the unmatched-name groups
    sas_grouped = (
      sas_df.groupby(["storenum", "store_name"], dropna=False)
      .agg(
        sas_total=("sas_amount", sum_amounts),
        sas_error_notes=("sas_error_notes", lambda notes: join_notes(*notes)),
      )
      .reset_index()
    )

    # sas_grouped.to_csv("test_sas_agged.csv", header=True, index=False)

    # join sas_total to ryo_df on storenum, or on the original name for rows without one
    merged_df = ryo_df.merge(sas_grouped, on=["storenum", "store_name"], how="outer")
    merged_df = merged_df.sort_values("storenum", na_position="last", kind="stable")

    # Anything that went wrong with a row is written on that row, so whoever reads the sheet sees it.
    store_notes = [
      None
      if not isna(storenum)
      else f"No store number found in the name {name!r}"
      if isinstance(name, str)
      else "No store name or number"
      for storenum, name in zip(merged_df["storenum"], merged_df["store_name"], strict=True)
    ]
    merged_df["error_notes"] = [
      join_notes(*notes) for notes in zip(store_notes, merged_df["ryo_error_notes"], merged_df["sas_error_notes"], strict=True)
    ]
    flagged = merged_df["error_notes"].dropna().tolist()
    if flagged:
      logger.warning("%s: %d balance sheet row(s) have error notes: %s", self.__class__.__name__, len(flagged), flagged)

    merged_df["storenum"] = merged_df["storenum"].astype("string").fillna(merged_df["store_name"])
    # merged_df.to_csv("test_merged.csv", header=True, index=True)
    merged_df = merged_df[
      [
        # "store",
        "storenum",
        "ryo_total",
        "sas_total",
        "ryo_notes",
        "error_notes",
      ]
    ]

    io_stream = StringIO(newline=None)

    # csv.writer quotes a raw, unreadable date that happens to contain a comma.
    writer(io_stream, lineterminator="\n").writerows([["RYO Date:", ryo_date], ["RYO Draft Date:", ryo_draft_date]])

    merged_df.to_csv(io_stream, header=True, index=False)

    now = datetime.now(tz=SETTINGS.tz)

    out_file = self.job_output_folder / f"sas_ryo_balance_sheet_{now.strftime('%Y%m%d%H%M%S%f')}.csv"

    # UTF-8 with a BOM so Excel shows any non-ASCII text from the source files (e.g. in notes) correctly.
    with out_file.open("w", encoding="utf-8-sig") as report_file:
      report_file.write(io_stream.getvalue())

    return out_file

  def email_report(self, report_path: Path) -> None:
    """Email the assembled balance sheet to this job's recipients as an attachment."""
    assert isinstance(SETTINGS.alerts_email, str), "SETTINGS.alerts_email must be a string"
    msg = prepare_email_message(
      EmailMessageParts(
        subject=f"SAS/RYO Balance Sheet Report - {report_path.stem}",
        body="Attached is the latest RYO/SAS balance sheet report.",
        from_addr=("SFT Bot", None, None, SETTINGS.alerts_email),
        to_addrs=self.email_recipients,
        attachments=report_path,
      )
    )

    batch_send_emails(msg)

    logger.info("Email sent with report %s to %s", report_path.name, self.email_recipients)

  def _test_download(self, ftp_key: Literal["ryo", "sas"]) -> Path | None:
    self.download_file(ftp_key)

  def _test_assemble_report(self, downloaded_files: DownloadedFiles) -> Path:
    return self.assemble_report(downloaded_files)

  def _test_job_no_send(self) -> None:
    downloaded_files = self.download_all_files()

    try:
      report_path = self.assemble_report(downloaded_files)  # noqa: F841
    except Exception as e:
      logger.exception("%s: Error assembling report:", self.__class__.__name__, exc_info=e)
      raise JobError(f"{type(e).__name__}: {e}", reason="error in report assembly") from e


if __name__ == "__main__":
  # First party imports
  from aeth_ext import initialize

  initialize(asyncio=True, logging=True)
  test_job = BalanceSheetJob()

  test_job.main_job()

  # result = test_job._test_download("ryo")

  # test_job._test_assemble_report(
  #   DownloadedFiles(
  #     ryo=HOLDING_FOLDER / "balancesheetjob" / "ryo" / "RYO_ACH_Drafts_20260618164600000000.csv",
  #     sas=HOLDING_FOLDER / "balancesheetjob" / "sas" / "Sweet_Fire_2026-06-17T03_31_24.476.csv",
  #   )
  # )
  # report_path = HOLDING_FOLDER / "balancesheetjob" / "output" / "sas_ryo_balance_sheet_20260617093924911622.csv"

  # test_job.email_report(report_path)
