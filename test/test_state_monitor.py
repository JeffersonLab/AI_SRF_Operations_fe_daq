import logging
from unittest import TestCase
import concurrent.futures
import time

import epics
import numpy as np

from cavity import Cavity
from detector import NDXElectrometer
from linac import Zone, Linac
from state_monitor import StateMonitor

logger = logging.getLogger()
logger.setLevel(logging.ERROR)

def reinit_all():
    # "Restart EPICS
    epics.ca.destroy_context()
    time.sleep(0.1)
    epics.ca.create_context()

    # Clear out the state of previous PVs
    StateMonitor.clear_state()


def flapping_pv(n=3, max_sleep=0.001):
    for i in range(n):
        time.sleep(np.random.uniform(0, max_sleep))
        StateMonitor.pv_disconnected()
        StateMonitor.pv_reconnected()


def flapping_rf(n=3, max_sleep=0.001):
    for i in range(n):
        time.sleep(np.random.uniform(0, max_sleep))
        StateMonitor.rf_turned_off()
        StateMonitor.rf_turned_on()


class TestStateMonitor(TestCase):
    def test_daq_good(self):
        StateMonitor.clear_state()
        self.assertTrue(StateMonitor.daq_good())
        ns = [10] * 1000
        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.map(flapping_pv, ns)
            executor.map(flapping_rf, ns)

        self.assertTrue(StateMonitor.daq_good())

    def test_daq_good_with_cavities(self):
        logger.setLevel(logging.INFO)
        logger.warning("Starting SM test")

        # Clear out previous state
        reinit_all()

        # We have no PVs, so this should be good
        self.assertTrue(StateMonitor.daq_good(), StateMonitor.output_state())

        # Create a cavity with supporting structure
        linac = Linac(name="NorthLinac")
        z_1L22 = Zone(name='1L22', linac=linac, controls_type='2.0')
        logger.warning("Creating R1M1 cavity")
        cav = Cavity(name='1L22-1', epics_name='adamc:R1M1', cavity_type='C100', length=0.7, bypassed=False,
                     zone=z_1L22)
        logger.warning("Initializing R1M1 cavity")
        cav.wait_for_connections()

        # The test IOC start with RF off.  Verify daq_good == False, Turn rf on, Verify daq_good == Good
        self.assertTrue(StateMonitor.daq_good(), StateMonitor.output_state())
        cav.rf_on.put(1, wait=True)
        self.assertTrue(StateMonitor.daq_good(), StateMonitor.output_state())

        # Turn RF Off, verify that daq_good == False, then put it back
        cav.rf_on.put(0, wait=True)
        time.sleep(0.01)  # Ensure the callback had a chance to run
        self.assertFalse(StateMonitor.daq_good(), StateMonitor.output_state())
        cav.rf_on.put(1, wait=True)
        time.sleep(0.01)  # Ensure the callback had a chance to run
        self.assertTrue(StateMonitor.daq_good(), StateMonitor.output_state())

    def test_monitor_good(self):
        # Clear out previous state
        reinit_all()

        # Create a cavity with supporting structure
        linac = Linac(name="NorthLinac")
        z_1L22 = Zone(name='1L22', linac=linac, controls_type='2.0')
        logger.warning("Creating R1M1 cavity")
        cav = Cavity(name='1L22-1', epics_name='adamc:R1M1', cavity_type='C100', length=0.7, bypassed=False,
                     zone=z_1L22)
        logger.warning("Initializing R1M1 cavity")
        cav.wait_for_connections()

        cav.rf_on.put(1, wait=True)
        time.sleep(0.01)

        start, end = StateMonitor.monitor(duration=0.5, user_input=False)
        if (end - start).total_seconds() > 0.6:
            self.fail("StateMonitor waited more than 0.6 while monitoring for 0.5 s")

    def test_monitor_bad(self):
        # Clear out previous state
        reinit_all()

        # Create a cavity with supporting structure
        linac = Linac(name="NorthLinac")
        z_1L22 = Zone(name='1L22', linac=linac, controls_type='2.0')
        logger.warning("Creating R1M1 cavity")
        cav = Cavity(name='1L22-1', epics_name='adamc:R1M1', cavity_type='C100', length=0.7, bypassed=False,
                     zone=z_1L22)
        logger.warning("Initializing R1M1 cavity")
        cav.wait_for_connections()

        cav.rf_on.put(0, wait=True)
        time.sleep(0.01)
        with self.assertRaises(Exception) as context:
            StateMonitor.monitor(duration=0, user_input=False)

        # Check that the StateMonitor sees bad HV
        ndxe = NDXElectrometer(name="NDX1L05", epics_name="adamc:NDX1L05")
        ndxe.hv_read_back.put(0, wait=True)
        time.sleep(0.01)
        with self.assertRaises(Exception) as context:
            StateMonitor.monitor(duration=0, user_input=False)
        ndxe.hv_read_back.put(975, wait=True)
