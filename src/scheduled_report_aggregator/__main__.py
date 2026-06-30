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
from datetime import datetime, timedelta, timezone
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


# Standard library imports
from datetime import datetime, timedelta, timezone

# Third party imports
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron.fields import (
  DEFAULT_VALUES,
  BaseField,
  DayOfMonthField,
  DayOfWeekField,
  MonthField,
  WeekField,
)
from apscheduler.util import (
  astimezone,
  convert_to_datetime,
  datetime_ceil,
  datetime_repr,
  datetime_utc_add,
)
from tzlocal import get_localzone

UTC = timezone.utc


class CronTrigger(BaseTrigger):
  """
  Triggers when current time matches all specified time constraints,
  similarly to how the UNIX cron scheduler works.

  :param int|str year: 4-digit year
  :param int|str month: month (1-12)
  :param int|str day: day of month (1-31)
  :param int|str week: ISO week (1-53)
  :param int|str day_of_week: number or name of weekday (0-6 or mon,tue,wed,thu,fri,sat,sun)
  :param int|str hour: hour (0-23)
  :param int|str minute: minute (0-59)
  :param int|str second: second (0-59)
  :param datetime|str start_date: earliest possible date/time to trigger on (inclusive)
  :param datetime|str end_date: latest possible date/time to trigger on (inclusive)
  :param datetime.tzinfo|str timezone: time zone to use for the date/time calculations (defaults
      to scheduler timezone)
  :param int|None jitter: delay the job execution by ``jitter`` seconds at most

  .. note:: The first weekday is always **monday**.
  """

  FIELD_NAMES = (
    "year",
    "month",
    "day",
    "week",
    "day_of_week",
    "hour",
    "minute",
    "second",
  )
  FIELDS_MAP = {
    "year": BaseField,
    "month": MonthField,
    "week": WeekField,
    "day": DayOfMonthField,
    "day_of_week": DayOfWeekField,
    "hour": BaseField,
    "minute": BaseField,
    "second": BaseField,
  }

  __slots__ = "end_date", "fields", "jitter", "start_date", "timezone"

  def __init__(
    self,
    year=None,
    month=None,
    day=None,
    week=None,
    day_of_week=None,
    hour=None,
    minute=None,
    second=None,
    start_date=None,
    end_date=None,
    timezone=None,
    jitter=None,
  ):
    if timezone:
      self.timezone = astimezone(timezone)
    elif isinstance(start_date, datetime) and start_date.tzinfo:
      self.timezone = astimezone(start_date.tzinfo)
    elif isinstance(end_date, datetime) and end_date.tzinfo:
      self.timezone = astimezone(end_date.tzinfo)
    else:
      self.timezone = get_localzone()

    self.start_date = convert_to_datetime(start_date, self.timezone, "start_date")
    self.end_date = convert_to_datetime(end_date, self.timezone, "end_date")

    self.jitter = jitter

    values = dict((key, value) for (key, value) in locals().items() if key in self.FIELD_NAMES and value is not None)
    self.fields = []
    assign_defaults = False
    for field_name in self.FIELD_NAMES:
      if field_name in values:
        exprs = values.pop(field_name)
        is_default = False
        assign_defaults = not values
      elif assign_defaults:
        exprs = DEFAULT_VALUES[field_name]
        is_default = True
      else:
        exprs = "*"
        is_default = True

      field_class = self.FIELDS_MAP[field_name]
      field = field_class(field_name, exprs, is_default)
      self.fields.append(field)

  @classmethod
  def from_crontab(cls, expr, timezone=None):
    """
    Create a :class:`~CronTrigger` from a standard crontab expression.

    See https://en.wikipedia.org/wiki/Cron for more information on the format accepted here.

    :param expr: minute, hour, day of month, month, day of week
    :param datetime.tzinfo|str timezone: time zone to use for the date/time calculations (
        defaults to scheduler timezone)
    :return: a :class:`~CronTrigger` instance

    """
    values = expr.split()
    if len(values) != 5:
      raise ValueError(f"Wrong number of fields; got {len(values)}, expected 5")

    return cls(
      minute=values[0],
      hour=values[1],
      day=values[2],
      month=values[3],
      day_of_week=values[4],
      timezone=timezone,
    )

  def _increment_field_value(self, dateval, fieldnum):
    """
    Increments the designated field and resets all less significant fields to their minimum
    values.

    :type dateval: datetime
    :type fieldnum: int
    :return: a tuple containing the new date, and the number of the field that was actually
        incremented
    :rtype: tuple
    """

    values = {}
    i = 0
    while i < len(self.fields):
      field = self.fields[i]
      if not field.REAL:
        if i == fieldnum:
          fieldnum -= 1
          i -= 1
        else:
          i += 1
        continue

      if i < fieldnum:
        values[field.name] = field.get_value(dateval)
        i += 1
      elif i > fieldnum:
        values[field.name] = field.get_min(dateval)
        i += 1
      else:
        value = field.get_value(dateval)
        maxval = field.get_max(dateval)
        if value == maxval:
          fieldnum -= 1
          i -= 1
        else:
          values[field.name] = value + 1
          i += 1

    difference = datetime(**values) - dateval.replace(tzinfo=None)
    dateval = datetime_utc_add(dateval, difference)
    return dateval, fieldnum

  def _set_field_value(self, dateval, fieldnum, new_value):
    values = {}
    for i, field in enumerate(self.fields):
      if field.REAL:
        if i < fieldnum:
          values[field.name] = field.get_value(dateval)
        elif i > fieldnum:
          values[field.name] = field.get_min(dateval)
        else:
          values[field.name] = new_value

    return datetime(**values, tzinfo=self.timezone, fold=dateval.fold)

  def get_next_fire_time(self, previous_fire_time, now):
    logger.info(
      "get_next_fire_time | ENTER | previous_fire_time=%r  now=%r  timezone=%s  start_date=%r  end_date=%r  jitter=%r",
      previous_fire_time,
      now,
      self.timezone,
      self.start_date,
      self.end_date,
      self.jitter,
    )

    if previous_fire_time:
      start_date = min(
        now.astimezone(UTC),
        datetime_utc_add(previous_fire_time, timedelta(microseconds=1)).astimezone(UTC),
      ).astimezone(self.timezone)
      logger.info("  start_date (from previous_fire_time) = %r", start_date)
      if start_date == previous_fire_time:
        start_date = datetime_utc_add(start_date, timedelta(microseconds=1))
        logger.info("  start_date (adjusted +1µs, equalled previous_fire_time) = %r", start_date)
    else:
      start_date = max(now.astimezone(UTC), self.start_date.astimezone(UTC)).astimezone(self.timezone) if self.start_date else now
      logger.info("  start_date (no previous_fire_time) = %r", start_date)

    fieldnum = 0
    next_date = datetime_ceil(start_date).astimezone(self.timezone)
    logger.info("  fieldnum=%d  next_date (ceiled start_date) = %r", fieldnum, next_date)

    while 0 <= fieldnum < len(self.fields):
      field = self.fields[fieldnum]
      curr_value = field.get_value(next_date)
      next_value = field.get_next_value(next_date)
      logger.info(
        "  [iter] field=%-12s  fieldnum=%d  curr_value=%s  next_value=%s  REAL=%s",
        field.name,
        fieldnum,
        curr_value,
        next_value,
        field.REAL,
      )

      if next_value is None:
        # No valid value was found
        next_date, fieldnum = self._increment_field_value(next_date, fieldnum - 1)
        logger.info("    -> no valid next_value; incremented  =>  next_date=%r  fieldnum=%d", next_date, fieldnum)
      elif next_value > curr_value:
        # A valid, but higher than the starting value, was found
        if field.REAL:
          next_date = self._set_field_value(next_date, fieldnum, next_value)
          fieldnum += 1
          logger.info("    -> higher value (REAL); set field  =>  next_date=%r  fieldnum=%d", next_date, fieldnum)
        else:
          next_date, fieldnum = self._increment_field_value(next_date, fieldnum)
          logger.info("    -> higher value (non-REAL); incremented  =>  next_date=%r  fieldnum=%d", next_date, fieldnum)
      else:
        # A valid value was found, no changes necessary
        fieldnum += 1
        logger.info("    -> valid value; advancing  =>  fieldnum=%d", fieldnum)

      # Return if the date has rolled past the end date
      if self.end_date and next_date > self.end_date:
        logger.info("  next_date %r > end_date %r; returning None", next_date, self.end_date)
        return None

    if fieldnum >= 0:
      next_date = self._apply_jitter(next_date, self.jitter, now)
      logger.info("  next_date (after jitter) = %r", next_date)
      result = min(next_date, self.end_date) if self.end_date else next_date
      logger.info("get_next_fire_time | RETURN | result=%r", result)
      return result

    logger.info("get_next_fire_time | RETURN | fieldnum=%d < 0; returning None", fieldnum)

  def __getstate__(self):
    return {
      "version": 2,
      "timezone": self.timezone,
      "start_date": self.start_date,
      "end_date": self.end_date,
      "fields": self.fields,
      "jitter": self.jitter,
    }

  def __setstate__(self, state):
    # This is for compatibility with APScheduler 3.0.x
    if isinstance(state, tuple):
      state = state[1]

    if state.get("version", 1) > 2:
      raise ValueError(
        f"Got serialized data for version {state['version']} of {self.__class__.__name__}, but only versions up to 2 can be handled"
      )

    self.timezone = astimezone(state["timezone"])
    self.start_date = state["start_date"]
    self.end_date = state["end_date"]
    self.fields = state["fields"]
    self.jitter = state.get("jitter")

  def __str__(self):
    options = [f"{f.name}='{f}'" for f in self.fields if not f.is_default]
    return "cron[{}]".format(", ".join(options))

  def __repr__(self):
    options = [f"{f.name}='{f}'" for f in self.fields if not f.is_default]
    if self.start_date:
      options.append(f"start_date={datetime_repr(self.start_date)!r}")
    if self.end_date:
      options.append(f"end_date={datetime_repr(self.end_date)!r}")
    if self.jitter:
      options.append(f"jitter={self.jitter}")

    return "<{} ({}, timezone='{}')>".format(
      self.__class__.__name__,
      ", ".join(options),
      self.timezone,
    )


test = CronTrigger(**CronArgs(day_of_week=DayOfWeek.TUESDAY, hour=12, minute=50, second=0))

test2 = test.get_next_fire_time(None, datetime.now(tz=SETTINGS.tz))


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

  # await reschedule_jobs()  # Schedule all jobs on startup

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
    **CronArgs(day_of_week=DayOfWeek.TUESDAY, hour=12, minute=50, second=0),
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
