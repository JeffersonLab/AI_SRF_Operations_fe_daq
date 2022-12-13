from unittest import TestCase
import logging
import numpy as np

from fe_daq import app_config as config
from fe_daq.detector import NDXElectrometer, NDXDetector


# logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger()


def setUpModule():
    config.parse_config_file(config.app_root + "/test/dummy_fe_daq.json")


ndxe_toggled_off = {}


def turned_off_cb(pvname, value, **kwargs):
    if value == 0:
        logging.warning(f"{pvname} turned off")
        ndxe_toggled_off[pvname] = True


class TestNDXElectrometer(TestCase):

    def test_toggle_data_acquisition(self):
        e = NDXElectrometer(name="NDX1L24", epics_name="adamc:NDX1L24")

        if not e.daq_enabled.wait_for_connection(timeout=2):
            self.fail(msg=f"Could not connect to {e.daq_enabled.pvname}")

        # Check that we end in the on state no matter what
        e.daq_enabled.put(0, wait=True)
        e.toggle_data_acquisition()
        self.assertEqual(1, e.daq_enabled.get(use_monitor=False))

        e.daq_enabled.value = 1
        e.toggle_data_acquisition()
        self.assertEqual(1, e.daq_enabled.get(use_monitor=False))

        # Check that the DAQ is actually disabled
        e.daq_enabled.add_callback(turned_off_cb)
        e.toggle_data_acquisition()
        self.assertTrue(ndxe_toggled_off[e.daq_enabled.pvname])

    def test_set_for_fe_onset(self):
        e = NDXElectrometer(name="NDX1L24", epics_name="adamc:NDX1L24")

        e.capacitor_switch.put(1000, wait=True)
        e.integration_period.put(2, wait=True)

        e.set_for_fe_onset()
        self.assertEqual("10pF", e.capacitor_switch.get(use_monitor=False, as_string=True))
        self.assertEqual(1, e.integration_period.get(use_monitor=False))
        self.assertEqual(1, e.daq_enabled.get(use_monitor=False))

    def test_set_for_operations(self):
        e = NDXElectrometer(name="NDX1L24", epics_name="adamc:NDX1L24")

        e.capacitor_switch.put(10, wait=True)
        e.integration_period.put(2, wait=True)

        e.set_for_operations()
        self.assertEqual("1000pF", e.capacitor_switch.get(use_monitor=False, as_string=True))
        self.assertEqual(1, e.integration_period.get(use_monitor=False))
        self.assertEqual(1, e.daq_enabled.get(use_monitor=False))


