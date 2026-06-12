# Standard library imports
import sys
from logging import getLogger
from os import environ
from pathlib import Path
from typing import Annotated

# Third party imports
from pydantic import Field
from pydantic_settings import SettingsConfigDict

# First party imports
from sft_ext.settings import BaseSettings

logger = getLogger(__name__)

environ.setdefault("PYDANTIC_ERRORS_INCLUDE_URL", "false")


CWD = Path(__file__).parent if getattr(sys, "frozen", False) else Path.cwd()


class Settings(BaseSettings):
  model_config = (
    SettingsConfigDict(
      env_file=CWD / ".env",
      env_file_encoding="utf-8",
      env_ignore_empty=True,
      extra="ignore",
    )
    if __debug__
    else SettingsConfigDict()
  )

  persisted_dir_loc: Annotated[Path, Field(alias="PERSISTED_DIR_LOC")] = (
    CWD / "persisted_data" if __debug__ else Path("/app/persisted_data")
  )

  timeclock_employee_input_loc: Annotated[Path, Field(alias="TIMECLOCK_EMPLOYEE_INPUT_LOC")] = (
    persisted_dir_loc / "timeclock_employee_input"
  )
  timeclock_font_input_loc: Annotated[Path, Field(alias="TIMECLOCK_FONT_INPUT_LOC")] = persisted_dir_loc / "timeclock_font_input"

  @property
  def sft_website_creds_file(self) -> Path:
    return self.creds_file_reusable("SFT website creds file not found at expected location", "secrets", "sft_ftp_creds.json")

  @property
  def sas_ftp_creds_file(self) -> Path:
    return self.creds_file_reusable("SAS FTP creds file not found at expected location", "secrets", "sas_ftp_creds.json")

  @property
  def ryo_ftp_creds_file(self) -> Path:
    return self.creds_file_reusable("RYO FTP creds file not found at expected location", "secrets", "ryo_ftp_creds.json")
