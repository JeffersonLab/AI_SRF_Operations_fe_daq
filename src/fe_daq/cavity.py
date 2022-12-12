import math
import time
from datetime import datetime
from typing import Optional

import epics
import logging
import numpy as np

from fe_daq.state_monitor import connection_cb, rf_on_cb, StateMonitor, get_threshold_cb
from fe_daq import app_config as config

logger = logging.getLogger(__name__)


class Cavity:
    @classmethod
    def get_cavity(cls, name: str, epics_name: str, cavity_type: str, length: float,
                   bypassed: bool, zone: 'Zone', Q0: float, gset_no_fe: float = None, gset_fe_onset: float = None,
                   gset_max: float = None):
        if zone.controls_type == '1.0':
            gmes_step_size = config.get_parameter('LLRF1_gmes_step_size')
            gmes_sleep_interval = config.get_parameter('LLRF1_gmes_sleep_interval')
            tuner_recovery_margin = config.get_parameter('LLRF1_tuner_recovery_margin')
            cavity = LLRF1Cavity(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length,
                                 bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe,
                                 gset_fe_onset=gset_fe_onset, gset_max=gset_max, gmes_step_size=gmes_step_size,
                                 gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin)
        elif zone.controls_type == '2.0':
            gmes_step_size = config.get_parameter('LLRF2_gmes_step_size')
            gmes_sleep_interval = config.get_parameter('LLRF2_gmes_sleep_interval')
            tuner_recovery_margin = config.get_parameter('LLRF2_tuner_recovery_margin')
            cavity = LLRF2Cavity(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length,
                                 bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe,
                                 gset_fe_onset=gset_fe_onset, gset_max=gset_max, gmes_step_size=gmes_step_size,
                                 gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin)
        elif zone.controls_type == '3.0':
            gmes_step_size = config.get_parameter('LLRF3_gmes_step_size')
            gmes_sleep_interval = config.get_parameter('LLRF3_gmes_sleep_interval')
            tuner_recovery_margin = config.get_parameter('LLRF3_tuner_recovery_margin')
            cavity = LLRF3Cavity(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length,
                                 bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe,
                                 gset_fe_onset=gset_fe_onset, gset_max=gset_max, gmes_step_size=gmes_step_size,
                                 gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin)
        else:
            raise ValueError(f"Unsupported controls_type '{zone.controls_type}")

        # Update gset_max and wait_for_connections should be called after cavities have been created.
        return cavity

    # Importing Zone would result in circular imports
    # noinspection PyUnresolvedReferences
    def __init__(self, name: str, epics_name: str, cavity_type: str, length: float,
                 bypassed: bool, zone: 'Zone', Q0: float, gset_no_fe: float = None, gset_fe_onset: float = None,
                 gset_max: float = None, gset_min: float = None, gmes_step_size: float = 0.1,
                 gmes_sleep_interval: float = 1, tuner_recovery_margin: float = 1.0):
        self.name = name
        self.epics_name = epics_name
        self.zone_name = zone.name
        self.cavity_type = cavity_type
        self.controls_type = zone.controls_type  # These should be '1.0', '2.0', etc. LLRF controls.
        self.length = length
        self.bypassed = bypassed
        self.zone = zone
        self.Q0 = Q0
        self.cavity_number = int(name[5:6])
        self.tuner_recovery_margin = tuner_recovery_margin

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

    def is_tuning_required(self):
        raise NotImplementedError("Must be implemented by child classes")

    def wait_for_tuning(self, tune_timeout: float = 60):
        """Method that waits for a cavity to be brought back within tune limits.  No Waiting if no tuning required.

        THis uses the global state monitor and prompts for user interaction for any problem anywhere.
        """
        start_ramp = datetime.now()
        needed_tuning = False
        while self.is_tuning_required():
            if not needed_tuning:
                # Only do this stuff the first time
                logger.info(f"{self.name}: Waiting for tuner (timeout = {tune_timeout})")
                needed_tuning = True

            StateMonitor.monitor(0.05)
            if (datetime.now() - start_ramp).total_seconds() > tune_timeout:
                logger.warning(f"{self.name} is taking a long time to tune.")
                response = input(
                    f"Waited {tune_timeout} seconds for {self.name} to tune.  Continue waiting? (n|y): ").lstrip().lower()
                if not response.startswith("y"):
                    msg = f"User requested exit while waiting on {self.name} to tune."
                    logger.error(msg)
                    raise RuntimeError(msg)
                # Restart the counter by pretending we just started to tune
                start_ramp = datetime.now()

        if needed_tuning:
            logger.info(f"{self.name}: Done tuning")

    def _wait_for_tuning(self, timeout: float):
        """Check the status of the tuners and wait at most timeout seconds if tuning is required"""
        start_ramp = datetime.now()
        needed_tuning = False
        margin = 0
        while self.is_tuning_required(margin=margin):
            margin = self.tuner_recovery_margin
            needed_tuning = True
            logger.info(f"{self.name}: Waiting {timeout} seconds for tuner to finish")
            time.sleep(0.05)
            if (datetime.now() - start_ramp).total_seconds() > timeout:
                logger.warning(f"{self.name} timed out waiting for tuner.")
                raise RuntimeError(f"{self.name} tuner timed out")

        if needed_tuning:
            logger.info(f"{self.name}: Done tuning")

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

    def walk_gradient(self, gset: float, step_size: Optional[float] = 1.0, wait_interval: Optional[float] = 0.0,
                      **kwargs) -> None:
        """Move the gradient of the cavity to gset in steps.

        This always waits for the gradient to ramp, but allows for user specified settle_time and ramping timeouts.

        Args:
            gset:  The target gradient set point
            step_size:  The maximum size steps to make in MV/m by absolute value.  Use class gmes_step_size if None.
            wait_interval: How long to wait between steps.  Some cavities do not ramp themselves.  Use object's
                           gmes_sleep_interval if None.

        Additional kwargs are passed to set_gradient.
        """

        if step_size is None:
            step_size = self.gmes_step_size
        if wait_interval is None:
            wait_interval = self.gmes_sleep_interval

        # Determine step direction
        actual_gset = self.gset.get(use_monitor=False)
        if gset >= actual_gset:
            step_dir = 1
        else:
            step_dir = -1

        if gset > self.odvh.value:
            msg = f"Requested {self.name} gradient higher than ODVH {self.odvh.value}."
            logger.error(msg)
            raise ValueError(msg)

        logger.info(f"{self.name}: Walking {actual_gset} to {gset} in {step_size} MV/m steps with {wait_interval}s "
                    f"waits.")

        # Walk step size until we're within a single step
        while abs(gset - actual_gset) > step_size:
            next_gset = actual_gset + (step_dir * step_size)
            self.set_gradient(gset=next_gset, **kwargs)
            actual_gset = self.gset.get(use_monitor=False)
            if wait_interval > 0:
                time.sleep(wait_interval)

        # We should be within a single step here.
        self.set_gradient(gset=gset, **kwargs)

    def _validate_requested_gradient(self, gset: float, force: bool):
        """Run a series of checks to ensure that the new requested gradient makes is a viable.

        Args:
            gset: The requested gset
            force: Are we allowed to exceed single step limits.
        """
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
                      gradient_epsilon: float) -> None:
        # Instead of trying to watch tuner status, etc., we just set the gradient, then sleep some requested amount of
        # time.  This will need to be approached differently if used in a more general purpose application.
        # C100's will ramp gradient for you, but we need to wait for it.
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
                    logger.info(f"{self.name} is ramping gradient")
                    break
                # Add a sleep/monitor and check if we've reached our target gradient.  If we're this close and the
                # cavity is not ramping, then it's very unlikely  to ramp.  This difference is the usual noise in GMES.
                StateMonitor.monitor(0.05)
                if math.fabs(gset - self.gmes.value) < gradient_epsilon:
                    break

            if ramp_started:
                # Here we have to wait for the gradient to finish ramping, assuming it actually started
                logger.info(f"{self.name} waiting for gradient to ramp")
                start_ramp = datetime.now()
                while self.is_gradient_ramping():
                    StateMonitor.monitor(0.1)
                    if (datetime.now() - start_ramp).total_seconds() > ramp_timeout:
                        logger.warning(f"{self.name} is taking a long time to ramp.")
                        response = input(
                            f"Waited {ramp_timeout} seconds for {self.name} to ramp.  Continue? (n|y): "
                        ).lstrip().lower()
                        if not response.startswith("y"):
                            msg = f"User requested exit while waiting on {self.name} to ramp."
                            logger.error(msg)
                            raise RuntimeError(msg)
                        start_ramp = datetime.now()
            else:
                logger.info(f"{self.name} did not ramp gradient")

        if settle_time > 0:
            logger.info(f"{self.name} Waiting {settle_time} seconds for cryo to adjust")
        StateMonitor.monitor(duration=settle_time)

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
        lp_recovery_point = self.zone.linac.linac_pressure_max - self.zone.linac.lp_recovery_margin
        wait_for_linac_pressure, linac_pressure = self.zone.linac.check_linac_pressure()
        if wait_for_linac_pressure:
            logger.warning(f"{self.name}: Found linac pressure too high. {self.zone.linac.linac_pressure.pvname}="
                           f"{linac_pressure}.  Waiting max {timeout} seconds to recover to"
                           f" {lp_recovery_point}.")
        start = datetime.now()
        while wait_for_linac_pressure:
            time.sleep(0.01)
            wait_for_linac_pressure, linac_pressure = self.zone.linac.check_linac_pressure(threshold=lp_recovery_point)
            if not wait_for_linac_pressure:
                logger.info(f"{self.name}: Linac pressure recovered to {linac_pressure}")
            elif (datetime.now() - start).total_seconds() > timeout:
                logger.warning(f"{self.name}: JT valve unrecovered. {self.zone.linac.linac_pressure.pvname}"
                               f"={linac_pressure}.")
                raise RuntimeError("Timed out waiting for JT Valve")

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

    def _do_gradient_ramping(self, gset: float, settle_time: float, tune_timeout: float = 60, **kwargs):
        """Slow ramp gradient to a new values.  Not all cavities allow time for tuners on a gset caput.

        This is designed so that it pauses whenever the cavity requires tuning, not when the cavity is tuning.  The
        idea is that tuners engage when a cavity crosses a detune max threshold, and the tuners run until the detuning
        hits a lower limit.  We don't want to keep changing gradient (and forcing more detuning) when the cavity is
        past the upper limit, but we don't want to wait until the tuners are completely done.
        """
        # Determine step direction
        actual_gset = self.gset.get(use_monitor=False)
        if gset >= actual_gset:
            step_dir = 1
        else:
            step_dir = -1

        logger.info(f"{self.name}: Manually ramping gradient from {actual_gset} to {gset} in {self.gmes_step_size}"
                    f" MV/m steps with {self.gmes_sleep_interval}s waits.")

        # Walk step size until we're within a single step
        while abs(gset - actual_gset) > self.gmes_step_size:
            # Don't make a change unless we are sufficiently tuned and cryo is happy.  These raise on timeout
            self.wait_for_tuning(tune_timeout=tune_timeout)
            self._wait_for_jt(timeout=60)
            self._wait_for_linac_pressure(timeout=60)
            self._wait_for_heater_margins(timeout=60)

            next_gset = actual_gset + (step_dir * self.gmes_step_size)

            # Make a small step.  Do not wait the settle time, we do that at the end.
            self._set_gradient(gset=next_gset, settle_time=0, **kwargs)
            actual_gset = self.gset.get(use_monitor=False)
            if self.gmes_sleep_interval > 0:
                time.sleep(self.gmes_sleep_interval)

        # We should be within a single step here.  Now wait the full settle time.
        self._set_gradient(gset=gset, settle_time=settle_time, **kwargs)

    def set_gradient(self, gset: float, settle_time: float = 6.0, wait_for_ramp=True, ramp_timeout=20,
                     force: bool = False, gradient_epsilon: float = 0.05):
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