class TestNDXDetector(TestCase):

    def test_take_measurement(self):
        ndxd = NDXDetector(name="INX1L23", epics_name="adamc:INX1L23", electrometer=None)
        # ndxd.clear_measurements()

        # Check that we start at zero
        self.assertEqual(0, np.sum(ndxd.gamma_measurements))
        self.assertEqual(0, np.sum(ndxd.neutron_measurements))

        ndxd.gamma_current.put(0.5, wait=True)
        ndxd.neutron_current.put(0.5, wait=True)

        # Normally we'd wait between samples, but here we just take samples.
        ndxd.take_measurement()
        ndxd.take_measurement()
        ndxd.gamma_current.put(1, wait=True)
        ndxd.neutron_current.put(1, wait=True)
        ndxd.take_measurement()

        # 2 x 0.5 + 1 = 2
        self.assertEqual(2, np.sum(ndxd.gamma_measurements), f"{ndxd.gamma_measurements}")
        self.assertEqual(2, np.sum(ndxd.neutron_measurements), f"{ndxd.neutron_measurements}")

    def test_update_background(self):
        ndxd = NDXDetector(name="INX1L23", epics_name="adamc:INX1L23", electrometer=None)

        # Samples should be 0.5, 0.5, 1.0
        ndxd.gamma_current.put(0.5, wait=True)
        ndxd.neutron_current.put(0.5, wait=True)
        ndxd.take_measurement()
        ndxd.take_measurement()
        ndxd.gamma_current.put(1, wait=True)
        ndxd.neutron_current.put(1, wait=True)
        ndxd.take_measurement()

        ndxd.update_background()

        self.assertListEqual(ndxd.gamma_background, ndxd.gamma_measurements)
        self.assertListEqual(ndxd.neutron_background, ndxd.neutron_measurements)

    def test_is_radiation_above_background(self):
        ndxd = NDXDetector(name="INX1L23", epics_name="adamc:INX1L23", electrometer=None)

        # Samples should be 0.5, 0.5, 1.0
        ndxd.gamma_current.put(0.00001, wait=True)
        ndxd.neutron_current.put(0.00001, wait=True)
        ndxd.take_measurement()
        ndxd.take_measurement()
        ndxd.gamma_current.put(0.00002, wait=True)
        ndxd.neutron_current.put(0.00002, wait=True)
        ndxd.take_measurement()

        ndxd.update_background()

        # Background and measurements should be equal
        is_rad, t_stat = ndxd.is_radiation_above_background()
        self.assertFalse(is_rad, "Error: Background was lower than itself!")
        self.assertEqual(0, t_stat, "Error: comparing background to itself.  Should always be t_stat == zero")

        # Now step up the gamma radiation and measure it.
        ndxd.clear_measurements()
        ndxd.gamma_current.put(0.0011, wait=True)
        ndxd.neutron_current.put(0.00001, wait=True)
        ndxd.take_measurement()
        ndxd.gamma_current.put(0.0012, wait=True)
        ndxd.take_measurement()
        ndxd.gamma_current.put(0.0009, wait=True)
        ndxd.neutron_current.put(0.00002, wait=True)
        ndxd.take_measurement()

        nb = ascii(ndxd.neutron_background)
        nm = ascii(ndxd.neutron_measurements)
        gb = ascii(ndxd.gamma_background)
        gm = ascii(ndxd.gamma_measurements)

        # gamma ttest_ind here should be 11.93515, and t_threshold by default is 5
        is_rad, t_stat = ndxd.is_radiation_above_background()
        self.assertTrue(is_rad, f"Error: expected radiation to be above background.\n"
                                f"nb = {nb}, nm = {nm}\n"
                                f"gb = {gb}, gm = {gm}")
        is_rad, t_stat = ndxd.is_radiation_above_background(t_stat_threshold=12)
        self.assertFalse(is_rad, f"Error: threshold doesn't match.\n"
                                 f"nb = {nb}, nm = {nm}\n"
                                 f"gb = {gb}, gm = {gm}")
        self.assertAlmostEqual(11.935155278687867, t_stat, 10)

        # Turn gamma down, but up neutron
        ndxd.clear_measurements()
        ndxd.gamma_current.put(0.00001, wait=True)
        ndxd.neutron_current.put(0.0011, wait=True)
        ndxd.take_measurement()
        ndxd.neutron_current.put(0.0012, wait=True)
        ndxd.take_measurement()
        ndxd.gamma_current.put(0.00002, wait=True)
        ndxd.neutron_current.put(0.0009, wait=True)
        ndxd.take_measurement()

        # neutron ttest_ind here should be 11.93515, and t_threshold by default is 5
        is_rad, t_stat = ndxd.is_radiation_above_background()
        self.assertTrue(is_rad, f"Error: expected radiation to be above background {t_stat} {ndxd.neutron_measurements}"
                                f" {ndxd.neutron_background}")
        self.assertAlmostEqual(11.935155278687867, t_stat, 10)
        is_rad, t_stat = ndxd.is_radiation_above_background(t_stat_threshold=12)
        self.assertFalse(is_rad, "Error: threshold doesn't match")

        # Now make the current negative so it has to be less than background.  We don't want to alert on this.
        # Turn gamma down, but up neutron
        ndxd.clear_measurements()
        ndxd.gamma_current.put(-1, wait=True)
        ndxd.neutron_current.put(-1, wait=True)
        ndxd.take_measurement()
        ndxd.gamma_current.put(-1.1, wait=True)
        ndxd.neutron_current.put(-1.1, wait=True)
        ndxd.take_measurement()
        ndxd.gamma_current.put(-1.02, wait=True)
        ndxd.neutron_current.put(-1.02, wait=True)
        ndxd.take_measurement()

        # Both t-stats should be negative and not alert even with a low threshold
        is_rad, t_stat = ndxd.is_radiation_above_background(t_stat_threshold=0)
        self.assertFalse(is_rad, f"Error: threshold doesn't match. t-stat={t_stat}")
        self.assertAlmostEqual(-34.04242710996176, t_stat, 10, f"{ndxd.gamma_measurements} {ndxd.neutron_measurements}")

