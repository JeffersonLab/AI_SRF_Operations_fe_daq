import json
import threading
import os
import logging
from typing import Any, List, Union

from functools import reduce
import operator

logger = logging.getLogger(__name__)

# The root directory of the app
app_root = os.path.realpath(os.path.join(os.path.basename(__file__), ".."))

# The configuration dictionary for the application
_CONFIG = {}

# Lock for accessing configuration
_CONFIG_LOCK = threading.Lock()


def _get_from_dict(d: dict, key_list: list):
    """Query a value from a nested dictionary using a list of keys."""
    return reduce(operator.getitem, key_list, d)


def _set_in_dict(d: dict, key_list: list, value: Any):
    """Set a values from a nested dictionary using a list of keys."""
    _get_from_dict(d, key_list[:-1])[key_list[-1]] = value


def parse_config_file(filename: str = f"{app_root}/fe_daq.cfg"):
    """Process an application level configuration file

    Args:
        filename:  The name of the file parse
    """
    global _CONFIG, _CONFIG_LOCK
    with _CONFIG_LOCK:
        try:
            with open(filename, mode="r") as f:
                # This will choke if a line has a comment after some content.  Comments MUST be on their own line.
                jsondata = ''.join(line.strip() for line in f if not line.strip().startswith('#'))
        except Exception as exc:
            logger.error(f"Error reading file '{filename}': {exc}")
            raise exc

        try:
            _CONFIG = json.loads(jsondata)
        except Exception as exc:
            logger.error(f"Error parsing _CONFIG file '{filename}': ")
            raise exc


def clear_config():
    """Clear the configuration"""
    global _CONFIG, _CONFIG_LOCK
    with _CONFIG_LOCK:
        _CONFIG = {}


def set_parameter(key: Union[str, List[str]], value: Any):
    """Set an individual _CONFIG parameter"""
    global _CONFIG, _CONFIG_LOCK
    with _CONFIG_LOCK:
        if type(key) == str:
            _CONFIG[key] = value
        elif len(key) == 1:
            _CONFIG[key[0]] = value
        else:
            _get_parameter(key[:-1])[key[-1]] = value


def get_parameter(key: Union[str, List[str], None]) -> Any:
    """Set an individual _CONFIG parameter.  If key is None, return entire dictionary.  Thread safe."""
    global _CONFIG_LOCK
    with _CONFIG_LOCK:
        return _get_parameter(key)


def _get_parameter(key: Union[str, List[str], None]) -> Any:
    """Set an individual config parameter.  If key is None, return entire dictionary.  Not thread safe, internal use."""
    global _CONFIG, _CONFIG_LOCK
    out = None
    try:
        if key is None:
            out = _CONFIG
        elif type(key) == str:
            out = _CONFIG[key]
        else:
            out = _get_from_dict(_CONFIG, key)
    except KeyError:
        # It's OK to request a parameter that doesn't exist, you get None back
        pass

    return out


def validate_config():
    """Make sure that a handful of required _CONFIG settings are present"""
    global _CONFIG, _CONFIG_LOCK
    required = [
        'LLRF1_gmes_step_size', 'LLRF1_gmes_sleep_interval',
        'LLRF2_gmes_step_size', 'LLRF2_gmes_sleep_interval',
        'LLRF3_gmes_step_size', 'LLRF3_gmes_sleep_interval',
        "linac_pressure_max", "linac_pressure_margin",
        "jt_valve_position_max", "jt_valve_margin",
        "cryo_heater_capacity_min", "cryo_heater_capacity_margin"
    ]

    with _CONFIG_LOCK:
        for key in required:
            if key not in _CONFIG.keys():
                raise RuntimeError(f"Configuration is missing '{key}")
