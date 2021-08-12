import epics
import logging
import numpy as np

from state_monitor import connection_cb, rf_on_cb, StateMonitor

logger = logging.getLogger(__name__)

class Cavity:
    def __init__(self, name: str, epics_name: str, cavity_type: str, length: float,
                 bypassed: bool, zone: 'Zone', gset_no_fe: float = None, gset_fe_onset: float = None):
        self.name = name
        self.epics_name = epics_name
        self.zone_name = zone.name
        self.cavity_type = cavity_type
        self.length = length
        self.bypassed = bypassed
        self.zone = zone

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
        self.pset = epics.PV(f"{self.epics_name}PSET", connection_callback=connection_cb)
        self.odvh = epics.PV(f"{self.epics_name}ODVH", connection_callback=connection_cb)
        self.pset_init = self.pset.get()
        self.gset_init = self.gset.get()

        # Create "RF On" PV and min stable gradient setting for each type of cavity
        if self.cavity_type in ("C100", "C75", "P1R"):
            # 1 = RF on, 0 = RF off
            self.rf_on = epics.PV(f"{self.epics_name}RFONr", connection_callback=connection_cb)
            self.gset_min = 5
        elif self.cavity_type in ("C50", "C25"):
            # 1 = RF on, 0 = RF off
            self.rf_on = epics.PV(f"{self.epics_name}ACK1.B6", connection_callback=connection_cb)
            self.gset_min = 3

        # Attach a callback that watches for RF to turn off.  Don't watch "RF on" if the cavity is bypassed.
        if not self.bypassed:
            self.rf_on.add_callback(rf_on_cb)

        self.pv_list = [self.gset, self.pset, self.odvh, self.rf_on]

        # Cavity can be effectively bypassed in a number of ways.  Work through that here.
        self.bypassed_eff = bypassed
        if self.gset_init == 0:
            self.bypassed_eff = True
        elif self.odvh.value == 0:
            self.bypassed_eff = True

    def get_jiggled_pset_value(self, delta: float) -> float:
        """Calculate a random.uniform offset from pset_init of maximum +/- 5.  No changes to EPICS"""
        return self.pset_init + np.random.uniform(-delta, delta)

    def get_low_gset(self):
        """Return the appropriate lowest no FE gradient.  Either the lowest stable or the highest known without FE."""
        return self.gset_min if self.gset_no_fe is None else self.gset_no_fe

    def set_gradient(self, gset: float, settle_time: float = 6.0):
        if gset != 0 and self.bypassed_eff:
            msg = f"{self.name}: Can't turn on bypassed cavity"
            logger.error(msg)
            raise ValueError(msg)
        if gset != 0 and gset < self.gset_min:
            msg = f"{self.name}: Can't turn cavity below operational min"
            logger.error(msg)
            raise ValueError(msg)
        if gset > self.odvh.value:
            msg = f"{self.name}: Can't turn cavity above operational max (ODVH={self.odvh.value})"
            logger.error(msg)
            raise ValueError(msg)

        # Instead of trying to watch tuner status, etc., we just set the gradient, then sleep some requested amount of
        # time.  This will need to be approached differently if used in a more general purpose application.
        self.gset.put(gset, wait=True)
        StateMonitor.monitor(duration=settle_time)

    def restore_pset(self):
        self.pset.put(self.pset_init, wait=True)

    def restore_gset(self):
        self.set_gradient(self.gset_init)


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
