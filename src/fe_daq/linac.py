import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
import requests
import epics

from fe_daq.cavity import Cavity
from fe_daq import app_config as config
from fe_daq.detector import NDXDetector, NDXElectrometer
from fe_daq.network import SSLContextAdapter
from fe_daq.state_monitor import StateMonitor, connection_cb, get_threshold_cb

logger = logging.getLogger(__name__)

test_prefix = "adamc:"


class Linac:
    def __init__(self, name: str, prefix: str, linac_pressure_min: float, linac_pressure_max: float,
                 linac_pressure_recovery_margin: float, heater_margin_min: float, heater_recovery_margin: float):
        self.name = name
        self.zones = {}
        self.cavities = {}
        self.ndx_detectors = {}
        self.ndx_electrometers = {}
        self.epics_lock = threading.Lock()
        self.linac_pressure_max = linac_pressure_max
        self.linac_pressure_min = linac_pressure_min
        self.lp_recovery_margin = linac_pressure_recovery_margin
        self.heater_margin_min = heater_margin_min
        self.heater_recovery_margin = heater_recovery_margin

        if name == "NorthLinac":
            self.linac_pressure = epics.PV(f"{prefix}CPI4107B", connection_callback=connection_cb)
            self.heater_margin = epics.PV(f"{prefix}CAPHTRMGN", connection_callback=connection_cb)
            self.autoheat_mode = epics.PV(f"{prefix}CAPBON", connection_callback=connection_cb)
        elif name == "SouthLinac":
            self.linac_pressure = epics.PV(f"{prefix}CPI5107B", connection_callback=connection_cb)
            self.heater_margin = epics.PV(f"{prefix}CAP2HTRMGN", connection_callback=connection_cb)
            self.autoheat_mode = epics.PV(f"{prefix}CAP2BON", connection_callback=connection_cb)
        else:
            raise ValueError(f"Unsupported linac name '{name}'")

        # We need to watch and make sure that we don't exceed linac pressure or heater margin requirements for stable
        # operations.  Nominal linac pressure is 0.0385.  Probably want marginal heater capacity at least > 2.
        self.linac_pressure.add_callback(get_threshold_cb(low=self.linac_pressure_min, high=self.linac_pressure_max))
        self.heater_margin.add_callback(get_threshold_cb(low=self.heater_margin_min))
        # We want to make sure that the autoheaters stay on.  Otherwise when we change gradients, the heaters won't
        # compensate.  1 == on.
        self.autoheat_mode.add_callback(get_threshold_cb(low=1, high=1))

        self.pv_list = [self.linac_pressure, self.heater_margin, self.autoheat_mode]

    def wait_for_connections(self, timeout: float = 2.0):
        """Wait for all of the PVs associated with this Linac to connect.  Raise exception if that doesn't happen."""
        # Ensure that we are connected
        for pv in self.pv_list:
            if not pv.connected:
                if not pv.wait_for_connection(timeout=timeout):
                    raise Exception(f"PV {pv.pvname} failed to connect.")


    def check_linac_pressure(self, min_val: Optional[float] = None, max_val: Optional[float] = None) -> Tuple[bool, float]:
        """Check that the linac pressue is not too high.  Use self.linac_pressure_max if threshold is None.

        Thread-safe.
        """
        maximum = max_val
        if maximum is None:
            if self.linac_pressure_max is None:
                raise RuntimeError("No max value given for linac pressure, and default is None.")
            maximum = self.linac_pressure_max
        minimum = min_val
        if minimum is None:
            if self.linac_pressure_min is None:
                raise RuntimeError("No min value given for linac pressure, and default is None.")
            minimum = self.linac_pressure_min

        # Not sure that we need to synchronize here, but it's better safe than sorry.
        with self.epics_lock:
            value = self.linac_pressure.value
        out_of_spec = False
        if value > maximum:
            out_of_spec = True
        elif value < minimum:
            out_of_spec = True

        return out_of_spec, value

    def check_heater_margin(self, threshold: Optional[float] = None) -> Tuple[bool, float]:
        """Check that the heater margin is not too low.  Use self. if threshold is None.  Thread-safe."""
        minimum = threshold
        if threshold is None:
            if self.heater_margin_min is None:
                raise RuntimeError("No min value given for heater capacity margin, and default is None.")
            minimum = self.heater_margin_min

        # Not sure that we need to synchronize here, but it's better safe than sorry.
        with self.epics_lock:
            value = self.heater_margin.value
        above = False
        if value <= minimum:
            above = True

        return above, value


    def add_cavity(self, cavity: Cavity):
        """Add a cavity to a linac and it's zone as needed"""

        # Add the cavity if needed
        if cavity.name not in self.cavities.keys():
            self.cavities[cavity.name] = cavity

        # Make sure we have the right zone for the cavity, then add it
        if cavity.zone.name not in self.zones.keys():
            raise ValueError("Trying to insert cavity into unrecognized zone")
        self.zones[cavity.zone.name].add_cavity(cavity)

    def jiggle_psets(self, delta: float):
        """Jiggle PSET values for all cavities in the linac about their starting point. Skip cavities with problems."""

        pvlist = []
        values = []
        for cavity in self.cavities.values():
            if not cavity.bypassed and not cavity.tuner_bad:
                pvlist.append(cavity.pset.pvname)
                values.append(cavity.get_jiggled_pset_value(delta=delta))

        logger.info(f"Jiggling PSETs for {pvlist}")
        epics.caput_many(pvlist, values, wait=True)
        logger.info("PSETs jiggled")

    def restore_psets(self):
        """Put all of the PSETs back.  Even the ones on cavities with broken tuners, etc.

        The only reason the PSET would change on a cavity with a bad tuner is if we made a mistake and changed it during
        this applicaiton.  It's better to put it back than to leave it in a bad place and hope that an operator sees
        a warning message."""
        for cavity in self.cavities.values():
            cavity.restore_pset()

    def get_radiation_measurements(self, num_samples=3, integration_time=1):
        """Save samples from all NDX detectors at roughly 1 Hz.  Clears measurements first"""

        for d in self.ndx_detectors.values():
            d.clear_measurements()

        # This will request user input if something goes wrong on the EPICS or RF side
        StateMonitor.check_state()

        # We have to wait for the integration time to be up before the detectors will reflect recent changes in GSET.
        # Add just a tiny bit more to make sure we get good data.
        time.sleep(integration_time + 0.05)

        # Want to sample once per second.  Some time will be taken up by the EPICS communication.  Try to account for
        # that.
        start = datetime.now()

        # clear any stale data, and take the first measurements on each detector
        for d in self.ndx_detectors.values():
            d.clear_measurements()
            d.take_measurement()

        for i in range(num_samples - 1):
            # Try to be smart about sleeping.  There is a basic attempt to account for time lost to EPICS, etc.
            end = start + timedelta(seconds=1)
            sleep_duration = (end - datetime.now()).total_seconds()

            # These PVs update at 1 Hz.  Don't sleep longer than 1 Hz, and don't try to sleep at all if EPICS took too
            # long.
            if sleep_duration > 1:
                logger.warning("get_radiation_measurements is trying to sleep more than one second.  Capping at one.")
                sleep_duration = 1
            if sleep_duration > 0:
                # It's possible that StateMonitor or EPICS held things up.  Only sleep if end is in the future.
                time.sleep(sleep_duration)

            # Update the start for the next loop
            start = datetime.now()

            # Check state and take the next set of measurements.
            StateMonitor.check_state()
            for d in self.ndx_detectors.values():
                d.take_measurement()

    def save_radiation_measurements_as_background(self):
        for ndxd in self.ndx_detectors.values():
            ndxd.update_background()

    def set_ndx_for_fe_onset(self):
        logger.info(f"{self.name} setting NDXElectrometers for FE onset")
        for ndxe in self.ndx_electrometers.values():
            ndxe.set_for_fe_onset()

    def set_ndx_for_operations(self):
        logger.info(f"{self.name} setting NDXElectrometers for operations")
        for ndxe in self.ndx_electrometers.values():
            ndxe.set_for_operations()

    def is_radiation_above_background(self, t_stat_threshold: float = 5) -> Tuple[bool, float, NDXDetector]:
        """Check all fo the NDX detectors for sign of radiation.

        Each individual signal is checked.  If any one of them reports a t-stat > t_stat_threshold, return true.
        Otherwise, return False.
        """
        max_detector = None
        is_rad = False
        max_t = float("-inf")
        for ndxd in self.ndx_detectors.values():
            ir, t_stat = ndxd.is_radiation_above_background(t_stat_threshold=t_stat_threshold)
            if max_t < t_stat:
                max_t = t_stat
                max_detector = ndxd
            is_rad = is_rad or ir

        return is_rad, max_t, max_detector


