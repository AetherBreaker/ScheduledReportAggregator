"""Pydantic-settings model for this app's environment configuration and secret file locations."""

# Standard library imports
from logging import getLogger
from os import environ
from pathlib import Path
from typing import Annotated

# Third party imports
from pydantic import Field

# First party imports
from aeth_ext.settings import BaseSettings

logger = getLogger(__name__)

environ.setdefault("PYDANTIC_ERRORS_INCLUDE_URL", "false")


__all__ = ["Settings"]


class Settings(BaseSettings):
  """App settings sourced from the environment (and `.env` in debug), plus derived paths."""

  # `model_config` and `persisted_dir_loc` are inherited: aeth_ext's `BaseSettings` already
  # handles the `.env`-in-debug / env-only-under-`-O` split and the `/app/persisted_data` default.

  # Derived from the *resolved* `persisted_dir_loc` (env override included), not the class-level
  # default -- the same `default_factory` pattern aeth_ext uses for `log_loc_folder`.
  timeclock_employee_input_loc: Annotated[
    Path,
    Field(alias="TIMECLOCK_EMPLOYEE_INPUT_LOC", default_factory=lambda data: data["persisted_dir_loc"] / "timeclock_employee_input"),
  ]
  timeclock_font_input_loc: Annotated[
    Path,
    Field(alias="TIMECLOCK_FONT_INPUT_LOC", default_factory=lambda data: data["persisted_dir_loc"] / "timeclock_font_input"),
  ]

  allotted_hours_sheet_id: Annotated[str, Field(alias="ALLOTTED_HOURS_SHEET_ID")] = (
    # "1Fn1FBZZAQwrB6v-wkMGkeIN12Aui7SyZvYpEBvc4Wjk"  # Production sheet ID
    "1XW_SPFAHw9oRCB-a9ppDjh3Ln1sGvvLNrezn4dwPoKg"  # Testing sheet ID
    if __debug__
    else "1Fn1FBZZAQwrB6v-wkMGkeIN12Aui7SyZvYpEBvc4Wjk"  # Production sheet ID
  )
  employee_discounts_report_sheet_id: Annotated[str, Field(alias="EMPLOYEE_DISCOUNTS_REPORT_SHEET_ID")] = (
    "14n2dIZ1A1DKy1BEZt82sJOg9d14R67EFFL0yQSNUhuI"
  )

  @property
  def sft_website_creds_file(self) -> Path:
    """Path to the SFT website FTP credentials JSON, erroring if the file is missing."""
    return self._creds_file_reusable("SFT website creds file not found at expected location", "secrets", "sft_ftp_creds.json")

  @property
  def sas_ftp_creds_file(self) -> Path:
    """Path to the SAS FTP credentials JSON, erroring if the file is missing."""
    return self._creds_file_reusable("SAS FTP creds file not found at expected location", "secrets", "sas_ftp_creds.json")

  @property
  def ryo_ftp_creds_file(self) -> Path:
    """Path to the RYO FTP credentials JSON, erroring if the file is missing."""
    return self._creds_file_reusable("RYO FTP creds file not found at expected location", "secrets", "ryo_ftp_creds.json")

  @property
  def google_api_key_file(self) -> Path:
    """Path to the Google service-account key JSON, erroring if the file is missing."""
    return self._creds_file_reusable(
      "Google API key file not found at expected location", "secrets", "scheduledreportaggregator-bdd6c704c6b1.json"
    )
