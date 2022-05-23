import threading
import time
from datetime import datetime
from threading import Thread
from unittest import TestCase
import logging

import epics

from cavity import Cavity

# logging.basicConfig(level=logging.DEBUG)
from linac import Zone, Linac

logger = logging.getLogger()


def get_cavity():
    linac = Linac("TestLinac")
    zone = Zone(name="1L22", linac=linac, controls_type='2.0')
    cav = Cavity(name="1L22-1", epics_name="adamc:R1M1", cavity_type="C100", length=0.7, bypassed=False, zone=zone)
    return cav


def get_gradient_step_size(cavity: Cavity):
    gset = cavity.gset.value
    step = 0.1
    if cavity.odvh.value < gset + 0.1:
        step = -0.1
    return step


def stop_ramping(pvname, delay=0.5):
    pv = epics.PV(pvname)
    time.sleep(delay)
    pv.put(0)


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

    def test_walk_gradient(self):

        values_lock = threading.Lock()
        values = {}
        def track_values_cb(pvname, value, **kwargs):
            with values_lock:
                if pvname not in values.keys():
                    values[pvname] = [value]
                else:
                    values[pvname].append(value)

        cav = get_cavity()

        # Can't walk higher than ODVH, and it shouldn't even try
        pre_walk_gset = cav.gset.value
        with self.assertRaises(Exception) as context:
            cav.walk_gradient(100)
        post_walk_gset = cav.gset.value

        self.assertEqual(pre_walk_gset, post_walk_gset,
                         "walk_gradient tried to change GSET with request higher than ODVH")

        start = cav.gset.value
        exp = [start - 1, start - 2, start - 2.5]
        cav.gset.add_callback(track_values_cb)
        cav.walk_gradient(gset=cav.gset.value-2.5, settle_time=0.01, wait_for_ramp=False)

        with values_lock:
            result = values[cav.gset.pvname].copy()

        self.assertListEqual(exp, result)


    def test_set_gradient(self):
        cav = get_cavity()
        with self.assertRaises(Exception) as context:
            cav.set_gradient(0.1, settle_time=0, wait_for_ramp=False)

        with self.assertRaises(Exception) as context:
            cav.set_gradient(100, settle_time=0, wait_for_ramp=False)

        # Can't step up more than 2
        gset = cav.gset.value
        with self.assertRaises(Exception) as context:
            cav.set_gradient(gset+2, settle_time=0, wait_for_ramp=False)

        # Should be fine since we're stepping less than 1 MV/m in a good direction
        step = get_gradient_step_size(cav)
        cav.set_gradient(gset + step, settle_time=0, wait_for_ramp=False)
        cav.set_gradient(gset, settle_time=0, wait_for_ramp=False)

        # Test that we can't adjust a bypassed cavity.
        cav.bypassed_eff = True
        with self.assertRaises(Exception) as context:
            cav.set_gradient(6, wait_for_ramp=False)
        cav.bypassed_eff = False

    def test_set_gradient_ramping(self):
        cav = get_cavity()

        ramp_time = 0.25
        gset = cav.gset.value
        step = 0.1
        if cav.odvh.value < gset + 0.1:
            step = -0.1

        # Test that we do wait for ramping to be done
        cav.stat1.put(2048)
        t1 = Thread(target=stop_ramping, args=(cav.stat1.pvname, ramp_time))
        start = datetime.now()
        t1.start()
        cav.set_gradient(gset + step, settle_time=0)
        end = datetime.now()
        t1.join()
        waited = abs(ramp_time - (end - start).total_seconds())
        self.assertTrue(waited < 0.1, msg=f"We didn't wait for ramping properly.  Exp={ramp_time}, Result={waited}")

    def test_set_gradient_ramping_interactive(self):
        # This test requires user input since the ramp_time will exceed 10s
        cav = get_cavity()

        ramp_time = 0.5
        gset = cav.gset.value
        step = get_gradient_step_size(cav)

        # Test that we do wait for ramping to be done
        cav.stat1.put(2048)
        t1 = Thread(target=stop_ramping, args=(cav.stat1.pvname, ramp_time))
        t1.start()
        cav.set_gradient(gset + step, settle_time=0, ramp_timeout=0.3)
        t1.join()

    def test_restore_pset(self):
        cav = get_cavity()
        exp = cav.pset_init

        cav.pset.put(exp + 1, wait=True)
        cav.restore_pset()

        time.sleep(0.01)
        self.assertEqual(exp, cav.pset.value)
