if __name__ == "__main__":
  # First party imports
  from aeth_ext.logging.init import init_logging

  init_logging()

# Standard library imports
from atexit import register
from datetime import date
from decimal import Decimal
from json import load
from logging import getLogger
from pathlib import Path
from subprocess import run
from sys import executable
from typing import TYPE_CHECKING, NamedTuple, override

# Third party imports
from google.oauth2.service_account import Credentials
from gspread.auth import authorize
from gspread.http_client import BackOffHTTPClient
from gspread.utils import DateTimeOption, Dimension, ValueRenderOption
from pandas import notna, read_csv

# First party imports
from aeth_ext.types import EmailMessageParts
from aeth_ext.utils import batch_send_emails, prepare_email_message
from scheduled_report_aggregator.environment_init_vars import CWD, SETTINGS
from scheduled_report_aggregator.jobs.base import CanRescheduleJobError, CronArgs, JobBase

# Local folder imports
from .allotted_hours_model import AllottedHoursModel

if TYPE_CHECKING:
  # Third party imports
  from gspread.client import Client

  # First party imports
  from scheduled_report_aggregator.custom_types import StoreNum

logger = getLogger(__name__)

__all__ = ["TimeclockJob"]

TIMECLOCK_PLAYGROUND = CWD / "timeclock_playground"
SUBPROCESS_EMPLOYEE_INPUT = TIMECLOCK_PLAYGROUND / "employee_input"
SUBPROCESS_FONT_INPUT = TIMECLOCK_PLAYGROUND / "font_input"
SUBPROCESS_OUTPUT_FOLDER = TIMECLOCK_PLAYGROUND / "output"

SUBPROCESS_PERSISTED_DATA_FOLDER = TIMECLOCK_PLAYGROUND / "persisted_data"

SUBPROCESS_MANIFEST_PATH = TIMECLOCK_PLAYGROUND / "manifest.json"


MANUAL_EMPLOYEE_LIST_CSV = max(SETTINGS.timeclock_employee_input_loc.iterdir(), key=lambda f: f.stat().st_mtime)


WEEK_DATA_EXPECTED_COLUMNS = (
  "Store",
  "Employee Name",
  "In Time",
  "Out Time",
  "Time Worked",
  "Hours Worked",
  "Date",
  "Store Number",
)

EMPLOYEE_INFO_EXPECTED_COLUMNS = (
  "Employee #",
  "First Name",
  "Last Name",
  "Status",
  "Manager",
  "Hire Date",
  "Group",
  "Work Type",
  "Notes",
  "Created",
  "Updated",
)


class ManifestEntry(NamedTuple):
  csv: Path
  pdf: Path


class OverUnderEntry(NamedTuple):
  store: StoreNum
  week_ending: date
  alloted_hours: int
  worked_hours: Decimal
  over_under_hours: Decimal  # positive for over, negative for under
  pdf_path: Path
  csv_path: Path


