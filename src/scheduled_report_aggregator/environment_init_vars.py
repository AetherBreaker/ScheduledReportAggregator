"""Process-wide singletons resolved at import time: the Settings instance and working directory."""

# Standard library imports
import os
from logging import getLogger
from pathlib import Path
from tempfile import gettempdir

# Local folder imports
from .environment_settings import Settings

logger = getLogger(__name__)

if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
  logger.warning("Process is running as root on a Unix system. This is not recommended for production.")  # pyright: ignore[reportUnreachable]


__all__ = ["CWD", "SCRATCH_DIR", "SETTINGS"]

# Settings
SETTINGS = Settings.get_settings()

# Folder paths
CWD = Path.cwd()

# Root for ephemeral scratch directories (per-job holding folders, the timeclock subprocess playground).
# In debug runs they stay beside the code so their contents can be inspected after a run. Under `-O`
# (the Docker image sets `PYTHONOPTIMIZE=1`) they move to the system temp dir: the standardized image
# keeps `/app` root-owned and runs the app as `nonroot`, so nothing can be created there at runtime.
SCRATCH_DIR = CWD if __debug__ else Path(gettempdir()) / "scheduled_report_aggregator"
