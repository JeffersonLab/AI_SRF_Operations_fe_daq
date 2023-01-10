from fe_daq.linac import Linac, Zone
from fe_daq.cavity import LLRF2Cavity, LLRF3Cavity
from fe_daq import app_config as config


PREFIX = "adamc:"


def get_linac_zone_cavity(controls_type='2.0', style_2='old'):
    config.validate_config()
    lp_min = config.get_parameter('linac_pressure_min')
    lp_max = config.get_parameter('linac_pressure_max')
    lp_recovery_margin = config.get_parameter('linac_pressure_margin')
    heater_capacity_min = config.get_parameter('cryo_heater_margin_min')
    heater_recover_margin = config.get_parameter('cryo_heater_margin_recovery_margin')
    jt_max = config.get_parameter('jt_valve_position_max')
    jt_recovery_margin = config.get_parameter('jt_valve_margin')

    # TODO: Add LLRF 1.0
    if controls_type == '2.0':
        tuner_recovery_margin = config.get_parameter('LLRF2_tuner_recovery_margin')
        linac = Linac("NorthLinac", prefix=PREFIX, linac_pressure_min=lp_min, linac_pressure_max=lp_max,
                      linac_pressure_recovery_margin=lp_recovery_margin, heater_margin_min=heater_capacity_min,
                      heater_recovery_margin=heater_recover_margin)
        zone = Zone(name="1L22", prefix=PREFIX, linac=linac, controls_type='2.0', jt_max=jt_max,
                    jt_recovery_margin=jt_recovery_margin, jt_suffix="")
        cav = LLRF2Cavity(name="1L22-1", epics_name=f"{PREFIX}R1M1", cavity_type="C100", length=0.7, bypassed=False,
                          zone=zone, Q0=6e9, tuner_recovery_margin=tuner_recovery_margin, tuner_bad=False)
        if style_2 == 'old':
            cav.fcc_firmware_version = 2018.0
    elif controls_type == '3.0':
        tuner_recovery_margin = config.get_parameter('LLRF3_tuner_recovery_margin')
        linac = Linac("NorthLinac", prefix=PREFIX, linac_pressure_min=lp_min,  linac_pressure_max=lp_max,
                      linac_pressure_recovery_margin=lp_recovery_margin, heater_margin_min=heater_capacity_min,
                      heater_recovery_margin=heater_recover_margin)
        zone = Zone(name="1L10", prefix=PREFIX, linac=linac, controls_type='3.0', jt_max=jt_max,
                    jt_recovery_margin=jt_recovery_margin, jt_suffix="")
        cav = LLRF3Cavity(name="1L10-1", epics_name=f"{PREFIX}R1A1", cavity_type="C75", length=0.4916, bypassed=False,
                          zone=zone, Q0=6.3e9, tuner_recovery_margin=tuner_recovery_margin, tuner_bad=False)
    else:
        raise RuntimeError("Unsupported controls_type")

    linac.wait_for_connections()
    zone.wait_for_connections()
    cav.wait_for_connections()

    cav.update_gset_max()

    return linac, zone, cav
