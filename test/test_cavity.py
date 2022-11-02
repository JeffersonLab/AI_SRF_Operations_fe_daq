import sys
import threading
import time
from datetime import datetime
from threading import Thread
from unittest import TestCase
import logging

import epics

from src.fe_daq.cavity import Cavity

# logging.basicConfig(level=logging.DEBUG)
from src.fe_daq.linac import Zone, Linac

logger = logging.getLogger()


def get_cavity(controls_type='2.0'):
    if controls_type == '2.0':
        linac = Linac("NorthLinac", prefix="adamc:")
        zone = Zone(name="1L22", linac=linac, controls_type='2.0')
        cav = Cavity(name="1L22-1", epics_name="adamc:R1M1", cavity_type="C100", length=0.7, bypassed=False, zone=zone,
                     Q0=6e9)
        cav.update_gset_max()
    elif controls_type == '3.0':
        linac = Linac("NorthLinac", prefix="adamc:")
        zone = Zone(name="1L10", linac=linac, controls_type='3.0')
        cav = Cavity(name="1L10-1", epics_name="adamc:R1A1", cavity_type="C75", length=0.4916, bypassed=False,
                     zone=zone, Q0=6.3e9)
        cav.update_gset_max()
    else:
        raise RuntimeError("Unsupported controls_type")

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

    def test_calculate_heat(self):
        cav = get_cavity()

        # Test that the answer is expected when we supply the gradient
        exp = 10 * 10 * 1e12 * 0.7 / (1241.3 * 6e9)
        result = cav.calculate_heat(gradient=10)
        self.assertAlmostEqual(exp, result, 2)

        # Test that the answer is as expected when it uses the default value (the current gset).
        g = cav.gset.value
        exp = g * g * 1e12 * 0.7 / (1241.3 * 6e9)
        result = cav.calculate_heat()
        self.assertAlmostEqual(exp, result, 2)

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

        cav.gset.put(cav.gset_min + 3, wait=True)
        time.sleep(0.01)
        start = cav.gset_min + 3
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

        old_max = cav.gset_max
        cav.gset_max = 7
        with self.assertRaises(Exception) as context:
            cav.set_gradient(8, wait_for_ramp=False)
        cav.gset_max = old_max

    def test_is_cavity_tuning(self):
        cav = get_cavity(controls_type='3.0')
        deta_old = cav.deta.value
        try:
            cav.deta.put(40)
            time.sleep(0.1)
            self.assertTrue(cav.is_cavity_tuning(max_deta=15))
            time.sleep(0.1)
            cav.deta.put(0)
            time.sleep(0.1)
            self.assertFalse(cav.is_cavity_tuning(max_deta=15))
        finally:
            cav.deta.put(deta_old)

    def test_set_gradient_tuning(self):
        cav = get_cavity(controls_type='3.0')
        deta_old = cav.deta.value
        gset_old = cav.gset.value

        def do_tune():
            cav.deta.put(0)

        tuning_time = 0.25
        try:
            cav.deta.put(20)
            start = datetime.now()
            threading.Timer(tuning_time, do_tune).start()
            cav.set_gradient(max(cav.gset_min, cav.gset.value-0.01), settle_time=0)
            end = datetime.now()
        finally:
            cav.gset.put(gset_old)
            cav.deta.put(deta_old)

        self.assertTrue((end - start).total_seconds() > tuning_time, f"Cavity did not wait for tuning (<{tuning_time})")

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
