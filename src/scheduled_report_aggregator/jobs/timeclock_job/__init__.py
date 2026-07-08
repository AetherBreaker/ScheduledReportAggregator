if __name__ == "__main__":
  # First party imports
  from aeth_ext.logging.init import init_logging

  init_logging()

# Standard library imports
from atexit import register
from datetime import date
from decimal import Decimal
from io import StringIO, TextIOWrapper
from json import load
from logging import getLogger
from os import environ
from pathlib import Path
from shutil import get_terminal_size
from subprocess import PIPE, CalledProcessError, Popen
from sys import executable, stderr as sys_stderr, stdout as sys_stdout
from threading import Thread
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
from scheduled_report_aggregator.jobs.base import CanRescheduleJobError, JobBase
from scheduled_report_aggregator.jobs.timeclock_job.allotted_hours_model import AllottedHoursModel

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
    logger.info("TimeclockJob starting")
    next_report = await self.get_next_timeclock_report()
    logger.info("Schedule report acquired: %s", next_report.name)

    manifest_data = self.run_processor(next_report)
    store_count = len(manifest_data)
    week_count = sum(len(weeks) for weeks in manifest_data.values())
    logger.info("Processor complete: %d store(s), %d week(s) total", store_count, week_count)

    overunder_data = self.calculate_overunder_hours(manifest_data)
    logger.info("Over/under calculation complete: %d result(s)", len(overunder_data))

    self.send_results(overunder_data)
    logger.info("TimeclockJob finished")

  async def get_next_timeclock_report(self) -> Path:
    logger.debug("Connecting to FTP to retrieve schedule report from %s", self.reports_pickup_folder)
    with self.ftp_handlers["sft"].start_session() as ftp:
      remote_files = list(ftp.listdir(self.reports_pickup_folder.as_posix()))
      logger.debug("Found %d remote file(s) in pickup folder", len(remote_files))

      try:
        youngest_file = max(remote_files, key=lambda f: f.modified_time)
      except ValueError:
        raise CanRescheduleJobError(
          "Error in locating timeclock schedule report", reason=f"No schedule files in {self.reports_pickup_folder}", count_error=False
        ) from None

      if self.check_if_this_week(youngest_file.modified_time.astimezone(SETTINGS.tz)):
        remote_file = self.reports_pickup_folder / youngest_file.filename
        local_file = self.schedule_local_holding / youngest_file.filename
        logger.debug("Downloading report: %s", youngest_file.filename)

        with local_file.open("wb") as f:
          ftp.download_file(remote_file.as_posix(), f.write)

        logger.debug("Report downloaded to %s", local_file)
        return local_file

      else:
        raise CanRescheduleJobError(
          "No schedule files from this week",
          reason=f"Youngest file {youngest_file.filename} modified at {youngest_file.modified_time} is not from this week",
          count_error=False,
        ) from None

  def run_processor(self, csv_file: Path) -> dict[StoreNum, dict[date, ManifestEntry]]:
    # Run timeclock_entry_processor as a subprocess via its CLI.
    logger.info("Running timeclock_entry_processor on %s", csv_file.name)

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

    def _tee(src: TextIOWrapper, buf: StringIO, dest: TextIOWrapper) -> None:
      for line in src:
        dest.write(line)
        dest.flush()
        buf.write(line)

    logger.debug("Launching subprocess: %s", exec_args[0])
    stdout_buf = StringIO()
    stderr_buf = StringIO()
    subprocess_env = environ.copy()
    subprocess_env["COLUMNS"] = str(get_terminal_size().columns)
    with Popen(exec_args, cwd=str(TIMECLOCK_PLAYGROUND), stdout=PIPE, stderr=PIPE, text=True, env=subprocess_env) as proc:
      t_out = Thread(target=_tee, args=(proc.stdout, stdout_buf, sys_stdout), daemon=True)
      t_err = Thread(target=_tee, args=(proc.stderr, stderr_buf, sys_stderr), daemon=True)
      t_out.start()
      t_err.start()
      t_out.join()
      t_err.join()
      returncode = proc.wait()
    captured_stdout = stdout_buf.getvalue()
    captured_stderr = stderr_buf.getvalue()
    logger.debug("Subprocess exited with code %d", returncode)
    if returncode != 0:
      if captured_stderr:
        logger.error("timeclock_entry_processor stderr:\n%s", captured_stderr)
      if captured_stdout:
        logger.error("timeclock_entry_processor stdout:\n%s", captured_stdout)
      raise CalledProcessError(returncode, exec_args, captured_stdout, captured_stderr)

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
    logger.debug("Fetching allotted hours from Google Sheet (id=%s)", SETTINGS.allotted_hours_sheet_id)
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
    logger.debug("Allotted hours fetched for %d store(s)", len(allotted_hours))
    return allotted_hours

  def calculate_overunder_hours(self, weeks_data: dict[StoreNum, dict[date, ManifestEntry]]) -> set[OverUnderEntry]:
    overunder_data: set[OverUnderEntry] = set()
    logger.debug("Calculating over/under hours for %d store(s)", len(weeks_data))

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
        logger.debug("Processing store %s week ending %s", store, week.isoformat())
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
    logger.info("Preparing over/under alert emails")
    emails_to_send = []

    for store, week_ending, allotted_hours, worked_hours, over_under_hours, pdf_path, csv_path in sorted(
      overunder_data,
      key=lambda x: x.over_under_hours,
    ):
      if self.MAX_ALLOWED_OVER_HOURS >= over_under_hours >= self.MAX_ALLOWED_UNDER_HOURS:
        logger.debug("Store %s week %s within tolerance (%.2f hrs), skipping", store, week_ending, over_under_hours)
        continue

      over_under_str = "Over" if over_under_hours > 0 else "Under"
      logger.info("Store %s week %s is %s by %.2f hrs", store, week_ending, over_under_str, abs(over_under_hours))
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
      logger.info("Sending %d alert email(s)", len(emails_to_send))
      batch_send_emails(emails_to_send)
    else:
      logger.info("No stores outside tolerance; no alert emails sent")

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
  # from scheduled_report_aggregator.custom_types import DayOfWeek
  # from scheduled_report_aggregator.scheduler_config import Scheduler

  # scheduler = Scheduler.init_scheduler()

  job = TimeclockJob()
  # job.init_job(
  #   scheduler=scheduler,
  #   job_id="test",
  #   **CronArgs(day_of_week=DayOfWeek.TUESDAY, hour=9, minute=0, second=0),
  # )

  # result = job.calculate_overunder_hours(job.load_manifest(CWD / "manifest.json"))
  # job.send_results(result)

  asyncio.run(job.main_job())
  pass
