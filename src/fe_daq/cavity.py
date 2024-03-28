import concurrent
import math
import time
import traceback
from datetime import datetime
from typing import Optional, TextIO, List, Dict, Tuple

import epics
import logging
import numpy as np

from fe_daq.state_monitor import connection_cb, rf_on_cb, StateMonitor, get_threshold_cb
from fe_daq import app_config as config
from fe_daq.utils import Status, write_data_index_row, user_alert_scan_paused, user_alert_update_gsets_fail
from fe_daq.exceptions import UserScanAbort

logger = logging.getLogger(__name__)


class Cavity:
    @classmethod
    def get_cavity(cls, name: str, epics_name: str, cavity_type: str, length: float, tuner_bad: bool,
                   bypassed: bool, zone: 'Zone', Q0: float, gset_no_fe: float = None, gset_fe_onset: float = None,
                   gset_max: float = None):
        if zone.controls_type == '1.0':
            gmes_step_size = config.get_parameter('LLRF1_gmes_step_size')
            gmes_sleep_interval = config.get_parameter('LLRF1_gmes_sleep_interval')
            tuner_recovery_margin = config.get_parameter('LLRF1_tuner_recovery_margin')
            tuner_timeout = config.get_parameter('LLRF1_tuner_timeout')
            cavity = LLRF1Cavity(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length,
                                 bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe, tuner_bad=tuner_bad,
                                 gset_fe_onset=gset_fe_onset, gset_max=gset_max, gmes_step_size=gmes_step_size,
                                 gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin,
                                 tuner_timeout=tuner_timeout)
        elif zone.controls_type == '2.0':
            gmes_step_size = config.get_parameter('LLRF2_gmes_step_size')
            gmes_sleep_interval = config.get_parameter('LLRF2_gmes_sleep_interval')
            tuner_recovery_margin = config.get_parameter('LLRF2_tuner_recovery_margin')
            tuner_timeout = config.get_parameter('LLRF2_tuner_timeout')
            cavity = LLRF2Cavity(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length,
                                 bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe, tuner_bad=tuner_bad,
                                 gset_fe_onset=gset_fe_onset, gset_max=gset_max, gmes_step_size=gmes_step_size,
                                 gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin,
                                 tuner_timeout=tuner_timeout)
        elif zone.controls_type == '3.0':
            gmes_step_size = config.get_parameter('LLRF3_gmes_step_size')
            gmes_sleep_interval = config.get_parameter('LLRF3_gmes_sleep_interval')
            tuner_recovery_margin = config.get_parameter('LLRF3_tuner_recovery_margin')
            tuner_timeout = config.get_parameter('LLRF3_tuner_timeout')
            cavity = LLRF3Cavity(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length,
                                 bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe, tuner_bad=tuner_bad,
                                 gset_fe_onset=gset_fe_onset, gset_max=gset_max, gmes_step_size=gmes_step_size,
                                 gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin,
                                 tuner_timeout=tuner_timeout)
        else:
            raise ValueError(f"Unsupported controls_type '{zone.controls_type}")

        # Update gset_max and wait_for_connections should be called after cavities have been created.
        return cavity

    # Importing Zone would result in circular imports
    # noinspection PyUnresolvedReferences
    def __init__(self, name: str, epics_name: str, cavity_type: str, length: float, tuner_timeout: float,
                 tuner_bad: bool, bypassed: bool, zone: 'Zone', Q0: float, gset_no_fe: float = None,
                 gset_fe_onset: float = None, gset_max: float = None, gset_min: float = None,
                 gmes_step_size: float = 0.1, gmes_sleep_interval: float = 1, tuner_recovery_margin: float = 1.0):
        self.name = name
        self.epics_name = epics_name
        self.zone_name = zone.name
        self.cavity_type = cavity_type
        self.controls_type = zone.controls_type  # These should be '1.0', '2.0', etc. LLRF controls.
        self.length = length
        self.bypassed = bypassed
        self.tuner_bad = tuner_bad
        self.zone = zone
        self.Q0 = Q0
        self.cavity_number = int(name[5:6])
        self.tuner_recovery_margin = tuner_recovery_margin
        self.tuner_timeout = tuner_timeout

        # This is a higher gradient where we have found no field emission.  Typically it is the highest integer MV/m
        # that does not produce a radiation signal. Useful as a baseline "high" gradient with some wiggle room from FE.
        self.gset_no_fe = gset_no_fe

        # This is the highest gradient we found without producing detectable field emission.
        self.gset_fe_onset = gset_fe_onset

        if zone.name != name[0:4]:
            msg = f"{self.name}: Zone name '{zone.name}' !~ cavity"
            logger.error(msg)
            raise ValueError(msg)

        self.gset = epics.PV(f"{self.epics_name}GSET", connection_callback=connection_cb)
        self.gmes = epics.PV(f"{self.epics_name}GMES", connection_callback=connection_cb)
        self.pset = epics.PV(f"{self.epics_name}PSET", connection_callback=connection_cb)
        self.odvh = epics.PV(f"{self.epics_name}ODVH", connection_callback=connection_cb)
        self.drvh = epics.PV(f"{self.epics_name}GSET.DRVH", connection_callback=connection_cb)
        self.pset_init = self.pset.get()
        self.gset_init = self.gset.get()

        # Minimum acceptable gradient.  Assigned in child classes.
        self.gset_min = gset_min

        if self.cavity_type == "C100":
            self.shunt_impedance = 1241.3
        elif self.cavity_type == "C75":
            self.shunt_impedance = 1049
        elif self.cavity_type == "C25":
            self.shunt_impedance = 960
        elif self.cavity_type == "C50":
            self.shunt_impedance = 960
        elif self.cavity_type == "P1R":
            self.shunt_impedance = 960
        else:
            self.shunt_impedance = 960
            logger.warning(f"{self.name}:  Unrecognized type '{self.cavity_type}'.  Using default impedance of "
                           f"{self.shunt_impedance}.")

        self.pv_list = [self.gset, self.gmes, self.drvh, self.pset, self.odvh]
        self.pv_list = [pv for pv in self.pv_list if pv is not None]

        # Cavity can be effectively bypassed in a number of ways.  Work through that here.
        self.bypassed_eff = bypassed
        if self.gset_init == 0:
            self.bypassed_eff = True
        elif self.odvh.value == 0:
            self.bypassed_eff = True

        # Each cavity keeps track of an externally set maximum value.  Make sure to update this after connecting to PVs.
        self.gset_max = self.gset_min
        self.gset_max_requested = gset_max

        # These dictate the defaults for walking a cavity.  Take steps of gmes_step_size and wait gmes_sleep_interval
        # seconds between steps.  Here we pick cautious defaults that child classes can override.
        self.gmes_step_size = gmes_step_size
        self.gmes_sleep_interval = gmes_sleep_interval

    def update_gset_max(self, gset_max: Optional[float] = None):
        """Update the maximum allowed gset.  If gset_max is None, use the original requested gset_max at construction"""
        if gset_max is not None:
            self.gset_max_requested = gset_max

        if self.gset_max_requested is None:
            self.gset_max = self.odvh.value
        elif self.gset_max_requested > self.drvh.value:
            logger.warning(
                f"{self.name}: Tried to set gset_max > GSET.DRVH.  Set gset_max = GSET.DRVH ({self.drvh.value})")
            self.gset_max = self.drvh.value
        else:
            self.gset_max = self.gset_max_requested

    def is_rf_on(self):
        raise NotImplementedError("Must be implemented by child classes")

    def is_gradient_ramping(self, gmes_threshold: float):
        raise NotImplementedError("Must be implemented by child classes")

    def is_tuning_required(self, margin: Optional[float] = None):
        raise NotImplementedError("Must be implemented by child classes")

    def wait_for_tuning(self, tune_timeout: Optional[float] = None, interactive: bool = True):
        """Method that waits for a cavity to be brought back within tune limits.  No Waiting if no tuning required."""
        if tune_timeout is None:
            tune_timeout = self.tuner_timeout

        if self.is_tuning_required(margin=0):
            start_ramp = datetime.now()
            logger.info(f"{self.name}: Waiting for tuner (timeout = {tune_timeout})")

            while self.is_tuning_required():
                time.sleep(0.05)
                if (datetime.now() - start_ramp).total_seconds() > tune_timeout:
                    logger.warning(f"{self.name} is taking a long time to tune.")
                    response = input(f"Waited {tune_timeout} seconds for {self.name} to tune.  "
                                     f"Continue waiting? (n|y): ").lstrip().lower()
                    if interactive:
                        if not response.startswith("y"):
                            msg = f"User requested exit while waiting on {self.name} to tune."
                            logger.error(msg)
                            raise RuntimeError(msg)
                        # Restart the counter
                        logger.info(f"{self.name}: Waiting for tuner (timeout = {tune_timeout})")
                        start_ramp = datetime.now()
                    else:
                        msg = f"{self.name}:  Timed out waiting on tuner."
                        logger.error(msg)
                        raise RuntimeError(msg)

    def get_jiggled_pset_value(self, delta: float) -> float:
        """Calculate a random.uniform offset from pset_init of maximum +/- 5.  No changes to EPICS"""
        return self.pset_init + np.random.uniform(-delta, delta)

    def get_low_gset(self):
        """Return the appropriate lowest no FE gradient.  Either the lowest stable or the highest known without FE."""
        return self.gset_min if self.gset_no_fe is None else self.gset_no_fe

    def calculate_heat(self, gradient: Optional[float] = None) -> float:
        g = gradient
        if g is None:
            g = self.gset.value

        # gradient is units of MV/m. formula expects V/m, so 1e12
        return (g * g * self.length * 1e12) / (self.shunt_impedance * self.Q0)

    def _get_step_direction(self, gset) -> Tuple[int, float]:
        """Determine the direction the gradient is changing.  Will wait if the gset PV value is None.

        The waiting happens because there are occasional issues where the gset PV returns a value of None with a timeout
        of ~1 second.  Waiting 15 seconds seems to allow this problem to always resolve itself.

        Args:
            gset: The requested new gradient.

        Returns:
            Step direction (-1 if lowering, 1 if raising), the current value of the gset PV
        """
        step_dir = None
        actual_gset = None
        while step_dir is None:
            actual_gset = self.gset.get(use_monitor=False)
            if actual_gset is None:
                logger.warning(f"{self.name}: Error getting gset.  Waiting 15 seconds then retrying.")
                StateMonitor.monitor(duration=15, user_input=False)
            elif gset >= actual_gset:
                step_dir = 1
            else:
                step_dir = -1

        return step_dir, actual_gset


    def walk_gradient(self, gset: float, step_size: Optional[float] = 1.0, wait_interval: Optional[float] = 0,
                      **kwargs) -> None:
        """Move the gradient of the cavity to gset in steps.  All cavities should ramp themselves.

        Some cavities ramp themselves in firmware, and likely do not watch cryo.  Cavities that ramp in this software
        have made an effort to watch multiple CEBAF systems, including cryo, and not enter any dangerous situations.
        Use the settle_time argument to provide dedicated opportunities for checking cryo.

        Args:
            gset:  The target gradient set point
            step_size:  The maximum size steps to make in MV/m by absolute value.  Use class gmes_step_size if None.
            wait_interval:  Additional sleep put between steps with no additional checks.  Use class gmes_sleep_interval
                            if None.

        Additional kwargs are passed to set_gradient.
        """

        if step_size is None:
            step_size = self.gmes_step_size
        if wait_interval is None:
            wait_interval = self.gmes_sleep_interval

        # Determine step direction
        step_dir, actual_gset = self._get_step_direction(gset=gset)

        if gset > self.gset_max:
            msg = f"Requested {self.name} gradient higher than max allowed GSET {self.gset_max}."
            logger.error(msg)
            raise ValueError(msg)

        logger.info(f"{self.name}: Walking {actual_gset} to {gset} in {step_size} MV/m steps.")

        # Walk step size until we're within a single step
        while abs(gset - actual_gset) > step_size:
            next_gset = actual_gset + (step_dir * step_size)
            self.set_gradient(gset=next_gset, **kwargs)
            if wait_interval > 0:
                time.sleep(wait_interval)
            actual_gset = self.gset.get(use_monitor=False)

        # We should be within a single step here.
        self.set_gradient(gset=gset, **kwargs)

    def _validate_requested_gradient(self, gset: float, force: bool):
        """Run a series of checks to ensure that the new requested gradient makes is a viable.

        Args:
            gset: The requested gset
            force: Are we allowed to exceed single step limits.
        """
        if self.zone.linac.autoheat_mode.value != 1:
            msg = (f"{self.name}: Can't change gradients when autoheat is disabled"
                   f" ({self.zone.linac.autoheat_mode.pvname} == {self.zone.linac.autoheat_mode.value})")
            logger.error(msg)
            raise RuntimeError(msg)
        if self.tuner_bad:
            msg = f"{self.name}: Can't change a cavity with a bad tuner."
            logger.error(msg)
            raise RuntimeError(msg)
        if gset != 0 and self.bypassed_eff:
            msg = f"{self.name}: Can't turn on bypassed cavity"
            logger.error(msg)
            logger.error(f"gset_init={self.gset_init}, self.bypassed={self.bypassed}, "
                         f"self.bypassed_eff={self.bypassed_eff}, self.gset={self.gset.value}")
            raise ValueError(msg)
        if gset != 0 and gset < self.gset_min:
            msg = f"{self.name}: Can't turn cavity below operational min"
            logger.error(msg)
            raise ValueError(msg)
        if gset > self.gset_max:
            msg = f"{self.name}: Can't turn cavity above operational max (gset_max={self.gset_max})"
            logger.error(msg)
            raise ValueError(msg)
        current = self.gset.get(use_monitor=False)
        if (not force) and abs(gset - current) > 1.001:
            msg = f"{self.name}: Can't move GSET more than 1 MV/m at a time. (new: {gset}, current: {current})"
            logger.error(msg)
            raise ValueError(msg)

    def _set_gradient(self, gset: float, settle_time: float, wait_for_ramp: bool, ramp_timeout: float,
                      gradient_epsilon: float, interactive: bool = True) -> None:
        """Internal call to set a cavity's gradient.

        If interactive, user will be prompted on gradient ramping timeouts.  Otherwise it raises an exception.
        """
        # This method only sets the gradient and waits from any ramping to occur if requested.  
        self.gset.put(gset)
        if wait_for_ramp:
            # Here we wait to see if the cavity will start to ramp.  Since this updates on a 1 Hz cycle, I might have to
            # wait as long as one second.
            ramp_started = False
            start_watching = datetime.now()
            last_checked = datetime.now()
            while (last_checked - start_watching).total_seconds() <= 1.01:
                last_checked = datetime.now()
                ramp_started = self.is_gradient_ramping()
                if ramp_started:
                    logger.info(f"{self.name}: is ramping gradient")
                    break
                # Add a sleep/monitor and check if we've reached our target gradient.  If we're this close and the
                # cavity is not ramping, then it's very unlikely  to ramp.  This difference is the usual noise in GMES.
                if math.fabs(gset - self.gmes.value) < gradient_epsilon:
                    break
                time.sleep(0.05)

            if ramp_started:
                # Here we have to wait for the gradient to finish ramping, assuming it actually started
                # logger.info(f"{self.name}: waiting for gradient to ramp")
                start_ramp = datetime.now()
                while self.is_gradient_ramping():
                    time.sleep(0.1)
                    if (datetime.now() - start_ramp).total_seconds() > ramp_timeout:
                        logger.warning(f"{self.name}: gradient ramp timed out.")
                        if interactive:
                            response = input(
                                f"Waited {ramp_timeout} seconds for {self.name} to ramp.  Continue? (n|y): "
                            ).lstrip().lower()
                            if not response.startswith("y"):
                                msg = f"User requested exit while waiting on {self.name} to ramp."
                                logger.error(msg)
                                raise RuntimeError(msg)
                        else:
                            raise RuntimeError(f"{self.name}: gradient ramp timed out ({ramp_timeout})")

                        # If we made it here, then a user decided to keep going.
                        start_ramp = datetime.now()
            # else:
            #     logger.info(f"{self.name}: did not ramp gradient")

        if settle_time > 0:
            logger.info(f"{self.name}: Waiting {settle_time} seconds for cryo to adjust")
        StateMonitor.monitor(duration=settle_time, user_input=False)

    def _wait_for_jt(self, timeout: float):
        """ Check to see if JT valve is too open.  Wait for it to recover if so.

        Raises RuntimeError upon timeout.
        """
        jt_recovery_point = self.zone.jt_max - self.zone.jt_recovery_margin

        wait_for_jt, jt_position = self.zone.check_jt_valve()
        if wait_for_jt:
            logger.warning(f"{self.name}: Found JT Valve too open. {self.zone.jt_stroke.pvname}={jt_position}."
                           f"Waiting max {timeout} seconds to recover to {jt_recovery_point}.")
        start = datetime.now()
        while wait_for_jt:
            time.sleep(0.01)
            wait_for_jt, jt_position = self.zone.check_jt_valve(threshold=jt_recovery_point)
            if not wait_for_jt:
                logger.info(f"{self.name}: JT valve recovered to {jt_position}")
            if (datetime.now() - start).total_seconds() > timeout:
                logger.warning(f"{self.name}: JT valve unrecovered. {self.zone.jt_stroke.pvname}={jt_position}.")
                raise RuntimeError("Timed out waiting for JT Valve")

    def _wait_for_linac_pressure(self, timeout: float):
        """Check to see if linac pressure is too high.  Wait for it too recover if so.

        Raises RuntimeError upon timeout.
        """
        lp_recovery_max = self.zone.linac.linac_pressure_max - self.zone.linac.lp_recovery_margin
        lp_recovery_min = self.zone.linac.linac_pressure_min + self.zone.linac.lp_recovery_margin
        wait_for_linac_pressure, linac_pressure = self.zone.linac.check_linac_pressure()
        if wait_for_linac_pressure:
            logger.warning(f"{self.name}: Found linac pressure out of spec. {self.zone.linac.linac_pressure.pvname}="
                           f"{linac_pressure}.  Waiting max {timeout} seconds to recover to within"
                           f" [{lp_recovery_min}, {lp_recovery_max}].")
        start = datetime.now()
        while wait_for_linac_pressure:
            time.sleep(0.01)
            wait_for_linac_pressure, linac_pressure = self.zone.linac.check_linac_pressure(max_val=lp_recovery_max,
                                                                                           min_val=lp_recovery_min)
            if not wait_for_linac_pressure:
                logger.info(f"{self.name}: Linac pressure recovered to {linac_pressure}")
            elif (datetime.now() - start).total_seconds() > timeout:
                logger.warning(f"{self.name}: JT valve unrecovered. {self.zone.linac.linac_pressure.pvname}"
                               f"={linac_pressure}.")
                raise RuntimeError("Timed out waiting for JT Valve")
            else:
                logger.info("Waiting for linac pressure to recover")

    def _wait_for_heater_margins(self, timeout: float):
        """Check if we have enough heater margin.  If not, wait for it to recover.  Raise on timeout."""
        hm_recovery_point = self.zone.linac.heater_margin_min + self.zone.linac.heater_recovery_margin
        wait_for_heaters, heater_margin = self.zone.linac.check_heater_margin()
        if wait_for_heaters:
            logger.warning(f"{self.name}: Found heater margin too low. {self.zone.linac.heater_margin.pvname}="
                           f"{heater_margin}.  Waiting max {timeout} seconds to recover to"
                           f" {hm_recovery_point}.")
        start = datetime.now()
        while wait_for_heaters:
            time.sleep(0.01)
            wait_for_heaters, heater_margin = self.zone.linac.check_heater_margin(threshold=hm_recovery_point)
            if not wait_for_heaters:
                logger.info(f"{self.name}: Heater margin recovered to {heater_margin}")
            elif (datetime.now() - start).total_seconds() > timeout:
                logger.warning(f"{self.name}: Heater margin unrecovered. {self.zone.linac.heater_margin.pvname}"
                               f"={heater_margin}.")
                raise RuntimeError("Timed out waiting for heater margin")

    def _do_gradient_ramping(self, gset: float, settle_time: float, tune_timeout: Optional[float] = None, **kwargs):
        """Slow ramp gradient to a new values.  Not all cavities allow time for tuners on a gset caput.

        This is designed so that it pauses whenever the cavity requires tuning, not when the cavity is tuning.  The
        idea is that tuners engage when a cavity crosses a detune max threshold, and the tuners run until the detuning
        hits a lower limit.  We don't want to keep changing gradient (and forcing more detuning) when the cavity is
        past the upper limit, but we don't want to wait until the tuners are completely done.
        """

        # Determine step direction
        step_dir, actual_gset = self._get_step_direction(gset=gset)

        # logger.info(f"{self.name}: Manually ramping gradient from {actual_gset} to {gset} in {self.gmes_step_size}"
        #             f" MV/m steps with {self.gmes_sleep_interval}s waits.")

        # Walk step size until we're within a single step
        while abs(gset - actual_gset) > self.gmes_step_size:
            # Don't make a change unless we are sufficiently tuned and cryo is happy.  These raise on timeout
            self._do_ramp_checks(tune_timeout=tune_timeout)

            next_gset = actual_gset + (step_dir * self.gmes_step_size)

            # Make a small step.  Do not wait the settle time, we do that at the end.
            self._set_gradient(gset=next_gset, settle_time=0, **kwargs)
            actual_gset = self.gset.get(use_monitor=False)
            if self.gmes_sleep_interval > 0:
                time.sleep(self.gmes_sleep_interval)

        # We should be within a single step here.  Now wait the full settle time.
        self._do_ramp_checks(tune_timeout=tune_timeout)
        self._set_gradient(gset=gset, settle_time=settle_time, **kwargs)

    def _do_ramp_checks(self, tune_timeout: Optional[float] = None):
        self.wait_for_tuning(tune_timeout=tune_timeout)
        self._wait_for_jt(timeout=60)
        self._wait_for_linac_pressure(timeout=60)
        self._wait_for_heater_margins(timeout=60)

    def set_gradient(self, gset: float, settle_time: float = 6.0, wait_for_ramp=True, ramp_timeout=20,
                     force: bool = False, gradient_epsilon: float = 0.05, interactive: bool = True):
        """Set a cavity's gradient and wait for supporting systems to compensate for the change.

        New change can't be more than 1 MV/m away from current value, without force.  This attempts to wait for a cavity
        to ramp if requested.  However, it also has a shortcut check that if the GMES is very close to the requested
        GSET, then it will stop watching as the ramping is essentially over if it happened at all.

        Args:
            gset: The new value to set GSET to
            settle_time: How long we need to wait for cryo system to adjust to change in heat load
            wait_for_ramp: Do we need to wait for the cavity gradient to ramp up?  C100s ramp for larger steps.
            ramp_timeout: How long to wait for ramping to finish once started?  This prompts a user on timeout.
            force: Are we going to force a big gradient move?  The 1 MV/m step is a very cautious limit.
            gradient_epsilon: The minimum difference between GSET and GMES that we consider as "noise".
            interactive: Should the user be prompted for interactive process control

        """
        raise NotImplementedError("Must be implemented by child classes")

    def restore_pset(self):
        self.pset.put(self.pset_init, wait=True)

    def restore_gset(self, **kwargs):
        if self.gset.value != self.gset_init:
            self.walk_gradient(self.gset_init, **kwargs)

    def wait_for_connections(self, timeout=2):
        """Wait for PVs to connect or timeout.  Then finish object initialization knowing PVs are connected.

        This will raise an exception if a PV fails to connect.  The intent is to allow application to exit gracefully if
        PVs are not available at the start.  Otherwise, we have to periodically check the StateMonitor to see if any
        problems have arisen.

        Note: It's best to run this on all the cavities at once after they have been constructed as connections
        should be happening in the background in parallel.  This is functionally an assert followed by any actions that
        can only happen after connections.
        """

        # Ensure that we are connected
        for pv in self.pv_list:
            if not pv.connected:
                if not pv.wait_for_connection(timeout=timeout):
                    raise Exception(f"PV {pv.pvname} failed to connect.")

    def run_callbacks(self):
        """Run all user defined callbacks right now.

        This is helpful in case some cavity PV is in a bad state prior to attaching the callback.
        """
        for pv in self.pv_list:
            pv.run_callbacks()


