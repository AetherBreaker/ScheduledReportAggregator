"""Shared type aliases and small structural types used across the scheduler and jobs."""

# Standard library imports
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, TypedDict

# First party imports
from aeth_ext.types import StrEnum

if TYPE_CHECKING:
  # Standard library imports
  from datetime import timedelta
  from typing import NotRequired
  from zoneinfo import ZoneInfo

  # Third party imports
  from dateutil.relativedelta import relativedelta


__all__ = [
  "DEFAULT_USE_ARGS",
  "CronArgsType",
  "DayOfWeek",
  "JobID",
  "JobIDPrefix",
  "JobIDSuffix",
  "StoreNum",
  "SubJobTriggerArgs",
  "UseArgs",
]


class CronArgsType(TypedDict):
  """Keyword arguments accepted by apscheduler's CronTrigger, as a typed mapping."""

  year: NotRequired[int | str | None]
  month: NotRequired[int | str | None]
  day: NotRequired[int | str | None]
  day_of_week: NotRequired[DayOfWeek | None]
  hour: NotRequired[int | str | None]
  minute: NotRequired[int | str | None]
  second: NotRequired[int | str | None]
  timezone: NotRequired[ZoneInfo | None]


class DayOfWeek(StrEnum):
  """Weekday names mapped to apscheduler's three-letter cron day-of-week codes."""

  SUNDAY = "sun"
  MONDAY = "mon"
  TUESDAY = "tue"
  WEDNESDAY = "wed"
  THURSDAY = "thu"
  FRIDAY = "fri"
  SATURDAY = "sat"


@dataclass
class UseArgs:
  """Flags for which cron fields a trigger actually uses, so rescheduling shifts only those."""

  year: bool = False
  month: bool = False
  day: bool = False
  day_of_week: bool = True
  hour: bool = True
  minute: bool = True
  second: bool = False


DEFAULT_USE_ARGS = UseArgs()


class SubJobTriggerArgs(NamedTuple):
  """A sub-job's schedule offset from its parent job plus the cron fields it varies."""

  delta: timedelta | relativedelta
  use_args: UseArgs = DEFAULT_USE_ARGS


type JobID = str
type JobIDSuffix = JobID
type JobIDPrefix = JobID
type StoreNum = int


class IsPydantic: ...
