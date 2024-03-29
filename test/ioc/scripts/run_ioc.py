import logging
import time
from datetime import datetime, timedelta
from typing import List, Dict

import epics
from fe_daq.network import SSLContextAdapter
import threading
import requests
import os
from epics import PV, caput_many
import numpy as np

logging.basicConfig(level=logging.DEBUG)

# This is supposed to run a local development softIOC.  Don't try to read/write anywhere else.
os.environ['EPICS_CA_ADDR_LIST'] = 'localhost'

# Master set of available PVs.  It's global, so be careful.
PVs = {}
fe_onset = {}
prefix = "adamc:"
fe_active = {}  # Is a cavity field emitting? [str(pvname), bool].  Managed by gset_cb
JT_valves = {}  # type: Dict[str, JTValve]

# Gets changed in main and callback threads.  Use a lock to ensure that we get consistent sets of PVs during callbacks
# or other functions.  This looks unnecessary at first glance.
gc_lock = threading.Lock()
gset_changed = False  # Has an RF PV change over last iteration?  We'll use this to update radiation PVs
gmes_changed = {}  # A dictionary of the cavities that have had a gradient change and the new gradient
cavity_lengths = {}  # Used to track the length of each cavity by name

# How the largest amount of noise in the gradient readback
max_gradient_noise = 0.05


def rf_zone_to_ced_zone(zone):
    names = {
        'R12': '1L02', 'R13': '1L03', 'R14': '1L04', 'R15': '1L05', 'R16': '1L06', 'R17': '1L07', 'R18': '1L08',
        'R19': '1L09', 'R1A': '1L10', 'R1B': '1L11', 'R1C': '1L12', 'R1D': '1L13', 'R1E': '1L14', 'R1F': '1L15',
        'R1G': '1L16', 'R1H': '1L17', 'R1I': '1L18', 'R1J': '1L19', 'R1K': '1L20', 'R1L': '1L21', 'R1M': '1L22',
        'R1N': '1L23', 'R1O': '1L24', 'R1P': '1L25', 'R1Q': '1L26',
        'R22': '2L02', 'R23': '2L03', 'R24': '2L04', 'R25': '2L05', 'R26': '2L06', 'R27': '2L07', 'R28': '2L08',
        'R29': '2L09', 'R2A': '2L10', 'R2B': '2L11', 'R2C': '2L12', 'R2D': '2L13', 'R2E': '2L14', 'R2F': '2L15',
        'R2G': '2L16', 'R2H': '2L17', 'R2I': '2L18', 'R2J': '2L19', 'R2K': '2L20', 'R2L': '2L21', 'R2M': '2L22',
        'R2N': '2L23', 'R2O': '2L24', 'R2P': '2L25', 'R2Q': '2L26'

    }
    return names[zone]


def ced_zone_to_rf_zone(zone):
    names = {
        '1L02': 'R12', '1L03': 'R13', '1L04': 'R14', '1L05': 'R15', '1L06': 'R16', '1L07': 'R17', '1L08': 'R18',
        '1L09': 'R19', '1L10': 'R1A', '1L11': 'R1B', '1L12': 'R1C', '1L13': 'R1D', '1L14': 'R1E', '1L15': 'R1F',
        '1L16': 'R1G', '1L17': 'R1H', '1L18': 'R1I', '1L19': 'R1J', '1L20': 'R1K', '1L21': 'R1L', '1L22': 'R1M',
        '1L23': 'R1N', '1L24': 'R1O', '1L25': 'R1P', '1L26': 'R1Q',
        '2L02': 'R22', '2L03': 'R23', '2L04': 'R24', '2L05': 'R25', '2L06': 'R26', '2L07': 'R27', '2L08': 'R28',
        '2L09': 'R29', '2L10': 'R2A', '2L11': 'R2B', '2L12': 'R2C', '2L13': 'R2D', '2L14': 'R2E', '2L15': 'R2F',
        '2L16': 'R2G', '2L17': 'R2H', '2L18': 'R2I', '2L19': 'R2J', '2L20': 'R2K', '2L21': 'R2L', '2L22': 'R2M',
        '2L23': 'R2N', '2L24': 'R2O', '2L25': 'R2P', '2L26': 'R2Q'

    }
    return names[zone]


