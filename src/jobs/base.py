# Standard library imports
from abc import abstractmethod
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from inspect import iscoroutinefunction
from logging import getLogger
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

# Third party imports
from apscheduler.triggers.cron import CronTrigger
from dateutil.relativedelta import FR, MO, SA, SU, TH, TU, WE, relativedelta

# First party imports
from custom_types import DEFAULT_USE_ARGS, CronArgs, DayOfWeek, JobID, JobIDPrefix, JobIDSuffix, SubJobTriggerArgs, UseArgs
from environment_init_vars import CWD, SETTINGS
from ftp_configs import RYOSFTPClient, SASSFTPClient, SFTSFTPClient
from sft_ext.errors.err_handling import FATAL_EVENT
from sft_ext.ftp.adapter import AdaptedSFTP, FTPAdapter
from sft_ext.types.abc import SingletonTypeABC
from sft_ext.utils import today

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Coroutine
  from datetime import timedelta
  from typing import Any, ClassVar, Unpack

  # Third party imports
  from dateutil._common import weekday

  # First party imports
  from scheduler_config import Scheduler

logger = getLogger(__name__)


FTP_CVAR = ContextVar("FTP_CVAR")


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


__all__ = ["HOLDING_FOLDER", "CanRescheduleJobError", "JobBase", "JobError"]


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


HOLDING_FOLDER = CWD / "file_holding"


