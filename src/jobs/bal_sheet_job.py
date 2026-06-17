# Standard library imports
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from email.message import EmailMessage
from io import StringIO
from logging import getLogger
from pathlib import PurePosixPath
from re import compile
from smtplib import SMTP
from ssl import create_default_context
from typing import TYPE_CHECKING, ClassVar, TypedDict

# Third party imports
from dateutil.relativedelta import SA, SU, relativedelta
from dateutil.rrule import DAILY, rrule
from pandas import concat, isna, read_csv, to_numeric

# First party imports
from environment_init_vars import CWD, SETTINGS
from jobs import CanRescheduleJobError, JobBase
from sft_ext.utils import today

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from pathlib import Path
  from re import Pattern
  from typing import Literal

logger = getLogger(__name__)


@dataclass
class FileVars:
  pickup_folder: PurePosixPath
  filename_pattern_factory: Callable[[datetime | None], Pattern[str]]
  local_holding_folder: Path

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
    r"(?P<second>\d{2})"
    r"(?P<microsecond>\d{6})"
    r")\.csv$"
  )
  return compile(pattern)


def _assemble_range_pattern(
  end_val: str | int,
  start_val: str | int = 0,
  d2_max: int = 9,
) -> str:
  """
  For a given two-digit number str (e.g. 26)
  convert it into a regex pattern (e.g. 2[0-6]|1[0-9]|0[0-9])
  Expects only 2 digits

  d2_max is inclusive
  """
  end_val = str(end_val)
  start_val = int(start_val)
  assert 0 <= start_val <= 9, "total_min must be between 0 and 9"  # noqa: PLR2004
  assert 0 <= d2_max <= 9, "d2_max must be between 0 and 9"  # noqa: PLR2004

  second_digit = None

  try:
    first_digit = int(end_val[0])
    if len(end_val) == 2:  # noqa: PLR2004
      second_digit = int(end_val[1])
  except ValueError as e:
    raise e

  if len(end_val) == 2:  # noqa: PLR2004
    patterns = []

    for d1 in range(0, first_digit + 1):
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


class DownloadedFiles(TypedDict):
  ryo: Path
  sas: Path


