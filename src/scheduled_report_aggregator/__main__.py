"""Application entry point: initializes aeth_ext then hands off to the async startup routine."""

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
HEARTBEAT_SLUG = "scheduled-report-aggregator"
# TESTING = __debug__


def run_app() -> None:
  """Run the main application loop."""
  try:
    # initialize(asyncio=True, logging=True)
    initialize(asyncio=True, logging="socket")

    # Deferred so `initialize` runs before `startup`'s module-level side effects (and before asyncio
    # is first imported).
    # Standard library imports
    from asyncio import run

    # First party imports
    from scheduled_report_aggregator.startup import main

    run(main())

  except KeyboardInterrupt:
    # aeth_ext's v8 shutdown exit nudge: an unconditional simulated SIGINT. Normally `main()`
    # returns on its own after awaiting SHUTDOWN_COMPLETE and the nudge is skipped; it lands here
    # only if `main()`'s tail or asyncio's own close outran the shutdown budget, or if a shutdown
    # was driven during the import window above -- most plausibly the central log server rejecting
    # this program's config during `initialize` (the deferred import alone takes seconds, against a
    # 0.9s FATAL nudge). Not an error either way; the exit code below says how we stopped.
    pass

  # Imported here, not at the top of the module, because `startup` may be exactly what failed to
  # finish importing -- this module stays reachable regardless.
  # First party imports
  from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownKind

  raise SystemExit(1 if SHUTDOWN.kind >= ShutdownKind.FATAL else 0)


if __name__ == "__main__":
  run_app()
