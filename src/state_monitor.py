import logging
import threading
import time
from datetime import datetime
from typing import Union, Tuple

logger = logging.getLogger(__name__)


def connection_cb(pvname: str = None, conn: bool = None, **kwargs) -> None:
    """This is a generic connection callback that pauses application operation when a disconnect occurs.

    Args:
        pvname: The name of the PV changing connection status
        conn: Is the PV connected.
    """

    if conn:
        StateMonitor.pv_reconnected(pvname=pvname)
        logger.info(f"{pvname} connected.")
    else:
        StateMonitor.pv_disconnected(pvname=pvname)
        logger.error(f"{pvname} disconnected.")


def hv_read_back_cb(pvname: str, value: float, **kwargs) -> None:
    """Watch that HV readbacks don't differ from nominal by more than 10%"""
    if value < 900 or value > 1100:
        StateMonitor.hv_has_problem(pvname=pvname)
        logger.error(f"{pvname} is wrong {value} (!= 1000 +/- 10%).")
    else:
        StateMonitor.hv_good(pvname=pvname)
        logger.info(f"{pvname} is ok {value} (!= 1000 +/- 10%).")


def rf_on_cb(pvname: str, value: float, **kwargs) -> None:
    """Monitor RF On PVs to make sure that the cavities are good to go for data collection"""

    if value == 1:
        StateMonitor.rf_turned_on(pvname=pvname)
        logger.info(f"{pvname} RF is On ({value})")
    else:
        StateMonitor.rf_turned_off(pvname=pvname)
        logger.error(f"{pvname} RF is Off ({value})")