class BalanceSheetJob(JobBase):
  reschedule_delay_minutes: ClassVar[int] = 10
  email_recipients = (
    "denirosaco@sweetfiretobacco.com",
    "jacob.ogden@sweetfiretobacco.com",
  )

  def __post_init__(self) -> None:
    self.file_details = {
      "ryo": FileVars(
        pickup_folder=PurePosixPath("/SFTAccounting/"),
        filename_pattern_factory=assemble_ryo_filename_pattern,
        local_holding_folder=self.job_holding_folder / "ryo",
      ),
      "sas": FileVars(
        pickup_folder=PurePosixPath("/Outgoing/ach_detail/"),
        filename_pattern_factory=assemble_sas_filename_pattern,
        local_holding_folder=self.job_holding_folder / "sas",
      ),
    }
    self.job_output_folder = self.job_holding_folder / "output"
    self.job_output_folder.mkdir(parents=True, exist_ok=True)

  async def main_job(self) -> None:
    downloaded_files = self.download_files()

    try:
      report_path = self.assemble_report(downloaded_files)
    except Exception as e:
      logger.exception(f"{self.__class__.__name__}: Error assembling report:", exc_info=e)
      raise CanRescheduleJobError(
        "error in report assembly",
        count_error=True,
      ) from e

    try:
      self.email_report(report_path)
    except Exception as e:
      logger.exception(f"{self.__class__.__name__}: Error emailing report:", exc_info=e)
      raise CanRescheduleJobError("error in emailing report", count_error=True) from e

  def download_files(self) -> DownloadedFiles:
    downloaded_files: dict[str, Path] = {}
    with self.jobname_cvar.set(self.__class__.__name__):
      for ftp_key, file_vars in self.file_details.items():
        with self.ftp_handlers[ftp_key].start_session() as conn:
          files = conn.listdir(file_vars.pickup_folder.as_posix())
          pattern = file_vars.filename_pattern_factory(today())

          filtered_files = filter(lambda f: pattern.match(f.filename), files)

          # check that filtered_files is not empty before calling max, otherwise it will raise a ValueError
          try:
            youngest_file = max(filtered_files, key=lambda f: f.modified_time)
          except ValueError:
            logger.warning(f"No matching files found in {file_vars} for FTP {ftp_key}")
            for file in downloaded_files.values():
              file.unlink(missing_ok=True)
            raise CanRescheduleJobError(
              f"Error in downloading files: missing {ftp_key} file", reason=f"missing {ftp_key} file", count_error=False
            ) from None

          remote_file = file_vars.pickup_folder / youngest_file.filename
          local_file = file_vars.local_holding_folder / youngest_file.filename
          with local_file.open("wb") as file:
            conn.download_file(remote_path=remote_file.as_posix(), callback=file.write)

          downloaded_files[ftp_key] = local_file

    return DownloadedFiles(**downloaded_files)

  def assemble_report(self, downloaded_files: DownloadedFiles) -> Path:
    with downloaded_files["ryo"].open("r", encoding="utf-8") as ryo_file:
      ryo_first_line = ryo_file.readline()
      ryo_df = read_csv(
        ryo_file,
        header=0,
        names=["store", "ryo", "ryo_notes", "store2", "ryo2", "notes2"],
        dtype=str,
      )
    first_line_parts = ryo_first_line.strip().split(",")
    ryo_extracted_date = date.strptime(first_line_parts[1], "%m/%d/%Y")
    ryo_extracted_draft_date = date.strptime(first_line_parts[3], "%m/%d/%Y")

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
    ryo_cleaned_storenums = ryo_df["store"].str.extract(r"^.*?(\d+).*?$", expand=False)
    ryo_df["storenum"] = to_numeric(ryo_cleaned_storenums, errors="coerce").astype(int)

    # ryo_cleaned_amounts = ryo_df["ryo"].str.replace(r"[$,]", "", regex=True).str.replace("^-$", "0", regex=True)
    # ryo_df["ryo"] = to_numeric(ryo_cleaned_amounts, errors="coerce").astype(float)
    ryo_df["ryo_total"] = ryo_df["ryo"].str.replace(r"[$,]", "", regex=True).str.replace("^-$", "0.00", regex=True).map(Decimal)

    # ryo_df.to_csv("test_ryo.csv", header=True, index=False)

    sas_df = read_csv(
      downloaded_files["sas"],
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
    )

    sas_df.loc[:, "storenum"] = sas_df["store"].str.extract(r"^.*?(\d+).*?$").astype(int)

    sas_df["sas_total"] = sas_df["amount"].map(Decimal)
    # sas_df.to_csv("test_sas.csv", header=True, index=False)

    # group by store and aggregate the amount column
    sas_grouped = sas_df.groupby("storenum").agg({"sas_total": "sum"}).reset_index()

    # sas_grouped.to_csv("test_sas_agged.csv", header=True, index=False)

    # join sas_total to ryo_df on storenum
    merged_df = ryo_df.merge(sas_grouped, on="storenum", how="outer")
    merged_df = merged_df[
      [
        "store",
        "storenum",
        "ryo_total",
        "sas_total",
        "ryo_notes",
      ]
    ]

    io_stream = StringIO(newline=None)

    io_stream.writelines(
      [
        f"RYO Date:,{ryo_extracted_date.strftime('%Y/%m/%d')}\n",
        f"RYO Draft Date:,{ryo_extracted_draft_date.strftime('%Y/%m/%d')}\n",
      ]
    )

    merged_df.to_csv(io_stream, header=True, index=False)

    now = datetime.now(tz=SETTINGS.tz)

    out_file = self.job_output_folder / f"sas_ryo_balance_sheet_{now.strftime('%Y%m%d%H%M%S%f')}.csv"

    with out_file.open("w") as report_file:
      report_file.write(io_stream.getvalue())

    return out_file

  def email_report(self, report_path: Path) -> None:
    msg = EmailMessage()
    msg.set_content("Attached is the latest RYO/SAS balance sheet report.")
    msg["Subject"] = f"SAS/RYO Balance Sheet Report - {report_path.stem}"
    msg["From"] = SETTINGS.alerts_email
    msg["To"] = ", ".join(self.email_recipients)
    msg[""]

    ctx = create_default_context()

    msg.add_attachment(
      report_path.read_bytes(),
      maintype="text",
      subtype="csv",
      filename=report_path.name,
    )

    with SMTP(SETTINGS.alerts_smtp_server, SETTINGS.alerts_smtp_port) as smtp:
      smtp.ehlo()
      smtp.starttls(context=ctx)
      smtp.ehlo()
      smtp.login(SETTINGS.alerts_email, SETTINGS.alerts_email_pwd)
      smtp.send_message(msg)
    logger.info(f"Email sent with report {report_path.name} to {self.email_recipients}")

  def _test_download(self, ftp_key: Literal["ryo", "sas"]) -> Path | None:
    file_vars = self.file_details[ftp_key]
    with self.ftp_handlers[ftp_key].start_session() as conn:
      files = list(conn.listdir(file_vars.pickup_folder.as_posix()))
      pattern = file_vars.filename_pattern_factory(today())

      filtered_files = list(filter(lambda f: pattern.match(f.filename), files))

      for file in filtered_files:
        logger.info(f"Matched file: {file.filename} with modified time {file.modified_time}")

      # check that filtered_files is not empty before calling max, otherwise it will raise a ValueError
      try:
        youngest_file = max(filtered_files, key=lambda f: f.modified_time)
      except ValueError:
        logger.warning(f"No matching files found in {file_vars} for FTP {ftp_key}")
        return

      remote_file = file_vars.pickup_folder / youngest_file.filename
      local_file = file_vars.local_holding_folder / youngest_file.filename
      with local_file.open("wb") as file:
        conn.download_file(remote_path=remote_file.as_posix(), callback=file.write)

  def _test_assemble_report(self, downloaded_files: DownloadedFiles) -> Path:
    return self.assemble_report(downloaded_files)


if __name__ == "__main__":
  test_job = BalanceSheetJob()

  # result = test_job._test_download("sas")

  test_job._test_assemble_report(
    DownloadedFiles(
      ryo=CWD / "file_holding" / "balancesheetjob" / "ryo" / "SFT - RYO ACH Drafts(1).csv",
      sas=CWD / "file_holding" / "balancesheetjob" / "sas" / "Sweet_Fire_2026-06-17T03_31_24.476.csv",
    )
  )
  report_path = CWD / "file_holding" / "balancesheetjob" / "output" / "sas_ryo_balance_sheet_20260617093924911622.csv"

  test_job.email_report(report_path)