class LLRF1Cavity(Cavity):
    def __init__(self, name: str, epics_name: str, cavity_type: str, length: float, tuner_bad: bool,
                 bypassed: bool, zone: 'Zone', Q0: float, tuner_recovery_margin: float,
                 gset_no_fe: float = None, gset_fe_onset: float = None, gset_max: float = None,
                 gmes_step_size: float = 0.1, gmes_sleep_interval: float = 1.0, tuner_timeout: float = 300):
        super().__init__(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length, tuner_bad=tuner_bad,
                         tuner_timeout=tuner_timeout, bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe,
                         gset_fe_onset=gset_fe_onset, gset_max=gset_max, gset_min=3.0, gmes_step_size=gmes_step_size,
                         gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin)

        self.rf_on = epics.PV(f"{self.epics_name}ACK1.B6", connection_callback=connection_cb)
        self.tdeta = epics.PV(f"{self.epics_name}TDETA", connection_callback=connection_cb)

        # We want tuners to be in auto mode.  Tuners might not be used if bypassed or known tuner problem
        # 1 is auto, 0 is manual
        self.tuner_mode = epics.PV(f"{self.epics_name}TMODI", connection_callback=connection_cb)
        if self.tuner_bad or self.bypassed_eff:
            logger.info(f"{self.name}: Is bypassed or tuner_bad.  Not monitoring tuner mode.")
        else:
            self.tuner_mode.add_callback(get_threshold_cb(low=1, high=1))  # == 1 implies auto mode

        # P1 from microcontroller
        self.fsd1 = epics.PV(f"{self.epics_name}STAT.B3", connection_callback=connection_cb)
        # P1 from IOC
        self.fsd2 = epics.PV(f"{self.epics_name}STAT.B4", connection_callback=connection_cb)

        # # What is the max steps to take at a time before checking it's effect.  100 is normal mode, 1000 is turbo
        # self.tuner_turbo = epics.PV(f"{self.epics_name}THDRV.C", connection_callback=connection_cb)
        # # If == 50,000 for more than one second, set TSEL.A to 3 to reset
        # self.tuner_step_count = epics.PV(f"{self.epics_name}TSELI", connection_callback=connection_cb)
        # # Set to 3 to trigger a step
        # self.tuner_step_reset = epics.PV(f"{self.epics_name}TSEL.A", connection_callback=connection_cb)

        tdeta_n_suffix = ".N"
        if config.get_parameter('testing'):
            tdeta_n_suffix = "_N"
        self.tdeta_n = epics.PV(f"{self.epics_name}TDETA{tdeta_n_suffix}", connection_callback=connection_cb)
        self.gset_min = 3

        # Don't monitor faults on cavities that effectively bypassed.
        if not self.bypassed_eff:
            self.rf_on.add_callback(rf_on_cb)
            self.fsd1.add_callback(get_threshold_cb(low=0, high=0))  # == 1 implies fault
            self.fsd2.add_callback(get_threshold_cb(low=0, high=0))  # == 1 implies fault

        self.pv_list.append(self.rf_on)
        self.pv_list.append(self.tdeta)
        self.pv_list.append(self.tdeta_n)
        self.pv_list.append(self.tuner_mode)

    def is_rf_on(self):
        """A simple method to determine if RF is on in a cavity"""
        return self.rf_on.value == 1

    def is_gradient_ramping(self, gmes_threshold: float = 0.3) -> bool:
        """Determine if the cavity gradient is ramping to the target.

        The approach is different for every cavity type.  As a fallback, check if gmes is "close" to gset.  Some
        cavities are off by more 0.25 MV/m while others are within <0.1 MV/m.  The default tolerance has to be
        surprisingly high because of this.

        Args:
            gmes_threshold: The minimum absolute difference between gmes and gset that indicates ramping.  Only used if
                            no better way of checking for ramping is present.
        """
        if not self.is_rf_on():
            raise RuntimeError(f"RF is off at {self.name}")

        is_ramping = math.fabs(self.gmes.value - self.gset.value) > gmes_threshold
        return is_ramping

    def is_tuning_required(self, margin: Optional[float] = None):
        """Check if tuning is required.

        Note the tuners may be running even when not 'required' as they start when required, but then run down to a
        lower level to give more margin for detuning.
        """

        if margin is None:
            margin = self.tuner_recovery_margin

        return math.fabs(self.tdeta.value) > (self.tdeta_n.value - margin)

    def set_gradient(self, gset: float, settle_time: float = 6.0, wait_for_ramp=True, ramp_timeout=20,
                     force: bool = False, gradient_epsilon: float = 0.05, interactive: bool = True):
        """Set a cavity's gradient and wait for supporting systems to compensate for the change.

        New change can't be more than 1 MV/m away from current value, without force.  LLRF 1.0 cavities (C25/C50s) do
        not ramp.  Typically, it doesn't seem to be needed, however it seems like it could still be possible to trip
        something by accident.  Since we have the logic in place, just ramp them.

        Args:
            gset: The new value to set GSET to
            settle_time: How long we need to wait for cryo system to adjust to change in heat load
            wait_for_ramp: Do we need to wait for the cavity gradient to ramp up?  C100s ramp for larger steps.
            ramp_timeout: How long to wait for ramping to finish once started?  This prompts a user on timeout.
            force: Are we going to force a big gradient move?  The 1 MV/m step is a very cautious limit.
            gradient_epsilon: The minimum difference between GSET and GMES that we consider as "noise".
            interactive: Should the user be prompted for interactive process control

        """

        logger.info(f"{self.name}: Setting gradient from {self.gset.value} to {gset}.")

        # Sometimes we end up some small delta above the max by rounding errors.  If we're close, just treat it like a
        # set to the max.
        if (gset > self.gset_max) and ((gset - 0.0001) < self.gset_max):
            logger.info(f"Change requested gset to gset_max ({self.gset_max}).  Likely rounding error.")
            gset = self.gset_max

        self._validate_requested_gradient(gset=gset, force=force)
        self._do_gradient_ramping(gset=gset, settle_time=settle_time, wait_for_ramp=False,
                                  ramp_timeout=ramp_timeout, gradient_epsilon=gradient_epsilon, interactive=interactive)


