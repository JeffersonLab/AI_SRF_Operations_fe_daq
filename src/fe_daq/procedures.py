import concurrent.futures
import logging
import math
import random

import numpy as np
from datetime import datetime, timedelta
from operator import attrgetter
from typing import Union, List, Optional, Dict, Tuple

from fe_daq.cavity import Cavity, collect_data_at_gradients
from fe_daq.linac import Zone, Linac
from fe_daq.state_monitor import StateMonitor
from fe_daq.exceptions import UserScanAbort

from fe_daq import utils

logger = logging.getLogger(__name__)


def run_find_fe_process(zone: Zone, linac: Linac, no_fe_file: str, fe_onset_file: str) -> None:
    """High level function of measuring field emission onset within a single zone.

    This includes linac setup, course 'no FE' search, and fine 'FE onset' search."""

    # Do prep work for measuring field emission onsets
    setup_for_find_fe_process(linac=linac)

    # Record the a "high" gradient for each cavity in the zone that does not produce field emission, and set gradient
    # to that value.
    find_no_fe_gsets(zone=zone, linac=linac, data_file=no_fe_file)

    # Now find closer values of FE onset.  find_no_fe_gsets should have started us close.
    find_fe_onset(zone=zone, linac=linac, data_file=fe_onset_file)


def setup_for_find_fe_process(linac: Linac) -> None:
    """Setup for finding FE onset.  Turn NDX to correct settings and measure background radiation."""
    linac.set_ndx_for_fe_onset()
    response = input(
        "About to measure NDX radiation and save as baseline background.\nContinue? (n|y): ").lstrip().lower()
    if not response.startswith("y"):
        raise RuntimeError("User stopped FE onset detection at measure background step.")

    # Measure the radiation and save it as the background
    ndxd_names = [ndxd.name for ndxd in sorted(linac.ndx_detectors.values(), key=attrgetter('name'))]
    logger.info(f"Starting initial background radiation measurements using {','.join(ndxd_names)}")
    linac.get_radiation_measurements(num_samples=10)
    linac.save_radiation_measurements_as_background()


