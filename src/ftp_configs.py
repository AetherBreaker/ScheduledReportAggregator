# Standard library imports
from json import loads
from logging import getLogger
from socket import gaierror
from typing import override

# Third party imports
from paramiko import AutoAddPolicy, SFTPClient, SSHClient

# First party imports
from environment_init_vars import SETTINGS
from sft_ext.ftp.adapter import ProtocolEnum, ServerNotAvailableError, SFTPProtocol

logger = getLogger(__name__)


__all__ = ["RYOSFTPClient", "SASSFTPClient", "SFTSFTPClient"]


class SFTSFTPClient(SFTPProtocol):
  policy = AutoAddPolicy()
  creds = loads(SETTINGS.sft_website_creds_file.read_text())
  KIND = ProtocolEnum.SFTP

  @override
  def get_conn_handler(self) -> SFTPClient:
    try:
      self.ssh_client = SSHClient()
      self.ssh_client.set_missing_host_key_policy(self.policy)

      self.ssh_client.connect(
        hostname=self.creds["HOSTNAME"],
        port=self.creds.get("PORT", 22),
        username=self.creds["USER"],
        password=self.creds["PWD"],
      )
      self.handler = self.ssh_client.open_sftp()
    except ConnectionRefusedError as e:
      raise ServerNotAvailableError(
        f"Could not connect to SFTP server at {self.creds['HOSTNAME']}:{self.creds.get('PORT', 22)}"
        f"\n Server exists but is not running an SFTP service or is blocking the connection."
      ) from e
    except TimeoutError as e:
      raise ServerNotAvailableError(
        f"Connection to SFTP server at {self.creds['HOSTNAME']}:{self.creds.get('PORT', 22)} timed out."
        f"\n Server may be offline or experiencing connectivity issues."
      ) from e
    except gaierror as e:
      raise ServerNotAvailableError(
        f"SFTP server hostname {self.creds['HOSTNAME']} could not be resolved.\n DNS has likely failed"
      ) from e

    return self.handler

  @override
  def close_conn_handler(self) -> None:
    self.handler.close()
    self.ssh_client.close()


class SASSFTPClient(SFTPProtocol):
  policy = AutoAddPolicy()
  creds = loads(SETTINGS.sas_ftp_creds_file.read_text())
  KIND = ProtocolEnum.SFTP

  @override
  def get_conn_handler(self) -> SFTPClient:
    try:
      self.ssh_client = SSHClient()
      self.ssh_client.set_missing_host_key_policy(self.policy)

      self.ssh_client.connect(
        hostname=self.creds["HOSTNAME"],
        port=self.creds.get("PORT", 22),
        username=self.creds["USER"],
        password=self.creds["PWD"],
      )
      self.handler = self.ssh_client.open_sftp()
    except ConnectionRefusedError as e:
      raise ServerNotAvailableError(
        f"Could not connect to SFTP server at {self.creds['HOSTNAME']}:{self.creds.get('PORT', 22)}"
        f"\n Server exists but is not running an SFTP service or is blocking the connection."
      ) from e
    except TimeoutError as e:
      raise ServerNotAvailableError(
        f"Connection to SFTP server at {self.creds['HOSTNAME']}:{self.creds.get('PORT', 22)} timed out."
        f"\n Server may be offline or experiencing connectivity issues."
      ) from e
    except gaierror as e:
      raise ServerNotAvailableError(
        f"SFTP server hostname {self.creds['HOSTNAME']} could not be resolved.\n DNS has likely failed"
      ) from e

    return self.handler

  @override
  def close_conn_handler(self) -> None:
    self.handler.close()
    self.ssh_client.close()


class RYOSFTPClient(SFTPProtocol):
  policy = AutoAddPolicy()
  creds = loads(SETTINGS.ryo_ftp_creds_file.read_text())
  KIND = ProtocolEnum.SFTP

  @override
  def get_conn_handler(self) -> SFTPClient:
    try:
      self.ssh_client = SSHClient()
      self.ssh_client.set_missing_host_key_policy(self.policy)

      self.ssh_client.connect(
        hostname=self.creds["HOSTNAME"],
        port=self.creds.get("PORT", 22),
        username=self.creds["USER"],
        password=self.creds["PWD"],
      )
      self.handler = self.ssh_client.open_sftp()
    except ConnectionRefusedError as e:
      raise ServerNotAvailableError(
        f"Could not connect to SFTP server at {self.creds['HOSTNAME']}:{self.creds.get('PORT', 22)}"
        f"\n Server exists but is not running an SFTP service or is blocking the connection."
      ) from e
    except TimeoutError as e:
      raise ServerNotAvailableError(
        f"Connection to SFTP server at {self.creds['HOSTNAME']}:{self.creds.get('PORT', 22)} timed out."
        f"\n Server may be offline or experiencing connectivity issues."
      ) from e
    except gaierror as e:
      raise ServerNotAvailableError(
        f"SFTP server hostname {self.creds['HOSTNAME']} could not be resolved.\n DNS has likely failed"
      ) from e
    return self.handler

  @override
  def close_conn_handler(self) -> None:
    self.handler.close()
    self.ssh_client.close()