class LLRF2Cavity(Cavity):
    def __init__(self, name: str, epics_name: str, cavity_type: str, length: float, tuner_recovery_margin: float,
                 tuner_bad: bool, bypassed: bool, zone: 'Zone', Q0: float, gset_no_fe: float = None,
                 gset_fe_onset: float = None, gset_max: float = None, gmes_step_size: float = 0.1,
                 gmes_sleep_interval: float = 1.0, tuner_timeout: float = 300):
        super().__init__(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length, tuner_bad=tuner_bad,
                         bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe, gset_fe_onset=gset_fe_onset,
                         gset_max=gset_max, gset_min=3.0, gmes_step_size=gmes_step_size, tuner_timeout=tuner_timeout,
                         gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin)

        # Define constants for this type of cavity
        self.gset_min = 5

        self.fccver = epics.PV(f"{self.epics_name}FCCVER", connection_callback=connection_cb)
        self.rf_on = epics.PV(f"{self.epics_name}RFONr", connection_callback=connection_cb)
        self.stat1 = epics.PV(f"{self.epics_name}STAT1", connection_callback=connection_cb)
        self.deta = epics.PV(f"{self.epics_name}DETA", connection_callback=connection_cb)
        self.fsd = epics.PV(f"{self.epics_name}FBRIO", connection_callback=connection_cb)

        # Detune in hertz
        self.cfqe = epics.PV(f"{self.epics_name}CFQE", connection_callback=connection_cb)
        # Max detune in hertz before tuner needs to be engaged
        self.detahzhi = epics.PV(f"{self.epics_name}DETAHZHI", connection_callback=connection_cb)

        self.pset_init = self.pset.get()
        self.gset_init = self.gset.get()

        # Cavity can be effectively bypassed in a number of ways.  Work through that here.
        self.bypassed_eff = bypassed
        if self.gset_init == 0:
            self.bypassed_eff = True
        elif self.odvh.value == 0:
            self.bypassed_eff = True

        self.fcc_firmware_version = self.fccver.get()
        if self.fcc_firmware_version is None:
            raise RuntimeError(f"{self.epics_name}: Could not get FCC Version")

        # Attach a callback that watches for RF to turn off or for faults.  Don't watch RF if the cavity is
        # bypassed.
        if not self.bypassed_eff:
            self.rf_on.add_callback(rf_on_cb)
            # Only new LLRF 2.0 has this at 768
            if self.fcc_firmware_version >= 2021:
                # If not 768, then we have an FSD being pulled.
                self.fsd.add_callback(get_threshold_cb(low=768, high=768))
            elif self.fcc_firmware_version < 2021:
                # If not 972, then we have an FSD being pulled.
                self.fsd.add_callback(get_threshold_cb(low=972, high=972))

        # We want tuners to be in auto mode.  Tuners might not be used if bypassed or known tuner problem
        # 1 is auto, 0 is manual
        self.tuner_mode = epics.PV(f"{self.epics_name}TCMDbits.B7", connection_callback=connection_cb)
        if self.tuner_bad or self.bypassed_eff:
            logger.info(f"{self.name}: Tuner bad or bypassed.  Not monitoring tuner mode")
        else:
            self.tuner_mode.add_callback(get_threshold_cb(low=1, high=1))  # == 1 implies auto mode

        # List of all PVs related to a cavity.
        self.pv_list = self.pv_list + [self.rf_on, self.deta, self.stat1, self.fsd, self.fccver, self.cfqe,
                                       self.detahzhi, self.tuner_mode]

        # Each cavity keeps track of an externally set maximum value.  Make sure to update this after connecting to PVs.
        self.gset_max = self.gset_min
        self.gset_max_requested = gset_max

    def is_tuning_required(self, margin: Optional[float] = None) -> bool:
        """Check if we are outside the bounds where the tuner should be active."""
        if margin is None:
            margin = self.tuner_recovery_margin

        return math.fabs(self.cfqe.value) >= (self.detahzhi.value - margin)

    def is_rf_on(self) -> bool:
        """A simple method to determine if RF is on in a cavity"""
        return self.rf_on.value == 1

    def is_gradient_ramping(self, gmes_threshold: float = 0.2) -> bool:
        """Determine if the cavity gradient is ramping to the target.

        'Old' LLRF2.0 nicely provide a status bit.  'New' LLRF2.0 do not ramp (2022-11-22), but may one day soon.

        Args:
            gmes_threshold: The minimum absolute difference between gmes and gset that indicates ramping.  NOT USED.
        """
        if not self.is_rf_on():
            raise RuntimeError(f"RF is off at {self.name}")

        # For C100, the "is ramping" field is the 11th bit counting from zero.  If it's zero, then we're not
        # ramping.  Otherwise, we're ramping.
        is_ramping = int(self.stat1.value) & 0x0800 > 0
        return is_ramping

    def set_gradient(self, gset: float, settle_time: float = 6.0, wait_for_ramp: bool = True, ramp_timeout: float = 20,
                     force: bool = False, gradient_epsilon: float = 0.05, interactive: bool = True):
        """Set a cavity's gradient and wait for supporting systems to compensate for the change.

        New change can't be more than 1 MV/m away from current value, without force.  This attempts to wait for a cavity
        to ramp if requested.  However, it also has a shortcut check that if the GMES is very close to the requested
        GSET, then it will stop watching as the ramping is essentially over if it happened at all.

        Args:
            gset: The new value to set GSET to
            settle_time: How long we need to wait for cryo system to adjust to change in heat load
            wait_for_ramp: Do we need to wait for the cavity gradient to ramp up?  Some C100s ramp for larger step.
                           This option only used for older 2.0 cavities.  If newer 2.0 cavities, we don't wait for ramp,
                           as they do the ramping in this software and introduce a wait as needed.
            ramp_timeout: How long to wait for ramping to finish once started?  This prompts a user on timeout.
            force: Are we going to force a big gradient move?  The 1 MV/m step is a very cautious limit.
            gradient_epsilon: The minimum difference between GSET and GMES that we consider as "noise".
            interactive: Should the user be prompted for interactive process control

        """

        logger.info(f"{self.name}:  Setting gradient from {self.gset.value} to {gset}.")

        # Sometimes we end up some small delta above the max by rounding errors.  If we're close, just treat it like a
        # set to the max.
        if (gset > self.gset_max) and ((gset - 0.0001) < self.gset_max):
            logger.info(f"Change requested gset to gset_max ({self.gset_max}).  Likely rounding error.")
            gset = self.gset_max

        try:
            self._validate_requested_gradient(gset=gset, force=force)
        except Exception as ex:
            logger.error(f"{self.name}: Error with requested gradient {gset} (force={force}).  {ex}")
            raise ex
        # Newer style cavities are ramping in this software, so we don't have to wait and watch for external ramping.
        if self.fcc_firmware_version > 2019:
            wait_for_ramp = False

        # Newer style control systems provide ramping that track detune issues, but not necessarily anything else.
        # It's safest to always ramp, since the worst case scenario is that we wait a little longer while than
        # necessary at the cost of not exceeding cryo, etc. limits.
        try:
            self._do_gradient_ramping(gset=gset, settle_time=settle_time, wait_for_ramp=wait_for_ramp,
                                      ramp_timeout=ramp_timeout, gradient_epsilon=gradient_epsilon, interactive=interactive)
        except Exception as ex:
            logger.error(f"{self.name}: Error ramping gradient to {gset}.  {ex}")
            traceback.print_exc()
            raise ex


