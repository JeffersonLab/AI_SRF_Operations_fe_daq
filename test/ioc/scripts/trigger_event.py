#!/usr/csite/pubtools/bin/python3.7

import logging
import argparse
from time import sleep
from typing import Optional

from epics import PV

logging.basicConfig(level=logging.DEBUG)

PREFIX = "adamc:"


def pressure_event(name: str, level: str, duration: float, recover: float, **kwargs):
    if name == '1L':
        pv = PV(f"{PREFIX}CPI4107B")
    elif name == '2L':
        pv = PV(f"{PREFIX}CPI5107B")
    else:
        raise ValueError(f"Invalid name '{name}'")

    if level == 'high':
        excursion = 0.04
    elif level == 'low':
        excursion = 0.037
    else:
        raise ValueError(f"Invalid level '{level}'")

    pv.put(excursion)

    if duration is None:
        input(f"Press any key to recover linac pressure to {recover}")
    else:
        print(f"Sleeping {duration} seconds before recovering {name} to {recover} pressure")
        sleep(duration)

    walk_pv(pv, recover)


def heater_event(name: str, duration: float, recover: float, **kwargs):
    if name == '1L':
        pv = PV(f"{PREFIX}CAPHTRMGN")
    elif name == '2L':
        pv = PV(f"{PREFIX}CAPHTR2MGN")
    else:
        raise ValueError(f"Invalid name '{name}'")

    excursion = 0.5
    pv.put(excursion)

    if duration is None:
        input(f"Press any key to recover heater capacity to {recover}")
    else:
        print(f"Sleeping {duration} seconds before recovering {name} to {recover} heater capacity")
        sleep(duration)

    walk_pv(pv, recover)


def rf_fault_event(name: str, controls_type: str, duration: float, recover: Optional[float], **kwargs):
    if controls_type == '1.0':
        pv = PV(f"{PREFIX}{name}STAT.B3")
        excursion = 1
        if recover is None:
            recover = 0
    elif controls_type == 'new_2.0':
        pv = PV(f"{PREFIX}{name}FBRIO")
        excursion = 256
        if recover is None:
            recover = 768
    elif controls_type == 'old_2.0':
        pv = PV(f"{PREFIX}{name}FBRIO")
        excursion = 256
        if recover is None:
            recover = 972
    elif controls_type == '3.0':
        pv = PV(f"{PREFIX}{name}FBRIO")
        excursion = 256
        if recover is None:
            recover = 768
    else:
        raise ValueError(f"Unsupported controls_type '{controls_type}'")

    pv.put(excursion)

    if duration is None:
        input(f"Press any key to recover {name} fault to {pv.pvname} = {recover}")
    else:
        print(f"Sleeping {duration} seconds before recovering {name} fault to {pv.pvname} = {recover}")
        sleep(duration)

    pv.put(recover)


def tuner_event(name: str, controls_type: str, duration: float, recover: float, **kwargs):
    if controls_type == '1.0':
        pv = PV(f"{PREFIX}{name}TDETA")
        excursion = 20
    elif controls_type == '2.0':
        pv = PV(f"{PREFIX}{name}CFQE")
        excursion = 20
    elif controls_type == '3.0':
        pv = PV(f"{PREFIX}{name}CFQE")
        excursion = 20
    else:
        raise ValueError(f"Unsupported controls_type '{controls_type}'")

    pv.put(excursion)

    if duration is None:
        input(f"Press any key to recover {name} fault to {pv.pvname} = {recover}")
    else:
        print(f"Sleeping {duration} seconds before recovering {name} fault to {pv.pvname} = {recover}")
        sleep(duration)

    walk_pv(pv, recover)


def jt_event(name: str, duration: float, recover: float, **kwargs):
    pv = PV(f"{PREFIX}CEV{name}JT")

    excursion = 95
    pv.put(excursion)

    if duration is None:
        input(f"Press any key to recover JT valve position to {recover}")
    else:
        print(f"Sleeping {duration} seconds before recovering {name} to {recover} JT valve position")
        sleep(duration)

    walk_pv(pv, recover)