# A quick and dirty attempt at a singleton in Python.
class StateMonitor:
    """A simple class for tracking the state of CEBAF as it relates to RF PVs.  Meant as a singleton.

    Note: Many methods have a public and private version.  The intent is that public versions worry about locking and
    private versions do not.  This helps to avoid deadlocks and makes clear your locking intent.
    """

    # EPICS CA callbacks can happen in threads.  Synchronize access to these counters
    __state_lock = threading.Lock()

    # Dictionary of known PVs, RF status, and NDX HV status
    __pv_connected = {}
    __rf_on = {}
    __hv_bad = {}

    @classmethod
    def clear_state(cls):
        with cls.__state_lock:
            cls.__pv_connected = {}
            cls.__rf_on = {}

    @classmethod
    def output_state(cls) -> str:
        with cls.__state_lock:
            out = cls.__output_state()
        return out

    @classmethod
    def __output_state(cls) -> str:
        msg = f"""__disconnected_pvs: {cls.__get_disconnected_pv_count()}
__rf_off_cav_count: {cls.__get_rf_off_count()}
__hv_bad_count: {cls.__get_hv_bad_count()}
__pv_connected: {ascii(cls.__pv_connected)}
__rf_on: {ascii(cls.__rf_on)}"""
        return msg

    @classmethod
    def get_disconnected_pv_count(cls):
        with cls.__state_lock:
            out = cls.__get_disconnected_pv_count()
        return out

    @classmethod
    def get_hv_bad_count(cls):
        with cls.__state_lock:
            out = cls.__get_hv_bad_count()
        return out

    @classmethod
    def get_no_rf_cavity_count(cls):
        with cls.__state_lock:
            out = cls.__get_rf_off_count()
        return out

    @classmethod
    def pv_disconnected(cls, pvname) -> None:
        """Increment the disconnected PV counter."""
        with cls.__state_lock:
            cls.__pv_connected[pvname] = False

    @classmethod
    def pv_reconnected(cls, pvname) -> None:
        """Decrement the disconnected PV counter."""
        with cls.__state_lock:
            cls.__pv_connected[pvname] = True

    @classmethod
    def hv_has_problem(cls, pvname) -> None:
        """Decrement the disconnected PV counter."""
        with cls.__state_lock:
            cls.__hv_bad[pvname] = True

    @classmethod
    def hv_good(cls, pvname) -> None:
        """Decrement the disconnected PV counter."""
        with cls.__state_lock:
            cls.__hv_bad[pvname] = False

    @classmethod
    def __get_disconnected_pv_count(cls):
        count = 0
        for conn in cls.__pv_connected.values():
            if not conn:
                count += 1
        return count

    @classmethod
    def __get_rf_off_count(cls):
        count = 0
        for rf_on in cls.__rf_on.values():
            if not rf_on:
                count += 1
        return count

    @classmethod
    def __get_hv_bad_count(cls):
        count = 0
        for hv_bad in cls.__hv_bad.values():
            if hv_bad:
                count += 1
        return count

    @classmethod
    def rf_turned_off(cls, pvname) -> None:
        """Increment the RF off counter."""
        with cls.__state_lock:
            cls.__rf_on[pvname] = False

    @classmethod
    def rf_turned_on(cls, pvname) -> None:
        """Decrement the RF off counter."""
        with cls.__state_lock:
            cls.__rf_on[pvname] = True

    @classmethod
    def daq_good(cls) -> bool:
        """Is DAQ good to proceed.  True if we should take data, false if not."""
        with cls.__state_lock:
            return cls.__daq_good()

    @classmethod
    def __daq_good(cls) -> bool:
        """Is DAQ good to proceed.  True if we should take data, false if not."""
        if cls.__get_rf_off_count() == 0 and cls.__get_disconnected_pv_count() == 0:
            return True
        else:
            return False

    @classmethod
    def monitor(cls, duration: Union[float, None], user_input=True) -> Tuple[datetime, datetime]:
        """Sleep while periodically checking if we 're still in a good DAQ state.  Raises if we have a problem.

        Args:
            duration: How long should we monitor for?  If none, do one check and exit.
            user_input: Should we wait on user input (True, default) or immediately raise an exception
        """
        # If we're given a settle time, then sleep in small increments until that time is up.  Check the state after
        # waking up.
        start = datetime.now()
        if duration is not None and duration > 0.0:
            cls.check_state(user_input=user_input)
            while (datetime.now() - start).total_seconds() < duration:
                time.sleep(0.1)

        cls.check_state(user_input=user_input)
        end = datetime.now()

        return start, end

    @classmethod
    def check_state(cls, user_input=True):
        """Check if CEBAF is in a good state to collect data.  Ask for user direction if problem is found.

        Args:
            user_input:  Should a user's input be requested (True), or should we raise exception no matter what

        Raises:
            RuntimeError:  Bad state is found and users requests program exists.
        """
        with cls.__state_lock:
            try:
                if not cls.__daq_good():
                    n_dps = cls.__get_disconnected_pv_count()
                    n_no_rf = cls.__get_rf_off_count()
                    if n_dps > 0:
                        pvs = ""
                        count = 0
                        for pv_name in sorted(cls.__pv_connected.keys()):
                            if count >= 3:
                                pvs += "...\n"
                                break
                            if not cls.__pv_connected[pv_name]:
                                count += 1
                                pvs += f"{pv_name} disconnected\n"
                        raise RuntimeError(f"StateMonitor detected {n_dps} disconnected PVs.\n{pvs}")
                    elif n_no_rf > 0:
                        pvs = ""
                        count = 0
                        for pv_name in sorted(cls.__rf_on.keys()):
                            if count >= 3:
                                pvs += "...\n"
                                break
                            if not cls.__rf_on[pv_name]:
                                count += 1
                                pvs += f"{pv_name} has RF off\n"
                        raise RuntimeError(f"StateMonitor detected {n_no_rf} cavities without RF on")
                    else:
                        raise RuntimeError(f"StateMonitor detected something wrong.\n{cls.__output_state()}")
            except Exception as ex:
                msg = f"StateMonitor found error.\n{ex}"
                logger.error(msg)
                response = 'n'
                if user_input:
                    response = input(f"{msg}\nContinue (n|Y): ").lower().lstrip()
                if not response.startswith('y'):
                    logger.info("Exiting after error based on user response.")
                    raise RuntimeError("User indicated unrecoverable error.")
