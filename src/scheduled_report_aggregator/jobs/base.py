"""Shared job infrastructure: cron argument handling, the JobBase singleton, and job errors."""

# Standard library imports
from abc import abstractmethod
from contextvars import ContextVar
from datetime import datetime, timedelta
from functools import wraps
from inspect import iscoroutinefunction
from json import loads
from logging import getLogger
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

# Third party imports
from apscheduler.triggers.cron import CronTrigger
from dateutil.relativedelta import FR, MO, SA, SU, TH, TU, WE, relativedelta
from pydantic import SecretStr
from pydantic.dataclasses import dataclass

# First party imports
from aeth_ext.errors.shutdown import ShutdownKind, run_shutdown
from aeth_ext.ftp import create_ftp_adapter
from aeth_ext.ftp.credentials import SFTPCredentials
from aeth_ext.types import IsPydantic
from aeth_ext.types.abc import SingletonTypeABC
from aeth_ext.utils import today
from scheduled_report_aggregator.custom_types import DEFAULT_USE_ARGS, CronArgsType, DayOfWeek, SubJobTriggerArgs, UseArgs
from scheduled_report_aggregator.environment_init_vars import CWD, SETTINGS

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Coroutine
  from datetime import timedelta
  from pathlib import Path
  from typing import Any, ClassVar, Unpack

  # Third party imports
  from dateutil._common import weekday

  # First party imports
  from aeth_ext.ftp.pool.sftp_adapter import SFTPAdapter
  from scheduled_report_aggregator.custom_types import JobID, JobIDPrefix, JobIDSuffix
  from scheduled_report_aggregator.scheduler_config import Scheduler

logger = getLogger(__name__)


FTP_CVAR: ContextVar[str] = ContextVar("FTP_CVAR")


def _load_sftp_credentials(creds_file: Path) -> SFTPCredentials:
  """Builds redacting SFTP credentials from a ``{"HOSTNAME", "USER", "PWD", "PORT"?}`` JSON secrets file.

  The parsed plaintext lives only inside this frame: the password leaves it wrapped in a
  ``SecretStr`` (rendered as ``**********`` by repr/str/logging) and is only unwrapped by aeth_ext at
  the paramiko connect call.
  """
  raw = loads(creds_file.read_text())
  try:
    return SFTPCredentials(
      host=raw["HOSTNAME"],
      username=raw["USER"],
      password=SecretStr(raw["PWD"]),
      port=int(raw.get("PORT", 22)),
      host_key_policy="auto_add",
    )
  finally:
    del raw


DTUTIL_WEEKDAY_MAP: dict[DayOfWeek | None, weekday] = {
  DayOfWeek.MONDAY: MO,
  DayOfWeek.TUESDAY: TU,
  DayOfWeek.WEDNESDAY: WE,
  DayOfWeek.THURSDAY: TH,
  DayOfWeek.FRIDAY: FR,
  DayOfWeek.SATURDAY: SA,
  DayOfWeek.SUNDAY: SU,
  None: lambda x: None,  # for when day_of_week is not specified in cron args # type: ignore
}

NUM_TO_WEEKDAY_MAP: dict[int, DayOfWeek] = {
  0: DayOfWeek.MONDAY,
  1: DayOfWeek.TUESDAY,
  2: DayOfWeek.WEDNESDAY,
  3: DayOfWeek.THURSDAY,
  4: DayOfWeek.FRIDAY,
  5: DayOfWeek.SATURDAY,
  6: DayOfWeek.SUNDAY,
}


__all__ = ["HOLDING_FOLDER", "CanRescheduleJobError", "JobBase", "JobError"]
HOLDING_FOLDER = CWD / "file_holding"


@dataclass
class CronArgs(IsPydantic):
  year: int | str | None = None
  month: int | str | None = None
  day: int | str | None = None
  day_of_week: DayOfWeek | None = None
  hour: int | str | None = None
  minute: int | str | None = None
  second: int | str | None = None
  timezone: ZoneInfo | None = SETTINGS.tz

  def keys(self):
    return self.__dict__.keys()

  def __getitem__(self, key: str) -> Any:
    """Returns the value for a given field name."""
    return getattr(self, key)

  def get(self, key: str, default: Any = None) -> Any:
    return self.__dict__.get(key, default)

  def __contains__(self, key: str) -> bool:
    return key in self.__dict__


class CanRescheduleJobError(Exception):
  """Custom exception to indicate that a job should be automatically rescheduled."""

  def __init__(self, message: str, reason: str | None = None, count_error: bool = False):
    """Store the reschedule reason and whether this failure counts toward the error threshold."""
    super().__init__(message)
    self.reason = reason or message
    self.count_error = count_error


