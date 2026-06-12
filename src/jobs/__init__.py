# Standard library imports
from abc import abstractmethod
from contextvars import ContextVar
from datetime import datetime
from logging import getLogger
from typing import TYPE_CHECKING, TypedDict, overload

# Third party imports
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from dateutil.relativedelta import relativedelta

# First party imports
from environment_init_vars import HOLDING_FOLDER, SETTINGS
from ftp_configs import RYOSFTPClient, SASSFTPClient, SFTSFTPClient
from sft_ext.errors.err_handling import FATAL_EVENT
from sft_ext.ftp.adapter import AdaptedSFTP, FTPAdapter
from sft_ext.types import StrEnum
from sft_ext.types.abc import SingletonTypeABC

if TYPE_CHECKING:
  # Standard library imports
  from datetime import timezone
  from typing import ClassVar, NotRequired, Unpack
  from zoneinfo import ZoneInfo

  # Third party imports
  from apscheduler.triggers.base import BaseTrigger

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


class CronArgs(TypedDict):
  year: NotRequired[int | str]
  month: NotRequired[int | str]
  day: NotRequired[int | str]
  day_of_week: NotRequired[DayOfWeek]
  hour: NotRequired[int | str]
  minute: NotRequired[int | str]
  second: NotRequired[int | str]
  timezone: NotRequired[ZoneInfo | timezone]


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

    self.cron_args = kwargs  # save ioriginal cron args to reset schedule each week
    self.temp_cron_args = kwargs  # replace stored cron args
    self.last_run_time = None

    if trigger is None:
      self.original_trigger = self.generate_trigger(**kwargs)
    else:
      self.original_trigger = trigger
    self.trigger = self.original_trigger

    self.job_holding_folder = HOLDING_FOLDER / self.__class__.__name__.lower()
    self.job_holding_folder.mkdir(parents=True, exist_ok=True)

    self.sub_jobs_hook(scheduler)
    self.schedule_self()
    self.__post_init__()  # call post init hook for any additional setup in subclasses

  def __post_init__(self):
    pass

  def sub_jobs_hook(self, scheduler: Scheduler) -> None:
    """Hook for adding sub-jobs to the scheduler. Override in subclasses if needed."""
    pass

  async def run_main_job(self) -> None:
    """Wrapper for main_job to handle error state."""
    if self.errored:
      logger.error(f"{self.__class__.__name__}: Job is in errored state. Skipping execution.")
      return
    self.last_run_time = datetime.now(tz=SETTINGS.tz)
    await self.main_job()

  @abstractmethod
  async def main_job(self) -> None:
    """Main job logic goes here. Override in subclasses."""
    raise NotImplementedError("Subclasses must implement the main_job method.")

  def cancel_self(self) -> None:
    """Cancels this job from the scheduler."""
    self.scheduler.remove_job(self.job_id)

  def schedule_self(self) -> None:
    """Schedules this job in the scheduler."""
    self.scheduler.add_job(self.main_job, self.original_trigger, id=self.job_id, replace_existing=True)

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

  def generate_trigger(self, **kwargs: Unpack[CronArgs]) -> BaseTrigger:
    """Generates a trigger based on the provided cron arguments."""
    self.temp_cron_args = kwargs  # store the alternative cron args

    return CronTrigger(**kwargs)

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