class LLRF1Cavity(Cavity):
    def __init__(self, name: str, epics_name: str, cavity_type: str, length: float,
                 bypassed: bool, zone: 'Zone', Q0: float, tuner_recovery_margin: float,
                 gset_no_fe: float = None, gset_fe_onset: float = None, gset_max: float = None,
                 gmes_step_size: float = 0.1, gmes_sleep_interval: float = 1.0):
        super().__init__(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length,
                         bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe, gset_fe_onset=gset_fe_onset,
                         gset_max=gset_max, gset_min=3.0, gmes_step_size=gmes_step_size,
                         gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin)

        self.rf_on = epics.PV(f"{self.epics_name}ACK1.B6", connection_callback=connection_cb)
        self.tdeta = epics.PV(f"{self.epics_name}TDETA", connection_callback=connection_cb)

        # P1 from microcontroller
        self.fsd1 = epics.PV(f"{self.epics_name}STAT.B3", connection_callback=connection_cb)
        self.fsd1.add_callback(get_threshold_cb(low=0, high=0))  # == 1 implies fault

        # P1 from IOC
        self.fsd2 = epics.PV(f"{self.epics_name}STAT.B4", connection_callback=connection_cb)
        self.fsd2.add_callback(get_threshold_cb(low=0, high=0))  # == 1 implies fault

        tdeta_n_suffix = ".N"
        if config.get_parameter('testing'):
            tdeta_n_suffix = "_N"
        self.tdeta_n = epics.PV(f"{self.epics_name}TDETA{tdeta_n_suffix}", connection_callback=connection_cb)
        self.gset_min = 3

        if not self.bypassed:
            self.rf_on.add_callback(rf_on_cb)

        self.pv_list.append(self.rf_on)
        self.pv_list.append(self.tdeta)
        self.pv_list.append(self.tdeta_n)


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

    def is_tuning_required(self):
        """Check if tuning is required.

        Note the tuners may be running even when not 'required' as they start when required, but then run down to a
        lower level to give more margin for detuning.
        """
        return math.fabs(self.tdeta.value) > self.tdeta_n.value

    def set_gradient(self, gset: float, settle_time: float = 6.0, wait_for_ramp=True, ramp_timeout=20,
                     force: bool = False, gradient_epsilon: float = 0.05):
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

        """

        self._validate_requested_gradient(gset=gset, force=force)
        self._do_gradient_ramping(gset=gset, settle_time=settle_time, wait_for_ramp=False,
                                  ramp_timeout=ramp_timeout, gradient_epsilon=gradient_epsilon)


class LLRF2Cavity(Cavity):
    def __init__(self, name: str, epics_name: str, cavity_type: str, length: float,  tuner_recovery_margin: float,
                 bypassed: bool, zone: 'Zone', Q0: float, gset_no_fe: float = None, gset_fe_onset: float = None,
                 gset_max: float = None, gmes_step_size: float = 0.1, gmes_sleep_interval: float = 1.0):
        super().__init__(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length,
                         bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe, gset_fe_onset=gset_fe_onset,
                         gset_max=gset_max, gset_min=3.0, gmes_step_size=gmes_step_size,
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
        self.fcc_firmware_version = self.fccver.get()
        if self.fcc_firmware_version is None:
            raise RuntimeError(f"{self.epics_name}: Could not get FCC Version")

        # Attach a callback that watches for RF to turn off or for faults.  Don't watch RF if the cavity is
        # bypassed.
        if not self.bypassed:
            self.rf_on.add_callback(rf_on_cb)
            # Only new LLRF 2.0 has this at 768
            if self.fcc_firmware_version >= 2021:
                # If not 768, then we have an FSD being pulled.
                self.fsd.add_callback(get_threshold_cb(low=768, high=768))
            elif self.fcc_firmware_version < 2021:
                # If not 972, then we have an FSD being pulled.
                self.fsd.add_callback(get_threshold_cb(low=972, high=972))

        # List of all PVs related to a cavity.
        self.pv_list = self.pv_list + [self.rf_on, self.deta, self.stat1, self.fsd, self.fccver, self.cfqe,
                                       self.detahzhi]

        # Cavity can be effectively bypassed in a number of ways.  Work through that here.
        self.bypassed_eff = bypassed
        if self.gset_init == 0:
            self.bypassed_eff = True
        elif self.odvh.value == 0:
            self.bypassed_eff = True

        # Each cavity keeps track of an externally set maximum value.  Make sure to update this after connecting to PVs.
        self.gset_max = self.gset_min
        self.gset_max_requested = gset_max

    def is_tuning_required(self) -> bool:
        """Check if we are outside the bounds where the tuner should be active."""
        return math.fabs(self.cfqe.value) >= self.detahzhi.value

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
                     force: bool = False, gradient_epsilon: float = 0.05):
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

        """

        self._validate_requested_gradient(gset=gset, force=force)
        if self.fcc_firmware_version > 2019:
            # Newer firmware versions do not ramp the gradient and will trip if you move more than ~0.2 MV/m in a step
            self._do_gradient_ramping(gset=gset, settle_time=settle_time, wait_for_ramp=False,
                                      ramp_timeout=ramp_timeout, gradient_epsilon=gradient_epsilon)
        else:
            # Older firmware versions will ramp the gradient and handle tuning as they go
            self._set_gradient(gset=gset, settle_time=settle_time, wait_for_ramp=wait_for_ramp,
                               ramp_timeout=ramp_timeout, gradient_epsilon=gradient_epsilon)


