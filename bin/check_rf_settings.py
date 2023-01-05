#!/usr/csite/pubtools/bin/python3.7

import re
import sys
import subprocess
from datetime import datetime, timedelta
from io import StringIO
from typing import List

import pandas as pd


def print_help():
    print("Show GSET and PSET PVs that are different at the two given times.  NL and SL only.")
    print("requires two arguments - start and end times")
    print("in Y-m-d H:M:S format (e.g. '2022-01-28 03:15:45')")
    print(f"\nEx: {sys.argv[0]} '2022-01-15 00:15:45' '2022-01-15 03:05:46'")


def get_pvs(linac: str):
    """linac should be 1 or 2"""
    psets = [f"R{linac}{z}{c}PSET" for z in '23456789ABCDEFGHIJKLMNOP' for c in '12345678']
    gsets = [f"R{linac}{z}{c}GSET" for z in '23456789ABCDEFGHIJKLMNOP' for c in '12345678']
    return psets, gsets


def get_pv_values(date: datetime, psets: List[str], gsets: List[str], fmt: str):
    s1_string = datetime.strftime(date, fmt)
    s2_string = datetime.strftime(date + timedelta(seconds=1), fmt)
    process = subprocess.run(['myData', '-b', s1_string, '-e', s2_string] + psets + gsets,
                             stdout=subprocess.PIPE, universal_newlines=True)
    out = re.sub(' +', ' ', process.stdout)
    df = pd.read_csv(StringIO(out), sep=' ').iloc[:, 2:]
    return df


def print_pv_changes(start: datetime, end: datetime, linac: int, fmt: str):
    psets, gsets = get_pvs(linac)
    sdf = get_pv_values(start, psets, gsets, fmt=fmt)
    edf = get_pv_values(end, psets, gsets, fmt=fmt)

    for i in range(len(sdf.columns)):
        if sdf.iloc[0, i] != edf.iloc[0, i]:
            print(f"{sdf.columns[i]}: {sdf.iloc[0, i]} => {edf.iloc[0, i]}")


def main():
    fmt = '%Y-%m-%d %H:%M:%S'

    if len(sys.argv) != 3:
        print_help()
        exit(1)

    try:
        start = datetime.strptime(sys.argv[1], fmt)
        end = datetime.strptime(sys.argv[2], fmt)
    except Exception as ex:
        print(f"Error parsing start and end time inputs:\n{ex}")
        exit(1)

    print("======= North Linac ==========")
    print_pv_changes(start, end, linac=1, fmt=fmt)

    print("\n\n======= South Linac ==========")
    print_pv_changes(start, end, linac=2, fmt=fmt)


if __name__ == '__main__':
    main()