def walk_pv(pv: PV, value: float, time: float = 5.0, n_steps: int = 10):
    """Walk a PV to a target value using n_steps over a period of time."""
    start = pv.value
    step = (value - start) / n_steps

    for i in range(1, n_steps + 1):
        pv.put(start + (i * step))
        if i < n_steps:
            sleep(time / (n_steps - 1))

    # Just in case there were some rounding errors, put the PV to the desired setting
    pv.put(value)


def optional_float(value: str):
    if value == 'None':
        return None
    else:
        return float(value)


def main():
    parser = argparse.ArgumentParser(description="Trigger events that could disrupt data collection")
    subparsers = parser.add_subparsers(dest="command")

    pressure = subparsers.add_parser('pressure', help='Trigger a pressure excursion')
    heater = subparsers.add_parser('heater', help='Trigger a low heater capacity margin event')
    rf_fault = subparsers.add_parser('rf_fault', help='Trigger an RF fault')
    tuner = subparsers.add_parser('tuner', help='Trigger an cavity requires tuning event')
    jt = subparsers.add_parser('jt', help='Trigger a high JT valve event')

    pressure.add_argument('-n', '--name', help='linac name', required=True, type=str, choices=['1L', '2L'])
    pressure.add_argument('-l', '--level', help='High or low pressure', required=True, type=str,
                          choices=['high', 'low'])
    pressure.add_argument('-d', '--duration', help='How long in seconds until recovery', type=optional_float,
                          default=10)
    pressure.add_argument('-r', '--recover', help='What value to set pressure to after duration seconds',
                          default=0.0385, type=float)

    heater.add_argument('-n', '--name', help='linac name', required=True, type=str, choices=['1L', '2L'])
    heater.add_argument('-d', '--duration', help='How long in seconds until recovery', type=optional_float, default=10)
    heater.add_argument('-r', '--recover', help='What value to set heater capacity to after duration seconds',
                        default=6, type=float)

    rf_fault.add_argument('-n', '--name', help='cavity EPICS name (R123)', required=True, type=str)
    rf_fault.add_argument('-c', '--controls_type', help='cavity controls type', required=True, type=str,
                          choices=['1.0', '2.0', 'new_2.0', '3.0'])
    rf_fault.add_argument('-d', '--duration', help='How long in seconds until recovery', type=optional_float,
                          default=10)
    rf_fault.add_argument('-r', '--recover', help='Value to set fault bit too', type=optional_float,
                          default=None)

    tuner.add_argument('-n', '--name', help='cavity EPICS name (R123)', required=True, type=str)
    tuner.add_argument('-c', '--controls_type', help='cavity controls type', required=True, type=str,
                       choices=['1.0', '2.0', '3.0'])
    tuner.add_argument('-d', '--duration', help='How long in seconds until recovery', type=optional_float, default=10)
    tuner.add_argument('-r', '--recover', help='What value to set detune PV to after duration seconds',
                       default=0, type=float)

    jt.add_argument('-n', '--name', help='Zone CED name (1L02)', required=True, type=str)
    jt.add_argument('-d', '--duration', help='How long in seconds until recovery', type=optional_float, default=10)
    jt.add_argument('-r', '--recover', help='What value to set JT position to after duration seconds',
                    default=80, type=float)

    args = parser.parse_args()

    if args.command == "pressure":
        pressure_event(**vars(args))
    elif args.command == "heater":
        heater_event(**vars(args))
    elif args.command == "rf_fault":
        rf_fault_event(**vars(args))
    elif args.command == "tuner":
        tuner_event(**vars(args))
    elif args.command == "jt":
        jt_event(**vars(args))


if __name__ == "__main__":
    main()