class JobError(Exception):
  """Custom exception to indicate that a job has encountered an error."""

  def __init__(self, message: str, reason: str | None = None, count_error: bool = False):
    """Store the error reason and whether this failure counts toward the error threshold."""
    super().__init__(message)
    self.reason = reason or message
    self.count_error = count_error


type FTPHandlerKey = Literal["sft", "sas", "ryo"]

type FTPHandlersType = dict[FTPHandlerKey, SFTPAdapter]


class JobBase(metaclass=SingletonTypeABC):
  """Singleton base for all scheduled jobs: FTP access, scheduling, and error/reschedule logic."""

  jobname_cvar = FTP_CVAR

  # One pooled adapter per server, shared by every job (class-level, like the singleton jobs
  # themselves). `container_cvar` labels each session's log lines with the running job's name.
  ftp_handlers: ClassVar[FTPHandlersType] = {
    "sft": create_ftp_adapter(_load_sftp_credentials(SETTINGS.sft_website_creds_file), container_cvar=FTP_CVAR),
    "sas": create_ftp_adapter(_load_sftp_credentials(SETTINGS.sas_ftp_creds_file), container_cvar=FTP_CVAR),
    "ryo": create_ftp_adapter(_load_sftp_credentials(SETTINGS.ryo_ftp_creds_file), container_cvar=FTP_CVAR),
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
    """Set up job tracking, create the holding folder, and run the subclass post-init hook."""
    self.active_jobs = {}  # track active jobs for cleanup if needed
    self.active_args = {}  # track active jobs' trigger args for rescheduling logic
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
    **kwargs: Unpack[CronArgsType],
  ) -> JobBase:
    """Bind the singleton to a scheduler, base job id, and its main cron schedule."""
    self = cls()
    self.base_job_id = job_id
    self.scheduler = scheduler
    self.jobstore = jobstore

    self.main_cron_args = CronArgs(**kwargs)

    self.jobs_register = {
      "main_job": (self.main_job, self.main_cron_args),
    }

    return self

  def __post_init__(self):
    """Hook for subclass setup after `__init__`; the base implementation does nothing."""

  def schedule_registered_jobs(self, base_cron_args: CronArgs | None = None) -> None:
    """Hook for adding sub-jobs to the scheduler. Override in subclasses if needed."""
    now = datetime.now(tz=SETTINGS.tz)
    for job_id_suffix, (job_func, job_args) in self.jobs_register.items():
      wrapped_func, trigger, job_id = self.prep_job(job_func, job_args, job_id_suffix, base_cron_args or self.main_cron_args)
      logger.info("%s: Scheduling job '%s' to run at %s", self.__class__.__name__, job_id, trigger.get_next_fire_time(None, now))

      self.scheduler.add_job(
        wrapped_func,
        trigger=trigger,
        id=job_id,
        replace_existing=True,
        misfire_grace_time=None,  # pyright: ignore[reportArgumentType]
      )

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
    async def wrapper(*args: Params_T.args, **kwargs: Params_T.kwargs) -> Return_T | None:
      if self.errored:
        logger.error("%s: Job is in errored state. Skipping execution.", self.__class__.__name__)
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
          logger.error("%s: Job encountered a major error. Freezing this jobs execution", self.__class__.__name__, exc_info=e)
          self.errored = True
          run_shutdown(ShutdownKind.FATAL)  # drive the shutdown `main()` is awaiting

        except Exception:
          # Anything beyond CanRescheduleJobError/JobError is unexpected: mark
          # the job errored and defer to aeth_ext's handle_fatal_exc_sync (wired
          # up on the executor's future callback in scheduler_config.py) for
          # logging, alerting, and driving the fatal shutdown.
          self.errored = True
          raise

    return wrapper

  @abstractmethod
  async def main_job(self) -> None:
    """Main job logic goes here. Override in subclasses."""
    raise NotImplementedError("Subclasses must implement the main_job method.")

  def cancel_self(self) -> None:
    """Cancels this job from the scheduler."""
    for job_id in self.active_jobs.copy():
      self.scheduler.remove_job(job_id)
      self.active_jobs.pop(job_id, None)
      self.active_args.pop(job_id, None)

  def reset_schedule(self) -> None:
    """Resets the job's schedule to the original cron arguments."""
    self.cancel_self()
    self.schedule_registered_jobs()

  def reschedule_self(self, **kwargs: Unpack[CronArgsType]) -> None:
    """Clears this job and rebuilds it's schedule with a new base trigger."""
    self.cancel_self()
    self.main_cron_args = CronArgs(**kwargs)
    self.jobs_register["main_job"] = (self.main_job, self.main_cron_args)
    self.schedule_registered_jobs()

  def error_reschedule(self, count: bool = False, reason: str = "error in job") -> None:
    """Reschedule after a failure; at the consecutive-error threshold, trigger shutdown instead."""
    if count:
      self.err_counter += 1

      if self.err_counter >= self.err_max_threshold:
        logger.error("%s: Maximum error threshold reached. Marking job as errored and triggering shutdown.", self.__class__.__name__)
        self.errored = True
        run_shutdown(ShutdownKind.FATAL)  # drive the shutdown `main()` is awaiting
        return

    logger.info("%s: Rescheduling due to %s", self.__class__.__name__, reason)

    delta = relativedelta(minutes=self.reschedule_delay_minutes)
    new_args = self.shift_cron_args(self.main_cron_args, delta)

    self.reschedule_self(**new_args)

  @staticmethod
  def check_if_this_week(dt: datetime) -> bool:
    """Whether the given datetime falls in the current Sunday-through-Saturday week."""
    now_day = today(tzinfo=SETTINGS.tz)
    start_of_week = now_day - relativedelta(weekday=SU(-1))
    end_of_week = start_of_week + relativedelta(weekday=SA(+1), hour=23, minute=59, second=59, microsecond=999999)
    return start_of_week <= dt <= end_of_week

  @staticmethod
  def extract_use_args(trigger_args: CronArgs) -> UseArgs:
    """Extract which cron args the trigger uses, to determine which to shift when rescheduling.

    If this fails, it will default to DEFAULT_USE_ARGS.
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
      logger.exception("Failed to extract use_args from trigger_args. Defaulting to DEFAULT_USE_ARGS.")
      return DEFAULT_USE_ARGS

  def shift_cron_args(self, args: CronArgs, delta: timedelta | relativedelta, use_args: UseArgs | None = None) -> CronArgs:
    """Shifts the cron arguments by a specified timedelta."""
    if use_args is None:
      use_args = self.extract_use_args(args)

    new_cron_args = {
      "year": args.get("year") if use_args.year else None,
      "month": args.get("month") if use_args.month else None,
      "day": args.get("day") if use_args.day else None,
      # "day_of_week": args.get("day_of_week") if use_args.day_of_week else None,
      "hour": args.get("hour") if use_args.hour else None,
      "minute": args.get("minute") if use_args.minute else None,
      "second": args.get("second") if use_args.second else None,
      "tzinfo": args.get("timezone"),
    }

    reldel_args = {
      "year": args.get("year") if use_args.year else None,
      "month": args.get("month") if use_args.month else None,
      "day": args.get("day") if use_args.day else None,
      "weekday": DTUTIL_WEEKDAY_MAP[args.get("day_of_week")](-1) if use_args.day_of_week else None,
      "hour": args.get("hour") if use_args.hour else None,
      "minute": args.get("minute") if use_args.minute else None,
      "second": args.get("second") if use_args.second else None,
    }

    new_reldel = relativedelta(**reldel_args)

    # convert new_cron_args to a datetime by using the current time as a base and replacing the specified fields with the cron args values
    now = datetime.now(tz=SETTINGS.tz)
    # Filter out None values and tzinfo=True (meaning "keep existing") so datetime.replace() only receives valid args
    replace_args = {k: v for k, v in new_cron_args.items() if v is not None}
    base_dt = now.replace(**replace_args)

    shifted_dt = base_dt + new_reldel + delta

    # convert shifted_dt back to cron args by taking the relevant fields from the shifted datetime
    return CronArgs(
      year=(year if isinstance(year := args.get("year"), str) else shifted_dt.year) if use_args.year else None,
      month=(month if isinstance(month := args.get("month"), str) else shifted_dt.month) if use_args.month else None,
      day=(day if isinstance(day := args.get("day"), str) else shifted_dt.day) if use_args.day else None,
      day_of_week=NUM_TO_WEEKDAY_MAP[shifted_dt.weekday()] if use_args.day_of_week else None,
      hour=(hour if isinstance(hour := args.get("hour"), str) else shifted_dt.hour) if use_args.hour else None,
      minute=(minute if isinstance(minute := args.get("minute"), str) else shifted_dt.minute) if use_args.minute else None,
      second=(second if isinstance(second := args.get("second"), str) else shifted_dt.second) if use_args.second else None,
      timezone=args.get("timezone"),
    )
