import logging

from unittest import TestCase
import concurrent.futures
import time

import epics
import numpy as np

from fe_daq import app_config as config
from fe_daq.cavity import Cavity
from fe_daq.detector import NDXElectrometer
from fe_daq.state_monitor import StateMonitor, get_threshold_cb
from test.t_utils import get_linac_zone_cavity

logger = logging.getLogger()
prefix = "adamc:"
jt_suffix = ""


def setUpModule():
    config.parse_config_file(config.app_root + "/test/dummy_fe_daq.json")


def reinit_all():
    old_level = logger.level
    logger.setLevel(logging.CRITICAL)
    # "Restart" EPICS CA.
    epics.ca.clear_cache()

    # Clear out the state of previous PVs
    StateMonitor.clear_state()
    logger.setLevel(old_level)

    # Reinitialize the configuration
    config.clear_config()
    config.parse_config_file(config.app_root + "/test/dummy_fe_daq.json")


def flapping_pv(pvname, n=3, max_sleep=0.001):
    for i in range(n):
        time.sleep(np.random.uniform(0, max_sleep))
        StateMonitor.pv_disconnected(pvname=pvname)
        StateMonitor.pv_reconnected(pvname=pvname)


def flapping_rf(pvname, n=3, max_sleep=0.001):
    for i in range(n):
        time.sleep(np.random.uniform(0, max_sleep))
        StateMonitor.rf_turned_off(pvname=pvname)
        StateMonitor.rf_turned_on(pvname=pvname)


class TestStateMonitor(TestCase):
    def test_daq_good(self):
        """Test that any resolved problems do not stick with the state monitor."""
        # Clear out previous state
        reinit_all()

        linac, zone, cav = get_linac_zone_cavity()
        pvs = [cav.rf_on.pvname]
        for i in range(2, 9):
            cav2 = Cavity.get_cavity(name='1L22-2', epics_name='adamc:R1M2', cavity_type='C100', length=0.7,
                                     bypassed=False, zone=zone, Q0=6e9, tuner_bad=False)
            pvs.append(cav2.rf_on.pvname)
        n = 1000
        n_sleeps = [n, ] * len(pvs)

        self.assertTrue(StateMonitor.daq_good())

        # This should run for 0.1 seconds
        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.map(flapping_pv, pvs, n_sleeps)
            executor.map(flapping_rf, pvs, n_sleeps)

        # Sleep long enough for all of the flapping to have stopped.
        time.sleep(n * 0.001 * 1.5)
        self.assertTrue(StateMonitor.daq_good())

    def test_daq_good_with_cavities(self):
        logger.setLevel(logging.INFO)
        logger.warning("Starting SM test")

        # Clear out previous state
        reinit_all()

        # We have no PVs, so this should be good
        self.assertTrue(StateMonitor.daq_good(), StateMonitor.output_state())

        # Create a cavity with supporting structure
        linac, zone, cav = get_linac_zone_cavity()

        # The test IOC start with RF on.
        # 1. Verify daq_good == True
        # 2. Turn rf off, Verify daq_good == False
        # 3. Turn rf on, Verify daq_good == True
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
        linac, zone, cav = get_linac_zone_cavity()

        cav.rf_on.put(1, wait=True)
        time.sleep(0.01)

        start, end = StateMonitor.monitor(duration=0.5, user_input=False)
        if (end - start).total_seconds() > 0.6:
            self.fail("StateMonitor waited more than 0.6 while monitoring for 0.5 s")

    def test_monitor_cavity_fsd(self):
        reinit_all()

        # Create a cavity with supporting structure
        linac, zone, cav = get_linac_zone_cavity()

        cav.fsd.put(256, wait=True)
        time.sleep(0.01)
        with self.assertRaises(Exception) as context:
            StateMonitor.monitor(duration=0, user_input=False)
        cav.fsd.put(768, wait=True)
        time.sleep(0.01)
        StateMonitor.monitor(duration=0, user_input=False)

    def test_monitor_bad(self):
        # Clear out previous state
        reinit_all()

        # Create a cavity with supporting structure
        linac, zone, cav = get_linac_zone_cavity()

        cav.rf_on.put(0, wait=True)
        time.sleep(0.01)
        with self.assertRaises(Exception) as context:
            StateMonitor.monitor(duration=0, user_input=False)
        cav.rf_on.put(1, wait=True)

        # Check that the StateMonitor sees bad HV
        ndxe = NDXElectrometer(name="NDX1L05", epics_name=f"{prefix}NDX1L05", I400=1)
        old_value = ndxe.hv_read_back.get(use_monitor=False)
        ndxe.hv_read_back.put(0, wait=True)
        time.sleep(0.01)
        with self.assertRaises(Exception) as context:
            StateMonitor.monitor(duration=0, user_input=False)
        ndxe.hv_read_back.put(old_value, wait=True)

    def test_jt_valve_monitoring(self):
        # Clear out previous state
        reinit_all()

        # Create a cavity with supporting structure
        linac, zone, cav = get_linac_zone_cavity()
        old_value = zone.jt_stroke.get(use_monitor=False)
        zone.jt_stroke.put(95, wait=True)

        time.sleep(0.05)
        with self.assertRaises(Exception) as context:
            StateMonitor.check_state(user_input=False)
        time.sleep(0.05)
        zone.jt_stroke.put(old_value, wait=True)
        time.sleep(0.05)
        StateMonitor.check_state(user_input=False)

    def test_linac_pressure_monitoring(self):
        # Clear out previous state
        reinit_all()

        # Create a cavity with supporting structure
        linac, zone, cav = get_linac_zone_cavity()
        old_value = linac.linac_pressure.value

        # Set too high
        linac.linac_pressure.put(0.04, wait=True)
        time.sleep(0.01)

        try:
            with self.assertRaises(Exception) as context:
                StateMonitor.check_state(user_input=False)

            # Set OK
            linac.linac_pressure.put(0.0385, wait=True)
            time.sleep(0.01)
            StateMonitor.check_state(user_input=False)

            # Set too low
            linac.linac_pressure.put(0.037, wait=True)
            time.sleep(0.01)

            with self.assertRaises(Exception) as context:
                StateMonitor.check_state(user_input=False)
        finally:
            linac.linac_pressure.put(old_value, wait=True)

        time.sleep(0.01)
        StateMonitor.check_state(user_input=False)

    def test_linac_heat_margin_monitoring(self):
        # Clear out previous state
        reinit_all()

        # Create a cavity with supporting structure
        linac, zone, cav = get_linac_zone_cavity()
        old_value = linac.heater_margin.value
        linac.heater_margin.put(1, wait=True)
        time.sleep(0.05)

        try:
            with self.assertRaises(Exception) as context:
                StateMonitor.check_state(user_input=False)
        finally:
            linac.heater_margin.put(old_value, wait=True)
        time.sleep(0.01)
        StateMonitor.check_state(user_input=False)

    def test_cb_threshold_bitshift_mask(self):
        reinit_all()
        cb = get_threshold_cb(low=0, high=0, bitshift=2, mask=1)

        # Should be no alert
        cb(pvname='test_pv', value=0)

        # Make sure the state is good before we alert on a bad state
        StateMonitor.check_state(user_input=False)

        # Should alert
        # 0d15 = 0b1111, 0b1111 >> 2 = 0b11, 0b11 AND 0b01 = 0b01, 0b01 == 0d1 => ALERT
        cb(pvname='test_pv', value=15)
        with self.assertRaises(Exception) as context:
            StateMonitor.check_state(user_input=False)

        # Should be no alert
        cb(pvname='test_pv', value=0)
        StateMonitor.check_state(user_input=False)
