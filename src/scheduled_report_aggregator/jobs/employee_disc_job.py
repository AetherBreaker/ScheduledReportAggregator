# Standard library imports
from csv import DictWriter, reader
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from logging import getLogger
from pathlib import Path, PurePosixPath
from re import IGNORECASE, compile, sub
from typing import TYPE_CHECKING, Annotated, override

# Third party imports
from dateutil.relativedelta import relativedelta
from dateutil.rrule import DAILY, rrule
from google.oauth2.service_account import Credentials
from gspread.auth import authorize
from gspread.http_client import BackOffHTTPClient
from gspread.utils import DateTimeOption, Dimension, ValueInputOption, ValueRenderOption
from pydantic.dataclasses import dataclass as pyd_dataclass
from pydantic.functional_validators import BeforeValidator

# First party imports
from aeth_ext.types import EmailMessageParts, IsPydanticSlots
from aeth_ext.utils import batch_send_emails, prepare_email_message, today
from scheduled_report_aggregator.environment_init_vars import SETTINGS
from scheduled_report_aggregator.jobs.base import CanRescheduleJobError, FTPHandlerKey, JobBase

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from re import Pattern
  from typing import ClassVar, Literal

  # Third party imports
  from gspread.client import Client

logger = getLogger(__name__)


__all__ = ["EmployeeDiscountsJob"]

_CUSTOMER_PATTERN = compile(r"^Customer: (?P<customer>.*)$", flags=IGNORECASE)
_END_OF_SECTION_PATTERN = compile(r"^Totals for: .*$", flags=IGNORECASE)
_STORE_PATTERN = compile(r"^([\d]+) - .*$", flags=IGNORECASE)
_REPORT_PATTERN = compile(r"Automated-Employee-Discounts-Report_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.csv", flags=IGNORECASE)

_EXPECTED_HEADER = [val.casefold() for val in ["Store", "Date", "Reg #", "Receipt #", "Discount Qty", "Discount Amount"]]

_DEFAULT_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def parse_currency(value: str) -> str:
  value = sub(r"[^0-9.-]", "", value)
  return value


def parse_datetime(value: str) -> datetime:
  return datetime.strptime(value, "%m/%d/%Y %I:%M %p").astimezone(SETTINGS.tz)


def parse_store(value: str) -> int:
  if match := _STORE_PATTERN.match(value):
    return int(match.group(1))
  else:
    raise ValueError(f"Invalid store format: {value}")


@pyd_dataclass(frozen=True, slots=True)
class ReportRecord(IsPydanticSlots):
  customer: str
  store: Annotated[int, BeforeValidator(parse_store)]
  date: Annotated[datetime, BeforeValidator(parse_datetime)]
  reg_num: int
  receipt_num: int
  discount_qty: int
  discount_amt: Annotated[Decimal, BeforeValidator(parse_currency)]


@dataclass(frozen=True, slots=True)
class FileVars:
  pickup_folder: PurePosixPath
  filename_pattern: Callable[[datetime | None], Pattern[str]] | Pattern[str]
  local_holding_folder: Path

  def __post_init__(self) -> None:
    self.local_holding_folder.mkdir(parents=True, exist_ok=True)


type FileKey = Literal["employee_veterans_discounts_report"]
type SheetURL = str