class LLRF3Cavity(Cavity):
    def __init__(self, name: str, epics_name: str, cavity_type: str, length: float, tuner_recovery_margin: float,
                 tuner_bad: bool, bypassed: bool, zone: 'Zone', Q0: float, gset_no_fe: float = None,
                 gset_fe_onset: float = None, gset_max: float = None, gmes_step_size: float = 0.1,
                 gmes_sleep_interval: float = 1.0, tuner_timeout: float = 300):
        super().__init__(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length, tuner_bad=tuner_bad,
                         bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe, gset_fe_onset=gset_fe_onset,
                         gset_max=gset_max, gset_min=3.0, gmes_step_size=gmes_step_size, tuner_timeout=tuner_timeout,
                         gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin)

        # Define constants for this type of cavity
        self.gset_min = 5

        # 1 = RF on, 0 = RF off
        self.rf_on = epics.PV(f"{self.epics_name}RFONr", connection_callback=connection_cb)
        self.stat1 = epics.PV(f"{self.epics_name}STAT1", connection_callback=connection_cb)
        self.deta = epics.PV(f"{self.epics_name}DETA", connection_callback=connection_cb)

        # FBRIO should catch everything, but it's broken on some zones.  Need to check to lower-level PVs for now.
        # Rama had suggested R..XCIEN and R...KFLT in it's place.  Scott said the public version of those are XIFLTCIEN
        # and KFLTT.
        # self.fsd = epics.PV(f"{self.epics_name}FBRIO", connection_callback=connection_cb)

        # R..XIFLTCIEN.Bx as a per cavity MBBI => Cavity 1 == B0, Cavity 2 == B1, ....  Fault = 1, ok = 0
        self.fsd0 = epics.PV(f"{self.epics_name[:-1]}XIFLTCIEN.B{self.cavity_number - 1}",
                             connection_callback=connection_cb, callback=get_threshold_cb(low=0, high=0))

        # self.fsd1 = epics.PV(f"{self.epics_name[:-1]}XCIEN", connection_callback=connection_cb)
        # R...KFLTT.Bx is a per cavity PV.  Klystron related faults at B0 - B7, Fault = 1, ok = 0
        self.fsd1 = epics.PV(f"{self.epics_name}KFLTT.B0", connection_callback=connection_cb,
                             callback=get_threshold_cb(low=0, high=0))
        self.fsd2 = epics.PV(f"{self.epics_name}KFLTT.B1", connection_callback=connection_cb,
                             callback=get_threshold_cb(low=0, high=0))
        self.fsd3 = epics.PV(f"{self.epics_name}KFLTT.B2", connection_callback=connection_cb,
                             callback=get_threshold_cb(low=0, high=0))
        self.fsd4 = epics.PV(f"{self.epics_name}KFLTT.B3", connection_callback=connection_cb,
                             callback=get_threshold_cb(low=0, high=0))
        self.fsd5 = epics.PV(f"{self.epics_name}KFLTT.B4", connection_callback=connection_cb,
                             callback=get_threshold_cb(low=0, high=0))
        self.fsd6 = epics.PV(f"{self.epics_name}KFLTT.B5", connection_callback=connection_cb,
                             callback=get_threshold_cb(low=0, high=0))
        self.fsd7 = epics.PV(f"{self.epics_name}KFLTT.B6", connection_callback=connection_cb,
                             callback=get_threshold_cb(low=0, high=0))
        self.fsd8 = epics.PV(f"{self.epics_name}KFLTT.B7", connection_callback=connection_cb,
                             callback=get_threshold_cb(low=0, high=0))

        # Detune in hertz
        self.cfqe = epics.PV(f"{self.epics_name}CFQE", connection_callback=connection_cb)
        # Max detune in hertz before tuner needs to be engaged
        self.detahzhi = epics.PV(f"{self.epics_name}DETAHZHI", connection_callback=connection_cb)

        self.pset_init = self.pset.get()
        self.gset_init = self.gset.get()

        # Cavity can be effectively bypassed in a number of ways.  Work through that here.
        self.bypassed_eff = bypassed
        if self.gset_init == 0:
            self.bypassed_eff = True
        elif self.odvh.value == 0:
            self.bypassed_eff = True

        # Attach a callback that watches for RF to turn off.  Don't watch "RF on" if the cavity is bypassed.
        if not self.bypassed_eff:
            self.rf_on.add_callback(rf_on_cb)

        # We want tuners to be in auto mode.  Tuners might not be used if bypassed or known tuner problem
        # 1 is auto, 0 is manual
        self.tuner_mode = epics.PV(f"{self.epics_name}TCMDbits.B7", connection_callback=connection_cb)
        if self.tuner_bad or self.bypassed_eff:
            logger.info(f"{self.name}: Tuner bad or bypassed.  Not monitoring tuner mode.")
        else:
            self.tuner_mode.add_callback(get_threshold_cb(low=1, high=1))  # == 1 implies auto mode

        # List of all PVs related to a cavity.
        self.pv_list = self.pv_list + [self.rf_on, self.deta, self.stat1, self.cfqe, self.detahzhi, self.tuner_mode,
                                       self.fsd0, self.fsd1, self.fsd2, self.fsd3, self.fsd4, self.fsd5, self.fsd6,
                                       self.fsd7, self.fsd8]
        self.pv_list = [pv for pv in self.pv_list if pv is not None]



        # Each cavity keeps track of an externally set maximum value.  Make sure to update this after connecting to PVs.
        self.gset_max = self.gset_min
        self.gset_max_requested = gset_max

    def is_rf_on(self):
        """A simple method to determine if RF is on in a cavity"""
        return self.rf_on.value == 1

    def is_tuning_required(self, margin: Optional[float] = None):
        """A check if the cavity detune has passed it's tuner activation threshold."""

        if margin is None:
            margin = self.tuner_recovery_margin

        if math.fabs(self.cfqe.value) > (self.detahzhi.value - margin):
            return True
        return False

    def is_gradient_ramping(self, gmes_threshold: float = 0.3) -> bool:
        """Determine if the cavity gradient is ramping to the target by checking status bits.

        Args:
            gmes_threshold: The minimum absolute difference between gmes and gset that indicates ramping.  NOT USED.
        """
        if not self.is_rf_on():
            raise RuntimeError(f"RF is off at {self.name}")

        # For C75, the "is ramping" field is the 15th bit counting from zero.  If it's zero, then we're not
        # ramping.  Otherwise, we're ramping.  (Per K. Hesse)
        is_ramping = int(self.stat1.value) & 0x8000 > 0
        return is_ramping

    def set_gradient(self, gset: float, settle_time: float = 6.0, wait_for_ramp=False, ramp_timeout=20,
                     force: bool = False, gradient_epsilon: float = 0.05, interactive: bool = True):
        """Set a cavity's gradient and wait for supporting systems to compensate for the change.

        New change can't be more than 1 MV/m away from current value, without force.  This attempts to wait for a cavity
        to ramp if requested.  However, it also has a shortcut check that if the GMES is very close to the requested
        GSET, then it will stop watching as the ramping is essentially over if it happened at all.

        Args:
            gset: The new value to set GSET to
            settle_time: How long we need to wait for cryo system to adjust to change in heat load
            wait_for_ramp: Do we need to wait for the cavity gradient to ramp up?  C75s don't ramp at this time.
            ramp_timeout: How long to wait for ramping to finish once started?  This prompts a user on timeout.
            force: Are we going to force a big gradient move?  The 1 MV/m step is a very cautious limit.
            gradient_epsilon: The minimum difference between GSET and GMES that we consider as "noise".
            interactive: Should the user be prompted for interactive process control

        """

        logger.info(f"{self.name}:  Setting gradient from {self.gset.value} to {gset}.")

        # Sometimes we end up some small delta above the max by rounding errors.  If we're close, just treat it like a
        # set to the max.
        if (gset > self.gset_max) and ((gset - 0.0001) < self.gset_max):
            logger.info(f"Change requested gset to gset_max ({self.gset_max}).  Likely rounding error.")
            gset = self.gset_max

        try:
            self._validate_requested_gradient(gset=gset, force=force)
        except Exception as ex:
            logger.error(f"{self.name}: Error with requested gradient {gset} (force={force}).  {ex}")
            raise ex

        # self.wait_for_tuning()
        try:
            self._do_gradient_ramping(gset=gset, settle_time=settle_time, wait_for_ramp=wait_for_ramp,
                                      ramp_timeout=ramp_timeout, gradient_epsilon=gradient_epsilon, interactive=interactive)
        except Exception as ex:
            logger.error(f"{self.name}: Error ramping gradient to {gset}.  {ex}")
            traceback.print_exc()
            raise ex


