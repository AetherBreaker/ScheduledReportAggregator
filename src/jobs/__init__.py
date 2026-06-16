# Standard library imports
from abc import abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from logging import getLogger
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, NamedTuple, TypedDict, overload

# Third party imports
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from dateutil.relativedelta import FR, MO, SA, SU, TH, TU, WE, relativedelta

# First party imports
from environment_init_vars import HOLDING_FOLDER, SETTINGS
from ftp_configs import RYOSFTPClient, SASSFTPClient, SFTSFTPClient
from sft_ext.errors.err_handling import FATAL_EVENT
from sft_ext.ftp.adapter import AdaptedSFTP, FTPAdapter
from sft_ext.types import StrEnum
from sft_ext.types.abc import SingletonTypeABC
from sft_ext.utils import today

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from datetime import timedelta, timezone
  from typing import Any, ClassVar, NotRequired, Unpack
  from zoneinfo import ZoneInfo

  # Third party imports
  from apscheduler.triggers.base import BaseTrigger
  from dateutil._common import weekday

  # First party imports
  from scheduler_config import Scheduler

logger = getLogger(__name__)

FTP_CVAR = ContextVar("FTP_CVAR")


class DayOfWeek(StrEnum):
  SUNDAY = "sun"
  MONDAY = "mon"
  TUESDAY = "tue"
  WEDNESDAY = "wed"
  THURSDAY = "thu"
  FRIDAY = "fri"
  SATURDAY = "sat"


dtutil_weekday_map: dict[DayOfWeek | None, weekday] = {
  DayOfWeek.MONDAY: MO,
  DayOfWeek.TUESDAY: TU,
  DayOfWeek.WEDNESDAY: WE,
  DayOfWeek.THURSDAY: TH,
  DayOfWeek.FRIDAY: FR,
  DayOfWeek.SATURDAY: SA,
  DayOfWeek.SUNDAY: SU,
  None: lambda x: None,  # for when day_of_week is not specified in cron args # type: ignore
}

num_to_weekday_map: dict[int, DayOfWeek] = {
  0: DayOfWeek.MONDAY,
  1: DayOfWeek.TUESDAY,
  2: DayOfWeek.WEDNESDAY,
  3: DayOfWeek.THURSDAY,
  4: DayOfWeek.FRIDAY,
  5: DayOfWeek.SATURDAY,
  6: DayOfWeek.SUNDAY,
}


class CronArgs(TypedDict):
  year: NotRequired[int | str | None]
  month: NotRequired[int | str | None]
  day: NotRequired[int | str | None]
  day_of_week: NotRequired[DayOfWeek | None]
  hour: NotRequired[int | str | None]
  minute: NotRequired[int | str | None]
  second: NotRequired[int | str | None]
  timezone: NotRequired[ZoneInfo | timezone | None]


@dataclass
class UseArgs:
  year: bool = False
  month: bool = False
  day: bool = False
  day_of_week: bool = True
  hour: bool = True
  minute: bool = True
  second: bool = False


DEFAULT_USE_ARGS = UseArgs()


class SubJobTriggerArgs(NamedTuple):
  delta: timedelta | relativedelta
  use_args: UseArgs = DEFAULT_USE_ARGS


class CanRescheduleJobError(Exception):
  """Custom exception to indicate that a job should be automatically rescheduled."""

  def __init__(self, message: str, reason: str | None = None, count_error: bool = False):
    super().__init__(message)
    self.reason = reason or message
    self.count_error = count_error


class JobError(Exception):
  """Custom exception to indicate that a job has encountered an error."""

  def __init__(self, message: str, reason: str | None = None, count_error: bool = False):
    super().__init__(message)
    self.reason = reason or message
    self.count_error = count_error