def find_no_fe_gsets(zone: Zone, linac: Linac, data_file: str, step_size: float = 1.0) -> None:
    """This method finds the highest gradient per-cavity without field emission (at 1 MV/m granularity).

    Assumes that background radiation levels have been saved and that the linac is set to a low level without radiation.
    This "low" level can be 5 MV/m, but it can be higher if we are continuing an interrupted FE onset search.

    This is similar to, but different from, finding field emission onset.  Here we set a rough baseline of no FE from
    which we can do a finer search for FE onset.  This is because adjacent cavities can accelerate FE electrons and
    amplify the radiation signal.  Since NDX are not highly sensitive to FE onset, we use this acceleration to alleviate
    the detector insensitivity at the cost of some unknown interaction effects.

    Return:
        None.  Each cavity in the zone has it's gset_no_fe attribute adjusted.
    """
    logger.info(f"Stepping cavities up to a no FE gradient in {zone.name}.")

    # Step up cavities until they show radiation on NDX, then back them down 1 MV/m
    # Keys are cavity name, values are booleans
    reached_max = {}
    for cavity in zone.cavities.values():
        if cavity.bypassed_eff:
            reached_max[cavity.name] = True
            logger.info(f"{cavity.name} is bypassed.  Unable to find no FE gset.")
        else:
            reached_max[cavity.name] = False

    try:
        # Run until we've maxed out all of the cavities at some no FE gradient.
        while not all(reached_max.values()):
            # Step one cavity up at a time.
            for cavity in sorted(zone.cavities.values(), key=attrgetter('name')):

                # Skip cavities that have already hit the max - includes bypassed cavities
                if not reached_max[cavity.name]:
                    # Check that control system is in good state
                    StateMonitor.check_state()

                    # What are the current and next values
                    val = cavity.gset.value
                    next_val = val + step_size

                    # We can't push the cavities beyond their ODVH
                    if next_val >= cavity.odvh.value:
                        next_val = cavity.odvh.value
                        reached_max[cavity.name] = True

                    logger.info(f"Stepping up {cavity.name} {val} -> {next_val}")
                    cavity.set_gradient(next_val)

                    # Check that control system is in good state.  Changing gradient can take several seconds
                    StateMonitor.check_state()

                    # Measure radiation.  Turn cavity back down if we see anything above background.
                    logger.info("Measuring radiation")
                    linac.get_radiation_measurements(3)
                    # TODO: Update t-threshold.  Was 10
                    is_rad, t_stat, max_d = linac.is_radiation_above_background(t_stat_threshold=10)
                    if is_rad:
                        # We've had some trouble where FE shows up later, despite turning this back down one step.
                        # Now we turn it down to 2.5 below the level where we saw FE.
                        no_fe = max(val - 1.5, 5)
                        logger.info(f"Found coarse 'no FE' gset for {cavity.name} at {val} MV/m")
                        logger.info(f"Max radiation t-stat is {t_stat} at {max_d.name}")
                        logger.info(f"Saving cautionary 'no FE' gset of {no_fe} MV/m")
                        logger.info(f"Turning {cavity.name} down to cautionary {no_fe} MV/m")

                        reached_max[cavity.name] = True
                        cavity.gset_no_fe = no_fe
                        cavity.walk_gradient(no_fe)

                    elif reached_max[cavity.name]:
                        # Implies next_val == cavity.odvh.value, but that float comparison could be misleading.
                        logger.info(f"Found no FE at {cavity.name} at ODVH of {val} MV/m")
                        cavity.gset_no_fe = next_val

                        # It's possible that the cavity is really just over the onset point and making small amounts
                        # FE electrons.  Step it down more to be safe.
                        # TODO: Fix this.  Should be cavity.gset_no_fe???
                        safe_val = max(cavity.gset_min - 2, 5)
                        cavity.walk_gradient(safe_val)

    finally:
        # Note that some of these may not be what we
        logger.info(f"Saving the coarse 'no FE' levels to {data_file}")
        # Write out the results so that they can be used later.
        with open(data_file, mode="a") as f:
            f.write(f"# {zone.name} step_size={step_size} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            for cavity in zone.cavities.values():
                f.write(f"{cavity.gset.pvname}\t{cavity.gset_no_fe}\n")


def find_fe_onset(zone: Zone, linac: Linac, data_file: str, step_size: float = 0.125, n_tries: int = 3) -> None:
    """Find the cavity gradients within a zone where field emission begins.

    This is a finer grained search for the fe onset that probes a single cavity at a time.
    """

    logger.info(f"Finding FE onsets for cavities in {zone.name}")
    with open(data_file, mode="a") as f:
        f.write(f"# {zone.name} step_size={step_size} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # Save the starting no_fe values, then walk up 0.2 MV/m until we find FE onset.  Then back to starting value.
    for cavity in zone.cavities.values():
        if cavity.bypassed_eff:
            logger.info(f"{cavity.name} is bypassed.  Unable to find field emission onset.  Skipping.")
            continue

        # Check that we have a starting point
        if cavity.gset_no_fe is None:
            logger.warning(f"{cavity.name} gset_no_fe is None.  Should be a value or skipped as bypassed.")
            continue

        # Tracking if we have found FE onset, and the number of attempts we've made at finding it.
        logger.info(f"Finding FE onset for {cavity.name}.")
        found_onset = False
        count = 1

        # Where to start the initial search.
        start = cavity.gset_no_fe
        if start >= cavity.odvh.value:
            # If we ran the coarse search to the max without seeing radiation
            logger.info(f"{cavity.name} already checked up to ODVH by coarse scan.  Skipping fine-grained search.")
            cavity.gset_fe_onset = 100
        else:
            if abs(start - cavity.gset.value) > 0.01:
                logger.warning(
                    f"Starting fine-grained FE onset check of {cavity.name}.  Start GSET ({cavity.gset.value}"
                    f" != 'No FE' GSET ({start}).")
                cavity.walk_gradient(start)

            # Now try at most three times to detect the FE onset for this cavity
            while not found_onset and count <= n_tries:
                logger.info(f"Starting FE onset search for {cavity.name}.  Attempt {count} of {n_tries}")
                try:
                    # TODO: Add check here for if we walked up to up ODVH.  Otherwise we cycle three times.
                    found_onset = walk_cavity_gradient_up(cavity=cavity, linac=linac, start=start, step_size=step_size,
                                                          rad_t_threshold=10)
                except Exception as exc:
                    logger.error(f"Exception while walking {cavity.name} up.\n{exc}")
                    response = input(f"Something went wrong when walking {cavity.name}.  Try again? ").lstrip().lower()
                    if not response.startswith('y'):
                        break
                finally:
                    count += 1

                # We we able to find anything after several repeated attempts.  If not, then background radiation has
                # probably changed.  Ask user if we should update it.  If so, reset the count and do this cavity again.
                if not found_onset and count > n_tries:
                    logger.warning(f"{cavity.name} could not find FE onset gradient.")
                    response = input("Should we re-baseline background radiation? (n|y): ").lstrip().lower()
                    if response.startswith("y"):
                        logger.warning("Updating background radiation readings with current levels")
                        linac.get_radiation_measurements(10)
                        linac.save_radiation_measurements_as_background()

                    response = input(f"Should we retry {cavity.name} (n|y): ").lstrip().lower()
                    if response.startswith("y"):
                        logger.info(f"Restarting fine-grained FE onset check of {cavity.name}.")
                        count = 1

            logger.info(f"Walking {cavity.name} down to 'no FE' (from {cavity.gset.value} to {cavity.gset_no_fe}")
            cavity.walk_gradient(cavity.gset_no_fe)

        with open(data_file, mode="a") as f:
            logger.info(f"Saving FE Onset value to {data_file} for {cavity.name}.")
            f.write(f"{cavity.gset.pvname}\t{cavity.gset_fe_onset}\n")


def walk_cavity_gradient_up(cavity: Cavity, linac: Linac, start: float, step_size: float,
                            n_rad_samples: float = 10, rad_t_threshold: float = 10) -> bool:
    """This walks an individual cavity's gradient up until radiation is seen on an NDX detector.

    This assumes that the machine is setup in such a way that current radiation levels at 'start' will be similar to
    the NDX recorded background.  After new radiation is detected, this function turns the cavity back down to verify
    that the radiation goes away.  The FE onset value is saved to the cavity.

    Args:
        cavity: The cavity to test
        linac: The rest of a linac that is under test
        start: A gset value that is known to produce no radiation.  The first new measurement is made one step above.
        step_size: The amount by which gradient should be increased at each step
        n_rad_samples: The number of radiation samples to take when checking for radiation
        rad_t_threshold: Min t-stat for asserting radiation

    Returns:
        True/False - was FE onset found?
    """

    # Test that we are not starting at the highest gradient value.
    if start >= cavity.odvh.value:
        logger.info(f"start value is cavity ODVH value.  Saving fe_onset as 100 since we won't be able to find it.")
        cavity.gset_fe_onset = 100
        return True

    # Prime the following while loop
    found_onset = False
    cavity.walk_gradient(start)
    val = start

    # Walk the cavity gradient up in small steps until we see a change in radiation
    while val < cavity.odvh.value:
        next_val = val + step_size

        # We can't run the cavities higher than ODVH
        if next_val > cavity.odvh.value:
            next_val = cavity.odvh.value

        logger.info(f"Stepping {cavity.name} from {val} to {next_val}.")
        cavity.set_gradient(next_val)

        # Measure radiation.  Turn cavity back down if we see anything above background.
        logger.info(f"Taking {n_rad_samples} radiation measurements")
        linac.get_radiation_measurements(n_rad_samples)
        is_rad, t_stat, max_d = linac.is_radiation_above_background(t_stat_threshold=rad_t_threshold)
        if is_rad:
            logger.info(f"Found FE onset for {cavity.name} at {val} MV/m (t-stat = {t_stat} at {max_d.name}).")
            logger.info(f"Turning cavity down to verify radiation elimination.")
            cavity.set_gradient(val)
            linac.get_radiation_measurements(3)
            is_rad, t_stat, max_d = linac.is_radiation_above_background(t_stat_threshold=rad_t_threshold)
            if is_rad:
                logger.info(
                    f"Found radiation when cavity turned down (t-stat = {t_stat} at {max_d.name}).  Search Failed.")
            else:
                found_onset = True
                cavity.gset_fe_onset = val
                logger.info(f"Found no radiation when cavity turned down.  Search succeeded.")
            break

        # Read in the current GSET for the next loop iteration
        val = cavity.gset.get(use_monitor=False)

    return found_onset


def setup_zone_baseline_gradient(zone, linac, baseline_gradient, background_n_samples):
    # Establish the low baseline
    logger.info(f"Walking cavities to baseline ({baseline_gradient} MV/m)")
    for cav in zone.cavities:
        if baseline_gradient < cav.gset_min:
            raise ValueError(
                f"{cav.name} - Can't set baseline ({baseline_gradient}) below minimum stable gradient "
                f"({cav.gset_min})")
        if abs(cav.gset.value - baseline_gradient) > 0.01:
            logger.info(f"Walking {cav.name} from {cav.gest.value} -> {baseline_gradient}")
            cav.walk_gradient(baseline_gradient)

    logger.info(f"Measuring {background_n_samples} radiation samples to use as background")
    linac.get_radiation_measurements(10)
    linac.save_radiation_measurements_as_background()


def walk_cavity_up_from_baseline(cavity, linac, coarse_step_size, quick_n_samples, slow_n_samples, quick_t_threshold,
                                 slow_t_threshold):
    found_rad = False

    # Walk a cavity up until we see radiation, or until we hit it's ODVH.
    while cavity.gset.value < cavity.odvh.value:
        next_gset = cavity.gset.value + coarse_step_size
        if next_gset > cavity.odvh.value:
            logger.info(f"Taking {cavity.name} to ODVH of {cavity.odvh.value} MV/m")
            next_gset = cavity.odvh.value

        logger.info(f"Walking {cavity.name} from {cavity.gset.value} -> {next_gset}")
        cavity.walk_gradient(next_gset)

        # Take a quick sample
        logger.info(f"Taking {quick_n_samples} radiation samples.")
        linac.get_radiation_measurements(quick_n_samples)
        is_rad, t_stat, max_d = linac.is_radiation_above_background(quick_t_threshold)
        if is_rad:
            logger.info(f"Found radiation at {max_d.name} ({t_stat} > {quick_t_threshold} threshold)")
            logger.info(f"Taking {slow_n_samples} samples to verify.")
            linac.get_radiation_measurements(slow_n_samples)
            is_rad, t_stat, max_d = linac.is_radiation_above_background(slow_t_threshold)
            if is_rad:
                found_rad = True
                # Save the no_fe value as the previous step
                cavity.gset_no_fe = cavity.gset.value - coarse_step_size
                logger.info(f"Found radiation at {max_d.name} ({t_stat} > {quick_t_threshold} threshold)")
                break

    # We walked this up to it's max and didn't see any radiation with quick checks.  Make sure with a long one.
    if not found_rad:
        logger.info(f"Walked {cavity.name} to ODVH without seeing radiation.")
        logger.info(f"Taking {slow_n_samples} samples to verify.")
        linac.get_radiation_measurements(slow_n_samples)
        is_rad, t_stat, max_d = linac.is_radiation_above_background(slow_t_threshold)
        if is_rad:
            found_rad = True
            cavity.gset_no_fe = cavity.gset.value - coarse_step_size
            logger.info(f"Found radiation at {max_d.name} ({t_stat} > {quick_t_threshold} threshold)")
        else:
            logger.info(
                f"Largest non-significant radiation at {max_d.name} ({t_stat} < {quick_t_threshold} threshold)")

    return found_rad


def walk_cavity_down_from_radiation(cavity, linac, fine_step_size, quick_n_samples, slow_n_samples, quick_t_threshold,
                                    slow_t_threshold):
    rad_gone = False
    while cavity.gset.value > cavity.gset_min:

        next_gset = cavity.gset.value - fine_step_size
        if next_gset < cavity.gset_min:
            logger.info(f"Setting {cavity.name} to it's min stable gradient ({cavity.gset_min} MV/m)")
            next_gset = cavity.gset_min

        # Walk the cavity
        logger.info(f"Walking {cavity.name} from {cavity.gset.value} -> {next_gset}.")
        cavity.walk_gradient(next_gset)

        # Take a quick sample
        logger.info(f"Taking {quick_n_samples} radiation samples.")
        linac.get_radiation_measurements(quick_n_samples)
        is_rad, t_stat, max_d = linac.is_radiation_above_background(quick_t_threshold)
        if not is_rad:
            logger.info(f"Found no significant radiation.  Worst at {max_d.name} ({t_stat} > "
                        f"{quick_t_threshold} threshold)")
            logger.info(f"Taking {slow_n_samples} samples to verify.")
            linac.get_radiation_measurements(slow_n_samples)
            is_rad, t_stat, max_d = linac.is_radiation_above_background(slow_t_threshold)
            if not is_rad:
                rad_gone = True
                # Save the no_fe value as the previous step
                cavity.gset_fe_onset = cavity.gset.value
                logger.info(f"Found no significant radiation.  Worst at {max_d.name} ({t_stat} > "
                            f"{quick_t_threshold} threshold)")
                break

    return rad_gone


def find_fe_onset_low_baseline(zone: Zone, linac: Linac, no_fe_file: str, fe_onset_file: str, coarse_step_size: float,
                               fine_step_size: float, baseline_gradient: float = 5.0):
    """Set all cavities in a zone to a static baseline gradient.  Walk each cavity up in big steps, down in small.

    Walk a cavity up in big steps doing quick checks for radiation.  If we see some, do a longer scan.  Then step down
    in small steps, with the same scanning pattern.  Report when no radiation is seen on the way down.
    """

    quick_t_threshold = 10
    slow_t_threshold = 20
    quick_n_samples = 4
    slow_n_samples = 20
    background_n_samples = 10
    logger.info("Running find_fe_onset_low_baseline procedure.")

    with open(no_fe_file, mode="a") as f:
        f.write(f"Using find_fe_onset_low_baseline (big steps up, small steps down) approach.")
        f.write(f"# {zone.name} step_size={coarse_step_size}, t-thresh={slow_t_threshold}, "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    with open(fe_onset_file, mode="a") as f:
        f.write(f"Using find_fe_onset_low_baseline (big steps up, small steps down) approach.")
        f.write(f"# {zone.name} step_size={fine_step_size} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # Walk each cavity up in big steps, to find a radiation signal.  Then back down in small steps until it goes away
    for cavity in zone.cavities:
        # We may want to redo the search.  Leave it up to operator discretion.
        redo_search = True
        while redo_search:
            try:
                redo_search = False

                # Establish baseline gradients and radiation
                setup_zone_baseline_gradient(zone=zone, linac=linac, baseline_gradient=baseline_gradient,
                                             background_n_samples=background_n_samples)

                # Walk cavity up until we see radiation or until we max out ODVH
                found_rad = walk_cavity_up_from_baseline(cavity=cavity, linac=linac, coarse_step_size=coarse_step_size,
                                                         quick_n_samples=quick_n_samples, slow_n_samples=slow_n_samples,
                                                         quick_t_threshold=quick_t_threshold,
                                                         slow_t_threshold=slow_t_threshold)

                # We still haven't found radiation, even after looking really hard
                if not found_rad:
                    logger.info(f"No radiation seen for {cavity.name}")
                    cavity.gset_no_fe = 100
                    cavity.gset_fe_onset = 100
                else:
                    # We found radiation, so now take small steps until it's gone.
                    logger.info(f"Found radiation.  Walking down in small steps until it disappears.")
                    rad_gone = walk_cavity_down_from_radiation(cavity=cavity, linac=linac,
                                                               fine_step_size=fine_step_size,
                                                               quick_n_samples=quick_n_samples,
                                                               slow_n_samples=slow_n_samples,
                                                               quick_t_threshold=quick_t_threshold,
                                                               slow_t_threshold=slow_t_threshold)

                    # If we walked the cavity all the way back down and still have radiation, then we have a problem
                    if not rad_gone:
                        logger.info(f"Walked {cavity.name} to min gradient ({cavity.gset_min} MV/m) without radiation"
                                    f" disappearing")
                        response = input("Re-baseline and try search again? [n/y]: ").strip().lower()
                        if response.startswith("y"):
                            logger.info("User instructed to re-baseline and retry search")
                            cavity.gset_fe_onset = None
                            cavity.gset_no_fe = None
                            redo_search = True
                            continue

            except Exception as exc:
                logger.error(f"Problem during cavity search {exc}")
                response = input("Re-baseline and try search again? [n/y]: ").strip().lower()
                if response.startswith("y"):
                    logger.info("User instructed to re-baseline and retry search")
                    cavity.gset_fe_onset = None
                    cavity.gset_no_fe = None
                    redo_search = True
                    continue

                if cavity.gset_no_fe > cavity.gset_fe_onset:
                    logger.warning("Coarse search found a higher gradient without FE than the fine search.")

            with open(no_fe_file, mode="a") as f:
                logger.info(f"Saving No FE value to {no_fe_file} for {cavity.name}.")
                f.write(f"{cavity.gset.pvname}\t{cavity.gset_no_fe}\n")

            with open(fe_onset_file, mode="a") as f:
                logger.info(f"Saving FE Onset value to {fe_onset_file} for {cavity.name}.")
                f.write(f"{cavity.gset.pvname}\t{cavity.gset_fe_onset}\n")


def run_gradient_scan_levelized_walk(linac: Linac, avg_time: float, num_steps: int, data_file: str,
                                     step_size: float = 1, settle_time: float = 6, n_cavities: int = None,
                                     max_cavity_steps=None):
    """Turn down group of cavities, one cavity at a time, in a random order.

    Don't allow same cavity twice until all in group were done once.  If n_cavities and max_cavity_steps are None, then
    all cavities are stepped down each iteration across num_steps.  n_cavities and max_cavity_steps allows for
    variations on a more random walk (n_cavities=1 means one cavity at random, repeats allowed, will be turned down ever
    n_steps).

    This procedure supports a settle time of 6 as we need Cavity.set_gradient call to wait six seconds for
    cryo which is a little longer than what we'd want otherwise.

    Args:
        linac:  The linac to operate on.
        avg_time:  How long to pause for mya to record data after the systems settle.  Typically used to create a less
                   noisy average of the signal.
        num_steps: How many times should we walk down the zones.
        data_file: The path to location where the data sample metadata should be saved.
        step_size:  The downward step size in MV/m used to reduce the gradient of a cavity.  Only positive numbers
                    supported (positive number will lower GSET).
        settle_time:  The amount of time to allow systems to settle after making a change to gradient.  Here this
                      time is handled by the Cavity being changed and includes the time for cryo to adjust.  For
                      step_size of 1 MV/m, settle time should be 6.
        n_cavities:  The number of cavities randomly selected in the group to be turned down.  None (default) implies
                     all.
        max_cavity_steps:  The maximum number of steps a single cavity is allowed to take.  None (default) implies
                           unlimited.
    """

    if num_steps < 1:
        raise ValueError("num_steps must be a positive integer.")

    if step_size < 0 or step_size > 1:
        raise ValueError("Only step_size between 0 and 1 MV/m are supported.")

    if settle_time < 6:
        raise ValueError("settle_time is restricted to at least 6 seconds to protect cryogenic systems.")

    # Track how many steps we're taking on each cavity
    if max_cavity_steps is None:
        max_cavity_steps = math.inf
    cavity_steps = {}
    allowable_cavities = {}
    for cavity in linac.cavities.values():
        cavity_steps[cavity.name] = 0
        allowable_cavities[cavity.name] = cavity

    zone_names = ','.join([z for z in sorted(linac.zones.keys())])
    logger.info("Setting NDX to operations settings")
    linac.set_ndx_for_operations()
    logger.info(f"Starting gradient scan of {zone_names}")

    try:
        for i in range(num_steps):
            logger.info(f"Running iteration {i + 1} of {num_steps}")
            with open(data_file, mode="a") as f:
                f.write(f"# active zones: {zone_names}, step_size={step_size}\n")
                f.write(
                    f"#settle_start,settle_end,avg_start,avg_end,settle_dur,avg_dur,cavity_name,cavity_epics_name\n")

                # Get the randomly ordered set of cavities to update
                if len(allowable_cavities) == 0:
                    logger.info("All cavities have reached the max steps.  Stopping scan.")
                    return
                cavities = list(allowable_cavities.values())
                random.shuffle(cavities)
                if n_cavities is not None:
                    n_cavities_ = min(len(cavities), n_cavities)
                    cavities = cavities[:n_cavities_]
                    logger.info(f"Cavities {[cavity.name for cavity in cavities]} chosen for this round of changes.")

                # Got through and update the number of steps each cavity has taken.  Exclude cavity from allowable cavities
                # if it is out of steps.
                for cavity in cavities:
                    cavity_steps[cavity.name] += 1
                    logger.info(f"{cavity.name} is taking step {cavity_steps[cavity.name]}.")
                    if cavity_steps[cavity.name] >= max_cavity_steps:
                        del allowable_cavities[cavity.name]
                        logger.info(f"{cavity.name} is taking it's last allowable step ({max_cavity_steps}).")

                for cavity in cavities:
                    try:
                        logger.info("Jiggling linac phases within +/- 5 degrees of initial")
                        linac.jiggle_psets(5.0)

                        gset = cavity.gset.value
                        new_gset = gset - step_size
                        logger.info(f"Stepping down {cavity.name} from {gset} -> {new_gset}")

                        # Check that we're good prior to making any changes
                        StateMonitor.check_state()

                        # Update gradient.  This should wait for gradient ramping and some time for cryo
                        cavity.set_gradient(gset=new_gset, settle_time=settle_time)

                        # We expect set_gradient to wait six seconds for cryo.  This is plenty of settle time for us.
                        settle_end = datetime.now()
                        settle_start = settle_end - timedelta(seconds=settle_time)

                        # If we're given a settle time, then sleep in small increments until that time is up.  Channel
                        # Access should be running in a different thread, but documentation was hazy about if this was
                        # needed.  This also checks the state of the control system and throws if there is a problem.
                        # logger.info(f"Waiting on settle time ({settle_time} seconds)")
                        # settle_start, settle_end = StateMonitor.monitor(duration=settle_time)

                        logger.info(f"Waiting on averaging time ({avg_time} seconds)")
                        avg_start, avg_end = StateMonitor.monitor(duration=avg_time)

                        # Write out sample time to file
                        fmt = "%Y-%m-%d %H:%M:%S.%f"
                        settle_start_str = settle_start.strftime(fmt)
                        settle_end_str = settle_end.strftime(fmt)
                        avg_start_str = avg_start.strftime(fmt)
                        avg_end_str = avg_end.strftime(fmt)

                        # Write out the timestamps for this sample
                        logger.info("Writing to data log")
                        f.write(
                            f"{settle_start_str},{settle_end_str},{avg_start_str},{avg_end_str},{settle_time},"
                            f"{avg_time},{cavity.name},{cavity.epics_name}\n")
                        f.flush()

                    except Exception as ex:
                        msg = f"Exception occurred during gradient scan\n{ex}"
                        logger.error(msg)
                        response = input(f"{msg}\nContinue (n|Y): ").lower().lstrip()
                        if not response.startswith('y'):
                            logger.info("Exiting after error based on user response.")
                            raise ex

    finally:
        linac.restore_psets()

    return

#
# Don't use these.  They will wreck the cryo system with all of the massive gyrations.
#
# def run_gradient_scan(zone: Zone, linac: Linac, avg_time: float, settle_time: float, n_levels: int = 3,
#                       zone_levels: List[str] = ('low', 'high', 'high'),
#                       linac_levels: List[str] = ('low', 'high', 'high')) -> None:
#     # Set the NDX like they would be for normal operations
#     linac.set_ndx_for_operations()
#
#     logger.info(f"Starting gradient scan of {zone.name}")
#     for cavity in sorted(zone.cavities.values(), key=attrgetter('name')):
#         scan_cavity_gradient(cavity=cavity, zone=zone, linac=linac, avg_time=avg_time, settle_time=settle_time,
#                              n_levels=n_levels, zone_levels=zone_levels, linac_levels=linac_levels)
#
#
# def scan_cavity_gradient(cavity: Cavity, zone: Zone, linac: Linac, avg_time: float, settle_time: float,
#                          n_levels: int = 3,
#                          zone_levels: List[str] = ('low', 'high', 'high'),
#                          linac_levels: List[str] = ('low', 'high', 'high')) -> None:
#     logger.info(f"Starting gradient scan of {cavity.name}")
#     with open("data_log.txt", mode="a") as f:
#         zone_names = ','.join([z for z in sorted(linac.zones.keys())])
#         f.write(f"# active zones: {zone_names}\n")
#         f.write(f"#settle_start,settle_end,avg_start,avg_end,settle_dur,avg_dur,cavity_name,cavity_epics_name\n")
#
#         # Check that we're good prior to making any changes
#         StateMonitor.check_state()
#
#         gset_base = cavity.get_low_gset()
#         gset_step_size = (cavity.odvh.value - cavity.get_low_gset()) / (n_levels - 1)
#         for i in range(n_levels):
#             for z_level in zone_levels:
#                 for l_level in linac_levels:
#                     try:
#                         logger.info(f"Setting cavity gradient ({cavity.name} = {gset_base + gset_step_size * i})")
#                         cavity.set_gradient(gset_base + gset_step_size * i)
#                         logger.info(f"Setting zone gradients ({zone.name} = {z_level})")
#                         zone.set_gradients(exclude_cavs=[cavity], level=z_level)
#                         logger.info(f"Setting linac gradients ({linac.name} = {l_level})")
#                         linac.set_gradients(exclude_cavs=[cavity], exclude_zones=[zone], level=l_level)
#                         logger.info("Jiggling linac phases [-5, 5] degrees")
#                         linac.jiggle_psets(5.0)
#
#                         # If we're given a settle time, then sleep in small increments until that time is up.  Channel
#                         # Access should be running in a different thread, but documentation was hazy about if this was
#                         # needed.  This also checks the state of the control system and throws if there is a problem.
#                         logger.info(f"Waiting on settle time ({settle_time} seconds)")
#                         settle_start, settle_end = StateMonitor.monitor(duration=settle_time)
#                         logger.info(f"Waiting on averaging time ({avg_time} seconds)")
#                         avg_start, avg_end = StateMonitor.monitor(duration=avg_time)
#
#                         # Write out sample time to file
#                         fmt = "%Y-%m-%d %H:%M:%S.%f"
#                         settle_start_str = settle_start.strftime(fmt)
#                         settle_end_str = settle_end.strftime(fmt)
#                         avg_start_str = avg_start.strftime(fmt)
#                         avg_end_str = avg_end.strftime(fmt)
#                         logger.info("Writing to data log")
#                         f.write(
#                             f"{settle_start_str},{settle_end_str},{avg_start_str},{avg_end_str},{settle_time},"
#                             f"{avg_time},{cavity.name},{cavity.epics_name}\n")
#                         f.flush()
#                     except Exception as ex:
#                         msg = f"Exception occurred during gradient scan\n{ex}"
#                         logger.error(msg)
#                         response = input(f"{msg}\nContinue (n|Y): ").lower().lstrip()
#                         if not response.startswith('y'):
#                             logger.info("Exiting after error based on user response.")
#                             raise ex


def run_simple_gradient_scan(linac: Linac, avg_time: float, data_file: str, step_size: float = 1,
                             settle_time: float = 6.0, max_cavity_steps: int = 2) -> None:
    """This performs a simple scan on all cavities in a linac, one cavity at a time.

    Each cavity is stepped up from their initial value at most max_cavity_steps, in step of size step_size.  If the
    next step would have exceeded the maximum/minimum allowable gradient for that cavity, we set it to the max. Then the
    cavity is returned to it's initial value, and stepped down in a similar fashion.  Finally the cavity is returned to
    it's initial value, and the next cavity is scanned.

    Args:
        linac:  The linac to operate on.
        avg_time:  How long to pause for mya to record data after the systems settle.  Typically used to create a less
                   noisy average of the signal.
        data_file: The path to location where the data sample metadata should be saved.
        step_size:  The downward step size in MV/m used to reduce the gradient of a cavity.  Only positive numbers
                    supported (positive number will lower GSET).
        settle_time:  The amount of time to allow systems to settle after making a change to gradient.  Here this
                      time is handled by the Cavity being changed and includes the time for cryo to adjust.  For
                      step_size of 1 MV/m, settle time should be 6.
        max_cavity_steps:  The maximum number of steps a single cavity is allowed to take.  None (default) implies
                           unlimited.
    """

    logger.info(f"Starting simple_gradient_scan")
    zone_names = list(linac.zones.keys())
    logger.info("Setting NDX to operations settings")
    linac.set_ndx_for_operations()

    if step_size == 0:
        raise ValueError("Must specify a non-zero step_size")
    if max_cavity_steps <= 0:
        raise ValueError("Must specify a positive max_cavity_steps")

    with open(data_file, mode="a") as f:
        try:
            utils.write_data_index_header(f, type='simple_gradient_scan', active_zones=zone_names, step_size=step_size)

            for cav_name in linac.cavities.keys():
                cavity = linac.cavities[cav_name]
                if cavity.bypassed_eff:
                    logger.info(f"{cavity.name} is effectively bypassed.  Skipping.")
                    logger.info(f"{cavity.name}: gset_init={cavity.gset_init}, self.bypassed={cavity.bypassed}, "
                                f"self.bypassed_eff={cavity.bypassed_eff}, self.gset={cavity.gset.value}")
                    continue
                gset_curr = cavity.gset.value
                gset_max = cavity.gset_max
                gset_min = cavity.gset_min
                logger.info(f"Scanning {cavity.name}.  gset_curr={gset_curr}, gset_min={gset_min}, gset_max={gset_max}")

                # Figure out the applicable gradient steps for this cavity.  First include current value and go up.
                gsets_up = [gset_curr]
                for i in range(1, max_cavity_steps + 1):
                    gradient = gset_curr + (step_size * i)
                    if gradient >= gset_max:
                        logger.info(f"{cavity.name}: Limited to +{gset_max - gset_curr} MV/m above initial.")
                        gsets_up.append(gset_max)
                        break
                    gsets_up.append(gradient)

                # Figure out the values to scan going down.
                gsets_down = []
                for i in range(1, max_cavity_steps + 1):
                    gradient = gset_curr - (step_size * i)
                    if gradient <= gset_min:
                        logger.info(f"{cavity.name}: Limited to -{gset_curr - gset_min} MV/m below initial.")
                        gsets_down.append(gset_min)
                        break
                    gsets_down.append(gradient)

                for gsets in [gsets_up, gsets_down]:
                    logger.info(f"{cavity.name}:  GSETs to scan {gsets}")
                    for gset_next in gsets:
                        try:
                            gset_curr = cavity.gset.value
                            # We jiggled phases for a long time, but I don't see the point in adding that noise
                            # logger.info("Jiggling linac phases within +/- 5 degrees of initial")
                            # linac.jiggle_psets(5.0)

                            # Here we are allowing larger step sizes than 1 MV/m (force=True)
                            StateMonitor.check_state()
                            logger.info(f"{cavity.name}:  Stepping gradient {gset_curr} => {gset_next}.")
                            cavity.set_gradient(gset=gset_next, settle_time=settle_time, wait_for_ramp=True, force=True)

                            # We expect set_gradient to possible wait for cryo.  This scan will likely not wait for cryo.
                            settle_end = datetime.now()
                            settle_start = settle_end - timedelta(seconds=settle_time)

                            logger.info(f"Waiting on averaging time ({avg_time} seconds)")
                            avg_start, avg_end = StateMonitor.monitor(duration=avg_time)

                            # Write out the timestamps for this sample
                            logger.info("Writing to data log")
                            # f.write(
                            utils.write_data_index_row(f, settle_start=settle_start, settle_end=settle_end,
                                                       avg_start=avg_start, avg_end=avg_end, settle_time=settle_time,
                                                       avg_time=avg_time, cavity_name=cavity.name,
                                                       cavity_epics_name=cavity.epics_name)
                            f.flush()

                        except UserScanAbort:
                            raise
                        except Exception as ex:
                            do_continue = utils.user_alert_scan_paused(f"Exception occurred during gradient scan\n{ex}")
                            if not do_continue:
                                logger.info(f"{cavity.name}: Attempting to restoring cavity gradient")
                                cavity.restore_gset()
                                linac.restore_psets()
                                raise ex

                            # msg = f"Exception occurred during gradient scan\n{ex}"
                            # logger.error(msg)
                            # response = input(f"{msg}\nContinue (n|Y): ").lower().lstrip()
                            # if not response.startswith('y'):
                            #     logger.info("Exiting after error based on user response.")
                            #     logger.info(f"{cavity.name}: Attempting to restoring cavity gradient")
                            #     cavity.restore_gset()
                            #     linac.restore_psets()
                            #     raise ex
                            # else:
                            #     logging.info("Continuing scan")

                    # Walk the gradient back, and wait one second before each step.
                    logger.info(f"{cavity.name}: Restoring original gradient.")
                    cavity.restore_gset(settle_time=1)
        finally:
            logger.info("Restoring PSETs.")
            linac.restore_psets()

        return


def run_random_sample_random_offset_gradient_scan(linac: Linac, avg_time: float, data_file: str, n_samples: int,
                                                  settle_time: float = 6.0, n_cavities: int = 10,
                                                  offset_list: Optional[List[float]] = None,
                                                  max_zone_heat_change: float = 10.0, repair: bool = False) -> None:
    """This randomly selects cavities and applies a random offset perturbation to gradients from their initial setting.

    At each iteration, n_cavities are selected to be perturbed.  Then each cavity's gradient is changed by a small
    amount randomly chosen from offset_list.  Once all cavities have been set to the new value, the system waits
    avg_time seconds so that data can be collected and stored by MYA.  After waiting the cavities are returned to their
    initial gradients.

    Note: The changes in gradient are not allowed to exceed the cavity's gset_max or gset_mix.

    Args:

    """

    logger.info(f"Starting random sample random offset gradient scan")

    zone_names = list(linac.zones.keys())
    logger.info("Setting NDX to operations settings")
    linac.set_ndx_for_operations()

    # Get the list of available cavities
    available_cavities = {}
    bypassed_cavity_names = []
    for cav in linac.cavities.values():
        if cav.bypassed_eff:
            bypassed_cavity_names.append(cav.name)
            continue
        available_cavities[cav.name] = cav

    if len(available_cavities) < n_cavities:
        raise RuntimeError(f"Fewer cavities available ({len(available_cavities)}) than requested sample size "
                           f"({n_cavities})")

    # If nothing is given, specify a range between -1.5 and 1.5 excluding small steps between [-0.5, 0.5]
    if offset_list is None:
        offset_list = np.round(np.linspace(-1.5, -0.5, 11), 1).tolist()
        offset_list += np.round(np.linspace(0.5, 1.5, 11), 1).tolist()
    offset_list = np.array(offset_list)

    with open(data_file, mode="a") as f:
        utils.write_data_index_header(f, type='random_sample_gradient_scan', active_zones=zone_names,
                                      bypassed_cavities=bypassed_cavity_names, gradient_delta_range=offset_list)

        for i in range(1, n_samples + 1):
            logger.info(f"Starting sample round {i} of {n_samples}.")
            try:
                cavs, new_gsets, old_gsets, zones_gsets = get_random_cavity_gradients_changes(
                    population=list(available_cavities.values()),
                    n_cavities=n_cavities, offset_list=offset_list)
                if repair:
                    # Scale the gradients up across the whole linac to meet the energy.
                    cavs, new_gsets, old_gsets, zones_gsets = linac.scale_gradients_to_meet_energy(gsets=new_gsets)

                # Check that the gradients won't affect heat too much in a given CM
                skip = False
                for zone in zones_gsets.keys():
                    try:
                        rel_change, new_heat, old_heat = zone.check_percent_heat_change(gradients=zones_gsets[zone],
                                                                                        percentage=max_zone_heat_change)
                        logger.info(f"{zone.name}: Expected heat change OK.  {np.round(old_heat, 1)}W ->"
                                    f" {np.round(new_heat, 1)}W ({np.round(rel_change, 1)}%)")
                    except Exception as ex:
                        logger.error(ex)
                        skip = True
                        break
                if skip:
                    logger.info("Skipping this sample round due to large heat changes.")
                    continue

                # We used to jiggle psets, but now it seems like I'm only adding unhelpful noise.
                # Jiggle PSETs to make predictions slightly more robust to a likely source of noise
                # linac.jiggle_psets(delta=5.0)

                # Do the updates, and track in the lookup index file.
                collect_data_at_gradients(cavs=cavs, new_gsets=new_gsets, old_gsets=old_gsets,
                                          settle_time=settle_time, avg_time=avg_time, file=f)

            except UserScanAbort:
                # Don't do anything if the user requested an abort.  Just need to bypass other exception handling at
                # this layer
                raise
            except Exception as ex:
                logger.error(f"Exception raised: {ex}")
                response = input(f"Restore cavities to initial values? (n|y): ").lower().lstrip()
                if response.startswith('y'):
                    logger.info("Attempting to restore PSETs and GSETs")
                    for cav in cavs:
                        logger.info(f"Restoring {cav.name}")
                        cav.restore_pset()
                        cav.restore_gset(settle_time=1)
                response = input(f"Continue with next iteration of scan (n|y)? No aborts entire scan: ")
                if response.lower().lstrip().startswith('n'):
                    raise UserScanAbort(f"Requested abort after seeing exception '{ex}'.")

    logger.info("Scan complete")


def get_random_cavity_gradients_changes(population: List[Cavity], n_cavities: int,
                                        offset_list: np.ndarray) -> Tuple[List[Cavity],
                                                                          Dict[str, float],
                                                                          Dict[str, float],
                                                                          Dict[Zone, List[Optional[float]]]]:
    """Determine the set of cavities to update and what their new gradient values should be.

    This randomly samples n_cavities from the population list to be updated.  Then the cavity gradients are updated
    by selecting an offset at random from the offset list and applying it to the current value.  Only offsets that
    would respect the cavity's gset_min and gset_max attributes are considered, which may lead to a zero offset being
    applied.
    """
    # Sample the cavities to be adjusted this time
    cavs = sorted(random.sample(population, n_cavities), key=lambda x: x.name)
    # Track which gsets are changing for each zone involved
    zones_gsets = {}
    # Track the new gsets for the cavities involved
    new_gsets = {}
    old_gsets = {}
    logger.info(f"Adjusting {','.join([cav.name for cav in cavs])}")
    update_msg = ""
    for cav in cavs:
        cav_offsets = offset_list[(offset_list + cav.gset.value > cav.gset_min) &
                                  (offset_list + cav.gset.value < cav.gset_max)].tolist()
        if len(cav_offsets) > 0:
            offset = random.sample(cav_offsets, k=1)[0]
        else:
            offset = 0
            logger.info(f"{cav.name}: No valid offsets.  Current, Min, Max GSET: {cav.gset.value},"
                        f" {cav.gset_min}, {cav.gset_max}.  Cavity unchanged.")
        new_gsets[cav.name] = cav.gset.value + offset
        old_gsets[cav.name] = cav.gset.value
        update_msg += f" {cav.name}: {np.round(cav.gset.value, 2)}->{np.round(new_gsets[cav.name], 2)}"

        if cav.zone not in zones_gsets.keys():
            zones_gsets[cav.zone] = [None] * 8
        zones_gsets[cav.zone][cav.cavity_number - 1] = new_gsets[cav.name]

    logger.info(f"Sample will use these updates: {update_msg}")

    return cavs, new_gsets, old_gsets, zones_gsets

