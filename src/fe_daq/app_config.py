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
    """Make sure that a handful of required _CONFIG settings are present and of correct type."""
    global _CONFIG, _CONFIG_LOCK
    required = [
        ('LLRF1_gmes_step_size', float), ('LLRF1_gmes_sleep_interval', float), ('LLRF1_tuner_recovery_margin', float),
        ('LLRF2_gmes_step_size', float), ('LLRF2_gmes_sleep_interval', float), ('LLRF2_tuner_recovery_margin', float),
        ('LLRF3_gmes_step_size', float), ('LLRF3_gmes_sleep_interval', float), ('LLRF3_tuner_recovery_margin', float),
        ("linac_pressure_max", float), ("linac_pressure_margin", float),
        ("jt_valve_position_max", float), ("jt_valve_margin", float),
        ("cryo_heater_capacity_min", float), ("cryo_heater_capacity_margin", float)
    ]

    with _CONFIG_LOCK:
        for entry in required:
            (key,typ) = entry
            if key not in _CONFIG.keys():
                raise ValueError(f"Configuration is missing '{key}")
            # Check that all of these are floats / numbers
            if type(_CONFIG[key]) != typ:
                raise ValueError(f"Required config parameter '{key}' is not required type '{typ}'."
                                 f"  Received '{_CONFIG[key]}' of type '{type(_CONFIG[key])}'")
