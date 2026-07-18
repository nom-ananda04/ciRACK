"""ain_ao_route: round-robins the Keysight SW_AO_MUX switch through all 5
ports (see mux_rig.py), driving whichever port is currently active with a
constant AIN_AO_CONST_VOLTAGE and reading BOTH of the two sense pins the
switch lands on (AIN2+AIN3 on LabJacks, AI1+AI2 on NI daqs) on all six
MUX_SENSE_DEVICES every pass. This is the mux-DEPENDENT signal path only --
see ain_ao_loopback.py for the separate, always-on DAC1->AIN0 self-loop
test that has nothing to do with the switch."""

from instro.daq import InstroDAQ
from instro.daq.drivers.labjack import LabJackTSeriesDriver
from instro.daq.drivers.ni import NIDAQDriver
from instro.daq.types import Direction

from btop_test_suite import AIN_AOControl

from headless_tests.mux_rig import MUX_PORT_SEQUENCE, MUX_SENSE_DEVICES

TEST_ID = "ain_ao_route"
REQUIRED_DRIVER = "keysight_34980a"
KIND = "continuous"

AIN_AO_CONST_VOLTAGE = 1.0

# ni9263 has no self-loop of its own, so MUX_PORT_SEQUENCE (shared with
# fgen_sweep.py) leaves its physical channel as None there, meaning "reuse
# the ao0 channel already configured elsewhere in that file" -- fgen_sweep.py
# has its own separate self-loop-style device list that does that. This
# file is mux-ONLY (no self-loop list at all, see ain_ao_loopback.py), so
# there is no "elsewhere" here -- ni9263's real physical channel is needed
# directly.
_NI9263_MUX_AO_PHYS = "cDAQ1Mod1/ao0"

# Round-robin hold: only one mux port is connected at a time (confirmed), so
# exactly one device is routed+driven at a time, holding for this many
# call-passes (this test's own "pass" is one quick per-poll call,
# POLL_S=0.5s) before advancing to the next port.
AIN_AO_MUX_HOLD_PASSES = 6