def collect_data_at_gradients(cavs: List[Cavity], new_gsets: Dict[str, float], old_gsets: Dict[str, float],
                              settle_time: float, avg_time: float, file: TextIO):
    """This method changes the gradient settings, writes data to the index, and rolls back gradient changes.

    Users are prompted should something go wrong.  UserScanAbort is raised if user requests we abort the entire scan.

    Returns the final status
    """
    status = Status.UNKNOWN
    while status != Status.SUCCESS:
        logger.info("Attempting cavity updates.")
        status, failed = update_cavity_gsets_parallel(cavs=cavs, new_gsets=new_gsets, settle=0,
                                                      force=True)
        if status != Status.SUCCESS:
            logger.warning(f"{len(failed)} cavities had problems updating")
            status = update_gsets_failure_prompt(failed=failed)
            if status == Status.ABORT:
                raise UserScanAbort("Aborting after errors updating gradients.")
            elif status == Status.FAIL:
                # User requested we skip this update, roll back any changes, and move on with the
                # procedure
                break
            else:
                # User indicated either success (take data and move on even if there were some problems)
                # or retry (which means we'll go through this loop again.)
                pass

    if status == Status.SUCCESS:
        logger.info(f"Begin settling and data collection period")
        # Only take data if we declared success on updating gradients
        settle_and_collect_data(cavs=cavs, file=file, settle_time=settle_time, avg_time=avg_time)

    # Now roll back the changes
    status = Status.UNKNOWN
    while status != Status.SUCCESS:
        logger.info("Attempting cavity gradient rollback.")
        status, failed = update_cavity_gsets_parallel(cavs=cavs, new_gsets=old_gsets, settle=0,
                                                      force=True)
        logger.warning(f"Update status = {status}")
        if status != Status.SUCCESS:
            retry = user_alert_scan_paused("Rollback Failed.  Try again?")
            if not retry:
                raise UserScanAbort("Aborting after error rolling back gradient changes.")

            # response = input("Try to rollback gradients again (y/n)?  'n' aborts scan.")
            # if response.strip().lower().startswith("n"):
            #     raise UserScanAbort("Aborting after error rolling back gradient changes.")


