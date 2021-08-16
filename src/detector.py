import copy
import logging
from typing import Tuple

import epics
from scipy.stats import ttest_ind
from state_monitor import connection_cb, hv_read_back_cb

logger = logging.getLogger(__name__)


class NDXElectrometer:

    def __init__(self, name: str, epics_name: str):
        self.name = name
        self.epics_name = epics_name

        self.capacitor_switch = epics.PV(f"{self.epics_name}_CAPACITOR_SW", connection_callback=connection_cb)
        self.daq_enabled = epics.PV(f"{self.epics_name}_RESET", connection_callback=connection_cb)
        self.integration_period = epics.PV(f"{self.epics_name}_PERIOD", connection_callback=connection_cb)
        self.hv_set_point = epics.PV(f"{self.epics_name}_HV_BIAS", connection_callback=connection_cb)
        self.hv_read_back = epics.PV(f"{self.epics_name}_HV_RBCK", connection_callback=connection_cb)
        self.hv_read_back.add_callback(hv_read_back_cb)

        # Do this check once at the start.  The rest of the time we monitor the read back in the StateMonitor to make
        # sure the actual value is close.
        if self.hv_set_point.get(use_monitor=False) != 1000:
            raise RuntimeError(f"{self.name} HV set point ({self.hv_set_point.pvname} != 1000")

    def toggle_data_acquisition(self):
        # Toggle the DAQ off and back on.  There are some circumstances where it may be in "Acquire" mode according to
        # EPICS, but not actually acquiring.
        logger.info(f"{self.name}: Turn DAQ off, then back on")
        self.daq_enabled.put(0, wait=True)
        self.daq_enabled.put(1, wait=True)


    def set_for_fe_onset(self):
        """ Set the electrometer to the needed settings for determining FE onset"""
        # Use the sensitive capacitor setting (10 pF)
        self.capacitor_switch.put("10pF")

        # Make sure that the electrometer is integrating signal for one second when averaging out dose rate
        self.integration_period.put(1)

        self.toggle_data_acquisition()

    def set_for_operations(self):
        """Set the electrometer for normal operations"""
        # Use the less sensitive capacitor setting (1000 pF)
        self.capacitor_switch.put("1000pF")

        # Make sure that the electrometer is integrating signal for one second when averaging out dose rate
        self.integration_period.put(1)

        self.toggle_data_acquisition()


class NDXDetector:

    def __init__(self, name: str, epics_name: str, electrometer: NDXElectrometer):
        self.name = name
        self.epics_name = epics_name
        self.electrometer = electrometer

        self.gamma_current = epics.PV(f"{self.epics_name}_gCur", connection_callback=connection_cb)
        self.neutron_current = epics.PV(f"{self.epics_name}_nCur", connection_callback=connection_cb)

        self.gamma_background = None
        self.neutron_background = None

        self.gamma_measurements = []
        self.neutron_measurements = []

    def update_background(self) -> None:
        """Copies current measurement history to the data representing background radiation."""
        self.gamma_background = copy.deepcopy(self.gamma_measurements)
        self.neutron_background = copy.deepcopy(self.neutron_measurements)

    def clear_measurements(self):
        """Clear out any existing measurements.  This is useful when starting to record samples from a new period."""
        self.gamma_measurements = []
        self.neutron_measurements = []

    def take_measurement(self):
        """Add the current value of the detector's dose rates to the circular history buffer."""
        self.gamma_measurements.append(self.gamma_current.get(use_monitor=False))
        self.neutron_measurements.append(self.neutron_current.get(use_monitor=False))

    def is_radiation_above_background(self, t_stat_threshold: float = 5.0) -> Tuple[bool, float]:
        """Tests if the radiation sampled (at 1 Hz) during specified duration differs from background using t-test.

        Returns:
            2-tuple, First is boolean about whether any detector found significantly more radiation than background,
            the second is the maximum t-score found among the detector signals.
        """
        t = self.get_gamma_t_stat()
        max_t = t

        if t > t_stat_threshold:
            return True, max_t

        t = self.get_neutron_t_stat()
        if t > max_t:
            max_t = t

        if t > t_stat_threshold:
            return True, max_t

        return False, max_t

    def get_gamma_t_stat(self):
        t, p = ttest_ind(self.gamma_measurements, self.gamma_background, equal_var=False)
        return t

    def get_neutron_t_stat(self):
        t, p = ttest_ind(self.neutron_measurements, self.neutron_background, equal_var=False)
        return t
