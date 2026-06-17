# Standard library imports
from typing import TYPE_CHECKING, NamedTuple, TypedDict

# First party imports
from jobs import DEFAULT_USE_ARGS, UseArgs
from sft_ext.types import StrEnum

if TYPE_CHECKING:
  # Standard library imports
  from datetime import timedelta, timezone
  from typing import NotRequired
  from zoneinfo import ZoneInfo

  # Third party imports
  from dateutil.relativedelta import relativedelta


class DayOfWeek(StrEnum):
  SUNDAY = "sun"
  MONDAY = "mon"
  TUESDAY = "tue"
  WEDNESDAY = "wed"
  THURSDAY = "thu"
  FRIDAY = "fri"
  SATURDAY = "sat"


class CronArgs(TypedDict):
  year: NotRequired[int | str | None]
  month: NotRequired[int | str | None]
  day: NotRequired[int | str | None]
  day_of_week: NotRequired[DayOfWeek | None]
  hour: NotRequired[int | str | None]
  minute: NotRequired[int | str | None]
  second: NotRequired[int | str | None]
  timezone: NotRequired[ZoneInfo | timezone | None]


class SubJobTriggerArgs(NamedTuple):
  delta: timedelta | relativedelta
  use_args: UseArgs = DEFAULT_USE_ARGS


type JobID = str
type JobIDSuffix = JobID
type JobIDPrefix = JobID
