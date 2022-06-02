import time
from typing import List
from unittest import TestCase
import logging
import numpy as np
import epics

from app_config import Config
from cavity import Cavity
# from config import Config
from linac import LinacFactory, Linac, Zone

# logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger()


# This is a routine that should not be used with a real linac since it could overwhelm cryo and cause it to trip.
def set_gradients(linac: Linac, exclude_cavs: List[Cavity] = None, exclude_zones: List['Zone'] = None,
                  level: str = "low") -> None:
    """Set the cavity gradients high/low for cavities in the zone, optionally excluding some cavities

    Arguments:
        linac: The linac on which to make changes
        exclude_cavs: A list of cavities that should not be changed.  None if all cavities should be changed
        exclude_zones: A list of zones that should not be changed.  None if all zones should be changed
        level:  'low' for their defined low level, 'high' for close to ODVH

    """

    # We'll use the put_many call since we're dealing with multiple PVs
    pvlist = []
    values = []
    for cav in linac.cavities.values():

        # Check if we are excluding this cavity or zone from change
        if exclude_cavs is not None:
            skip = False
            for ex_cav in exclude_cavs:
                if cav.name == ex_cav.name:
                    skip = True
            if skip:
                logger.debug(f"Skipping cavity {cav.name} explicitly")
                continue
        if exclude_zones is not None:
            skip = False
            for ex_zone in exclude_zones:
                if cav.zone.name == ex_zone.name:
                    logger.debug(f"Skipping cavity {cav.name} in excluded {ex_zone.name}")
                    skip = True
            if skip:
                continue

        if cav.bypassed:
            continue

        if not cav.gset.pvname.startswith("adamc:"):
            raise RuntimeError("Do not under any circumstances try this with real PVs!.")

        pvlist.append(cav.gset.pvname)
        if level == "high":
            # For varying over the linac we want a broader range since this includes cavities with trip models
            val = np.random.uniform(cav.odvh.value - 3, cav.odvh.value)
        elif level == "low":
            val = cav.get_low_gset()
        else:
            msg = "Unsupported level specified"
            logger.error(msg)
            raise ValueError(msg)
        values.append(val)
        logger.debug(f"Cav: {cav.name},  ODVH: {cav.odvh.get()}, GSET: {val}")

    epics.caput_many(pvlist, values, wait=True)


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
        # Add some gset_max limits via config
        Config.config['gset_max'] = {'R1M1': 6, 'R1M2': 500}

        lf = LinacFactory(testing=True)

        # Check that the segmask filtering works
        linac = Linac("NorthLinac", prefix="adamc:")
        lf._setup_zones(linac)
        lf._setup_cavities(linac)

        Config.clear_config()

        self.assertEqual(linac.zones['1L19'].cavities['1L19-1'].name, '1L19-1')
        self.assertEqual(linac.cavities['1L19-1'].name, '1L19-1')
        self.assertTrue("2L10-1" not in linac.cavities.keys())

        # Test that the config is being read, applied, and sanity checked
        # R1M1 GSET.DRVH should be higher than 6, so this value should stick
        # R1M2GSET.DRVH must be less than 25, so 500 should not be applied.
        self.assertEqual(linac.cavities['1L22-1'].gset_max, 6)
        self.assertNotEqual(linac.cavities['1L22-2'].gset_max, 500, "GSET.DRVH is not being used to set gset_max")
        self.assertTrue(linac.cavities['1L22-2'].gset_max <= 25)

    def test__setup_zones(self):
        lf = LinacFactory(testing=True)

        # Check that the segmask filtering works
        linac = Linac("NorthLinac", prefix="adamc:")
        lf._setup_zones(linac)

        self.assertEqual(linac.zones['1L19'].name, '1L19')
        self.assertEqual(linac.zones['1L23'].name, '1L23')
        self.assertTrue("2L10" not in linac.zones.keys())

        # Check that the zone_names filtering works
        linac = Linac("NorthLinac", prefix="adamc:")
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
        linac = Linac("NorthLinac", prefix="adamc:")
        zone = Zone(name='1L11', linac=linac, controls_type='1.0')
        linac.zones[zone.name] = zone
        cavity = Cavity(name="1L11-1", epics_name="adamc:R1B1", cavity_type='C25', length=0.5, bypassed=True, zone=zone,
                        Q0=6e9)

        # Test that the cavity is missing
        self.assertFalse(cavity.name in linac.cavities.keys())
        self.assertFalse(cavity.name in linac.zones['1L11'].cavities.keys())

        # Add the cavity, check that it is present
        linac.add_cavity(cavity)
        self.assertTrue(cavity.name in linac.cavities.keys())
        self.assertTrue(cavity.name in linac.zones['1L11'].cavities.keys())

    def test_get_radiation_measurements(self):
        lf = LinacFactory(testing=True)
        linac = lf.create_linac("NorthLinac")

        set_gradients(linac, level="low")
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

        set_gradients(linac=linac, level="high")
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
        linac = Linac("NorthLinac", prefix="adamc:")
        zone = Zone(name='1L11', linac=linac, controls_type='1.0')
        linac.zones[zone.name] = zone
        cavity = Cavity(name="1L11-1", epics_name="adamc:R1B1", cavity_type='C25', length=0.5, bypassed=True, zone=zone,
                        Q0=6e9)

        # Test that the cavity is missing
        self.assertFalse(cavity.name in linac.cavities.keys())
        self.assertFalse(cavity.name in linac.zones['1L11'].cavities.keys())

        # Add the cavity, check that it is present, but only in zone
        zone.add_cavity(cavity)
        self.assertFalse(cavity.name in linac.cavities.keys())
        self.assertTrue(cavity.name in linac.zones['1L11'].cavities.keys())

    def test_check_percent_heat_change(self):
        linac = Linac("NorthLinac", prefix="adamc:")
        zone = Zone(name='1L11', linac=linac, controls_type='1.0')
        linac.zones[zone.name] = zone
        zone.add_cavity(
            Cavity(name="1L11-1", epics_name="adamc:R1B1", cavity_type='C25', length=0.5, bypassed=True, zone=zone,
                   Q0=6e9))
        zone.add_cavity(
            Cavity(name="1L11-2", epics_name="adamc:R1B2", cavity_type='C25', length=0.5, bypassed=True, zone=zone,
                   Q0=6e9))
        zone.add_cavity(
            Cavity(name="1L11-3", epics_name="adamc:R1B3", cavity_type='C25', length=0.5, bypassed=True, zone=zone,
                   Q0=6e9))
        zone.add_cavity(
            Cavity(name="1L11-4", epics_name="adamc:R1B4", cavity_type='C25', length=0.5, bypassed=True, zone=zone,
                   Q0=6e9))
        zone.add_cavity(
            Cavity(name="1L11-5", epics_name="adamc:R1B5", cavity_type='C25', length=0.5, bypassed=True, zone=zone,
                   Q0=6e9))
        zone.add_cavity(
            Cavity(name="1L11-6", epics_name="adamc:R1B6", cavity_type='C25', length=0.5, bypassed=True, zone=zone,
                   Q0=6e9))
        zone.add_cavity(
            Cavity(name="1L11-7", epics_name="adamc:R1B7", cavity_type='C25', length=0.5, bypassed=True, zone=zone,
                   Q0=6e9))
        zone.add_cavity(
            Cavity(name="1L11-8", epics_name="adamc:R1B8", cavity_type='C25', length=0.5, bypassed=True, zone=zone,
                   Q0=6e9))

        # This should be a 100% heat loss
        zone.check_percent_heat_change(gradients=[0, 0, 0, 0, 0, 0, 0, 0], percentage=101)
        with self.assertRaises(Exception):
            zone.check_percent_heat_change(gradients=[0, 0, 0, 0, 0, 0, 0, 0], percentage=99)

        # This should be a 0% heat change
        zone.check_percent_heat_change(gradients=[None, None, None, None, None, None, None, None], percentage=10)

        # This should be a relatively
        g5 = zone.cavities['1L11-5'].gset.value
        g7 = zone.cavities['1L11-7'].gset.value
        zone.check_percent_heat_change(gradients=[None, None, None, None, g5+1, None, g7+1, None], percentage=10)


    # def test_set_gradients(self):
    #     lf = LinacFactory(testing=True)
    #     linac = lf.create_linac(name="NorthLinac")
    #     z_1L22 = linac.zones['1L22']
    #     cav = z_1L22.cavities['1L22-1']
    #
    #     # Set everything to min gset.  Check that it worked
    #     z_1L22.set_gradients(level="low")
    #     self.assertEqual(5.0, cav.gset.get(use_monitor=False))
    #
    #     # Set cav to middle level and everything else to high
    #     cav.gset.put(10)
    #     z_1L22.set_gradients(exclude_cavs=[cav], level='high')
    #
    #     # Check that only cavities not excluded were updated
    #     odvh = z_1L22.cavities['1L22-2'].odvh.value
    #     result = z_1L22.cavities['1L22-2'].gset.get(use_monitor=False)
    #     self.assertEqual(10, cav.gset.get(use_monitor=False))
    #     self.assertTrue(odvh - 2 <= result <= odvh, f"ODVH:{odvh}, result:{result}")
