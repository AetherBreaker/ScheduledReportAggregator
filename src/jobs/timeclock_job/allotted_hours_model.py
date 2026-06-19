# Third party imports
from pydantic import BaseModel, ConfigDict

__all__ = ["AllottedHoursModel"]


class AllottedHoursModel(BaseModel):
  model_config = ConfigDict(extra="ignore")

  store: int
  allotted_hours: int