DEFAULT_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class TimeclockJob(JobBase):
  creds = Credentials.from_service_account_file(SETTINGS.google_api_key_file, scopes=DEFAULT_SCOPES)

  sheet_tab_store_range = "'Sheet1'!R2C1:C1"
  sheet_tab_allotted_hours_range = "'Sheet1'!R2C3:C3"

  email_recipients = (
    # "denirosaco@sweetfiretobacco.com",
    # "jacob.ogden@sweetfiretobacco.com",
    # "franksaco@sweetfiretobacco.com",
    # "spt@sweetfiretobacco.com",
    # "pauline@sweetfiretobacco.com",
    "timeclockhoursreports@sweetfiretobacco.com",
  )

  def __post_init__(self) -> None:
    self.output_folder = self.job_holding_folder / "output"
    self.manifest_path = self.job_holding_folder / "manifest.json"

    self.manifest_path.touch(exist_ok=True)
    self.output_folder.mkdir(exist_ok=True)
    TIMECLOCK_PLAYGROUND.mkdir(exist_ok=True)

    # ensure manifest file is empty
    self.manifest_path.write_text("")

    SUBPROCESS_EMPLOYEE_INPUT.unlink(missing_ok=True)
    SUBPROCESS_FONT_INPUT.unlink(missing_ok=True)
    SUBPROCESS_OUTPUT_FOLDER.unlink(missing_ok=True)
    SUBPROCESS_PERSISTED_DATA_FOLDER.unlink(missing_ok=True)
    SUBPROCESS_MANIFEST_PATH.unlink(missing_ok=True)

    SUBPROCESS_EMPLOYEE_INPUT.symlink_to(SETTINGS.timeclock_employee_input_loc, target_is_directory=True)
    SUBPROCESS_FONT_INPUT.symlink_to(SETTINGS.timeclock_font_input_loc, target_is_directory=True)
    SUBPROCESS_OUTPUT_FOLDER.symlink_to(self.output_folder, target_is_directory=True)
    SUBPROCESS_PERSISTED_DATA_FOLDER.symlink_to(SETTINGS.persisted_dir_loc, target_is_directory=True)
    SUBPROCESS_MANIFEST_PATH.symlink_to(self.manifest_path, target_is_directory=False)

    register(SUBPROCESS_EMPLOYEE_INPUT.unlink, missing_ok=True)
    register(SUBPROCESS_FONT_INPUT.unlink, missing_ok=True)
    register(SUBPROCESS_OUTPUT_FOLDER.unlink, missing_ok=True)
    register(SUBPROCESS_PERSISTED_DATA_FOLDER.unlink, missing_ok=True)
    register(SUBPROCESS_MANIFEST_PATH.unlink, missing_ok=True)

    self.schedule_local_holding = self.job_holding_folder / "schedule_reports"
    self.schedule_local_holding.mkdir(exist_ok=True)

    self.reports_pickup_folder = self.reports_pickup_base_folder / "Automated Schedule Report"

  @property
  def client(self) -> Client:
    return authorize(self.creds, http_client=BackOffHTTPClient)

  @override
  async def main_job(self) -> None:
    next_report = await self.get_next_timeclock_report()
    manifest_data = self.run_processor(next_report)

    overunder_data = self.calculate_overunder_hours(manifest_data)

    self.send_results(overunder_data)

  async def get_next_timeclock_report(self) -> Path:
    with self.ftp_handlers["sft"].start_session() as ftp:
      remote_files = ftp.listdir(self.reports_pickup_folder.as_posix())

      try:
        youngest_file = max(remote_files, key=lambda f: f.modified_time)
      except ValueError:
        raise CanRescheduleJobError(
          "Error in locating timeclock schedule report", reason=f"No schedule files in {self.reports_pickup_folder}", count_error=False
        ) from None

      if self.check_if_this_week(youngest_file.modified_time.astimezone(SETTINGS.tz)):
        remote_file = self.reports_pickup_folder / youngest_file.filename
        local_file = self.schedule_local_holding / youngest_file.filename

        with local_file.open("wb") as f:
          ftp.download_file(remote_file.as_posix(), f.write)

        return local_file

      else:
        raise CanRescheduleJobError(
          "No schedule files from this week",
          reason=f"Youngest file {youngest_file.filename} modified at {youngest_file.modified_time} is not from this week",
          count_error=False,
        ) from None

  def run_processor(self, csv_file: Path) -> dict[StoreNum, dict[date, ManifestEntry]]:
    # Run timeclock_entry_processor as a subprocess via its CLI.

    self.manifest_path.touch(exist_ok=True)
    self.manifest_path.write_text("")  # ensure manifest is empty before processing

    if __debug__:
      exec_args = [
        executable,
        "-c",
        ";".join(
          [
            "import runpy, sys",
            "sys.argv[0] = 'timeclock_entry_processor'",
            "runpy.run_module('timeclock_entry_processor', run_name='__main__', alter_sys=True)",
          ]
        ),
        str(csv_file),
        str(self.manifest_path),
        str(self.output_folder),
      ]
    else:
      exec_args = [
        executable,
        "-m",
        "timeclock_entry_processor",
        str(csv_file),
        str(self.manifest_path),
        str(self.output_folder),
      ]

    run(exec_args, check=True, cwd=str(TIMECLOCK_PLAYGROUND))

    return self.load_manifest(self.manifest_path)

  def load_manifest(self, manifest_path: Path) -> dict[StoreNum, dict[date, ManifestEntry]]:
    # ensure that manifest file was created and has content
    if not manifest_path.exists() or manifest_path.stat().st_size == 0:
      raise RuntimeError(f"Expected manifest file at {manifest_path} was not created or is empty after processing")

    manifest_data = {}

    with manifest_path.open("r") as f:
      for store, weeks in load(f).items():
        manifest_data[int(store)] = {
          date.fromisoformat(week): ManifestEntry(csv=Path(entry["csv"]), pdf=Path(entry["pdf"])) for week, entry in weeks.items()
        }

    return manifest_data

  def get_allotted_hours(self) -> dict[StoreNum, int]:
    result = self.client.http_client.values_batch_get(
      id=SETTINGS.allotted_hours_sheet_id,
      ranges=[self.sheet_tab_store_range, self.sheet_tab_allotted_hours_range],
      params={
        "majorDimension": Dimension.rows,
        "valueRenderOption": ValueRenderOption.unformatted,
        "dateTimeRenderOption": DateTimeOption.formatted_string,
      },
    )

    allotted_hours = {}

    for store, allotted_hrs in zip(result["valueRanges"][0]["values"], result["valueRanges"][1]["values"], strict=False):
      if not store or not allotted_hrs:
        continue  # skip rows with missing values
      validated_row = AllottedHoursModel.model_validate({"store": store[0], "allotted_hours": allotted_hrs[0]})
      allotted_hours[validated_row.store] = validated_row.allotted_hours
    return allotted_hours

  def calculate_overunder_hours(self, weeks_data: dict[StoreNum, dict[date, ManifestEntry]]) -> set[OverUnderEntry]:
    overunder_data: set[OverUnderEntry] = set()

    allotted_hours = self.get_allotted_hours()

    employee_groups = read_csv(
      MANUAL_EMPLOYEE_LIST_CSV,
      header=0,
      names=EMPLOYEE_INFO_EXPECTED_COLUMNS,
      usecols=(
        "Employee #",
        "Group",
      ),
      index_col="Employee #",
      dtype=str,
    )["Group"].to_dict()

    for store, weeks in weeks_data.items():
      store_allotted_hours = allotted_hours.get(store, 0)

      for week, (week_csv, week_pdf) in weeks.items():
        week_df = read_csv(
          week_csv,
          header=0,
          names=WEEK_DATA_EXPECTED_COLUMNS,
          usecols=(
            # "Store Number",
            "Employee Name",
            "In Time",
            "Out Time",
            "Hours Worked",
          ),
          dtype=str,
        )

        week_df.loc[:, ["employee_id", "first_name", "last_name"]] = week_df["Employee Name"].str.extract(
          r"(?P<employee_id>\d+) - (?P<first_name>\w+) (?P<last_name>\w+)"
        )
        week_df.loc[:, "group"] = week_df["employee_id"].map(employee_groups)

        # map week_df "Hours Worked" to Decimal, treating non-convertible values as 0
        week_df["Hours Worked"] = week_df["Hours Worked"].map(lambda x: Decimal(x) if notna(x) else Decimal(0))

        week_worked_hours = week_df["Hours Worked"].sum()

        overunder_data.add(
          OverUnderEntry(
            store=store,
            week_ending=week,
            alloted_hours=store_allotted_hours,
            worked_hours=week_worked_hours,
            over_under_hours=week_worked_hours - store_allotted_hours,
            pdf_path=week_pdf,
            csv_path=week_csv,
          )
        )

    return overunder_data

  MAX_ALLOWED_OVER_HOURS = Decimal("1")
  MAX_ALLOWED_UNDER_HOURS = Decimal("-10")

  def send_results(self, overunder_data: set[OverUnderEntry]):
    emails_to_send = []

    for store, week_ending, allotted_hours, worked_hours, over_under_hours, pdf_path, csv_path in sorted(
      overunder_data,
      key=lambda x: x.over_under_hours,
    ):
      if self.MAX_ALLOWED_OVER_HOURS >= over_under_hours >= self.MAX_ALLOWED_UNDER_HOURS:
        continue

      over_under_str = "Over" if over_under_hours > 0 else "Under"
      message = (
        f"Store {store} is {over_under_str} allotted hours for the week ending {week_ending.isoformat()}.\n"
        f"  Allotted Hours: {allotted_hours}\n"
        f"  Worked Hours: {worked_hours}\n"
        f"  {over_under_str} Hours: {abs(over_under_hours)}\n"
      )

      emails_to_send.append(
        prepare_email_message(
          EmailMessageParts(
            subject=f"SFT{store:0>3} - Store {over_under_str} Allotted Hours by {abs(over_under_hours): >5} for Week Ending {week_ending.isoformat()}",
            body=message,
            from_addr=SETTINGS.alerts_email,
            to_addrs=self.email_recipients,
            attachments=[pdf_path, csv_path],
          )
        )
      )

    if emails_to_send:
      batch_send_emails(emails_to_send)

  @staticmethod
  def _debug_get_fixed_timeclock_src() -> Path:
    # Standard library imports
    from site import getsitepackages

    # Resolve editable install source without relying on importing the package first.
    for site_dir in getsitepackages():
      site_path = Path(site_dir)
      for pth_file in site_path.glob("__editable__.timeclock_entry_processor-*.pth"):
        src_path = Path(pth_file.read_text(encoding="utf-8").strip())
        if src_path.exists():
          return src_path

    # Fallback: resolve from the installed module if .pth lookup is unavailable.
    # First party imports
    import timeclock_entry_processor

    return Path(timeclock_entry_processor.__file__).resolve().parents[1]

  @staticmethod
  def _create_fixed_env() -> dict[str, str]:
    # Standard library imports
    import os

    child_env = os.environ.copy()
    child_env["PWD"] = str(TIMECLOCK_PLAYGROUND)

    if __debug__:
      timeclock_src = TimeclockJob._debug_get_fixed_timeclock_src()
      existing_pythonpath = child_env.get("PYTHONPATH", "")
      child_env["PYTHONPATH"] = str(timeclock_src) if not existing_pythonpath else f"{timeclock_src}{os.pathsep}{existing_pythonpath}"

    return child_env


if __name__ == "__main__":
  # Third party imports
  import winloop as asyncio

  # First party imports
  # csv_file = CWD / "Time-Clock-Entry-Report_2026-05-14_19-31-12.csv"
  # TimeclockJob().run_processor(csv_file)
  from scheduled_report_aggregator.custom_types import DayOfWeek

  job = TimeclockJob()
  job.init_job(
    scheduler="test",  # pyright: ignore[reportArgumentType]
    job_id="test",
    **CronArgs(day_of_week=DayOfWeek.TUESDAY, hour=9, minute=0, second=0),
  )

  test = job.error_reschedule()

  # result = job.calculate_overunder_hours(job.load_manifest(CWD / "manifest.json"))
  # job.send_results(result)

  asyncio.run(job.main_job())
  pass
