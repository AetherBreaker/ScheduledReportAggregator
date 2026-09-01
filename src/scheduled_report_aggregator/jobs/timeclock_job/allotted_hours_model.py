"""Pydantic model for one store's row in the allotted-hours Google Sheet."""

# Standard library imports
from decimal import Decimal
from typing import Annotated, Any

# Third party imports
from pydantic import BaseModel, BeforeValidator, ConfigDict

__all__ = ["AllottedHoursModel"]


def zero_if_err(value: Any) -> Decimal | Any:
  try:
    return Decimal(value)
  except Exception:  # noqa: BLE001
    return Decimal(0)


class AllottedHoursModel(BaseModel):
  """One store's allotted and training hours, with malformed training values coerced to zero."""

  model_config = ConfigDict(extra="ignore")

  store: int
  allotted_hours: int
  training_hours: Annotated[Decimal, BeforeValidator(zero_if_err)]
