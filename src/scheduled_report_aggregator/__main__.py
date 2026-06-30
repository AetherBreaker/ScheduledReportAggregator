if __name__ == "__main__":
  # Standard library imports
  from sys import platform

  # Third party imports
  from rich.console import Console

  # First party imports
  from aeth_ext import initialize

  RICH_CONSOLE = Console(
    width=None if platform == "win32" else 165,
    log_time=platform == "win32",
  )
  PROJECT_NAME = "ScheduledReportAggregator"
  LOGGING_TYPE = "daily"

  initialize(asyncio=True)
else:
  # Third party imports
  from rich import get_console

  RICH_CONSOLE = get_console()


# Standard library imports
import sys
from asyncio import run, sleep
from datetime import datetime
from logging import getLogger
from typing import TYPE_CHECKING

# Third party imports
from apscheduler.triggers.cron import CronTrigger

# First party imports
from aeth_ext.errors.err_handling import FATAL_EVENT
from scheduled_report_aggregator.custom_types import CronArgs, DayOfWeek
from scheduled_report_aggregator.environment_init_vars import SETTINGS
from scheduled_report_aggregator.jobs import HOLDING_FOLDER, BalanceSheetJob, TimeclockJob
from scheduled_report_aggregator.scheduler_config import Scheduler

if TYPE_CHECKING:
  # Standard library imports
  from typing import NoReturn

  # First party imports
  from scheduled_report_aggregator.jobs.base import JobBase


logger = getLogger(__name__)


if not __debug__:
  # Heartbeat file for health checks
  HEARTBEAT_FILE = SETTINGS.log_loc_folder / "heartbeat.txt"

  def write_heartbeat():
    """Write current timestamp to heartbeat file for health monitoring."""
    try:
      HEARTBEAT_FILE.write_text(datetime.now(SETTINGS.tz).isoformat())
    except Exception as e:
      logger.error(f"Failed to write heartbeat: {e}")
else:

  def write_heartbeat():
    pass


scheduler = Scheduler.init_scheduler()


jobs: tuple[tuple[type[JobBase], CronArgs], ...] = (
  (TimeclockJob, CronArgs(day_of_week=DayOfWeek.TUESDAY, hour=9, minute=0, second=0)),
  (BalanceSheetJob, CronArgs(day_of_week=DayOfWeek.WEDNESDAY, hour=7, minute=0, second=0)),
)


async def reschedule_jobs() -> None:
  if scheduler.running:
    scheduler.pause()

  scheduler.remove_all_jobs("general_jobs")

  for job_cls, cron_args in jobs:
    job = job_cls.init_job(
      scheduler=scheduler,
      job_id=job_cls.__name__,
      **cron_args,
    )
    job.schedule_registered_jobs()

  if scheduler.running:
    scheduler.resume()


async def main() -> NoReturn:  # sourcery skip: remove-empty-nested-block
  HOLDING_FOLDER.mkdir(exist_ok=True)
  RICH_CONSOLE.rule("[bold red]Booting...[/]", style="bold red")
  # scheduler.add_job(
  #   scheduler.print_jobs,
  #   CronTrigger(minute="*/1"),
  #   id="print_jobs",
  #   replace_existing=True,
  #   jobstore="system_jobs",
  # )

  await reschedule_jobs()  # Schedule all jobs on startup

  # Heartbeat job - writes timestamp every minute for health monitoring
  scheduler.add_job(
    write_heartbeat,
    CronTrigger(minute="*/1"),
    id="heartbeat",
    replace_existing=True,
    jobstore="system_jobs",
  )

  scheduler.add_job(
    reschedule_jobs,
    CronTrigger(
      day_of_week="sun",
      hour=0,
      minute=0,
      second=0,
    ),
    id="reschedule_jobs",
    replace_existing=True,
    jobstore="system_jobs",
  )

  scheduler.start()

  # Write initial heartbeat on startup
  write_heartbeat()

  scheduler.print_jobs()

  job = TimeclockJob.init_job(
    scheduler=scheduler,
    job_id=TimeclockJob.__name__,
    **CronArgs(day_of_week=DayOfWeek.TUESDAY, hour=11, minute=55, second=0),
  )
  job.schedule_registered_jobs()

  if __debug__:
    for job_cls, _ in jobs:
      await job_cls().main_job()  # Run each job once immediately in debug mode for testing
    pass

  RICH_CONSOLE.rule("[bold red]Boot Done[/]", style="bold red")
  # with RICH_CONSOLE.status("Application is running."):
  await FATAL_EVENT

  if any(job.errored for job, _ in jobs):
    await sleep(
      600
    )  # Sleep for 10 minutes to allow pending operations from non-error-state processors to flush through before exiting

  try:
    logger.warning("Fatal shutdown: stopping scheduler to freeze application state")
    scheduler.pause()
    scheduler.shutdown(wait=False)
  except Exception as e:
    logger.error(f"Fatal shutdown: failed to stop scheduler cleanly: {e}", exc_info=True)

  sys.exit(1)


if __name__ == "__main__":
  run(main())