def run(daq, inst, publish, state):
    if "ain_ao" not in state:
        tray = AIN_AOControl()
        tray._assert_34980a(daq)
        tray.startup_guard(daq)
        state["ain_ao"] = tray

        # One InstroDAQ session per mux-port device (see mux_rig.py's
        # MUX_PORT_SEQUENCE) -- these are the only devices that ever drive
        # in this file.
        analog_daqs = {}
        for device_key, driver_family, device_id, mux_ao_alias, mux_ao_phys, _port in MUX_PORT_SEQUENCE:
            if driver_family == "labjack":
                analog_daq = InstroDAQ(name=f"mux_{device_key}", driver=LabJackTSeriesDriver(device_id=device_id))
            elif driver_family == "ni":
                analog_daq = InstroDAQ(name=f"mux_{device_key}", driver=NIDAQDriver(device_id=device_id))
            else:
                raise ValueError(f"unknown analog driver family {driver_family!r}")
            analog_daq.open()
            phys = mux_ao_phys if mux_ao_phys is not None else _NI9263_MUX_AO_PHYS
            analog_daq.configure_analog_channel(direction=Direction.OUTPUT, physical_channel=phys,
                                                 alias=mux_ao_alias)
            analog_daq.write_analog_value(channel=mux_ao_alias, value=AIN_AO_CONST_VOLTAGE)
            analog_daqs[device_key] = analog_daq

        # NI9204/NI9207 only ever sense via the mux and never drive, so
        # they're not in MUX_PORT_SEQUENCE above -- open a session for each
        # of them here too.
        for device_key, driver_family, device_id, _a_alias, _a_phys, _b_alias, _b_phys in MUX_SENSE_DEVICES:
            if device_key in analog_daqs:
                continue   # t4/t7/t8/usb6421/ni9263 -- session already opened above
            if driver_family == "ni":
                analog_daq = InstroDAQ(name=f"mux_{device_key}", driver=NIDAQDriver(device_id=device_id))
            else:
                raise ValueError(f"unknown analog driver family {driver_family!r}")
            analog_daq.open()
            analog_daqs[device_key] = analog_daq

        # Every MUX_SENSE_DEVICES device needs BOTH its sense channels
        # configured (see mux_rig.py: each mux port lands on two pins, not
        # one), whether its session was just opened above (ni9204/ni9207)
        # or already exists from the drive loop (t4/t7/t8/usb6421).
        for device_key, _drv, _devid, ain_a_alias, ain_a_phys, ain_b_alias, ain_b_phys in MUX_SENSE_DEVICES:
            analog_daqs[device_key].configure_analog_channel(
                direction=Direction.INPUT, physical_channel=ain_a_phys, alias=ain_a_alias)
            analog_daqs[device_key].configure_analog_channel(
                direction=Direction.INPUT, physical_channel=ain_b_phys, alias=ain_b_alias)

        state["analog_daqs"] = analog_daqs
        state["mux_port_index"] = 0
        state["mux_pass_count"] = 0
        state["mux_routed_port"] = None

    tray = state["ain_ao"]
    analog_daqs = state["analog_daqs"]

    # Route the mux to the currently active port, if it changed since the
    # last call -- only one port can be connected at a time (confirmed), so
    # this is a plain round-robin, same pattern as di_raster_scan.py's
    # DI_STIMULUS_DEVICES.
    active_device_key, _drv, _devid, _ao_alias, _phys, active_port = \
        MUX_PORT_SEQUENCE[state["mux_port_index"]]
    if active_port != state["mux_routed_port"]:
        dac_ch = tray._chan(tray.BANK1_BASE, active_port)
        ok = tray.connect_dac(daq, dac_ch)
        print(f"[ain_ao_route] Routed port {active_port} ({active_device_key}, {dac_ch}) -> TB_AO_MUX  "
              f"[{'OK' if ok else 'FAIL'}]", flush=True)
        state["mux_routed_port"] = active_port

    # Read both sense channels on all six MUX_SENSE_DEVICES every pass,
    # regardless of which port is routed -- only the currently-routed
    # device's readings should track the constant voltage; the rest are on
    # a disconnected bus. One read_analog() call per device covers both of
    # its configured channels; pull each alias's value out of the same
    # returned channel_data rather than reading twice.
    mux_readings = {"mux_active_port": float(active_port)}
    for device_key, _drv, _devid, ain_a_alias, _a_phys, ain_b_alias, _b_phys in MUX_SENSE_DEVICES:
        analog_daq = analog_daqs[device_key]
        measurement = analog_daq.read_analog()
        if isinstance(measurement, list):
            measurement = measurement[0]
        mux_readings[ain_a_alias] = float(
            measurement.channel_data[f"{analog_daq.name}.{ain_a_alias}"][-1])
        mux_readings[ain_b_alias] = float(
            measurement.channel_data[f"{analog_daq.name}.{ain_b_alias}"][-1])
    publish(mux_readings, tags={"subsystem": "ain_ao_mux_route"})

    state["mux_pass_count"] += 1
    if state["mux_pass_count"] >= AIN_AO_MUX_HOLD_PASSES:
        state["mux_pass_count"] = 0
        state["mux_port_index"] = (state["mux_port_index"] + 1) % len(MUX_PORT_SEQUENCE)


def teardown(state, daq, inst):
    if "ain_ao" in state:
        state["ain_ao"]._open_all(daq)
    if "analog_daqs" in state:
        analog_daqs = state["analog_daqs"]
        for device_key, _drv, _devid, mux_ao_alias, _mux_ao_phys, _port in MUX_PORT_SEQUENCE:
            try:
                analog_daqs[device_key].write_analog_value(channel=mux_ao_alias, value=0.0)
            except Exception:
                pass
        for analog_daq in analog_daqs.values():
            try:
                analog_daq.close()
            except Exception:
                pass