class JobBase(metaclass=SingletonTypeABC):
  jobs_cvar = FTP_CVAR
  subjob_mainjob_offset: ClassVar[int] = 0  # seconds

  ftp_handlers: ClassVar = {
    "sft": FTPAdapter[AdaptedSFTP](SFTSFTPClient, container_cvar=FTP_CVAR),
    "sas": FTPAdapter[AdaptedSFTP](SASSFTPClient, container_cvar=FTP_CVAR),
    "ryo": FTPAdapter[AdaptedSFTP](RYOSFTPClient, container_cvar=FTP_CVAR),
  }

  errored: bool = False  # used by main to check whether this job experienced an error
  err_counter: int = 0
  err_max_threshold: int = 3  # number consecutive errors before setting errored state, triggering shutdown

  reschedule_delay_minutes: ClassVar[int] = 10  # minutes to delay when rescheduling after an error

  sub_jobs_register: ClassVar[dict[str, tuple[Callable[..., Any], SubJobTriggerArgs]]] = {}

  reports_pickup_base_folder = PurePosixPath("/upload")
  report_path_subfolder: str = ""

  reports_pickup_folder: PurePosixPath = reports_pickup_base_folder / report_path_subfolder

  @overload
  def __init__(
    self,
    scheduler: Scheduler,
    job_id: str,
    trigger: BaseTrigger,
  ) -> None: ...

  @overload
  def __init__(
    self,
    scheduler: Scheduler,
    job_id: str,
    **kwargs: Unpack[CronArgs],
  ) -> None: ...

  def __init__(
    self,
    scheduler: Scheduler,
    job_id: str,
    trigger: BaseTrigger | None = None,
    **kwargs: Unpack[CronArgs],
  ) -> None:
    self.job_id = job_id
    self.scheduler = scheduler

    self.original_cron_args = kwargs  # save original cron args to reset schedule each week
    self.cron_args = kwargs  # replace stored cron args
    self.last_run_time = None

    if trigger is None:
      self.original_trigger = self.generate_trigger(**kwargs)
    else:
      self.original_trigger = trigger
    self.trigger = self.original_trigger

    self.job_holding_folder = HOLDING_FOLDER / self.__class__.__name__.lower()
    self.job_holding_folder.mkdir(parents=True, exist_ok=True)

    self.sub_jobs_hook()
    self.schedule_self()
    self.__post_init__()  # call post init hook for any additional setup in subclasses

  def __post_init__(self):
    pass

  def sub_jobs_hook(self) -> None:
    """Hook for adding sub-jobs to the scheduler. Override in subclasses if needed."""
    for sub_job_id, (sub_job_func, sub_job_args) in self.sub_jobs_register.items():
      sub_trigger = self.generate_trigger(
        **self.shift_cron_args(self.original_cron_args, *sub_job_args),
      )
      self.scheduler.add_job(sub_job_func, trigger=sub_trigger, id=sub_job_id, replace_existing=True)

  async def run_main_job(self) -> None:
    """Wrapper for main_job to handle error state."""
    if self.errored:
      logger.error(f"{self.__class__.__name__}: Job is in errored state. Skipping execution.")
      return
    self.last_run_time = datetime.now(tz=SETTINGS.tz)

    try:
      await self.main_job()
    except CanRescheduleJobError as e:
      self.error_reschedule(count=e.count_error, reason=e.reason)

    except JobError as e:
      logger.error(f"{self.__class__.__name__}: Job encountered a major error. Freezing this jobs execution", exc_info=e)
      self.errored = True
      FATAL_EVENT.set()  # trigger shutdown in main

    except Exception as e:
      logger.exception(f"{self.__class__.__name__}: Unexpected error in main_job:", exc_info=e)
      self.errored = True
      FATAL_EVENT.set()  # trigger shutdown in main

  @abstractmethod
  async def main_job(self) -> None:
    """Main job logic goes here. Override in subclasses."""
    raise NotImplementedError("Subclasses must implement the main_job method.")

  def cancel_self(self) -> None:
    """Cancels this job from the scheduler."""
    self.scheduler.remove_job(self.job_id)

  def schedule_self(self) -> None:
    """Schedules this job in the scheduler."""
    self.scheduler.add_job(self.main_job, self.trigger, id=self.job_id, replace_existing=True)

  def reset_schedule(self) -> None:
    """Resets the job's schedule to the original cron arguments."""
    self.trigger = self.original_trigger
    self.scheduler.reschedule_job(self.job_id, trigger=self.trigger)

  @overload
  def reschedule_self(
    self,
    new_trigger: BaseTrigger,
  ) -> None: ...

  @overload
  def reschedule_self(
    self,
    **kwargs: Unpack[CronArgs],
  ) -> None: ...

  def reschedule_self(self, new_trigger: BaseTrigger | None = None, **kwargs: Unpack[CronArgs]) -> None:
    """Reschedules this job with a new trigger."""
    if new_trigger is None:
      new_trigger = self.generate_trigger(**kwargs)
    self.scheduler.reschedule_job(self.job_id, trigger=new_trigger)

  def error_reschedule(self, count: bool = False, reason: str = "error in job") -> None:
    if count:
      self.err_counter += 1

      if self.err_counter >= self.err_max_threshold:
        logger.error(f"{self.__class__.__name__}: Maximum error threshold reached. Marking job as errored and triggering shutdown.")
        self.errored = True
        FATAL_EVENT.set()  # trigger shutdown in main
        return

    logger.info(f"{self.__class__.__name__}: Rescheduling due to {reason}")
    now = datetime.now(tz=SETTINGS.tz)
    new_fire_time = (
      self.last_run_time + relativedelta(minutes=self.reschedule_delay_minutes)
      if self.last_run_time is not None
      else now + relativedelta(minutes=self.reschedule_delay_minutes)
    )
    self.reschedule_self(new_trigger=DateTrigger(run_date=new_fire_time))

  @staticmethod
  def check_if_this_week(dt: datetime) -> bool:
    now_day = today(tzinfo=SETTINGS.tz)
    start_of_week = now_day - relativedelta(weekday=SU(-1))
    end_of_week = start_of_week + relativedelta(weekday=SA(+1), hour=23, minute=59, second=59, microsecond=999999)
    return start_of_week <= dt <= end_of_week

  def shift_cron_args(self, args: CronArgs, delta: timedelta | relativedelta, use_args: UseArgs = DEFAULT_USE_ARGS) -> CronArgs:
    """Shifts the cron arguments by a specified timedelta."""

    new_cron_args = {
      "year": args.get("year") if use_args.year else None,
      "month": args.get("month") if use_args.month else None,
      "day": args.get("day") if use_args.day else None,
      "day_of_week": args.get("day_of_week") if use_args.day_of_week else None,
      "hour": args.get("hour") if use_args.hour else None,
      "minute": args.get("minute") if use_args.minute else None,
      "second": args.get("second") if use_args.second else None,
      "timezone": args.get("timezone"),
    }

    reldel_args = {
      "year": args.get("year") if use_args.year else None,
      "month": args.get("month") if use_args.month else None,
      "day": args.get("day") if use_args.day else None,
      "day_of_week": dtutil_weekday_map[args.get("day_of_week")](-1) if use_args.day_of_week else None,
      "hour": args.get("hour") if use_args.hour else None,
      "minute": args.get("minute") if use_args.minute else None,
      "second": args.get("second") if use_args.second else None,
    }

    new_reldel = relativedelta(**reldel_args)

    # convert new_cron_args to a datetime by using the current time as a base and replacing the specified fields with the cron args values
    now = datetime.now(tz=SETTINGS.tz)
    base_dt = now.replace(**new_cron_args)

    shifted_dt = base_dt + new_reldel + delta

    # convert shifted_dt back to cron args by taking the relevant fields from the shifted datetime
    return CronArgs(
      year=shifted_dt.year if use_args.year else None,
      month=shifted_dt.month if use_args.month else None,
      day=shifted_dt.day if use_args.day else None,
      day_of_week=num_to_weekday_map[shifted_dt.weekday()] if use_args.day_of_week else None,
      hour=shifted_dt.hour if use_args.hour else None,
      minute=shifted_dt.minute if use_args.minute else None,
      second=shifted_dt.second if use_args.second else None,
      timezone=args.get("timezone"),
    )

  def generate_trigger(self, **kwargs: Unpack[CronArgs]) -> CronTrigger:
    """Generates a trigger based on the provided cron arguments."""
    return CronTrigger(**kwargs)
