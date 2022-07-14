import math
from datetime import datetime
from typing import Optional

import epics
import logging
import numpy as np

from state_monitor import connection_cb, rf_on_cb, StateMonitor

logger = logging.getLogger(__name__)


class Cavity:
    # Importing Zone would result in circular imports
    # noinspection PyUnresolvedReferences
    def __init__(self, name: str, epics_name: str, cavity_type: str, length: float,
                 bypassed: bool, zone: 'Zone', Q0: float, gset_no_fe: float = None, gset_fe_onset: float = None,
                 gset_max: float = None):
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
        self.deta = epics.PV(f"{self.epics_name}DETA", connection_callback=connection_cb)
        self.pset_init = self.pset.get()
        self.gset_init = self.gset.get()

        # Create "RF On" PV and min stable gradient setting for each type of cavity
        if self.controls_type == '1.0':
            # 1 = RF on, 0 = RF off
            self.rf_on = epics.PV(f"{self.epics_name}ACK1.B6", connection_callback=connection_cb)
            self.stat1 = None
            self.gset_min = 3
        elif self.controls_type == '2.0':
            # 1 = RF on, 0 = RF off
            self.rf_on = epics.PV(f"{self.epics_name}RFONr", connection_callback=connection_cb)
            self.stat1 = epics.PV(f"{self.epics_name}STAT1", connection_callback=connection_cb)
            self.gset_min = 5
        elif self.controls_type == '3.0':
            # 1 = RF on, 0 = RF off
            self.rf_on = epics.PV(f"{self.epics_name}RFONr", connection_callback=connection_cb)
            self.stat1 = epics.PV(f"{self.epics_name}STAT1", connection_callback=connection_cb)
            self.gset_min = 5

        if self.cavity_type == "C100":
            self.shunt_impedance = 1241.3
        elif self.cavity_type == "C75":
            self.shunt_impedance = 1049
        else:
            self.shunt_impedance = 960

        # Attach a callback that watches for RF to turn off.  Don't watch "RF on" if the cavity is bypassed.
        if not self.bypassed:
            self.rf_on.add_callback(rf_on_cb)

        # List of all PVs related to a cavity.
        self.pv_list = [self.gset, self.gmes, self.drvh, self.pset, self.odvh, self.rf_on, self.deta]
        if self.stat1 is not None:
            self.pv_list.append(self.stat1)

        # Cavity can be effectively bypassed in a number of ways.  Work through that here.
        self.bypassed_eff = bypassed
        if self.gset_init == 0:
            self.bypassed_eff = True
        elif self.odvh.value == 0:
            self.bypassed_eff = True

        # Each cavity keeps track of an externally set maximum value.  Make sure to update this after connecting to PVs.
        self.gset_max = self.gset_min
        self.gset_max_requested = gset_max

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

    def is_cavity_tuning(self, max_deta: float = 7.5):
        """A simple method to determine if an RF cavity's tuners are in operation.

        If the detune angle is too big, then the cavity will be tuning.  If the detune angle is really too big, then
        the cavity will trip.  Only defined for LLRF 3.0."""
        if self.controls_type != "3.0":
            raise RuntimeError(f"{self.name}: is_cavity_tuning only defined for LLRF 3.0, not {self.controls_type}")
        if math.fabs(self.deta.get(use_monitor=False)) > max_deta:
            return True
        else:
            return False

    def is_rf_on(self):
        """A simple method to determine if RF is on in a cavity"""
        return self.rf_on.value == 1

    def is_gradient_ramping(self):
        """Determine if the cavity gradient is ramping to the target."""
        if not self.is_rf_on():
            raise RuntimeError(f"RF is off at {self.name}")

        if self.stat1 is None:
            # For 6 GeV controls (LLRF 1.0), I don't know a simple check.  Gradient should be close to GSET if not,
            # we will call it ramping.  I don't think these old cavities have a built-in ramping feature.
            is_ramping = math.fabs(self.gmes.value - self.gset.value) > 0.15
        else:
            if self.controls_type == '2.0':
                # For C100, the "is ramping" field is the 11th bit counting from zero.  If it's zero, then we're not
                # ramping.  Otherwise, we're ramping.
                is_ramping = int(self.stat1.value) & 0x0800 > 0
            elif self.controls_type == '3.0':
                # For C75, the "is ramping" field is the 15th bit counting from zero.  If it's zero, then we're not
                # ramping.  Otherwise, we're ramping.  (Per K. Hesse)
                is_ramping = int(self.stat1.value) & 0x8000 > 0
            else:
                raise RuntimeError(f"Unsupported controls type '{self.controls_type}'")

        return is_ramping

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

        # gradient is is units of MV/m. formula expects V/m, so 1e12
        return (g * g * self.length * 1e12) / (self.shunt_impedance * self.Q0)

    def walk_gradient(self, gset: float, step_size: float = 1, **kwargs) -> None:
        """Move the gradient of the cavity to gset in steps.

        This always waits for the gradient to ramp, but allows for user specified settle_time and ramping timeouts.

        Args:
            gset:  The target gradient set point
            step_size:  The maximum size steps to make in MV/m by absolute value.

        Additional kwargs are passed to set_gradient.
        """

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

        logger.info(f"Walking {self.name} from {actual_gset} to {gset} in {step_size} MV/m steps.")

        # Walk step size until we're within a single step
        while abs(gset - actual_gset) > step_size:
            next_gset = actual_gset + (step_dir * step_size)
            self.set_gradient(gset=next_gset, **kwargs)
            actual_gset = self.gset.get(use_monitor=False)

        # We should be within a single step here.
        self.set_gradient(gset=gset, **kwargs)

    def set_gradient(self, gset: float, settle_time: float = 6.0, wait_for_ramp=True, ramp_timeout=10,
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

        # LLRF 3.0 (C75) had some troubles with long tuning times for modest gradient changes.  If we get ahead of this,
        # then the cavity trips.
        if self.controls_type == "3.0":
            # In seconds
            notification_interval = 10
            # In seconds
            wait_sleep = 0.05
            wait_counter = notification_interval
            while self.is_cavity_tuning():
                if wait_counter >= notification_interval:
                    wait_counter = 0
                    logger.info(f"{self.name}: Waiting on cavity to tune.  {self.deta.pvname} = {self.deta.value}.")
                StateMonitor.monitor(wait_sleep)
                wait_counter += wait_sleep
            logger.info(f"{self.name}: Cavity is acceptably tuned. {self.deta.pvname} = {self.deta.value}")

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

        logger.info(f"{self.name} Waiting {settle_time} seconds for cryo to adjust")
        StateMonitor.monitor(duration=settle_time)

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
