import json
import threading
import os
import logging
from typing import Any

logger = logging.getLogger(__name__)


class Config:
    # The configuration dictionary for the application
    config = {}
    config_lock = threading.Lock()

    # The root directory of the app
    app_root = os.path.realpath(os.path.join(os.path.basename(__file__), ".."))

    @classmethod
    def parse_config_file(cls, filename: str = f"{app_root}/fe_daq.cfg"):
        """Process an application level configuration file

        Args:
            filename:  The name of the file parse
        """
        with cls.config_lock:
            try:
                with open(filename, mode="r") as f:
                    # This will choke if a line has a comment after some content.  Comments MUST be on their own line.
                    jsondata = ''.join(line.strip() for line in f if not line.strip().startswith('#'))
            except Exception as exc:
                logger.error(f"Error reading file '{filename}': {exc}")
                raise exc

            try:
                cls.config = json.loads(jsondata)
            except Exception as exc:
                logger.error(f"Error parsing config file '{filename}': ")
                raise exc

    @classmethod
    def clear_config(cls):
        """Clear the configuration"""

        with cls.config_lock:
            cls.config = {}

    @classmethod
    def set_parameter(cls, key: str, value: Any):
        """Set an individual config parameter"""
        with cls.config_lock:
            cls.config[key] = value