def update_cavity_gset(cavity: Cavity, gset: float, settle: float, force: bool, interactive: bool) -> bool:
    """Update a cavity and return true/false based on success/error."""

    success = False
    try:
        # Since we're setting multiple cavities, we will enforce a single common settle time after
        # all are set.
        cavity.set_gradient(gset=gset, settle_time=settle, force=force, interactive=interactive)
        success = True
    except UserScanAbort:
        raise
    except Exception as exc:
        logger.error(f"{cavity.name}: Error setting gradient.  {exc}")

    return success


def update_cavity_gsets_parallel(cavs: List[Cavity], new_gsets: Dict[str, float], settle: float,
                                 force: bool) -> Tuple[Status, List[Cavity]]:
    """Update cavity gradients in parallel.  Return True if data should be collected."""
    time.sleep(0.1)
    status = Status.FAIL
    failed = [cav for cav in cavs]
    interactive = False
    try:
        StateMonitor.check_state()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(cavs)) as executor:
            futures = {}
            for cav in cavs:
                logger.debug(f"Submitting {cav.name} for parallel GSET update")
                future = executor.submit(update_cavity_gset, cav, new_gsets[cav.name], settle, force, interactive)
                futures[future] = cav

            for future in concurrent.futures.as_completed(futures):
                # update_cavity_gset returns True if successful, false otherwise.
                cav = futures[future]
                if not future.result():
                    logger.error(f"{cav.name}: Error updating gradients.")
                else:
                    # Remove the cavity from the list of failures
                    for idx, cav2 in enumerate(failed):
                        if cav2.name == cav.name:
                            del failed[idx]

        if len(failed) > 0:
            status = Status.FAIL
        else:
            status = Status.SUCCESS

    except UserScanAbort:
        raise
    except Exception as ex:
        traceback.print_exc()
        logger.error(f"Bad state detected or trouble with cavities: {ex}")
        status = status.FAIL

    return status, failed


