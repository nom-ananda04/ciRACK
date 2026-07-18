"""fgen_sweep: drives a 1 Hz sine on T4/T7/T8/USB-6421's self-loop
(DAC1->AIN0, always on) AND round-robins the Keysight SW_AO_MUX switch
through all 5 ports (see mux_rig.py), driving whichever port is currently
active with the same sine via its DAC0 output and reading BOTH of the two
sense pins the switch lands on (AIN2+AIN3 on LabJacks, AI1+AI2 on NI daqs)
on all six MUX_SENSE_DEVICES every pass -- while ALSO running
FGEN_DIFFControl's own DAC x port sweep. See run()'s docstring for the
tradeoffs of this file's no-threading design."""

import math
import time

from instro.daq import InstroDAQ
from instro.daq.drivers.labjack import LabJackTSeriesDriver
from instro.daq.drivers.ni import NIDAQDriver
from instro.daq.types import Direction

from btop_test_suite import FGEN_DIFFControl, AIN_AOControl

from headless_tests.mux_rig import MUX_PORT_SEQUENCE, MUX_SENSE_DEVICES

TEST_ID = "fgen_sweep"
REQUIRED_DRIVER = "keysight_34980a"
KIND = "continuous"


class _StreamClient:
    """Minimal stand-in for connect_python.Client, covering only the one
    method FGEN_DIFFControl.sweep() actually calls: client.stream(stream_id,
    timestamp, value, name=...). Forwards each call to publish() as a
    single-channel Measurement tagged with the stream id, so sweep()'s
    per-route OK/FAIL reporting still reaches Nominal Core without sweep()
    itself needing to know it isn't talking to Connect."""

    def __init__(self, publish):
        self._publish = publish

    def stream(self, stream_id, timestamp, value, name):
        self._publish({name: float(value)}, tags={"subsystem": stream_id})


# Drives a 1 Hz, 1 Vpp-amplitude, 1 V offset sine wave. T4/T7/T8/USB-6421
# each self-loop directly on their own box -- DAC1->AIN0 (LabJack) /
# ao1->ai0 (NI), confirmed wiring -- so each one drives and senses its own
# sine independently. NI-9263 (cDAQ1Mod1) only drives (ao0); NI-9204/NI-9207
# have no self-loop of their own -- their sense channel is configured
# entirely via MUX_SENSE_DEVICES (see mux_rig.py), reached only through the
# shared Keysight SW_AO_MUX bus below.
#
# instro has no hardware-timed/buffered analog output for either LabJack or
# NI-DAQmx (confirmed from source) -- write_analog_value() is a single
# immediate write. A per-pass single sample at POLL_S=0.5s would only give
# ~2 samples/cycle at 1Hz (stair-stepped, not a real sine), so instead each
# call to run() runs a fast inner "burst" loop -- FGEN_SINE_UPDATE_HZ
# samples/sec for FGEN_SINE_BURST_S seconds -- as a single blocking call. No
# real threading, just a tighter loop.
#
# FGEN_SINE_BURST_S needs to be much longer than one period, not ~half of
# one: headless_rack_control.py's outer loop always sleeps POLL_S right
# after run() returns, regardless of how long the burst itself took -- with
# a burst exactly half the sine's period, that extra gap would land in the
# SAME phase every single cycle (confirmed on real hardware: rendered as a
# jagged, blocky waveform, not a sine). Making the burst span many full
# periods turns that same fixed gap into a small, infrequent blip instead
# of erasing half of every cycle.
FGEN_SINE_FREQ_HZ = 1.0
FGEN_SINE_AMPLITUDE_V = 1.0
FGEN_SINE_OFFSET_V = 1.0
FGEN_SINE_UPDATE_HZ = 40.0
FGEN_SINE_BURST_S = 20.0