class EmployeeDiscountsJob(JobBase):
  creds = Credentials.from_service_account_file(SETTINGS.google_api_key_file, scopes=_DEFAULT_SCOPES)
  reschedule_delay_minutes: ClassVar[int] = 10
  email_recipients = (
    "receiving@sweetfiretobacco.com",
    "it@sweetfiretobacco.com",
  )

  _spreadsheet_id: str = SETTINGS.employee_discounts_report_sheet_id
  _spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{_spreadsheet_id}/"

  def __post_init__(self) -> None:
    self.file_details: tuple[tuple[FTPHandlerKey, FileKey, FileVars], ...] = (
      (
        "sft",
        "employee_veterans_discounts_report",
        FileVars(
          pickup_folder=PurePosixPath("/upload/Automated Employee Discounts Report"),
          filename_pattern=_REPORT_PATTERN,
          local_holding_folder=self.job_holding_folder / "employee_veterans_discounts_report",
        ),
      ),
    )
    self.job_output_folder = self.job_holding_folder / "output"
    self.job_output_folder.mkdir(parents=True, exist_ok=True)

  @property
  def client(self) -> Client:
    return authorize(self.creds, http_client=BackOffHTTPClient)

  @override
  async def main_job(self) -> None:
    try:
      download_step_results = self._download_files()
    except Exception as e:
      logger.exception("%s: Error downloading report:", self.__class__.__name__, exc_info=e)
      raise CanRescheduleJobError(
        "Error in downloading report files",
        reason="Error in downloading report files",
        count_error=True,
      ) from e

    try:
      process_report_step_results = self._process_report(download_step_results)
    except Exception as e:
      logger.exception("%s: Error processing employee discounts report:", self.__class__.__name__, exc_info=e)
      raise CanRescheduleJobError(
        "Error in processing employee discounts report",
        reason="Error in processing employee discounts report",
        count_error=True,
      ) from e

    try:
      gsheets_upload_step_results = self._upload_to_gsheets(process_report_step_results)
    except Exception as e:
      logger.exception("%s: Error uploading employee discounts report to Google Sheets:", self.__class__.__name__, exc_info=e)
      raise CanRescheduleJobError(
        "Error in uploading employee discounts report to Google Sheets",
        reason="Error in uploading employee discounts report to Google Sheets",
        count_error=True,
      ) from e

    try:
      self._send_email(gsheets_upload_step_results)
    except Exception as e:
      logger.exception("%s: Error sending notification email for employee discounts report:", self.__class__.__name__, exc_info=e)
      raise CanRescheduleJobError(
        "Error in sending notification email for employee discounts report",
        reason="Error in sending notification email for employee discounts report",
        count_error=True,
      ) from e

  def _download_files(self) -> dict[FileKey, Path]:
    collected_file_vars: dict[FTPHandlerKey, dict[FileKey, FileVars]] = {}

    for ftp_key, file_key, file_vars in self.file_details:
      collected_file_vars.setdefault(ftp_key, {})[file_key] = file_vars

    found_files: dict[FileKey, Path] = {}

    try:
      for ftp_key, file_vars_list in collected_file_vars.items():
        with self.ftp_handlers[ftp_key].start_session() as conn:
          for file_key, file_vars in file_vars_list.items():
            files = conn.listdir(file_vars.pickup_folder.as_posix())
            pattern = file_vars.filename_pattern(today()) if callable(file_vars.filename_pattern) else file_vars.filename_pattern

            filtered_files = filter(lambda f: pattern.match(f.filename), files)

            # check that filtered_files is not empty before calling max, otherwise it will raise a ValueError
            try:
              youngest_file = max(filtered_files, key=lambda f: f.modified_time)
            except ValueError:
              logger.warning("No matching files found in %s for FTP %s", file_vars, ftp_key)
              raise CanRescheduleJobError(
                f"Error in downloading file: missing {ftp_key} file", reason=f"missing {ftp_key} file", count_error=False
              ) from None

            # check that the youngest file is from today, else raise a CanRescheduleJobError
            if youngest_file.modified_time.date() != today().date():
              logger.warning(
                "Youngest file %s in %s for FTP %s is not from today (%s)",
                youngest_file.filename,
                file_vars,
                ftp_key,
                today().date(),
              )
              raise CanRescheduleJobError(
                f"Error in downloading file: {ftp_key} file is not from today",
                reason=f"{ftp_key} file not found for today",
                count_error=False,
              ) from None

            remote_file = file_vars.pickup_folder / youngest_file.filename
            local_file = file_vars.local_holding_folder / youngest_file.filename
            with local_file.open("wb") as file:
              conn.download_file(remote_path=remote_file.as_posix(), callback=file.write)

            if local_file.exists():
              found_files[file_key] = local_file
            else:
              logger.warning("Downloaded file %s does not exist at %s", youngest_file.filename, local_file)
              raise CanRescheduleJobError(
                f"Error in downloading file: {ftp_key} file not found after download",
                reason=f"{ftp_key} file not found after download",
                count_error=False,
              ) from None
    except BaseException as e:
      # unlink previously found files to reset the state for the next run
      for file_path in found_files.values():
        if file_path.exists():
          file_path.unlink()

      raise e

    return found_files

  def _process_report(self, downloaded_files: dict[FileKey, Path]) -> Path:
    # We only care about the employee_veterans_discounts_report file for now
    report = downloaded_files["employee_veterans_discounts_report"]

    collected_rows: list[ReportRecord] = []
    with report.open("r", encoding="utf-8") as f:
      csv_reader = reader(f)

      current_customer = None
      skip_next = False

      for row in csv_reader:
        if skip_next:
          skip_next = False
          continue  # Skip the next row, which is a blank line
        elif match := _CUSTOMER_PATTERN.match(row[0]):
          current_customer = match.group("customer")
          skip_next = True  # Skip the next row, which is a blank line
        elif _END_OF_SECTION_PATTERN.match(row[0]):
          current_customer = None
        elif current_customer is not None:
          if [val.casefold() for val in row] == _EXPECTED_HEADER:
            continue  # Skip the header row

          pass

          record = {
            "customer": current_customer,
            "store": row[0],
            "date": row[1],
            "reg_num": row[2],
            "receipt_num": row[3],
            "discount_qty": row[4],
            "discount_amt": row[5],
          }
          collected_rows.append(ReportRecord(**record))

    out_file = self.job_output_folder / f"employee_veterans_discounts_report_{today().strftime('%Y%m%d')}.csv"

    with out_file.open("w", newline="", encoding="utf-8") as f:
      writer = DictWriter(f, fieldnames=ReportRecord.__annotations__.keys())
      writer.writeheader()
      for record in collected_rows:
        writer.writerow(asdict(record))

    return out_file

  def _test_process_report(self) -> Path:
    return self._process_report(
      {
        "employee_veterans_discounts_report": Path.cwd()
        / "example files"
        / "Automated-Employee-Discounts-Report_2026-07-23_17-15-06.csv"
      }
    )

  _base_valuerange_fmt = "'{name}'!R{rstart}C{cstart}:R{rend}C{cend}"

  def _upload_to_gsheets(self, report_path: Path) -> SheetURL:
    # Establish request body templates
    new_tab_batch_update_body = {"requests": []}

    data_values_batch_update_body = {
      "valueInputOption": ValueInputOption.user_entered,
      "includeValuesInResponse": False,
      "responseValueRenderOption": ValueRenderOption.unformatted,
      "responseDateTimeRenderOption": DateTimeOption.formatted_string,
      "data": [],
    }

    filter_formats_batch_update_body = {"requests": []}

    # separator

    now = today()

    dtstart = now + relativedelta(days=-7, hour=0, minute=0, second=0, microsecond=0)
    until = now + relativedelta(hour=23, minute=59, second=59, microsecond=999999)
    keep_dates_str = [dt.strftime("%m-%d-%Y") for dt in rrule(DAILY, dtstart=dtstart, until=until)]

    spreadsheet_metadata = self.client.http_client.fetch_sheet_metadata(self._spreadsheet_id)

    hide_sheets = [sheet for sheet in spreadsheet_metadata["sheets"] if sheet["properties"]["title"] not in keep_dates_str]

    # self.client.open_by_key(self._spreadsheet_id).sheet1.freeze(1)

    # Add new sheet tab for the new day

    new_sheet_name = now.strftime("%m-%d-%Y")

    # check if the new sheet already exists and delete it if it does
    existing_new_sheet = next(
      (sheet for sheet in spreadsheet_metadata["sheets"] if sheet["properties"]["title"] == new_sheet_name), None
    )
    if existing_new_sheet:
      del_sheet_request = {"deleteSheet": {"sheetId": existing_new_sheet["properties"]["sheetId"]}}
      new_tab_batch_update_body["requests"].append(del_sheet_request)

    # Create a new sheet tab for the report data
    with report_path.open("r", encoding="utf-8") as f:
      data = list(reader(f))

    row_count = len(data)
    col_count = max(len(row) for row in data)

    # get the largest existing sheet id
    new_sheet_id = max(sheet["properties"]["sheetId"] for sheet in spreadsheet_metadata["sheets"]) + 1

    add_sheet_req = {
      "addSheet": {
        "properties": {
          "title": new_sheet_name,
          "sheetId": new_sheet_id,
          "sheetType": "GRID",
          "gridProperties": {
            "rowCount": row_count,
            "columnCount": col_count,
          },
        },
      }
    }
    new_tab_batch_update_body["requests"].append(add_sheet_req)

    # hide outdated sheets
    for sheet in hide_sheets:
      new_tab_batch_update_body["requests"].append(
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

    # Constrain sheet dimensions to the shape of the data being uploaded
    ...

    # Add the report data to the values update request body
    data_values_batch_update_body["data"].append(
      {
        "range": self._base_valuerange_fmt.format(name=new_sheet_name, rstart=1, cstart=1, rend=row_count, cend=col_count),
        "majorDimension": Dimension.rows,
        "values": data,
      }
    )

    # Apply column formats
    storenum_format = {
      "repeatCell": {
        "range": {"sheetId": new_sheet_id, "startRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 2},
        "cell": {
          "userEnteredFormat": {
            "numberFormat": {
              "type": "NUMBER",
              "pattern": '"SFT"000',
            }
          },
        },
        "fields": "userEnteredFormat(numberFormat)",
      }
    }
    filter_formats_batch_update_body["requests"].append(storenum_format)

    datetime_format = {
      "repeatCell": {
        "range": {"sheetId": new_sheet_id, "startRowIndex": 1, "startColumnIndex": 2, "endColumnIndex": 3},
        "cell": {
          "userEnteredFormat": {
            "numberFormat": {
              "type": "DATE_TIME",
            },
          },
        },
        "fields": "userEnteredFormat(numberFormat)",
      }
    }
    filter_formats_batch_update_body["requests"].append(datetime_format)

    currency_format = {
      "repeatCell": {
        "range": {"sheetId": new_sheet_id, "startRowIndex": 1, "startColumnIndex": 6, "endColumnIndex": 7},
        "cell": {
          "userEnteredFormat": {
            "numberFormat": {
              "type": "CURRENCY",
            },
          },
        },
        "fields": "userEnteredFormat(numberFormat)",
      }
    }
    filter_formats_batch_update_body["requests"].append(currency_format)

    # Set a basic filter on the sheet tab
    filter_update_request = {
      "setBasicFilter": {"filter": {"range": {"sheetId": new_sheet_id, "startRowIndex": 0, "startColumnIndex": 0}}}
    }
    filter_formats_batch_update_body["requests"].append(filter_update_request)

    # freeze the first row (header)
    freeze_header_request = {
      "updateSheetProperties": {
        "properties": {
          "sheetId": new_sheet_id,
          "gridProperties": {
            "frozenRowCount": 1,
          },
        },
        "fields": "gridProperties/frozenRowCount",
      }
    }
    filter_formats_batch_update_body["requests"].append(freeze_header_request)

    # auto refit columns
    autofit_columns_request = {
      "autoResizeDimensions": {
        "dimensions": {
          "sheetId": new_sheet_id,
          "dimension": Dimension.cols,
          "startIndex": 0,
        },
      },
    }
    filter_formats_batch_update_body["requests"].append(autofit_columns_request)

    # Execute the api requests using the bodies built in the previous steps
    client = self.client.http_client

    client.batch_update(self._spreadsheet_id, new_tab_batch_update_body)
    client.values_batch_update(self._spreadsheet_id, data_values_batch_update_body)
    client.batch_update(self._spreadsheet_id, filter_formats_batch_update_body)

    # Return the URL of the newly created sheet tab
    return self._spreadsheet_url

  def _send_email(self, sheet_url: SheetURL) -> None:
    assert isinstance(SETTINGS.alerts_email, str), "SETTINGS.alerts_email must be a string"
    msg = prepare_email_message(
      EmailMessageParts(
        subject=f"Daily Employee Discounts Report - {today().strftime('%Y-%m-%d')}",
        body=(
          "The report for yesterdays employee discounts has been generated and uploaded to Google Sheets.\n"
          "You can access the report using the following link:\n"
          f"{sheet_url}"
        ),
        from_addr=("SFT Bot", None, None, SETTINGS.alerts_email),
        to_addrs=self.email_recipients,
      )
    )

    batch_send_emails(msg)

    logger.info("Notification email for employee discounts report sent to %s ", self.email_recipients)


if __name__ == "__main__":
  # Standard library imports
  from asyncio import run

  test_job = EmployeeDiscountsJob()

  run(test_job.main_job())

  # path = EmployeeDiscountsJob()._test_process_report()
  # EmployeeDiscountsJob()._upload_to_gsheets(path)

  # result = test_job._test_download("ryo")

  # test_job._test_assemble_report(
  #   DownloadedFiles(
  #     ryo=HOLDING_FOLDER / "balancesheetjob" / "ryo" / "RYO_ACH_Drafts_20260618164600000000.csv",
  #     sas=HOLDING_FOLDER / "balancesheetjob" / "sas" / "Sweet_Fire_2026-06-17T03_31_24.476.csv",
  #   )
  # )
  # report_path = CWD / "file_holding" / "balancesheetjob" / "output" / "sas_ryo_balance_sheet_20260617093924911622.csv"

  # test_job.email_report(report_path)