class LLRF3Cavity(Cavity):
    def __init__(self, name: str, epics_name: str, cavity_type: str, length: float, tuner_recovery_margin: float,
                 bypassed: bool, zone: 'Zone', Q0: float, gset_no_fe: float = None, gset_fe_onset: float = None,
                 gset_max: float = None, gmes_step_size: float = 0.1, gmes_sleep_interval: float = 1.0):
        super().__init__(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length,
                         bypassed=bypassed, zone=zone, Q0=Q0, gset_no_fe=gset_no_fe, gset_fe_onset=gset_fe_onset,
                         gset_max=gset_max, gset_min=3.0, gmes_step_size=gmes_step_size,
                         gmes_sleep_interval=gmes_sleep_interval, tuner_recovery_margin=tuner_recovery_margin)
        # Define constants for this type of cavity
        self.gset_min = 5

        # 1 = RF on, 0 = RF off
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

        # Attach a callback that watches for RF to turn off.  Don't watch "RF on" if the cavity is bypassed.
        if not self.bypassed:
            self.rf_on.add_callback(rf_on_cb)

        # Monitor the FSD in the global state monitor
        self.fsd.add_callback(get_threshold_cb(low=768, high=768))

        # List of all PVs related to a cavity.
        self.pv_list = self.pv_list + [self.rf_on, self.deta, self.stat1, self.fsd, self.cfqe, self.detahzhi]
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

    def is_rf_on(self):
        """A simple method to determine if RF is on in a cavity"""
        return self.rf_on.value == 1

    def is_tuning_required(self):
        """A check if the cavity detune has passed it's tuner activation threshold."""
        if math.fabs(self.cfqe.value) > self.detahzhi.value:
            return True
        return False

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

        # For C75, the "is ramping" field is the 15th bit counting from zero.  If it's zero, then we're not
        # ramping.  Otherwise, we're ramping.  (Per K. Hesse)
        is_ramping = int(self.stat1.value) & 0x8000 > 0
        return is_ramping

    def set_gradient(self, gset: float, settle_time: float = 6.0, wait_for_ramp=True, ramp_timeout=20,
                     force: bool = False, gradient_epsilon: float = 0.05):
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

        """

        self._validate_requested_gradient(gset=gset, force=force)
        self.wait_for_tuning()
        self._set_gradient(gset=gset, settle_time=settle_time, wait_for_ramp=wait_for_ramp, ramp_timeout=ramp_timeout,
                           gradient_epsilon=gradient_epsilon)