class JobBase(metaclass=SingletonTypeABC):
  jobname_cvar = FTP_CVAR

  ftp_handlers: ClassVar = {
    "sft": FTPAdapter[AdaptedSFTP](SFTSFTPClient, container_cvar=FTP_CVAR),
    "sas": FTPAdapter[AdaptedSFTP](SASSFTPClient, container_cvar=FTP_CVAR),
    "ryo": FTPAdapter[AdaptedSFTP](RYOSFTPClient, container_cvar=FTP_CVAR),
  }

  errored: bool = False  # used by main to check whether this job experienced an error
  err_counter: int = 0
  err_max_threshold: int = 3  # number consecutive errors before setting errored state, triggering shutdown

  reschedule_delay_minutes: ClassVar[int] = 10  # minutes to delay when rescheduling after an error

  reports_pickup_base_folder = PurePosixPath("/upload")

  reports_pickup_folder: PurePosixPath

  jobs_register: dict[JobIDSuffix, tuple[Callable[..., Any], CronArgs | SubJobTriggerArgs]]
  extra_jobs_register: dict[JobIDSuffix, tuple[Callable[..., Any], SubJobTriggerArgs]]

  active_jobs: dict[JobID, CronArgs | SubJobTriggerArgs]

  active_args: dict[JobID, CronArgs]

  job_id: ContextVar[JobID] = ContextVar("job_id")

  base_job_id: JobIDPrefix
  scheduler: Scheduler
  jobstore: str

  def __init__(self):
    self.active_jobs = {}  # track active jobs for cleanup if needed
    self.extra_jobs_register = {}

    self.job_holding_folder = HOLDING_FOLDER / self.__class__.__name__.lower()
    self.job_holding_folder.mkdir(parents=True, exist_ok=True)

    self.__post_init__()  # call post init hook for any additional setup in subclasses

  @classmethod
  def init_job(
    cls,
    scheduler: Scheduler,
    job_id: JobIDPrefix,
    jobstore: str = "general_jobs",
    **kwargs: Unpack[CronArgs],
  ) -> JobBase:
    self = cls()
    self.base_job_id = job_id
    self.scheduler = scheduler
    self.jobstore = jobstore

    self.main_cron_args = CronArgs(**kwargs)

    self.jobs_register = {
      "main_job": (self.main_job, self.main_cron_args),
    }

    return self

  def __post_init__(self): ...

  def schedule_registered_jobs(self, base_cron_args: CronArgs | None = None) -> None:
    """Hook for adding sub-jobs to the scheduler. Override in subclasses if needed."""
    for job_id_suffix, (job_func, job_args) in self.jobs_register.items():
      wrapped_func, trigger, job_id = self.prep_job(job_func, job_args, job_id_suffix, base_cron_args or self.main_cron_args)

      self.scheduler.add_job(wrapped_func, trigger=trigger, id=job_id, replace_existing=True)

  def prep_job(
    self,
    func: Callable[..., Any],
    trigger_args: CronArgs | SubJobTriggerArgs,
    job_id_suffix: str,
    base_cron_args: CronArgs | None = None,
  ) -> tuple[Callable[..., Any], CronTrigger, JobID]:
    """Schedules a job with the given function, trigger arguments, and job ID suffix."""
    job_id: JobID = f"{self.base_job_id}_{job_id_suffix}"

    evaled_args = (
      self.shift_cron_args(base_cron_args or self.main_cron_args, *trigger_args)  # Is sub job with delta args
      if isinstance(trigger_args, SubJobTriggerArgs)
      else trigger_args  # Is main job
    )

    trigger = CronTrigger(**evaled_args)

    # Add the job to the active jobs dict with its trigger args for tracking.
    self.active_jobs[job_id] = trigger_args
    self.active_args[job_id] = evaled_args

    # Wrap job in run_job to handle error state and rescheduling logic, then add to scheduler
    wrapped_func = self.run_job(func, job_id)

    return wrapped_func, trigger, job_id

  def run_job[**Params_T, Return_T: Any](
    self, func: Callable[Params_T, Return_T], job_id: JobID
  ) -> Callable[Params_T, Coroutine[Any, Any, Return_T | None]]:
    """Wrapper for main_job to handle error state."""

    @wraps(func)
    async def wrapper(*args, **kwargs) -> Return_T | None:
      if self.errored:
        logger.error(f"{self.__class__.__name__}: Job is in errored state. Skipping execution.")
        return

      with self.job_id.set(job_id), self.jobname_cvar.set(self.__class__.__name__):
        try:
          if iscoroutinefunction(func):
            return await func(*args, **kwargs)
          else:
            return func(*args, **kwargs)
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

    return wrapper

  @abstractmethod
  async def main_job(self) -> None:
    """Main job logic goes here. Override in subclasses."""
    raise NotImplementedError("Subclasses must implement the main_job method.")

  def cancel_self(self) -> None:
    """Cancels this job from the scheduler."""
    for job_id in self.active_jobs.copy().keys():
      self.scheduler.remove_job(job_id)
      self.active_jobs.pop(job_id, None)
      self.active_args.pop(job_id, None)

  def reset_schedule(self) -> None:
    """Resets the job's schedule to the original cron arguments."""
    self.cancel_self()
    self.schedule_registered_jobs()

  def reschedule_self(self, **kwargs: Unpack[CronArgs]) -> None:
    """Clears this job and rebuilds it's schedule with a new base trigger."""
    self.cancel_self()
    self.main_cron_args = CronArgs(**kwargs)
    self.jobs_register["main_job"] = (self.main_job, self.main_cron_args)
    self.schedule_registered_jobs()

  def error_reschedule(self, count: bool = False, reason: str = "error in job") -> None:
    if count:
      self.err_counter += 1

      if self.err_counter >= self.err_max_threshold:
        logger.error(f"{self.__class__.__name__}: Maximum error threshold reached. Marking job as errored and triggering shutdown.")
        self.errored = True
        FATAL_EVENT.set()  # trigger shutdown in main
        return

    logger.info(f"{self.__class__.__name__}: Rescheduling due to {reason}")

    delta = relativedelta(minutes=self.reschedule_delay_minutes)
    new_args = self.shift_cron_args(self.main_cron_args, delta)

    self.reschedule_self(**new_args)

  @staticmethod
  def check_if_this_week(dt: datetime) -> bool:
    now_day = today(tzinfo=SETTINGS.tz)
    start_of_week = now_day - relativedelta(weekday=SU(-1))
    end_of_week = start_of_week + relativedelta(weekday=SA(+1), hour=23, minute=59, second=59, microsecond=999999)
    return start_of_week <= dt <= end_of_week

  @staticmethod
  def extract_use_args(trigger_args: CronArgs) -> UseArgs:
    """
    Attempt to extract which cron args are being used in the provided trigger args to determine which ones to shift when rescheduling.
    If this fails, it will default to DEFAULT_USE_ARGS
    """
    try:
      return UseArgs(
        year="year" in trigger_args and trigger_args["year"] is not None,
        month="month" in trigger_args and trigger_args["month"] is not None,
        day="day" in trigger_args and trigger_args["day"] is not None,
        day_of_week="day_of_week" in trigger_args and trigger_args["day_of_week"] is not None,
        hour="hour" in trigger_args and trigger_args["hour"] is not None,
        minute="minute" in trigger_args and trigger_args["minute"] is not None,
        second="second" in trigger_args and trigger_args["second"] is not None,
      )
    except Exception:
      return DEFAULT_USE_ARGS

  def shift_cron_args(self, args: CronArgs, delta: timedelta | relativedelta, use_args: UseArgs | None = None) -> CronArgs:
    """Shifts the cron arguments by a specified timedelta."""
    if use_args is None:
      use_args = self.extract_use_args(args)

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
      year=(year if isinstance(year := args.get("year"), str) else shifted_dt.year) if use_args.year else None,
      month=(month if isinstance(month := args.get("month"), str) else shifted_dt.month) if use_args.month else None,
      day=(day if isinstance(day := args.get("day"), str) else shifted_dt.day) if use_args.day else None,
      day_of_week=num_to_weekday_map[shifted_dt.weekday()] if use_args.day_of_week else None,
      hour=(hour if isinstance(hour := args.get("hour"), str) else shifted_dt.hour) if use_args.hour else None,
      minute=(minute if isinstance(minute := args.get("minute"), str) else shifted_dt.minute) if use_args.minute else None,
      second=(second if isinstance(second := args.get("second"), str) else shifted_dt.second) if use_args.second else None,
      timezone=args.get("timezone"),
    )
