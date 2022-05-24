import logging
import time
from typing import List, Dict

from network import SSLContextAdapter
import threading
import requests
import os
from epics import PV, caput_many
import numpy as np

logging.basicConfig(level=logging.DEBUG)

# Master set of available PVs.  It's global, so be careful.
PVs = {}
fe_onset = {}
prefix = "adamc:"
fe_active = {}  # Is a cavity field emitting? [str(pvname), bool].  Managed by gset_cb

# Gets changed in main and callback threads
gc_lock = threading.Lock()
gset_changed = False  # Has an RF PV change over last iteration?  We'll use this to update radiation PVs


def gset_cb(pvname, value, **kwargs):
    if pvname not in fe_onset.keys():
        return

    # Register the change
    global gc_lock
    global gset_changed
    with gc_lock:
        gset_changed = True

    # Only log if this is a state change for the cavity
    if value > fe_onset[pvname]:
        if pvname not in fe_active.keys() or not fe_active[pvname]:
            logging.debug(f"{pvname} is ACTIVE (GSET: {value}, Onset: {fe_onset[pvname]})")
        fe_active[pvname] = True
    else:
        if pvname not in fe_active.keys() or fe_active[pvname]:
            logging.debug(f"{pvname} is DEACTIVE (GSET: {value}, Onset: {fe_onset[pvname]})")
        fe_active[pvname] = False


def save_pid():
    directory = os.path.dirname(os.path.realpath(__file__))
    with open(f"{directory}/pid.txt", mode="a") as f:
        f.write(f"{str(os.getpid())}\n")


def setup_ndx() -> None:
    for i in ['1L05', '1L06', '1L07', '1L08', '1L11', '1L21', '1L22', '1L23', '1L24', '1L25', '1L26', '1L27', '1S01',
              '1S02']:
        pv_name = f"{prefix}INX{i}_nCur"
        PVs[pv_name] = PV(pv_name)
        pv_name = f"{prefix}INX{i}_gCur"
        PVs[pv_name] = PV(pv_name)
    for i in ['1L05', '1L07', '1L11', '1L21', '1L23', '1L25', '1L27']:
        pv_name = f"{prefix}NDX{i}_CAPACITOR_SW"
        PVs[pv_name] = PV(pv_name)
        pv_name = f"{prefix}NDX{i}_PERIOD"
        PVs[pv_name] = PV(pv_name)


def setup_cavities() -> None:
    """Creates cavities from CED data and adds to linac and zone.  Expects _setup_zones to have been run."""
    ced_params = 't=CryoCavity&p=EPICSName&p=CavityType&p=MaxGSET&p=OpsGsetMax&p=Bypassed&p=Length&p=Housed_by' \
                 '&out=json'
    ced_url = f"http://ced.acc.jlab.org/inventory?{ced_params}"
    cavity_elements = get_ced_elements(ced_url=ced_url)

    pv_list = []
    val_list = []

    for elem in cavity_elements:

        # We only want to focus on the NL
        if elem['name'].startswith("0L"):
            continue
        if elem['name'].startswith("2L"):
            continue

        epics_name = elem['properties']['EPICSName']
        max_gset = elem['properties']['MaxGSET']
        cavity_type = elem['properties']['CavityType']
        if 'OpsGsetMax' in elem['properties'].keys():
            max_gset = elem['properties']['OpsGsetMax']

        # Set the ODVH records to the CED values
        pv_name = f"{prefix}{epics_name}ODVH"
        pv_list.append(pv_name)
        val_list.append(max_gset)

        # Set the cavities to RF On state.  C100s/C75s are RFONr.  C25s/C50s are a weird bitword thing.
        pv_name = f"{prefix}{epics_name}RFONr"
        pv_list.append(pv_name)
        val_list.append(1)

        # MBBI. ACK1.B6 is zone RF on, so assign 64 (=2^6) to set B6 to 1 (B# is zero-indexed)
        pv_name = f"{prefix}{epics_name}ACK1"
        pv_list.append(pv_name)
        val_list.append(64)

        # Set the cavities up so that they are at the minimum stable gradient for operations
        pv_name = f"{prefix}{epics_name}GSET"
        pv_list.append(pv_name)
        if cavity_type == "C100":
            val_list.append(10)
        elif cavity_type == "C75":
            val_list.append(5)
        elif cavity_type == "C50":
            val_list.append(3)
        elif cavity_type == "C25":
            val_list.append(3)
        elif cavity_type == "P1R":
            val_list.append(5)

        # Setup a callback that will make radiation signal appear above FE onset
        PVs[pv_name] = PV(pv_name)
        PVs[pv_name].add_callback(gset_cb)
        fe_onset[pv_name] = max(float(max_gset) - 1, 7)  # FE onset here is simply one less than max gradient

    caput_many(pv_list, val_list, wait=True)
    print("RF PVs Done!")


def get_ced_elements(ced_url: str) -> List[Dict]:
    """Queries the CED with the supplied URL.  URL MUST include out=json argument."""

    with requests.Session() as s:
        adapter = SSLContextAdapter()
        s.mount(ced_url, adapter)
        r = s.get(ced_url)

    if r.status_code != 200:
        raise ValueError(
            "Received error response from {}.  status_code={}.  response={}".format(ced_url, r.status_code, r.text))

    # The built-in JSON decoder will raise a ValueError if parsing non-JSON content
    out = r.json()
    if out['stat'] != 'ok':
        raise ValueError("Received non-ok status response")

    return out['Inventory']['elements']


def update_ndx(force_change):
    global gset_changed
    global gc_lock
    with gc_lock:
        if gset_changed or force_change:
            gset_changed = False
            num_active = 0
            noise = np.random.uniform(0, 0.01)

            # Sometimes the unit tests want to mess directly with the gCur/nCur values.  Only update if the GSET was
            # recently changed.  Make sure to mark the "recently changed" flag to false after we process it.
            for cav in fe_active.keys():
                if fe_active[cav]:
                    num_active += 1

            for pv in PVs.values():
                if pv.connected:
                    if pv.pvname.endswith("Cur"):
                        pv.value = num_active + noise


if __name__ == "__main__":
    save_pid()
    setup_ndx()
    setup_cavities()
    count = 0
    while True:
        # Make this slow enough so my unit tests have a chance to make some changes without
        # being overwritten.  0.01 was just a little too fast

        time.sleep(0.05)
        if count % 20 == 0:
            update_ndx(force_change=True)
        else:
            update_ndx(force_change=False)
        count += 1
