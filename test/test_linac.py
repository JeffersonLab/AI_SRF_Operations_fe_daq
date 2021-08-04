import time
from datetime import datetime
from unittest import TestCase
import logging

from cavity import Cavity
from linac import LinacFactory, Linac, Zone

# logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger()


class TestLinacFactory(TestCase):
    def test_create_linac(self):
        lf = LinacFactory(testing=True)
        linac = lf.create_linac("NorthLinac")

        # Check some of the cavities exist
        self.assertEqual(linac.zones['1L19'].cavities['1L19-1'].name, '1L19-1')
        self.assertEqual(linac.cavities['1L19-1'].name, '1L19-1')
        self.assertTrue("2L10-1" not in linac.cavities.keys())

        # Check some of the zones exist
        self.assertEqual(linac.zones['1L19'].name, '1L19')
        self.assertEqual(linac.zones['1L23'].name, '1L23')
        self.assertTrue("2L10" not in linac.zones.keys())

    def test__setup_cavities(self):
        lf = LinacFactory(testing=True)

        # Check that the segmask filtering works
        linac = Linac("NorthLinac")
        lf._setup_zones(linac)
        lf._setup_cavities(linac)

        self.assertEqual(linac.zones['1L19'].cavities['1L19-1'].name, '1L19-1')
        self.assertEqual(linac.cavities['1L19-1'].name, '1L19-1')
        self.assertTrue("2L10-1" not in linac.cavities.keys())

    def test__setup_zones(self):
        lf = LinacFactory(testing=True)

        # Check that the segmask filtering works
        linac = Linac("NorthLinac")
        lf._setup_zones(linac)

        self.assertEqual(linac.zones['1L19'].name, '1L19')
        self.assertEqual(linac.zones['1L23'].name, '1L23')
        self.assertTrue("2L10" not in linac.zones.keys())

        # Check that the zone_names filtering works
        linac = Linac("NorthLinac")
        lf._setup_zones(linac, zone_names=['1L19'])

        self.assertEqual(linac.zones['1L19'].name, '1L19')
        self.assertTrue("1L23" not in linac.zones.keys())
        self.assertTrue("2L10" not in linac.zones.keys())

    def test__get_ced_elements(self):
        url = "http://ced.acc.jlab.org/inventory?ced=ced&workspace=ops&t=CryoCavity&out=json"

        lf = LinacFactory(testing=True)
        elements = lf._get_ced_elements(url)
        self.assertEqual(418, len(elements))