def gset_cb(pvname, value, **kwargs):
    if pvname not in fe_onset.keys():
        return

    # Register the change
    global gc_lock
    global gset_changed
    with gc_lock:
        gset_changed = True
        gmes_changed[f"{pvname[0:(len(prefix) + 4)]}GMES"] = value

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
    for i in ['1L05', '1L06', '1L07', '1L08', '1L11', '1L15', '1L16', '1L21', '1L22', '1L23', '1L24', '1L25', '1L26', '1L27', '1S01',
              '1S02', '2L22', '2L23', '2L24', '2L25', '2L26', '2L27', '2S01', '2S02']:
        pv_name = f"{prefix}INX{i}_nCur"
        PVs[pv_name] = PV(pv_name)
        pv_name = f"{prefix}INX{i}_gCur"
        PVs[pv_name] = PV(pv_name)
    for i in ['1L05', '1L07', '1L11', '1L15', '1L16', '1L21', '1L23', '1L25', '1L27', '2L21', '2L23', '2L25', '2L27']:
        pv_name = f"{prefix}NDX{i}_CAPACITOR_SW"
        PVs[pv_name] = PV(pv_name)
        pv_name = f"{prefix}NDX{i}_PERIOD"
        PVs[pv_name] = PV(pv_name)


def append_pairs(names: List[str], values: List[float], name: str, value: float):
    """Append a name and value to arrays in the same order."""
    names.append(name)
    values.append(value)


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

        bypassed = False
        if 'Bypassed' in elem['properties'].keys():
            bypassed = True
        epics_name = elem['properties']['EPICSName']
        max_gset = elem['properties']['MaxGSET']
        cavity_type = elem['properties']['CavityType']
        if 'OpsGsetMax' in elem['properties'].keys():
            max_gset = elem['properties']['OpsGsetMax']
        max_gset = float(max_gset)
        cavity_lengths[epics_name] = float(elem['properties']['Length'])

        # Set the ODVH records to the CED values
        append_pairs(pv_list, val_list, f"{prefix}{epics_name}ODVH", max_gset)

        # Set the cavities to RF On state.  C100s/C75s are RFONr.  C25s/C50s are a weird bitword thing.
        append_pairs(pv_list, val_list, f"{prefix}{epics_name}RFONr", 1)

        # MBBI. ACK1.B6 is zone RF on, so assign 64 (=2^6) to set B6 to 1 (B# is zero-indexed)
        append_pairs(pv_list, val_list, f"{prefix}{epics_name}ACK1", 64)

        # FE onset here is simply one less than max gradient
        fe_onset[f"{prefix}{epics_name}GSET"] = max(float(max_gset) - 1, 7)

        if cavity_type == "C100":
            # Gradient PVs
            gradient = 0 if bypassed else np.random.uniform(5, max_gset)
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}GSET", gradient)
            gmes = gradient + np.random.uniform(0, max_gradient_noise, 1)[0]
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}GMES", gmes)

            # Tuner info
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}CFQE", 0)
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}DETAHZHI", 10)
        elif cavity_type == "C75":
            # Gradient PVs
            gradient = 0 if bypassed else np.random.uniform(5, max_gset)
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}GSET", gradient)
            gmes = gradient + np.random.uniform(0, max_gradient_noise, 1)[0]
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}GMES", gmes)

            # Tuner info
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}CFQE", 0)
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}DETAHZHI", 10)
        elif cavity_type == "C50":
            # Gradient PVs
            gradient = 0 if bypassed else np.random.uniform(3, max_gset)
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}GSET", gradient)
            gmes = gradient + np.random.uniform(0, max_gradient_noise, 1)[0]
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}GMES", gmes)
            # val_list.append(0 if bypassed else np.random.uniform(3, max_gset))

            # Tuner info
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}TDETA", 0)
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}TDETA_N", 10)
        elif cavity_type == "C25":
            # Gradient PVs
            gradient = 0 if bypassed else np.random.uniform(3, max_gset)
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}GSET", gradient)
            gmes = gradient + np.random.uniform(0, max_gradient_noise, 1)[0]
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}GMES", gmes)
            # val_list.append(0 if bypassed else np.random.uniform(3, max_gset))

            # Tuner info
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}TDETA", 0)
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}TDETA_N", 10)
        elif cavity_type == "P1R":
            # Gradient PVs
            gradient = 0 if bypassed else np.random.uniform(5, max_gset)
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}GSET", gradient)
            gmes = gradient + np.random.uniform(0, max_gradient_noise, 1)[0]
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}GMES", gmes)
            # val_list.append(0 if bypassed else np.random.uniform(5, max_gset))

            # Tuner info
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}CFQE", 0)
            append_pairs(pv_list, val_list, f"{prefix}{epics_name}DETAHZHI", 10)

    # Setup a callback that will make radiation signal appear above FE onset
    logging.debug("Creating Cavity PVs")
    for pv_name in pv_list:
        PVs[pv_name] = PV(pv_name)
    logging.debug("Finished Creating Cavity PVs")

    # We don't want to attached the gset_cb to this.
    # pv_name = f"{prefix}{epics_name}GMES"
    # PVs[pv_name] = PV(pv_name)
    # pv_list.append(pv_name)
    # val_list.append(gradient + np.random.uniform(0, max_gradient_noise, 1)[0])

    logging.debug("Initializing Cavity PVs")
    time.sleep(0.05)
    for pv_name in pv_list:
        if pv_name.endswith("GSET"):
            PVs[pv_name].add_callback(gset_cb)
    caput_many(pv_list, val_list, wait=True)
    time.sleep(0.05)
    logging.debug("Finished Initializing Cavity PVs")


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


