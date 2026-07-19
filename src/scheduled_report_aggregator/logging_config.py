# First party imports
from aeth_ext.logging.setup import BaseLoggingConfig


class LoggingConfig(BaseLoggingConfig):
  """Project logging configuration.

  All customization lives in the TOML override files shipped next to this
  module (discovered via the directory of ``__main__``):

  - ``logging_config.toml`` - local-mode apscheduler file split.
  - ``remote_logging_config.toml`` - the same split applied server-side by the
    central log server (merged into the remote config sent in the socket
    handshake).

  ``override_mode = "merge"`` merges those files onto the packaged aeth_ext
  defaults instead of replacing them.
  """

  override_mode = "merge"
