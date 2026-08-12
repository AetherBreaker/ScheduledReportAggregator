# Standard library imports
from typing import ClassVar, override

# First party imports
from scheduled_report_aggregator.jobs.base import JobBase


class TaxesJob(JobBase):
  reschedule_delay_minutes: ClassVar[int] = 10

  def __post_init__(self) -> None:
    self.job_output_folder = self.job_holding_folder / "output"
    self.job_output_folder.mkdir(parents=True, exist_ok=True)

  @override
  async def main_job(self) -> None: ...