# (device_key, driver_family, device_id, out_channel_or_None, sense_channel_or_None)
FGEN_ANALOG_DEVICES = [
    ("t4", "labjack", "440020473", "DAC1", "AIN0"),
    ("t7", "labjack", "470041016", "DAC1", "AIN0"),
    ("t8", "labjack", "480011030", "DAC1", "AIN0"),
    ("usb6421", "ni", "Dev1", "Dev1/ao1", "Dev1/ai0"),
    ("ni9263", "ni", "cDAQ1Mod1", "cDAQ1Mod1/ao0", None),
    ("ni9204", "ni", "cDAQ1Mod2", None, None),
    ("ni9207", "ni", "cDAQ1Mod3", None, None),
]

# Round-robin hold: only one mux port is connected at a time (confirmed), so
# exactly one device is routed+driven at a time. Each call to run() is
# already a full ~FGEN_SINE_BURST_S-second burst, so it only needs to hold
# 1 call before advancing to the next port.
FGEN_MUX_HOLD_PASSES = 1


def run(daq, inst, publish, state):
    """Ported from btop_fgen_diff_control.py: repeat forever, one full
    DAC-source x port sweep per cycle, CYCLE_PAUSE_S between cycles.
    client.stream(...) calls become publish() calls via _StreamClient.

    IMPORTANT: unlike every other continuous test, a single call to
    fgen.sweep() is NOT a quick per-pass step -- it holds each of
    DAC_PORTS x DEST_PORTS routes for DWELL_S seconds (5 DACs x 3 ports x
    ~6.5s/route =~ 100s per sweep, per the class's own constants). This
    project made an explicit choice to run tests straight, one after the
    other, with no threading -- so enabling "fgen_sweep" means every other
    enabled test stalls for the duration of each sweep. That's a real
    tradeoff of the no-threading design, not a bug; flagging it here rather
    than hiding it.

    Also drives the FGEN sine self-loop rig (see FGEN_ANALOG_DEVICES above)
    AND the SW_AO_MUX round-robin (see mux_rig.py) every call, before the
    sweep-cycle gate below -- so both run every pass regardless of where
    fgen.sweep() is in its own cycle.
    """
    if "fgen" not in state:
        fgen = FGEN_DIFFControl()
        fgen._assert_34980a(daq)
        fgen._open_all(daq)
        fgen.log.info("Starting automatic sweep (no trigger).")
        state["fgen"] = fgen
        state["fgen_stream_client"] = _StreamClient(publish)
        state["fgen_next_sweep_at"] = 0.0   # run the first sweep immediately

        fgen_analog_daqs = {}
        for device_key, driver_family, device_id, out_ch, sense_ch in FGEN_ANALOG_DEVICES:
            if driver_family == "labjack":
                analog_daq = InstroDAQ(name=f"fgen_{device_key}", driver=LabJackTSeriesDriver(device_id=device_id))
            elif driver_family == "ni":
                analog_daq = InstroDAQ(name=f"fgen_{device_key}", driver=NIDAQDriver(device_id=device_id))
            else:
                raise ValueError(f"unknown analog driver family {driver_family!r}")
            analog_daq.open()
            if out_ch:
                analog_daq.configure_analog_channel(direction=Direction.OUTPUT, physical_channel=out_ch,
                                                     alias=f"{device_key}_ao0")
            if sense_ch:
                analog_daq.configure_analog_channel(direction=Direction.INPUT, physical_channel=sense_ch,
                                                     alias=f"{device_key}_ain1")
            fgen_analog_daqs[device_key] = analog_daq
        state["fgen_analog_daqs"] = fgen_analog_daqs
        state["fgen_sine_start"] = time.monotonic()

        # SW_AO_MUX round-robin (see mux_rig.py): each mux-drive channel is
        # a NEW channel on the same InstroDAQ session as the self-loop
        # above, except ni9263 which reuses its existing "ao0" channel (no
        # self-loop of its own). ain_ao_route's teardown always re-opens
        # every crosspoint before this test's slot even starts, so this
        # can't rely on that other test having left anything routed -- it
        # has to route it itself.
        for device_key, _drv, _devid, mux_ao_alias, mux_ao_phys, _port in MUX_PORT_SEQUENCE:
            if mux_ao_phys is not None:
                fgen_analog_daqs[device_key].configure_analog_channel(
                    direction=Direction.OUTPUT, physical_channel=mux_ao_phys, alias=mux_ao_alias)
        # Each mux port lands on two sense pins per device (see mux_rig.py:
        # AIN2+AIN3 on LabJacks, AI1+AI2 on NI daqs) -- configure both.
        for device_key, _drv, _devid, ain_a_alias, ain_a_phys, ain_b_alias, ain_b_phys in MUX_SENSE_DEVICES:
            fgen_analog_daqs[device_key].configure_analog_channel(
                direction=Direction.INPUT, physical_channel=ain_a_phys, alias=ain_a_alias)
            fgen_analog_daqs[device_key].configure_analog_channel(
                direction=Direction.INPUT, physical_channel=ain_b_phys, alias=ain_b_alias)

        mux_tray = AIN_AOControl()
        mux_tray._assert_34980a(daq)
        mux_tray.startup_guard(daq)
        state["mux_tray"] = mux_tray
        state["mux_port_index"] = 0
        state["mux_pass_count"] = 0
        state["mux_routed_port"] = None

    fgen_analog_daqs = state["fgen_analog_daqs"]

    # Route the mux to the currently active port, if it changed since the
    # last call -- only one port can be connected at a time (confirmed), so
    # this is a plain round-robin, same pattern as di_raster_scan.py's
    # DI_STIMULUS_DEVICES.
    mux_tray = state["mux_tray"]
    active_device_key, _drv, _devid, active_ao_alias, _phys, active_port = \
        MUX_PORT_SEQUENCE[state["mux_port_index"]]
    if active_port != state["mux_routed_port"]:
        dac_ch = mux_tray._chan(mux_tray.BANK1_BASE, active_port)
        ok = mux_tray.connect_dac(daq, dac_ch)
        print(f"[fgen_sweep] SW_AO_MUX -> port {active_port} ({active_device_key}, {dac_ch})  "
              f"[{'OK' if ok else 'FAIL'}]", flush=True)
        state["mux_routed_port"] = active_port

    step_s = 1.0 / FGEN_SINE_UPDATE_HZ
    n_steps = max(1, round(FGEN_SINE_BURST_S * FGEN_SINE_UPDATE_HZ))
    for _ in range(n_steps):
        elapsed = time.monotonic() - state["fgen_sine_start"]
        sine_value = FGEN_SINE_OFFSET_V + FGEN_SINE_AMPLITUDE_V * math.sin(
            2 * math.pi * FGEN_SINE_FREQ_HZ * elapsed
        )

        # Self-loop drive: DAC1->AIN0, unchanged from before.
        readings = {"fgen_sine_cmd": sine_value}
        for device_key, driver_family, device_id, out_ch, sense_ch in FGEN_ANALOG_DEVICES:
            if out_ch:
                fgen_analog_daqs[device_key].write_analog_value(channel=f"{device_key}_ao0", value=sine_value)

        # Mux drive: whichever port is currently routed gets the same sine
        # on its mux-ao channel.
        fgen_analog_daqs[active_device_key].write_analog_value(channel=active_ao_alias, value=sine_value)

        # One read_analog() call per device covers every AI channel
        # configured on it (self-loop AIN0 + mux AIN1, where both exist) --
        # cache the single returned measurement's channel_data and pull
        # each alias's value out of it by exact key, rather than reading
        # twice or assuming only one channel is configured (instro keys
        # channel_data as "{daq_name}.{alias}", confirmed from source, so
        # this is reliable even with multiple channels per session).
        read_cache = {}
        for device_key, driver_family, device_id, out_ch, sense_ch in FGEN_ANALOG_DEVICES:
            if not sense_ch:
                continue
            analog_daq = fgen_analog_daqs[device_key]
            measurement = analog_daq.read_analog()
            if isinstance(measurement, list):
                measurement = measurement[0]
            read_cache[device_key] = measurement.channel_data
            readings[f"{device_key}_ain1"] = float(
                measurement.channel_data[f"{analog_daq.name}.{device_key}_ain1"][-1])
        publish(readings, tags={"subsystem": "fgen_diff_analog"})

        # Mux sense: read both sense channels (see mux_rig.py: AIN2+AIN3 on
        # LabJacks, AI1+AI2 on NI daqs) on all six MUX_SENSE_DEVICES every
        # pass, regardless of which port is routed -- only the
        # currently-routed device's readings should track the sine; the
        # rest are on a disconnected bus. T4/T7/T8/USB-6421 were already
        # read above (they share a session with the self-loop rig, cached
        # in read_cache); NI9204/NI9207 have no self-loop channel at all,
        # so they haven't been read yet this pass -- read them fresh here.
        mux_readings = {"mux_active_port": float(active_port)}
        for device_key, _drv, _devid, ain_a_alias, _a_phys, ain_b_alias, _b_phys in MUX_SENSE_DEVICES:
            analog_daq = fgen_analog_daqs[device_key]
            channel_data = read_cache.get(device_key)
            if channel_data is None:
                measurement = analog_daq.read_analog()
                if isinstance(measurement, list):
                    measurement = measurement[0]
                channel_data = measurement.channel_data
                read_cache[device_key] = channel_data
            mux_readings[ain_a_alias] = float(channel_data[f"{analog_daq.name}.{ain_a_alias}"][-1])
            mux_readings[ain_b_alias] = float(channel_data[f"{analog_daq.name}.{ain_b_alias}"][-1])
        publish(mux_readings, tags={"subsystem": "fgen_mux_route"})

        time.sleep(step_s)

    state["mux_pass_count"] += 1
    if state["mux_pass_count"] >= FGEN_MUX_HOLD_PASSES:
        state["mux_pass_count"] = 0
        state["mux_port_index"] = (state["mux_port_index"] + 1) % len(MUX_PORT_SEQUENCE)

    if time.monotonic() < state["fgen_next_sweep_at"]:
        return   # still pausing between cycles

    fgen = state["fgen"]
    try:
        fgen.sweep(daq, state["fgen_stream_client"], fgen.DAC_PORTS, fgen.DEST_PORTS)
    except Exception as e:
        fgen.log.error(f"Sweep failed: {e}")
        fgen._open_all(daq)
    fgen.log.info(f"Sweep cycle done; restarting in {fgen.CYCLE_PAUSE_S}s.")
    state["fgen_next_sweep_at"] = time.monotonic() + fgen.CYCLE_PAUSE_S


def teardown(state, daq, inst):
    if "fgen" in state:
        state["fgen"]._open_all(daq)
    if "mux_tray" in state:
        state["mux_tray"]._open_all(daq)
    if "fgen_analog_daqs" in state:
        fgen_analog_daqs = state["fgen_analog_daqs"]
        for device_key, analog_daq in fgen_analog_daqs.items():
            try:
                if any(dk == device_key and out_ch for dk, _, _, out_ch, _ in FGEN_ANALOG_DEVICES):
                    analog_daq.write_analog_value(channel=f"{device_key}_ao0", value=0.0)
            except Exception:
                pass
        for device_key, _drv, _devid, mux_ao_alias, mux_ao_phys, _port in MUX_PORT_SEQUENCE:
            if mux_ao_phys is None:
                continue   # ni9263 reuses "_ao0", already zeroed above
            try:
                fgen_analog_daqs[device_key].write_analog_value(channel=mux_ao_alias, value=0.0)
            except Exception:
                pass
        for analog_daq in fgen_analog_daqs.values():
            try:
                analog_daq.close()
            except Exception:
                pass
