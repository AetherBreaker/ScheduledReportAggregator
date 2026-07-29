if __name__ == "__main__":
  # First party imports
  from aeth_ext.logging.init import init_logging

  init_logging()

# Standard library imports
from datetime import timedelta
from logging import getLogger
from typing import TYPE_CHECKING, override

# Third party imports
from dateutil.relativedelta import relativedelta
from dateutil.rrule import MO, SU, WEEKLY, rrule
from google.oauth2.service_account import Credentials
from gspread.auth import authorize
from gspread.http_client import BackOffHTTPClient

# First party imports
from aeth_ext.utils import today
from scheduled_report_aggregator.environment_init_vars import SETTINGS
from scheduled_report_aggregator.jobs.base import JobBase

if TYPE_CHECKING:
  # Third party imports
  from gspread.client import Client


logger = getLogger(__name__)


DEFAULT_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class FlipSheetJob(JobBase):
  creds = Credentials.from_service_account_file(SETTINGS.google_api_key_file, scopes=DEFAULT_SCOPES)

  sheet_tab_store_range = "'{sheet_name}'!R2C1:C1"
  sheet_tab_allotted_hours_range = "'{sheet_name}'!R2C3:C3"
  sheet_tab_training_range = "'{sheet_name}'!R2C3:C3"

  base_sheet_name: str = "Base Sheet"

  _spreadsheet_id: str = SETTINGS.allotted_hours_sheet_id

  @property
  def client(self) -> Client:
    return authorize(self.creds, http_client=BackOffHTTPClient)

  @override
  async def main_job(self, shift: timedelta | None = None) -> None:

    request_body = {"requests": []}

    # Prepping dates for sheet names
    todays_date = today() - relativedelta(weeks=1)
    if shift is not None:
      todays_date += shift

    this_week_anchor = todays_date + relativedelta(weekday=SU(+1), hour=23, minute=59, second=59, microsecond=999999)

    dtstart = this_week_anchor - relativedelta(weekday=MO(-1), hour=0, minute=0, second=0, microsecond=0, weeks=2)
    keep_dates_str = [
      f"Week Ending {dt.strftime('%m-%d-%Y')}" for dt in rrule(WEEKLY, dtstart=dtstart, until=this_week_anchor, byweekday=SU)
    ]
    keep_dates_str.append(self.base_sheet_name)

    spreadsheet_metadata = self.client.http_client.fetch_sheet_metadata(self._spreadsheet_id)

    base_sheet_id: int = next(
      sheet["properties"]["sheetId"]
      for sheet in spreadsheet_metadata["sheets"]
      if sheet["properties"]["title"] == self.base_sheet_name
    )

    hide_sheets = [sheet for sheet in spreadsheet_metadata["sheets"] if sheet["properties"]["title"] not in keep_dates_str]

    # self.client.open_by_key(self._spreadsheet_id).sheet1.freeze(1)

    new_sheet_name = f"Week Ending {this_week_anchor.strftime('%m-%d-%Y')}"

    # check if the new sheet already exists and delete it if it does
    existing_new_sheet = next(
      (sheet for sheet in spreadsheet_metadata["sheets"] if sheet["properties"]["title"] == new_sheet_name), None
    )
    if existing_new_sheet:
      del_sheet_request = {"deleteSheet": {"sheetId": existing_new_sheet["properties"]["sheetId"]}}
      request_body["requests"].append(del_sheet_request)

    # Add new sheet tab for the new week by duplicating the base sheet
    new_sheet_id = max(sheet["properties"]["sheetId"] for sheet in spreadsheet_metadata["sheets"]) + 1

    duplicate_sheet_request = {
      "duplicateSheet": {
        "sourceSheetId": base_sheet_id,
        "insertSheetIndex": len(spreadsheet_metadata["sheets"]),
        "newSheetId": new_sheet_id,
        "newSheetName": new_sheet_name,
      }
    }
    request_body["requests"].append(duplicate_sheet_request)

    # hide outdated sheets
    for sheet in hide_sheets:
      request_body["requests"].append(
        {
          "updateSheetProperties": {
            "properties": {
              "sheetId": sheet["properties"]["sheetId"],
              "hidden": True,
            },
            "fields": "hidden",
          }
        }
      )

    # Add protected range to the new sheet tab to prevent editing of the header row
    protect_header_request = {
      "addProtectedRange": {
        "protectedRange": {
          "range": {
            "sheetId": new_sheet_id,
            "startRowIndex": 0,
            "endRowIndex": 1,
            "startColumnIndex": 0,
          },
          "description": "Do not edit the header",
          "warningOnly": False,
          "requestingUserCanEdit": True,
          "editors": {
            "users": [
              "aetherbreaker7777@gmail.com",
              "scheduled-report-aggregator@scheduledreportaggregator.iam.gserviceaccount.com",
            ],
            "groups": [],
          },
        }
      }
    }
    request_body["requests"].append(protect_header_request)

    self.client.http_client.batch_update(self._spreadsheet_id, request_body)


async def main_test():
  job = FlipSheetJob()
  for idx in range(-3, 1):
    await job.main_job(shift=timedelta(weeks=idx))


if __name__ == "__main__":
  # Third party imports
  import winloop as asyncio

  # First party imports
  # csv_file = CWD / "Time-Clock-Entry-Report_2026-05-14_19-31-12.csv"
  # TimeclockJob().run_processor(csv_file)
  # from scheduled_report_aggregator.custom_types import DayOfWeek
  # from scheduled_report_aggregator.scheduler_config import Scheduler

  # scheduler = Scheduler.init_scheduler()

  job = FlipSheetJob()
  # job.init_job(
  #   scheduler=scheduler,
  #   job_id="test",
  #   **CronArgs(day_of_week=DayOfWeek.TUESDAY, hour=9, minute=0, second=0),
  # )

  # result = job.calculate_overunder_hours(job.load_manifest(CWD / "manifest.json"))
  # job.send_results(result)

  asyncio.run(main_test())
  pass
