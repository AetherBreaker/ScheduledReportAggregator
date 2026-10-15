"""Monthly state tobacco tax returns built from the warehouse database (Michigan so far; `states/` holds each state's rules)."""

# Standard library imports
import json
from datetime import date, datetime
from logging import getLogger
from typing import ClassVar, override

# Third party imports
import pyodbc
from dateutil.relativedelta import relativedelta

# First party imports
from scheduled_report_aggregator.environment_init_vars import SETTINGS
from scheduled_report_aggregator.jobs.base import CanRescheduleJobError, JobBase, JobError
from scheduled_report_aggregator.jobs.taxes_job.states import mi
from scheduled_report_aggregator.jobs.taxes_job.workbook import load_template, template_drift, write_rows

logger = getLogger(__name__)

__all__ = ["TaxesJob"]

WAREHOUSE_SERVER = "10.8.0.10"  # office-db-pc's WireGuard address (docs/office-database-access.md)
WAREHOUSE_DATABASE = "FasTraxWarehouse"
OUTPUT_NAME = (
  "Mi_{name}_Tobacco {period:%m.%y}.xlsx"  # the names the CPA files under, e.g. "Mi_SecondaryWholesaler_Tobacco 06.26.xlsx"
)


def _connection_string(server: str) -> str:
  """The warehouse's ODBC connection string, from `secrets/sql_login.json` (`UID`, `PWD`, `port`).

  Its own function so the login's values only ever live in this frame. aeth_ext's fatal alerts render
  every traceback frame's locals, so a malformed file is reported with those values dropped first.
  """
  try:
    login = json.loads(SETTINGS.sql_login_file.read_text(encoding="utf-8-sig"))
    # A braced value may hold ';' and doubles any '}'. The tunnel already encrypts the path and the
    # PC's certificate is self-signed, hence TrustServerCertificate.
    return (
      f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={server},{int(login['port'])};DATABASE={WAREHOUSE_DATABASE};"
      f"UID={{{str(login['UID']).replace('}', '}}')}}};PWD={{{str(login['PWD']).replace('}', '}}')}}};"
      "Encrypt=yes;TrustServerCertificate=yes"
    )
  except KeyError, ValueError, TypeError, AttributeError:
    login = None
    msg = f"{SETTINGS.sql_login_file.name} must be a JSON object with UID, PWD and a numeric port"
    raise JobError(msg) from None


class TaxesJob(JobBase):
  """Builds each month's state tobacco returns for review and filing."""

  reschedule_delay_minutes: ClassVar[int] = 10

  def __post_init__(self) -> None:
    """Create this job's output folder under its holding folder."""
    self.job_output_folder = self.job_holding_folder / "output"
    self.job_output_folder.mkdir(parents=True, exist_ok=True)

  @override
  def main_job(self, month: date | None = None, server: str = WAREHOUSE_SERVER) -> None:
    """Write both Michigan returns for *month* (default: last month) and their review notes to the output folder.

    The scheduler passes nothing; *month* and *server* exist for local runs (see the bottom of this module).
    """
    period = (month or datetime.now(tz=SETTINGS.tz).date() - relativedelta(months=1)).replace(day=1)
    notes = [warning for spec in mi.RETURNS.values() for warning in template_drift(spec)]

    try:
      conn = pyodbc.connect(_connection_string(server), readonly=True, timeout=30)
    except pyodbc.Error as e:
      msg = f"warehouse database at {server} unreachable ({type(e).__name__}: {e.args[0] if e.args else ''})"
      raise CanRescheduleJobError(msg, count_error=True) from None
    try:
      returns = mi.build_returns(conn.cursor(), period)
    except pyodbc.Error as e:
      msg = f"warehouse query failed ({type(e).__name__}: {e.args[0] if e.args else ''})"
      raise CanRescheduleJobError(msg, count_error=True) from None
    finally:
      conn.close()

    for name, spec in mi.RETURNS.items():
      workbook = load_template(spec)
      for sheet, rows in returns.sheets[name].items():
        write_rows(workbook[sheet], mi.header_for(sheet), mi.as_cells(rows), mi.NUMBER_FORMATS)
      workbook.save(self.job_output_folder / OUTPUT_NAME.format(name=name, period=period))

    notes_file = self.job_output_folder / f"Mi_Tobacco_review_notes {period:%m.%y}.txt"
    notes_file.write_text("".join(f"- {note}\n" for note in [*notes, *returns.warnings]), encoding="utf-8")
    logger.info(
      "%s: wrote the Michigan returns for %s to %s (%d review notes)",
      self.__class__.__name__,
      f"{period:%m/%Y}",
      self.job_output_folder,
      len(notes) + len(returns.warnings),
    )


if __name__ == "__main__":
  # A local run: June 2026 (the month the rules were fitted to) over Tailscale instead of WireGuard,
  # then each return scored against the filed copy when it's on disk. Scores print counts and cell
  # references only, so any month is safe to score.
  # Standard library imports
  from pathlib import Path

  # First party imports
  from aeth_ext import initialize
  from scheduled_report_aggregator.jobs.taxes_job.scoring import score_workbook

  initialize(asyncio=True, logging=True)
  run_period = date(2026, 6, 1)
  job = TaxesJob()
  job.main_job(month=run_period, server="100.80.74.15")
  filed_folder = Path.cwd() / "tax_project_files" / "example files"
  for return_name in mi.RETURNS:
    filed = filed_folder / OUTPUT_NAME.format(name=return_name, period=run_period)
    if filed.exists():
      print("\n".join(score_workbook(job.job_output_folder / filed.name, filed)))
