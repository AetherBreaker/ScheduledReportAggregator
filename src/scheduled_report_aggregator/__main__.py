# Standard library imports
from sys import platform

# Third party imports
from rich.console import Console

# First party imports
from aeth_ext import initialize

RICH_CONSOLE = Console(
  width=None if platform == "win32" else 165,
  log_time=platform == "win32",
)
PROJECT_NAME = "scheduled-report-aggregator"


def run_app() -> None:
  """Run the main application loop."""
  initialize(asyncio=True, logging="socket")

  # Standard library imports
  from asyncio import run

  # First party imports
  from scheduled_report_aggregator.startup import main

  run(main())


if __name__ == "__main__":
  run_app()