class Zone:
    def __init__(self, name: str, linac: Linac, controls_type: str, jt_max: float, prefix: str = "",
                 jt_suffix: str = ".ORBV", jt_recovery_margin: float = 2):
        self.name = name
        self.linac = linac
        self.cavities = {}
        self.epics_lock = threading.Lock()

        # J. Benesch suggest value for maximum OK JT valves position.  Beyond which we stop doing anything and see what
        # has gone awry.
        self.jt_max = jt_max
        self.jt_recovery_margin = jt_recovery_margin

        supported_controls = ('1.0', '2.0', '3.0')
        if controls_type not in supported_controls:
            raise ValueError(f"{name} has unsupported controls type '{controls_type}'")
        self.controls_type = controls_type

        # The JT strove PV is normally a ORBV field which is a read only field.  During testing we work with the VAL
        # field.
        self.jt_stroke = epics.PV(f"{prefix}CEV{name}JT{jt_suffix}", connection_callback=connection_cb)
        self.jt_stroke.add_callback(callback=get_threshold_cb(high=self.jt_max))

        self.pv_list = [self.jt_stroke]

    def add_cavity(self, cavity: Cavity):
        # Add the cavity to the zone if needed
        if cavity.name not in self.cavities.keys():
            self.cavities[cavity.name] = cavity

    def check_percent_heat_change(self, gradients: List[Optional[float]], percentage: float = 10.0) \
            -> Tuple[float, float, float]:
        """Raises exception if the supplied new gradients will cause too large a percent change in cryomodule heat.

        Returns the percent change, the new heat and the old heat as a tuple.
        """
        if len(gradients) != 8:
            raise ValueError("Must supply eight gradients.  Use None for no change.")
        old_heat = 0
        new_heat = 0
        for idx, gradient in enumerate(gradients):
            cav = self.cavities[f"{self.name}-{idx + 1}"]
            old_heat += cav.calculate_heat()
            if gradient is not None:
                new_heat += cav.calculate_heat(gradient)
            else:
                new_heat += cav.calculate_heat()

        rel_change = (new_heat - old_heat) / old_heat * 100
        if abs(rel_change) > percentage:
            raise RuntimeError(f"{self.name}: Gradients will change heat too much. {old_heat}W -> {new_heat}W "
                               f"({round(rel_change, 1)}%)")

        return rel_change, new_heat, old_heat

    def check_jt_valve(self, threshold: Optional[float] = None) -> Tuple[bool, float]:
        """Check if the JT valve is beyond the given threshold.  If None, use self.jt_max.  Thread-safe."""
        with self.epics_lock:
            maximum = threshold
            if threshold is None:
                if self.jt_max is None:
                    raise RuntimeError("No max value given for jt valve position, and default is None.")
                maximum = self.jt_max

            value = self.jt_stroke.value
            above = False
            if value >= maximum:
                above = True

            return above, value

    def wait_for_connections(self, timeout: float = 2.0):
        """Wait for all of the PVs associated with this Zone to connect.  Raise exception if that doesn't happen."""
        # Ensure that we are connected
        for pv in self.pv_list:
            if not pv.connected:
                if not pv.wait_for_connection(timeout=timeout):
                    raise Exception(f"PV {pv.pvname} failed to connect.")

    # def set_gradients(self, exclude_cavs: List[Cavity] = None, level: str = "low") -> None:
    #     """Set the cavity gradients high/low for cavities in the zone, optionally excluding some cavities
    #
    #     Arguments:
    #         exclude_cavs: A list of cavities that should not be changed.  None if all cavities should be changed
    #         level:  'low' for their defined low level, 'high' for close to ODVH
    #
    #     """
    #
    #     # We'll use the put_many call since we're dealing with multiple PVs
    #     pvlist = []
    #     values = []
    #     for cav in self.cavities.values():
    #
    #         StateMonitor.check_state()
    #         # Check if we are excluding this cavity from change
    #         if exclude_cavs is not None:
    #             skip = False
    #             for ex_cav in exclude_cavs:
    #                 if cav.name == ex_cav.name:
    #                     skip = True
    #             if skip:
    #                 continue
    #
    #         # Get the low/high level we want to set the cavity to
    #         if level == "high":
    #             # For varying over the zone we want a tighter range since we're probably dealing with no trip models
    #             val = np.random.uniform(cav.odvh.get() - 2, cav.odvh.get())
    #         elif level == "low":
    #             val = cav.gset_min if cav.gset_no_fe is None else cav.gset_no_fe
    #         else:
    #             msg = "Unsupported level specified"
    #             logger.error(msg)
    #             raise ValueError(msg)
    #
    #         pvlist.append(cav.gset.pvname)
    #         values.append(val)
    #         logger.debug(f"Cav: {cav.name} ({cav.gset.pvname})  ODVH: {cav.odvh.get()}, GSET: {val}")
    #
    #     # Check that we're up prior to applying changes
    #     StateMonitor.check_state()
    #     epics.caput_many(pvlist, values, wait=True)


