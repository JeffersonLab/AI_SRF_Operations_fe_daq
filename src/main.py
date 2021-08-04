import logging
import traceback
import argparse
from linac import LinacFactory

import procedures

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s')
logger = logging.getLogger(__name__)


def main() -> int:
    logger.debug("Starting main")

    parser = argparse.ArgumentParser(description='Run data collection for Cavity Field Emission project')
    parser.add_argument('-z', '--zone', help='The primary zone to test', required=True)
    parser.add_argument('-l', '--linac', required=True,
                        help="Which linac to use.  Must match CED SegMask, e.g., NorthLinac")
    parser.add_argument('--linac_zones', nargs="*",
                        help="Selection of zones from linac that will be included in test. All if empty")
    parser.add_argument('-d', '--detectors', nargs='*',
                        help='Selection of NDX detectors (whose electrometer is from linac) to include in test.'
                             '  All if empty.')
    parser.add_argument('-m', '--mode', help="The data collection mode to run", required=True,
                        choices=['fe_onset', 'g_scan'])
    parser.add_argument('-t', '--testing', help="Run in test mode with 'adamc:' EPICS prefix.", action='store_true')
    parser.add_argument('-s', '--settle-time', default=5,
                        help="How long in seconds to let CEBAF sit after making changes to RF")
    parser.add_argument('-a', '--average-time', default=3,
                        help="How many seconds of data should we allow the archiver to collect to average results")

    try:
        args = parser.parse_args()
        print(ascii(args))

        zone_name = args.zone
        linac_name = args.linac
        linac_zones = args.linac_zones
        detector_names = args.detectors
        testing = args.testing
        settle_time = args.settle_time
        average_time = args.average_time

        if testing:
            logger.info("Running in test mode")

        # Setup the Linac and Zone objects for the task at hand
        logger.debug("Creating linac")
        linac = LinacFactory(testing=testing).create_linac(name=linac_name, zone_names=linac_zones,
                                                           detector_names=detector_names)
        if zone_name not in linac.zones.keys():
            raise ValueError(f"{zone_name} was not found in {linac.name}'s zone list.")

        zone = linac.zones[zone_name]

        if args.mode == 'fe_onset':
            logger.info("Running fe_onset task")
            procedures.run_find_fe_process(zone, linac)

        if args.mode == 'g_scan':
            procedures.scan_gradient(cavity=cavity, zone=zone, linac=linac, avg_time=average_time,
                                     settle_time=settle_time)

        linac.restore_psets()
    except Exception as ex:
        print("Fatal exception raised.  Exiting")
        print(ex)
        print(traceback.print_exc())
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
