# Standard library imports
import os
from logging import getLogger
from pathlib import Path

# First party imports
from environment_settings import Settings

logger = getLogger(__name__)

if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
  logger.warning("Process is running as root on a Unix system. This is not recommended for production.")  # pyright: ignore[reportUnreachable]


__all__ = ["CWD", "SETTINGS"]

# Settings
SETTINGS = Settings.get_settings()

# Folder paths
CWD = Path.cwd()
