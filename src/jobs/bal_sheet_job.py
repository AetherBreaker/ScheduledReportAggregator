# Standard library imports
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from logging import getLogger
from pathlib import Path, PurePosixPath
from re import Pattern, compile
from typing import TYPE_CHECKING, ClassVar, TypedDict

# Third party imports
from dateutil.relativedelta import SA, SU, relativedelta
from dateutil.rrule import DAILY, rrule
from pandas import concat, isna, read_csv, to_numeric

# First party imports
from environment_init_vars import CWD, HOLDING_FOLDER, SETTINGS
from jobs import JobBase
from sft_ext.utils import today

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

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

  years = {str(date.year) for date in dates}
  months = {f"{date.month:02d}" for date in dates}
  days = {f"{date.day:02d}" for date in dates}

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


def assemble_sas_filename_pattern(now: datetime | None = None) -> Pattern[str]:
  now = today() if now is None else now
  dates = list(
    rrule(
      DAILY,
      dtstart=(now - relativedelta(weekday=SU(-1), hour=0, minute=0, second=0, microsecond=0)),
      until=(now + relativedelta(weekday=SA(+1), hour=23, minute=59, second=59, microsecond=999999)),
    )
  )

  years = {str(date.year) for date in dates}
  months = {f"{date.month:02d}" for date in dates}
  days = {f"{date.day:02d}" for date in dates}

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


class DownloadedFiles(TypedDict):
  ryo: Path
  sas: Path


job_output_folder = HOLDING_FOLDER / "balance_sheet_reports"
job_output_folder.mkdir(parents=True, exist_ok=True)


class BalanceSheetJob(JobBase):
  reschedule_delay_minutes: ClassVar[int] = 10

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

  async def main_job(self) -> None:
    downloaded_files = self.download_files()
    if downloaded_files is None:
      self.error_reschedule(reason="missing file")
      return

    try:
      report_path = self.assemble_report(downloaded_files)
    except Exception as e:
      logger.exception(f"{self.__class__.__name__}: Error assembling report:", exc_info=e)
      self.error_reschedule(count=True, reason="error in report assembly")
      return

    try:
      self.email_report(report_path)
    except Exception as e:
      logger.exception(f"{self.__class__.__name__}: Error emailing report:", exc_info=e)
      self.error_reschedule(count=True, reason="error in emailing report")

  def download_files(self) -> DownloadedFiles | None:
    downloaded_files: dict[str, Path] = {}
    with self.jobs_cvar.set(self.__class__.__name__):
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
            return

          remote_file = file_vars.pickup_folder / youngest_file.filename
          local_file = file_vars.local_holding_folder / youngest_file.filename
          with local_file.open("wb") as file:
            conn.download_file(remote_path=remote_file.as_posix(), callback=file.write)

          downloaded_files[ftp_key] = local_file

    return DownloadedFiles(**downloaded_files)

  @staticmethod
  def assemble_report(downloaded_files: DownloadedFiles) -> Path:
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
    merged_df = ryo_df.merge(sas_grouped, on="storenum", how="left")
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

    out_file = job_output_folder / f"sas_ryo_balance_sheet_{now.strftime('%Y%m%d%H%M%S%f')}.csv"

    with out_file.open("w") as report_file:
      report_file.write(io_stream.getvalue())

    return out_file

  def email_report(self, report_path: Path) -> None:
    pass


if __name__ == "__main__":
  test_files = DownloadedFiles(
    ryo=CWD / "example files" / "SFT - RYO ACH Drafts.csv",
    sas=CWD / "example files" / "searchresults.csv",
  )

  BalanceSheetJob.assemble_report(test_files)
