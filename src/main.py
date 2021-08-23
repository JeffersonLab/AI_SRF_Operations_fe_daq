import logging
import traceback
import argparse
from linac import LinacFactory

import procedures

# logging.DEBUG for gory details on linac operations.  LOTS of data.
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s')
logger = logging.getLogger(__name__)


def main() -> int:
    logger.info("Starting main")

    # TODO: Break this out into subparsers for the different modes
    # parser = argparse.ArgumentParser(description='Run data collection for Cavity Field Emission project')
    # parser.add_argument('-z', '--zone', help='The primary zone to test')
    # parser.add_argument('-l', '--linac', required=True,
    #                     help="Which linac to use.  Must match CED SegMask, e.g., NorthLinac")
    # parser.add_argument('--linac_zones', nargs="*",
    #                     help="Selection of zones from linac that will be included in test. All if empty")
    # parser.add_argument('-d', '--detectors', nargs='*',
    #                     help='Selection of NDX detectors (whose electrometer is from linac) to include in test.'
    #                          '  All if empty.')
    # parser.add_argument('-m', '--mode', help="The data collection mode to run", required=True,
    #                     choices=['fe_onset', 'g_scan'])
    # parser.add_argument('-t', '--testing', help="Run in test mode with 'adamc:' EPICS prefix.", action='store_true')
    # # parser.add_argument('-s', '--settle-time', default=5,
    # #                     help="How long in seconds to let CEBAF sit after making changes to RF")
    # parser.add_argument('-a', '--average-time', default=3,
    #                     help="How many seconds of data should we allow the archiver to collect to average results")

    parser = argparse.ArgumentParser(description='Run data collection for Cavity Field Emission project')
    parser.add_argument('-t', '--testing', help="Run in test mode with 'adamc:' EPICS prefix.", action='store_true')
    parser.add_argument('-l', '--linac', required=True,
                        help="Which linac to use.  Must match CED SegMask, e.g., NorthLinac")

    subparsers = parser.add_subparsers(dest="command")
    onset = subparsers.add_parser(title="fe_onset", help="Scan a zone for radiation/FE onset")
    gradient = subparsers.add_parser(title="gradient_scan", help="Investigate gradient parameter space of set of zones")

    onset.add_argument('-z', '--zone', help='The primary zone to test', required=True)
    onset.add_argument('-d', '--detectors', nargs='*',
                       help='Selection of NDX detectors available in zone\'s linac to include in test.  All if empty.')
    onset.add_argument('-e', '--electrometers', nargs='*',
                       help='Selection of NDX electrometers available in zone\'s linac to include in test.  Also '
                            'excludes associated detectors.  All if empty.')

    gradient.add_argument('-z, --zones', nargs="*", required=True,
                          help="Selection of zones from linac that will be included in test. All if empty")
    # parser.add_argument('-s', '--settle-time', default=5,
    #                     help="How long in seconds to let CEBAF sit after making changes to RF")
    gradient.add_argument('-a', '--average-time', default=3,
                          help="How many seconds of data should we allow the archiver to collect to average results")
    gradient.add_argument('-s', '--step-size', required=True, type=float,
                          help="How large of a gradient step to take each time. Max value of 1.0.")
    gradient.add_argument('-n', '--num-steps', required=True, type=int,
                          help="How many times should all zones be stepped down.")


    try:
        args = parser.parse_args()
        logger.info(f"CLI args = {ascii(args)}")

        linac_name = args.linac
        testing = args.testing

        if testing:
            logger.info("Running in test mode")

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
            procedures.run_find_fe_process(zone, linac)

        if args.mode == 'grad_scan':

            # Pull off the arguments this mode uses
            linac_zones = args.linac_zones
            average_time = float(args.average_time)
            step_size = float(args.step_size)
            # settle_time = args.settle_time

            # Setup the Linac and Zone objects for the task at hand
            logger.info("Creating linac")
            linac = LinacFactory(testing=testing).create_linac(name=linac_name, zone_names=linac_zones)

            procedures.run_gradient_scan_levelized_walk(linac=linac, avg_time=average_time, step_size=step_size)

        # Put the PSETs back where you found them.
        linac.restore_psets()
    except Exception as ex:
        print("Fatal exception raised.  Exiting")
        print(ex)
        print(traceback.print_exc())
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
