import json
import threading
import os


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
            with open(filename, mode="r") as f:
                jsondata = ''.join(line for line in f if not line.strip().startswith('#'))
                cls.config = json.loads(jsondata)

    @classmethod
    def clear_config(cls):
        """Clear the configuration"""

        with cls.config_lock:
            cls.config = {}
