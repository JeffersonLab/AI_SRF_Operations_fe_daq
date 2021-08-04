import logging
import time
from datetime import datetime
from operator import attrgetter
from typing import List

from cavity import Cavity
from linac import Zone, Linac
from state_monitor import StateMonitor

logger = logging.getLogger(__name__)


def run_find_fe_process(zone: Zone, linac: Linac, no_fe_file="./no_fe.tsv", fe_onset_file="./fe_onset.tsv") -> None:
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
    linac.set_gradients(level="low")
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
        if cavity.bypassed:
            reached_max[cavity.name] = True
            logger.info(f"{cavity.name} is bypassed.  Unable to find no FE gset.")
        else:
            reached_max[cavity.name] = False

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

                # TODO: Remove this for production code.  Look at API - maybe another way to ensure callbacks finished.
                # This may just be an artifact of my dumb testing IOC controller.
                time.sleep(0.05)

                # Measure radiation.  Turn cavity back down if we see anything above background.
                linac.get_radiation_measurements(3)
                is_rad, t_stat = linac.is_radiation_above_background(t_stat_threshold=5)
                if is_rad:
                    logger.info(f"Found no FE gset for {cavity.name} at {val} MV/m")
                    logger.info(f"Max radiation t-stat is {t_stat}")
                    logger.info(f"Set {cavity.name} back to {val}")

                    reached_max[cavity.name] = True
                    cavity.set_gradient(val)
                    cavity.gset_no_fe = val

                elif reached_max[cavity.name]:
                    # Implies next_val == cavity.odvh.value, but that float comparison could be misleading.
                    logger.info(f"Found no FE at {cavity.name} at ODVH of {val} MV/m")
                    cavity.gset_no_fe = next_val

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

    # Save the starting no_fe values, then walk up 0.2 MV/m until we find FE onset.  Then back to starting value.
    for cavity in zone.cavities.values():
        if cavity.bypassed:
            logger.info(f"{cavity.name} is bypassed.  Unable to find field emission onset.  Skipping.")
            continue

        # Check that we have a starting point
        if cavity.gset_no_fe is None:
            logger.warning(f"{cavity.name} gset_no_fe is None.  Should be a value or skipped as bypassed.")
            continue

        # Tracking if we have found FE onset, and the number of attempts we've made at finding it.
        logger.info(f"Finding FE onset for {cavity.name}.")
        found_onset = False
        count = 0

        # Where to start the initial search.  Note walk_cavity_gradient_up should handle the case where start >= odvh.
        start = cavity.gset_no_fe

        # Now try at most three times to detect the FE onset for this cavity
        while not found_onset or count < n_tries:
            found_onset = walk_cavity_gradient_up(cavity=cavity, linac=linac, start=start, step_size=step_size)
            count += 1

        # We we able to find anything after several repeated attempts.  If not, then background radiation has probably
        # changed.  Ask user if we should update it.
        if not found_onset:
            logger.warning(f"{cavity.name} could not find FE onset gradient.")
            response = input("Should we re-baseline background radiation? (n|y): ").lstrip().lower()
            if response.startswith("y"):
                logger.warning("Updating background radiation readings with current levels")
                linac.get_radiation_measurements(10)
                linac.save_radiation_measurements_as_background()

    with open(data_file, mode="a") as f:
        f.write(f"# {zone.name} step_size={step_size} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        for cavity in zone.cavities.values():
            f.write(f"{cavity.gset.pvname}\t{cavity.gset_fe_onset}\n")


def walk_cavity_gradient_up(cavity: Cavity, linac: Linac, start: float, step_size: float) -> bool:
    """This walks an individual cavity's gradient up until radiation is seen on an NDX detector.

    This assumes that the machine is setup in such a way that current radiation levels at 'start' will be similar to
    the NDX recorded background.  After new radiation is detected, this function turns the cavity back down to verify
    that the radiation goes away.  The FE onset value is saved to the cavity.

    Args:
        cavity: The cavity to test
        linac: The rest of a linac that is under test
        start: A gset value that is known to produce no radiation.  The first new measurement is made one step above.
        step_size: The amount by which gradient should be increased at each step

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
    cavity.set_gradient(start)
    val = start

    # Walk the cavity gradient up in small steps until we see a change in radiation
    while val <= cavity.odvh.value:
        next_val = val + step_size

        # We can't run the cavities higher than ODVH
        if next_val > cavity.odvh.value:
            next_val = cavity.odvh.value

        logger.info(f"Stepping {cavity.name} from {val} to {next_val}.")
        cavity.set_gradient(next_val)

        # Measure radiation.  Turn cavity back down if we see anything above background.
        linac.get_radiation_measurements(3)
        is_rad, t_stat = linac.is_radiation_above_background(t_stat_threshold=5)
        if is_rad:
            logger.info(f"Found FE onset for {cavity.name} at {val} MV/m (t-stat = {t_stat}).")
            logger.info(f"Turning cavity down to verify radiation elimination.")
            cavity.set_gradient(val)
            linac.get_radiation_measurements(3)
            is_rad, t_stat = linac.is_radiation_above_background(t_stat_threshold=5)
            if is_rad:
                logger.info(f"Found radiation when cavity turned down (t-stat = {t_stat}).  Search Failed.")
            else:
                found_onset = True
                cavity.gset_fe_onset = val
                logger.info(f"Found no radiation when cavity turned down.  Search succeeded.")
            break

        # Read in the current GSET for the next loop iteration
        val = cavity.gset.get(use_monitor=False)

    return found_onset


def run_gradient_scan(zone: Zone, linac: Linac, avg_time: float, settle_time: float, n_levels: int = 3,
                      zone_levels: List[str] = ('low', 'high', 'high'),
                      linac_levels: List[str] = ('low', 'high', 'high')) -> None:
    # Set the NDX like they would be for normal operations
    linac.set_ndx_for_operations()

    logger.info(f"Starting gradient scan of {zone.name}")
    for cavity in sorted(zone.cavities.values(), key=attrgetter('name')):
        scan_cavity_gradient(cavity=cavity, zone=zone, linac=linac, avg_time=avg_time, settle_time=settle_time,
                             n_levels=n_levels, zone_levels=zone_levels, linac_levels=linac_levels)

def scan_cavity_gradient(cavity: Cavity, zone: Zone, linac: Linac, avg_time: float, settle_time: float, n_levels: int = 3,
                  zone_levels: List[str] = ('low', 'high', 'high'),
                  linac_levels: List[str] = ('low', 'high', 'high')) -> None:
    logger.info(f"Starting gradient scan of {cavity.name}")
    with open("data_log.txt", mode="a") as f:
        zone_names = ','.join([z for z in sorted(linac.zones.keys())])
        f.write(f"# active zones: {zone_names}\n")
        f.write(f"#settle_start,settle_end,avg_start,avg_end,settle_dur,avg_dur,cavity_name,cavity_epics_name\n")

        # Check that we're good prior to making any changes
        StateMonitor.check_state()

        gset_base = cavity.get_low_gset()
        gset_step_size = (cavity.odvh.value - cavity.get_low_gset()) / (n_levels - 1)
        for i in range(n_levels):
            for z_level in zone_levels:
                for l_level in linac_levels:
                    try:
                        logger.info("Setting gradients")
                        cavity.set_gradient(gset_base + gset_step_size * i)
                        logger.info("Setting zone gradients")
                        zone.set_gradients(exclude_cavs=[cavity], level=z_level)
                        logger.info("Setting linac gradients")
                        linac.set_gradients(exclude_cavs=[cavity], exclude_zones=[zone], level=l_level)
                        logger.info("Setting linac phases")
                        linac.jiggle_psets(5.0)

                        # If we're given a settle time, then sleep in small increments until that time is up.  Channel
                        # Access should be running in a different thread, but documentation was hazy about if this was
                        # needed.  This also checks the state of the control system and throws if there is a problem.
                        logger.info(f"Waiting on settle time ({settle_time} seconds)")
                        settle_start, settle_end = StateMonitor.monitor(duration=settle_time)
                        logger.info(f"Waiting on averaging time ({avg_time} seconds)")
                        avg_start, avg_end = StateMonitor.monitor(duration=avg_time)

                        # Write out sample time to file
                        fmt = "%Y-%m-%d %H:%M:%S.%f"
                        settle_start_str = settle_start.strftime(fmt)
                        settle_end_str = settle_end.strftime(fmt)
                        avg_start_str = avg_start.strftime(fmt)
                        avg_end_str = avg_end.strftime(fmt)
                        logger.debug("Writing to data log")
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
