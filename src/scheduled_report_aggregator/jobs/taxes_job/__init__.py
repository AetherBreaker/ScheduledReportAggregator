"""Tobacco tax filing job (scaffolding; per-state logic lives in the states submodule)."""

# Standard library imports
from typing import ClassVar, override

# First party imports
from scheduled_report_aggregator.jobs.base import JobBase


class TaxesJob(JobBase):
  """Job that will assemble and submit state tobacco tax filings (not yet implemented)."""

  reschedule_delay_minutes: ClassVar[int] = 10

  def __post_init__(self) -> None:
    """Create this job's output folder under its holding folder."""
    self.job_output_folder = self.job_holding_folder / "output"
    self.job_output_folder.mkdir(parents=True, exist_ok=True)

  @override
  def main_job(self) -> None: ...
