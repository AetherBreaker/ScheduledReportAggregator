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

    # First party imports
    from scheduled_report_aggregator.startup import run_until_shutdown

    run_until_shutdown()
  except KeyboardInterrupt:
    # v8 made the shutdown exit nudge unconditional, and it is a simulated SIGINT. A shutdown driven
    # before `run_until_shutdown` installs its own guard -- most plausibly the central log server
    # rejecting this program's config during `initialize` -- would otherwise raise KeyboardInterrupt
    # somewhere in this frame (the deferred import alone takes seconds, against a 0.9s FATAL nudge)
    # and escape uncaught, exiting 130 with a "KeyboardInterrupt during import" traceback instead of
    # the code the recorded shutdown kind calls for.
    #
    # Mirrors `startup.exit_code_for_shutdown`, inlined because that module may be exactly what
    # failed to finish importing. Note `run_until_shutdown` exits via SystemExit, not
    # KeyboardInterrupt, so its exit code passes through here untouched.
    # First party imports
    from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownKind

    raise SystemExit(1 if SHUTDOWN.kind >= ShutdownKind.FATAL else 0) from None


if __name__ == "__main__":
  run_app()