class LinacFactory:

    def __init__(self, ced_server="ced.acc.jlab.org", ced_instance='ced', ced_workspace="ops", testing=False):
        self.ced_server = ced_server
        self.ced_instance = ced_instance
        self.ced_workspace = ced_workspace
        self.testing = testing
        self.pv_prefix = ""
        self.jt_suffix = ".ORBV"

        if self.testing:
            self.pv_prefix = test_prefix
            self.jt_suffix = ""
            logger.info(f"Using PV prefix '{self.pv_prefix}', no JT '.ORBV' suffix")
        else:
            logger.info(f"Using no PV prefix, but JT PV '.ORBV' suffix.")

    def create_linac(self, name: str, zone_names: List[str] = None, electrometer_names: List[str] = None,
                     detector_names: List[str] = None):
        """Construct a Linac.  The name should match the Linac's Segmask name without the 'A_' prefix.
        
        Segmask name format  is NorthLinac, SouthLinac.
        """

        linac = Linac(name, prefix=self.pv_prefix, linac_pressure_min=config.get_parameter("linac_pressure_min"),
                      linac_pressure_max=config.get_parameter("linac_pressure_max"),
                      linac_pressure_recovery_margin=config.get_parameter("linac_pressure_margin"),
                      heater_margin_min=config.get_parameter("cryo_heater_margin_min"),
                      heater_recovery_margin=config.get_parameter("cryo_heater_margin_recovery_margin"))
        self._setup_zones(linac=linac, zone_names=zone_names)
        self._setup_cavities(linac)
        self._setup_ndx(linac, electrometer_names=electrometer_names, detector_names=detector_names)

        # Make sure that the linac PVs connect.  The _setup_* methods should do the same.
        linac.wait_for_connections()

        return linac

    def _setup_zones(self, linac, zone_names: List[str] = None) -> None:
        """Queries CED for zone information.  Constructs zones and adds them to Linac."""
        logging.info("Setting up zones from CED")
        ced_params = 't=Cryomodule&p=EPICSName&p=ModuleType&p=ControlsType&p=SegMask&out=json'
        ced_url = f"http://{self.ced_server}/inventory?ced={self.ced_instance}&workspace={self.ced_workspace}" \
                  f"&{ced_params}"
        zones = self._get_ced_elements(ced_url)
        for z in zones:
            zone_name = z['name']
            segmask = z['properties']['SegMask']
            controls_type = z['properties']['ControlsType']
            if linac.name in segmask:
                if (zone_names is None) or (zone_name in zone_names):
                    # Don't filter on zone_name unless a list was supplied
                    if zone_name not in linac.zones.keys():
                        # Add a zone if we haven't seen this before.
                        linac.zones[zone_name] = Zone(name=zone_name, linac=linac, controls_type=controls_type,
                                                      prefix=self.pv_prefix, jt_suffix=self.jt_suffix,
                                                      jt_max=config.get_parameter("jt_valve_position_max"),
                                                      jt_recovery_margin=config.get_parameter("jt_valve_margin"))

        for zone in linac.zones.values():
            zone.wait_for_connections()

    def _setup_cavities(self, linac: Linac, no_fe_file="./cfg/no_fe.tsv", fe_onset_file="./cfg/fe_onset.tsv") -> None:
        """Creates cavities from CED data and adds to linac and zone.  Expects _setup_zones to have been run."""
        logging.info("Setting up cavities from CED")
        ced_params = 't=CryoCavity&p=EPICSName&p=CavityType&p=MaxGSET&p=OpsGsetMax&p=Bypassed&p=Length&p=Housed_by' \
                     '&p=Q0&p=TunerBad&out=json'
        ced_url = f"http://{self.ced_server}/inventory?ced={self.ced_instance}&workspace={self.ced_workspace}" \
                  f"&{ced_params}"
        cavity_elements = self._get_ced_elements(ced_url=ced_url)

        no_fe = None
        if os.path.exists(no_fe_file):
            no_fe = {}
            with open(no_fe_file, mode="r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('#'):
                        continue
                    tokens = line.split('\t')
                    # This will grab the last value in the file if repeats exist.
                    if tokens[1] == "None":
                        no_fe[tokens[0]] = None
                    else:
                        no_fe[tokens[0]] = float(tokens[1])

        fe_onset = None
        if os.path.exists(no_fe_file):
            fe_onset = {}
            with open(fe_onset_file, mode="r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('#'):
                        continue
                    tokens = line.split('\t')
                    # This will grab the last value in the file if repeats exist.
                    if tokens[1] == "None":
                        fe_onset[tokens[0]] = None
                    else:
                        fe_onset[tokens[0]] = float(tokens[1])

        self._add_cavity_to_linac(cavity_elements, linac, prefix=self.pv_prefix, no_fe_gsets=no_fe,
                                  fe_onset_gsets=fe_onset)

    def _setup_ndx(self, linac: Linac, electrometer_names: List[str], detector_names: List[str] = None) -> None:
        """Creates NDX related objects from CED and adds them to the supplied Linac."""
        logging.info("Setting up NDX detectors from CED")

        # Grab the NDX Electrometers for the linac.  Then get the detectors that are associated with those.
        em_params = f"t=NDX_Electrometer&out=json&p=SegMask&p=Detectors&p=I400"
        ced_url = f"http://{self.ced_server}/inventory?ced={self.ced_instance}&workspace={self.ced_workspace}" \
                  f"&{em_params}"

        em_elements = self._get_ced_elements(ced_url=ced_url)
        for e in em_elements:
            if linac.name not in e['properties']['SegMask']:
                continue

            name = e['name']
            p = e['properties']

            # Only process electrometers that are requested (or all in nothing was specified
            if electrometer_names is None or name in electrometer_names:
                logger.info(f"Adding {name} to {linac.name}'s electrometers")

                target_hv = config.get_parameter(['ndx_target_high_voltage', name])
                #
                # if 'ndx_hv' in config.get_parameter(f'ndx_hv.{name}') and name in Config.config['ndx_hv']:
                #     target_hv = Config.config['ndx_hv'][name]

                ndxe = NDXElectrometer(name=name, epics_name=f"{self.pv_prefix}{name}", target_hv=target_hv,
                                       I400=p['I400'])
                linac.ndx_electrometers[name] = ndxe
                for d in p['Detectors'].values():
                    if len(d) > 0:
                        if detector_names is None or d in detector_names:
                            logger.info(f"Adding {name} to {linac.name}'s NDX detectors")
                            linac.ndx_detectors[d] = NDXDetector(name=d, epics_name=f"{self.pv_prefix}{d}",
                                                                 electrometer=ndxe)

            for electrometer in linac.ndx_electrometers.values():
                electrometer.wait_for_connections()

            for detector in linac.ndx_detectors.values():
                detector.wait_for_connections()

    @staticmethod
    def _get_ced_elements(ced_url: str) -> List[Dict]:
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

    @staticmethod
    def _add_cavity_to_linac(elements, linac, prefix=None, no_fe_gsets=None, fe_onset_gsets=None):
        logger.info("Creating cavities and adding them to linac")
        for e in elements:
            # Grab cavity properties
            p = e['properties']
            name = e['name']
            cavity_type = p['CavityType']
            epics_name = p['EPICSName']
            Q0 = float(p['Q0'])
            if prefix is not None or prefix != "":
                epics_name = f"{prefix}{epics_name}"
            zone = p['Housed_by']
            length = float(p['Length'])
            bypassed = True if 'Bypassed' in p.keys() else False
            tuner_bad = True if 'TunerBad' in p.keys() else False

            # Pull a new GSET limit from config file.
            gset_max = config.get_parameter(['gset_max', p['EPICSName']])

            if config.get_parameter(['skip_cavity', p['EPICSName']]) is not None:
                logger.info(f"Skipping {name} per configuration file.")
                continue

            gset_no_fe = None
            if no_fe_gsets is not None and epics_name in no_fe_gsets.keys():
                gset_no_fe = no_fe_gsets[epics_name]

            gset_fe_onset = None
            if fe_onset_gsets is not None and epics_name in fe_onset_gsets.keys():
                gset_fe_onset = fe_onset_gsets[epics_name]

            # Only add cavities that are in zones in this Linac
            if zone in linac.zones.keys():
                cavity = Cavity.get_cavity(name=name, epics_name=epics_name, cavity_type=cavity_type, length=length,
                                           bypassed=bypassed, Q0=Q0, zone=linac.zones[zone], gset_no_fe=gset_no_fe,
                                           gset_fe_onset=gset_fe_onset, gset_max=gset_max, tuner_bad=tuner_bad)
                linac.add_cavity(cavity)

        # Here we check that all cavity PVs are able to connect and run any initialization that happens after
        # PVs are connected.
        logger.info("Waiting for cavities to establish EPICS CA connections and setting gset_max.")
        for cavity in linac.cavities.values():
            cavity.wait_for_connections()
            cavity.update_gset_max()

        # Do a second loop to make sure that all of the connections and callbacks have had time to initialize
        for cavity in linac.cavities.values():
            cavity.run_callbacks()
        logger.info("Done waiting for EPICS CA connections.")