class TestLinac(TestCase):

    def test_add_cavity(self):
        linac = Linac("TestLinac")
        zone = Zone(name='1L11', linac=linac)
        linac.zones[zone.name] = zone
        cavity = Cavity(name="1L11-1", epics_name="adamc:R1B1", cavity_type='C25', length=0.5, bypassed=True, zone=zone)

        # Test that the cavity is missing
        self.assertFalse(cavity.name in linac.cavities.keys())
        self.assertFalse(cavity.name in linac.zones['1L11'].cavities.keys())

        # Add the cavity, check that it is present
        linac.add_cavity(cavity)
        self.assertTrue(cavity.name in linac.cavities.keys())
        self.assertTrue(cavity.name in linac.zones['1L11'].cavities.keys())

    def test_set_gradients(self):
        lf = LinacFactory(testing=True)

        # Check that the segmask filtering works
        linac = Linac("NorthLinac")
        lf._setup_zones(linac)
        lf._setup_cavities(linac)

        z_1L22 = linac.zones['1L22']
        z_1L23 = linac.zones['1L23']

        # Set everything to min gset.  Check that it worked
        linac.set_gradients(level="low")
        self.assertEqual(5.0, z_1L22.cavities['1L22-1'].gset.get(use_monitor=False))

        # Set everything but 1L23 to high.
        linac.set_gradients(exclude_zones=[z_1L23], level="high")

        # auto_monitor is not reliable when recently doing puts
        result = z_1L22.cavities['1L22-1'].gset.get(use_monitor=False)
        odvh = z_1L22.cavities['1L22-1'].odvh.value

        # Did 1L22-1 get set within 1 of odvh?
        self.assertTrue(odvh - 3 <= result <= odvh, f"ODVH:{odvh}, result:{result}")

        # Did 1L23-1 get set to it's min (5.0) and not a "high" value
        result = z_1L23.cavities['1L23-1'].gset.get(use_monitor=False)
        self.assertEqual(5.0, result)

    def test_get_radiation_measurements(self):
        lf = LinacFactory(testing=True)
        linac = lf.create_linac("NorthLinac")

        linac.set_gradients(level="low")
        time.sleep(0.05)  # Sleep just long enough for my dumb IOC controller script to react to this.
        num_samples = 3
        linac.get_radiation_measurements(num_samples)

        for ndxd in linac.ndx_detectors.values():
            ndxd.update_background()

        for ndxd in linac.ndx_detectors.values():
            print(ndxd.name)
            print(ndxd.gamma_measurements, ndxd.gamma_background)
            print(ndxd.neutron_measurements, ndxd.neutron_background)
            self.assertEqual(num_samples, len(ndxd.gamma_measurements),
                             f"{ndxd.name}: gamma measurement length wrong. {ndxd.gamma_measurements}")
            self.assertEqual(num_samples, len(ndxd.neutron_measurements),
                             f"{ndxd.name}: neutron measurement length wrong. {ndxd.neutron_measurements}")
            is_rad, t_stat = ndxd.is_radiation_above_background()
            print(t_stat)
            self.assertFalse(is_rad,
                             f"{ndxd.name}: Rad too high, g_t: {ndxd.get_gamma_t_stat()},"
                             f" n_t: {ndxd.get_neutron_t_stat()}")

        linac.set_gradients(level="high")
        time.sleep(0.05)  # Sleep just long enough for my dumb IOC controller script to react to this.
        num_samples = 3
        linac.get_radiation_measurements(num_samples)

        for ndxd in linac.ndx_detectors.values():
            print(ndxd.name)
            print(ndxd.gamma_measurements, ndxd.gamma_background)
            print(ndxd.neutron_measurements, ndxd.neutron_background)

            self.assertEqual(num_samples, len(ndxd.gamma_measurements),
                             f"{ndxd.name}: gamma measurement length wrong. {ndxd.gamma_measurements}")
            self.assertEqual(num_samples, len(ndxd.neutron_measurements),
                             f"{ndxd.name}: neutron measurement length wrong. {ndxd.neutron_measurements}")
            is_rad, t_stat = ndxd.is_radiation_above_background()
            print(t_stat)
            self.assertTrue(is_rad,
                            f"{ndxd.name}: Rad too low, g_t: {ndxd.get_gamma_t_stat()}, n_t: {ndxd.get_neutron_t_stat()}")


class TestZone(TestCase):

    def test_add_cavity(self):
        linac = Linac("TestLinac")
        zone = Zone(name='1L11', linac=linac)
        linac.zones[zone.name] = zone
        cavity = Cavity(name="1L11-1", epics_name="adamc:R1B1", cavity_type='C25', length=0.5, bypassed=True, zone=zone)

        # Test that the cavity is missing
        self.assertFalse(cavity.name in linac.cavities.keys())
        self.assertFalse(cavity.name in linac.zones['1L11'].cavities.keys())

        # Add the cavity, check that it is present, but only in zone
        zone.add_cavity(cavity)
        self.assertFalse(cavity.name in linac.cavities.keys())
        self.assertTrue(cavity.name in linac.zones['1L11'].cavities.keys())

    def test_set_gradients(self):
        lf = LinacFactory(testing=True)
        linac = lf.create_linac(name="NorthLinac")
        z_1L22 = linac.zones['1L22']
        cav = z_1L22.cavities['1L22-1']

        # Set everything to min gset.  Check that it worked
        z_1L22.set_gradients(level="low")
        self.assertEqual(5.0, cav.gset.get(use_monitor=False))

        # Set cav to middle level and everything else to high
        cav.gset.put(10)
        z_1L22.set_gradients(exclude_cavs=[cav], level='high')

        # Check that only cavities not excluded were updated
        odvh = z_1L22.cavities['1L22-2'].odvh.value
        result = z_1L22.cavities['1L22-2'].gset.get(use_monitor=False)
        self.assertEqual(10, cav.gset.get(use_monitor=False))
        self.assertTrue(odvh - 2 <= result <= odvh, f"ODVH:{odvh}, result:{result}")
