import logging
import os
import sys
import traceback
import argparse
import signal
from datetime import datetime

from linac import LinacFactory

import procedures

# The root directory of the app
app_root = os.path.realpath(os.path.join(os.path.basename(__file__), ".."))


def sigint_handler(sig, frame):
    logging.info("Received SIGINT signal (Control-c).  Exiting.")
    sys.exit(1)


def init_logging(log_dir: str, run_log: str) -> None:
    """Setup logging configuration and directory structure.

    Args:
        log_dir: The full path to the log directory
        run_log: The name of the file to create with log_dir
    """
    # logging.DEBUG for gory details on linac operations.  LOTS of data.
    log_formatter = logging.Formatter('%(asctime)s %(name)-12s %(levelname)-8s %(message)s')
    root_logger = logging.getLogger('')
    root_logger.setLevel(logging.INFO)

    # Make the directory for logging this run
    if not os.path.exists(log_dir):
        os.mkdir(log_dir)

    # Fail if we can't create that.  os.mkdir may throw - not clear to me.
    if not os.path.exists(log_dir):
        msg = f"Error creating log directory {log_dir}. Exiting"
        logging.error(msg)
        raise RuntimeError(msg)

    # Add a file output to the logging module
    file_handler = logging.FileHandler(filename=os.path.join(log_dir, run_log))
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)

    # Add a stream handler that prints to console
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(log_formatter)
    root_logger.addHandler(stdout_handler)


def main() -> int:
    signal.signal(signal.SIGINT, sigint_handler)

    parser = argparse.ArgumentParser(description='Run data collection for Cavity Field Emission project')
    parser.add_argument('-t', '--testing', help="Run in test mode with 'adamc:' EPICS prefix.", action='store_true')
    parser.add_argument('-l', '--linac', required=True, choices=['NorthLinac', 'SouthLinac'],
                        help="Which linac to use.  Must match CED SegMask, e.g., NorthLinac")

    subparsers = parser.add_subparsers(dest="command")
    onset = subparsers.add_parser("fe_onset", help="Scan a zone for radiation/FE onset")
    gradient = subparsers.add_parser("gradient_scan", help="Investigate gradient parameter space of set of zones")

    onset.add_argument('-z', '--zone', help='The primary zone to test', required=True)
    onset.add_argument('-d', '--detectors', nargs='*',
                       help='Selection of NDX detectors available in zone\'s linac to include in test.  All if empty.')
    onset.add_argument('-e', '--electrometers', nargs='*',
                       help='Selection of NDX electrometers available in zone\'s linac to include in test.  Also '
                            'excludes associated detectors.  All if empty.')

    gradient.add_argument('--linac-zones', nargs="+", required=True,
                          help="Selection of zones from linac that will be included in test. All if empty")
    # parser.add_argument('-s', '--settle-time', default=5,
    #                     help="How long in seconds to let CEBAF sit after making changes to RF")
    gradient.add_argument('-a', '--average-time', default=3,
                          help="How many seconds of data should we allow the archiver to collect to average results")
    gradient.add_argument('-s', '--step-size', required=True, type=float,
                          help="How large of a gradient step to take each time. Max value of 1.0.")
    gradient.add_argument('-n', '--num-steps', required=True, type=int,
                          help="How many times should all zones be stepped down.")
    gradient.add_argument('--num-cavities', required=False, type=int,
                          help="How many cavities should be in the group stepped down each iteration (all by default).")
    gradient.add_argument('--max-cavity-steps', required=False, type=int,
                          help="How many times a single cavity is allowed to be stepped down (unlimited by default).")


    try:
        args = parser.parse_args()
        linac_name = args.linac
        testing = args.testing

        # Setup logging for the whole app
        dir_name = f"run-{linac_name}-{datetime.now().strftime('%Y-%m-%d_%H%M%S.%f')}"
        if testing:
            dir_name = f"run-testing-{linac_name}-{datetime.now().strftime('%Y-%m-%d_%H%M%S.%f')}"
        log_dir = os.path.join(app_root, "log", dir_name)
        init_logging(log_dir=log_dir, run_log="fe_daq.log")
        logger = logging.getLogger(__name__)

        logger.info(f"CLI args = {ascii(args)}")
        logger.info(f"Running in test mode: {testing}")

        if args.command == 'fe_onset':

            zone_name = args.zone
            detector_names = args.detectors
            electrometer_names = args.electrometers

            if testing:
                logger.info("Running in test mode")

            # Setup the Linac and Zone objects for the task at hand
            logger.info("Creating linac")
            linac = LinacFactory(testing=testing).create_linac(name=linac_name, zone_names=zone_name,
                                                               electrometer_names=electrometer_names,
                                                               detector_names=detector_names)

            logger.info("Running fe_onset task")

            # Make sure we were given a zone to work on
            if zone_name is None:
                raise RuntimeError("zone is a required argument for -m fe_onset")
            if zone_name not in linac.zones.keys():
                raise ValueError(f"{zone_name} was not found in {linac.name}'s zone list.")
            zone = linac.zones[zone_name]

            # Go find those FE Onsets
            procedures.run_find_fe_process(zone, linac, no_fe_file=os.path.join(log_dir, f"no_fe-{zone_name}.tsv"),
                                           fe_onset_file=os.path.join(log_dir, f"fe_onset-{zone_name}.tsv"))

        elif args.command == 'gradient_scan':

            # Pull off the arguments this mode uses
            zone_names = args.linac_zones
            average_time = float(args.average_time)
            step_size = float(args.step_size)
            num_steps = int(args.num_steps)
            n_cavities = args.num_cavities
            max_cavity_steps = args.max_cavity_steps
            # settle_time = args.settle_time

            # Setup the Linac and Zone objects for the task at hand
            logger.info("Creating linac")
            linac = LinacFactory(testing=testing).create_linac(name=linac_name, zone_names=zone_names)

            logger.info("Starting gradient scan")
            procedures.run_gradient_scan_levelized_walk(linac=linac, avg_time=average_time, num_steps=num_steps,
                                                        step_size=step_size,
                                                        data_file=os.path.join(log_dir, "gradient-scan.csv"),
                                                        n_cavities=n_cavities, max_cavity_steps=max_cavity_steps)

            # Put the PSETs back where you found them.
            linac.restore_psets()

        else:
            raise ValueError("Command required. fe_onset, gradient_scan")

    except Exception as ex:
        logging.exception("Fatal exception raised.  Exiting.")
        return 1

    logging.info("Program exiting normally.")
    return 0


if __name__ == "__main__":
    exit(main())