def update_gsets_failure_prompt(failed: List[Cavity]):
    # Exit the executor context manager to close out the thread pool.  Now process the results
    if len(failed) > 0:
        status = user_alert_update_gsets_fail(failed=failed)
        # print(f"Cavities {','.join([cav.name for cav in failed])} failed to update.  Options:")
        # print(f"(R) Retry cavity updates")
        # print(f"(A) Abort collection procedure.  PSETs restored, no further gradient changes.")
        # print(f"(S) Skip cavity updates and roll gradients back to their original settings.")
        # print(f"(K) Keep current gradients as is.  Helpful if some cavities updated but others fail on retry.")
        #
        # response = None
        # invalid = True
        # while invalid:
        #     response = input("Selection [R, A, S, K]: ").upper().strip()[0]
        #     if response in 'RASK':
        #         invalid = False
        #     else:
        #         print(f"Invalid response '{response}'")
        #
        # if response is None or type(response) != str or len(response) == 0:
        #     logger.info(f"Received unknown response '{response}'.  Using 'R' / retry as default")
        #     response = 'R'
        #
        # if response.startswith('R'):
        #     status = Status.RETRY
        #     # # Recurse back into this function and try again.  Return the eventual status of follow on
        #     # # attempts.
        #     # status = update_cavity_gsets_parallel(cavs, new_gsets, settle, force)
        # elif response.startswith('S'):
        #     status = Status.FAIL
        #     # Call this function again to put the gradients back to their original settings.  Return FAIL.
        #     # update_cavity_gsets_parallel(cavs, orig_gsets, settle, force)
        #     # status = Status.FAIL
        # elif response.startswith('A'):
        #     # The user requested we stop the procedure all together and make no more changes than to restore
        #     # PSETs
        #     status = Status.ABORT
        # elif response.startswith('K'):
        #     # Something went wrong, but the user thinks something updated successfully.  We'll call it success.
        #     status = Status.SUCCESS
    else:
        # We didn't have any failures
        status = Status.SUCCESS

    return status


