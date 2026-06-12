# Standard library imports
from atexit import register
from datetime import datetime
from logging import getLogger
from pathlib import Path
from subprocess import run
from sys import executable

# Third party imports
from apscheduler.triggers.date import DateTrigger

# First party imports
from environment_init_vars import CWD, SETTINGS
from jobs import JobBase
from scheduler_config import Scheduler

logger = getLogger(__name__)

TIMECLOCK_PLAYGROUND = CWD / "timeclock_playground"
SUBPROCESS_EMPLOYEE_INPUT = TIMECLOCK_PLAYGROUND / "employee_input"
SUBPROCESS_FONT_INPUT = TIMECLOCK_PLAYGROUND / "font_input"
SUBPROCESS_OUTPUT_FOLDER = TIMECLOCK_PLAYGROUND / "output"


class TimeclockJob(JobBase):
  def __post_init__(self) -> None:
    self.output_folder = self.job_holding_folder / "output"
    self.output_folder.mkdir(exist_ok=True)

    TIMECLOCK_PLAYGROUND.mkdir(exist_ok=True)

    SUBPROCESS_EMPLOYEE_INPUT.unlink(missing_ok=True)
    SUBPROCESS_FONT_INPUT.unlink(missing_ok=True)
    SUBPROCESS_OUTPUT_FOLDER.unlink(missing_ok=True)

    SUBPROCESS_EMPLOYEE_INPUT.symlink_to(SETTINGS.timeclock_employee_input_loc, target_is_directory=True)
    SUBPROCESS_FONT_INPUT.symlink_to(SETTINGS.timeclock_font_input_loc, target_is_directory=True)
    SUBPROCESS_OUTPUT_FOLDER.symlink_to(self.output_folder, target_is_directory=True)

    register(SUBPROCESS_EMPLOYEE_INPUT.unlink, missing_ok=True)
    register(SUBPROCESS_FONT_INPUT.unlink, missing_ok=True)
    register(SUBPROCESS_OUTPUT_FOLDER.unlink, missing_ok=True)

  async def main_job(self) -> None: ...

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

  _RUNPY_BOOTSTRAP = "import runpy, sys; sys.argv[0] = 'timeclock_entry_processor'; runpy.run_module('timeclock_entry_processor', run_name='__main__', alter_sys=True)"

  def run_processor(self, csv_file: Path) -> None:
    # Run timeclock_entry_processor as a subprocess via its CLI.

    if __debug__:
      exec_args = [
        executable,
        "-c",
        ";".join(
          [
            "import runpy, sys",
            "sys.argv[0] = 'timeclock_entry_processor'",
            "runpy.run_module('timeclock_entry_processor', run_name='__main__', alter_sys=True)",
          ]
        ),
        str(csv_file),
        str(self.output_folder),
      ]
    else:
      exec_args = [executable, "-m", "timeclock_entry_processor", str(csv_file), str(self.output_folder)]

    run(exec_args, check=True, cwd=str(TIMECLOCK_PLAYGROUND))


if __name__ == "__main__":
  csv_file = CWD / "Time-Clock-Entry-Report_2026-05-14_19-31-12.csv"

  TimeclockJob(
    Scheduler(),
    "test_timeclock_job",
    DateTrigger(run_date=datetime.now(tz=SETTINGS.tz)),
  ).run_processor(csv_file)
