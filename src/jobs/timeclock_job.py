# Standard library imports
from logging import getLogger
from pathlib import Path
from subprocess import run
from sys import executable

# First party imports
from environment_init_vars import CWD, SETTINGS
from jobs import JobBase

logger = getLogger(__name__)

TIMECLOCK_PLAYGROUND = CWD / "timeclock_playground"
EMPLOYEE_INPUT = TIMECLOCK_PLAYGROUND / "employee_input"
FONT_INPUT = TIMECLOCK_PLAYGROUND / "font_input"

TIMECLOCK_PLAYGROUND.mkdir(exist_ok=True)

EMPLOYEE_INPUT.unlink(missing_ok=True)
FONT_INPUT.unlink(missing_ok=True)

EMPLOYEE_INPUT.symlink_to(SETTINGS.timeclock_employee_input_loc, target_is_directory=True)
FONT_INPUT.symlink_to(SETTINGS.timeclock_font_input_loc, target_is_directory=True)
# EMPLOYEE_INPUT.mkdir(exist_ok=True)
# FONT_INPUT.mkdir(exist_ok=True)


class TimeclockJob(JobBase):
  @staticmethod
  def _debug_get_fixed_timeclock_src() -> Path:
    # Standard library imports
    from site import getsitepackages

    # Resolve editable install source without relying on importing the package first.
    for site_dir in getsitepackages():
      site_path = Path(site_dir)
      for pth_file in site_path.glob("__editable__.timeclock_entry_processor-*.pth"):
        src_path = Path(pth_file.read_text(encoding="utf-8").strip())
        if src_path.exists():
          return src_path

    # Fallback: resolve from the installed module if .pth lookup is unavailable.
    # First party imports
    import timeclock_entry_processor

    return Path(timeclock_entry_processor.__file__).resolve().parents[1]

  @staticmethod
  def _create_fixed_env() -> dict[str, str]:
    # Standard library imports
    import os

    child_env = os.environ.copy()
    child_env["PWD"] = str(TIMECLOCK_PLAYGROUND)

    if __debug__:
      timeclock_src = TimeclockJob._debug_get_fixed_timeclock_src()
      existing_pythonpath = child_env.get("PYTHONPATH", "")
      child_env["PYTHONPATH"] = str(timeclock_src) if not existing_pythonpath else f"{timeclock_src}{os.pathsep}{existing_pythonpath}"

    return child_env

  @staticmethod
  def run_processor(csv_file: Path) -> None:
    # Run timeclock_entry_processor as a subprocess via its CLI.

    env = TimeclockJob._create_fixed_env()

    run(
      [executable, "-m", "timeclock_entry_processor", str(csv_file)],
      check=True,
      env=env,
      cwd=str(TIMECLOCK_PLAYGROUND),
    )


if __name__ == "__main__":
  csv_file = CWD / "Time-Clock-Entry-Report_2026-05-14_19-31-12.csv"
  TimeclockJob.run_processor(csv_file)