def update_gmes():
    global gmes_changed
    global gc_lock
    with gc_lock:
        for gmes_pv_name in gmes_changed.keys():
            PVs[gmes_pv_name].value = gmes_changed[gmes_pv_name] + np.random.uniform(0, max_gradient_noise, 1)[0]
        gmes_changed = {}


class JTValve:

    def __init__(self, zone: str):
        self.zone = zone
        self.recovery_datetime = None
        # This should be the ORBV field and not VAL in production.  But I can't figure out how to write to that
        # directly during testing.  I think the ORBV field is read only and requires device driver support.
        self.stroke_pv = PV(f"{prefix}CEV{zone}JT")
        self.alarm = False
        PVs[self.stroke_pv.pvname] = self.stroke_pv
        if not self.stroke_pv.wait_for_connection(timeout=1):
            logging.error(f"{self.stroke_pv.pvname}: Timed out while waiting on connection.")

    def set_jt_high(self):
        self.recovery_datetime = datetime.now() + timedelta(seconds=5)
        self.stroke_pv.put(95.1, wait=False)
        self.alarm = True
        logging.warning(f"{self.zone} JT valve went high.")

    def check_jt_recovery(self):
        if self.alarm:
            if datetime.now() > self.recovery_datetime:
                self.alarm = False
                self.stroke_pv.put(np.random.uniform(40, 90))
                logging.warning(f"{self.zone} JT valve back in spec.")

def update_emes():
    for linac in '12':
        epicsnames = [name for name in sorted(cavity_lengths.keys()) if name[1] == linac]
        pvs = [f"{prefix}{epicsname}GMES" for epicsname in epicsnames]
        values = epics.caget_many(pvs)
        lengths = []
        for epicsname in epicsnames:
            lengths.append(cavity_lengths[epicsname])
        lengths = np.array(lengths)
        emes = np.sum(values * lengths)
        emes_pv = PV(f"{prefix}R{linac}XXEMES")
        emes_pv.put(emes)



def setup_jt_valves():
    logging.debug("Setting Up JT Valves")
    global JT_valves
    for linac in ['1L', '2L']:
        for zone in ['02', '03', '04', '05', '06', '07', '08', '09',
                     '10', '11', '12', '13', '14', '15', '16', '17', '18', '19',
                     '20', '21', '22', '23', '24', '25', '26']:
            z = f"{linac}{zone}"
            JT_valves[z] = JTValve(zone=z)
    logging.debug("Done setting Up JT Valves")


if __name__ == "__main__":
    save_pid()
    setup_ndx()
    setup_cavities()
    setup_jt_valves()
    count = 0
    p_jt_high = 0
    # p_jt_high = 1e-5

    logging.debug("Entering main run loop.")
    while True:
        # Make this slow enough so my unit tests have a chance to make some changes without
        # being overwritten.  0.01 was just a little too fast
        time.sleep(0.05)

        if len(gmes_changed.keys()) > 0:
            update_gmes()
            update_emes()

        if count % 20 == 0:
            update_ndx(force_change=True)
        else:
            update_ndx(force_change=False)
        count += 1

        for jt in JT_valves.values():
            if jt.alarm:
                jt.check_jt_recovery()
            # 25 valves * 20 chances per second * 5 second duration * prob_trip_every_step,
            # implies that 1/10000 prob there will be 0.025 JT valves too high on average.  The math is not the exact
            # right formula, but good enough for testing without cracking open a book.
            elif np.random.uniform(0, 1) > (1 - p_jt_high):
                jt.set_jt_high()
