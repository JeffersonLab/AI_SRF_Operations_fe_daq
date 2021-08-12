import time
from unittest import TestCase
import logging

from cavity import Cavity

# logging.basicConfig(level=logging.DEBUG)
from linac import Zone, Linac

logger = logging.getLogger()


def get_cavity():
    linac = Linac("TestLinac")
    zone = Zone(name="1L22", linac=linac)
    cav = Cavity(name="1L22-1", epics_name="adamc:R1M1", cavity_type="C100", length=0.7, bypassed=False, zone=zone)
    return cav


class TestCavity(TestCase):
    def test_get_jiggled_pset_value(self):
        cav = get_cavity()

        init = cav.pset_init

        # Check that we get back original when range is +/- 0
        result = cav.get_jiggled_pset_value(delta=0)
        self.assertEqual(init, result)

        # Check that we don't get any values back outside of range +/- delta
        max_val = 0
        delta = 100
        for i in range(100):
            tmp = cav.get_jiggled_pset_value(delta=delta) - init
            if abs(tmp) > max_val:
                max_val = tmp
        self.assertTrue(delta > max_val, f"Jiggled too much.  Observed max jiggle {max_val} (>{delta})")

    def test_set_gradient(self):
        cav = get_cavity()
        with self.assertRaises(Exception) as context:
            cav.set_gradient(0.1, settle_time=0)

        with self.assertRaises(Exception) as context:
            cav.set_gradient(100, settle_time=0)

        # Should be fine since we can turn a cavity off
        cav.set_gradient(0, settle_time=0)

        # Should be fine since this is the min stable gradient
        cav.set_gradient(5, settle_time=0)

        # Test that we can't turn on a bypassed cavity, but that we can set them to zero.
        cav.bypassed = True
        with self.assertRaises(Exception) as context:
            cav.set_gradient(6)
        cav.set_gradient(0)

    def test_restore_pset(self):
        cav = get_cavity()
        exp = cav.pset_init

        cav.pset.value = exp + 1
        cav.restore_pset()

        time.sleep(0.01)
        self.assertEqual(exp, cav.pset.value)
