"""Boot sequence: builds the scheduler, registers all jobs, and runs until a fatal event."""

# Standard library imports
from asyncio import CancelledError, create_task, run
from contextlib import suppress
from logging import INFO, WARNING, getLogger
from sys import exit as sys_exit
from typing import TYPE_CHECKING

# Third party imports
from rich import get_console

# First party imports
from aeth_ext.errors.shutdown import SHUTDOWN, SHUTDOWN_COMPLETE, ShutdownKind
from aeth_ext.monitoring import run_heartbeat_async
from apscheduler.triggers.cron import CronTrigger
from scheduled_report_aggregator.custom_types import DayOfWeek
from scheduled_report_aggregator.environment_init_vars import SETTINGS
from scheduled_report_aggregator.jobs import HOLDING_FOLDER, BalanceSheetJob, TimeclockJob
from scheduled_report_aggregator.jobs.base import CronArgs
from scheduled_report_aggregator.jobs.employee_disc_job import EmployeeDiscountsJob
from scheduled_report_aggregator.jobs.timeclock_job.flip_sheet_job import FlipSheetJob
from scheduled_report_aggregator.scheduler_config import Scheduler

if TYPE_CHECKING:
  # First party imports
  from scheduled_report_aggregator.jobs.base import JobBase


logger = getLogger(__name__)


RICH_CONSOLE = get_console()

HEARTBEAT_FILE = SETTINGS.log_loc_folder / "heartbeat.txt"


scheduler = Scheduler.init_scheduler()


jobs: tuple[tuple[type[JobBase], CronArgs], ...] = (
  (TimeclockJob, CronArgs(day_of_week=DayOfWeek.THURSDAY, hour=14, minute=0, second=0)),
  (BalanceSheetJob, CronArgs(day_of_week=DayOfWeek.WEDNESDAY, hour=7, minute=0, second=0)),
  (EmployeeDiscountsJob, CronArgs(hour=5, minute=0, second=0)),
  (FlipSheetJob, CronArgs(day_of_week=DayOfWeek.SUNDAY, hour=23, minute=59, second=59)),
)


async def reschedule_jobs() -> None:
  """Wipe and re-register every job's schedule; runs at boot and again weekly."""
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


def exit_code_for_shutdown(kind: ShutdownKind) -> int:
  """0 for RUNNING (never requested) or GRACEFUL, 1 for FATAL or FORCED.

  `ShutdownKind` is an IntEnum ordered by severity. Kept out of `main()` so `main()` never calls
  `sys.exit` itself.
  """
  return 1 if kind >= ShutdownKind.FATAL else 0


def run_until_shutdown() -> None:
  """Run `main()` to completion and exit the interpreter with a code that reflects how the app stopped."""
  try:
    run(main())
  except KeyboardInterrupt:
    # aeth_ext's exit nudge (simulated SIGINT). Normally main() returns on its own after awaiting
    # SHUTDOWN_COMPLETE and the nudge is skipped; it lands here only if main()'s tail or asyncio's
    # own close outran the shutdown budget. Not an error either way; the kind below says how we
    # stopped.
    pass
  sys_exit(exit_code_for_shutdown(SHUTDOWN.kind))


async def main() -> None:  # sourcery skip: remove-empty-nested-block
  """Boot the app, start the scheduler and heartbeat, then wait for a shutdown to be requested."""
  HOLDING_FOLDER.mkdir(exist_ok=True)
  RICH_CONSOLE.rule("[bold red]Booting...[/]", style="bold red")

  periodic_heartbeat_task = create_task(
    run_heartbeat_async(
      HEARTBEAT_FILE,
      ping_url=SETTINGS.alerts_healthcheck_ping_url,
      pingkey=SETTINGS.alerts_healthcheck_pingkey,
      tz=SETTINGS.tz,
    )
  )

  await reschedule_jobs()  # Schedule all jobs on startup

  # # Heartbeat job - writes timestamp every minute for health monitoring
  # scheduler.add_job(
  #   write_heartbeat,
  #   CronTrigger(minute="*/1"),
  #   id="heartbeat",
  #   replace_existing=True,
  #   jobstore="system_jobs",
  # )

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

  scheduler.print_jobs()

  # job = TimeclockJob.init_job(
  #   scheduler=scheduler,
  #   job_id=TimeclockJob.__name__,
  #   **CronArgs(day_of_week=DayOfWeek.TUESDAY, hour=14, minute=15, second=0, timezone=SETTINGS.tz),
  # )
  # job.schedule_registered_jobs()

  if __debug__:
    for job_cls, _ in jobs:
      await job_cls().main_job()  # Run each job once immediately in debug mode for testing

  RICH_CONSOLE.rule("[bold red]Boot Done[/]", style="bold red")
  # with RICH_CONSOLE.status("Application is running."):
  await SHUTDOWN

  # `await SHUTDOWN` resolves when a shutdown is *requested* (fatal exception, JobError, signal);
  # aeth_ext's threaded teardown pass has started but not finished. Freeze the scheduler so no new
  # job fires into a process that is going away, then wait for the pass before returning so the
  # normal path exits via `run_until_shutdown`'s `sys.exit`, not via the exit nudge.
  logger.log(INFO if SHUTDOWN.kind is ShutdownKind.GRACEFUL else WARNING, "Shutdown requested (%s); stopping", SHUTDOWN.kind.name)

  periodic_heartbeat_task.cancel()
  with suppress(CancelledError):
    await periodic_heartbeat_task

  try:
    scheduler.pause()
    scheduler.shutdown(wait=False)
  except Exception:
    logger.exception("Shutdown: failed to stop the scheduler cleanly")

  await SHUTDOWN_COMPLETE


if __name__ == "__main__":
  run_until_shutdown()