def settle_and_collect_data(cavs: List[Cavity], file: TextIO, settle_time: float, avg_time: float) -> None:
    """Wait the requested times for cryo to settle and explicit data collection.  Then write data to disk.

    Note: This assumes you have already opened a non-binary file for text writing.
    """
    # Do the common settle time now or simply check that we have a good state to begin collecting data
    if settle_time > 0:
        logger.info(f"Waiting {settle_time} seconds for cryo to settle.")
        settle_start = datetime.now()
        StateMonitor.monitor(duration=settle_time)
        settle_end = datetime.now()
    else:
        StateMonitor.check_state()
        settle_start = datetime.now()
        settle_end = settle_start

    # Do the data collection (averaging) time now
    logger.info(f"Waiting {avg_time} seconds for MYA to collect data.")
    avg_start = settle_end
    StateMonitor.monitor(duration=avg_time)
    avg_end = datetime.now()

    logger.info("Writing to data log")
    cav_names = [cav.name for cav in cavs]
    cav_epics_names = [cav.epics_name for cav in cavs]
    write_data_index_row(file, settle_start=settle_start, settle_end=settle_end, avg_start=avg_start,
                         avg_end=avg_end, settle_time=settle_time, avg_time=avg_time,
                         cavity_name=cav_names, cavity_epics_name=cav_epics_names)
    file.flush()
