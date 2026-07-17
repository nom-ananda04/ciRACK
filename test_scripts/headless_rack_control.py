

import json
import math
import pathlib
import time
from datetime import datetime, timezone

from instro.daq import InstroDAQ
from instro.daq.drivers import Keysight34980A
from instro.daq.drivers.labjack import LabJackTSeriesDriver
from instro.daq.drivers.ni import NIDAQDriver
from instro.daq.types import Direction, Logic
from instro.lib.publishers.nominal_core import NominalCorePublisher
from instro.lib.types import Measurement
from nominal.core import NominalClient

from btop_test_suite import (
    FGEN_DIFFControl,
    AIN_AOControl,
    diRasterScan,
    doDriveControl,
    Counter34980aControl,
    MultiCounterControl,
)


# ============================================================================
# Config file
# ============================================================================
CONFIG_PATH = pathlib.Path(__file__).with_name("headless_rack_control.config.json")

ALL_DRIVERS = ["keysight_34980a", "labjack", "ni_daqmx"]
ALL_TESTS = ["counter_totalize", "di_raster_scan", "do_drive", "multi_counter_clk",
             "fgen_sweep", "ain_ao_route"]

# Every test's required driver -- see module docstring for why this is
# currently a flat mapping to "keysight_34980a" for all six.
TEST_REQUIRED_DRIVER = {
    "counter_totalize": "keysight_34980a",
    "di_raster_scan": "keysight_34980a",
    "do_drive": "keysight_34980a",
    "multi_counter_clk": "keysight_34980a",
    "fgen_sweep": "keysight_34980a",
    "ain_ao_route": "keysight_34980a",
}


def _resolve_all(value, valid_ids, field_name):
    """Accept "all" (any case, or the descriptive "All Supported Drivers" /
    "ALL TESTS" style strings) or an explicit list of ids. Rejects unknown
    ids outright rather than silently ignoring a typo."""
    if isinstance(value, str) and value.strip().lower().startswith("all"):
        return list(valid_ids)
    if isinstance(value, list):
        unknown = [v for v in value if v not in valid_ids]
        if unknown:
            raise ValueError(f"{field_name}: unknown id(s) {unknown}; valid ids are {valid_ids}")
        return list(value)
    raise ValueError(f"{field_name}: expected \"all\" or a list of {valid_ids}, got {value!r}")


DEFAULT_DATASET_NAME = "Hardware CI RACK stream"


def load_config(path: pathlib.Path) -> dict:
    with open(path) as f:
        raw = json.load(f)

    # dataset_rid: optional, matching the pattern in t4_validate.py -- set it
    # to reuse the same dataset across runs (so past runs stay findable in
    # the same place instead of scattering across a new dataset every
    # invocation); leave it null/omitted to create a fresh dataset every run
    # (see main()), using dataset_name as its display name.
    dataset_rid = raw.get("dataset_rid") or None
    dataset_name = raw.get("dataset_name") or DEFAULT_DATASET_NAME

    drivers = _resolve_all(raw.get("drivers", "all"), ALL_DRIVERS, "drivers")
    tests = _resolve_all(raw.get("tests", "all"), ALL_TESTS, "tests")

    # A test only actually runs if its required driver is also enabled.
    enabled_tests = [t for t in tests if TEST_REQUIRED_DRIVER[t] in drivers]
    skipped = [t for t in tests if t not in enabled_tests]
    if skipped:
        print(f"[config] skipping test(s) {skipped}: required driver not in "
              f"enabled drivers {drivers}", flush=True)

    # asset_rid: the one persistent Asset every session's Runs bind to (see
    # setup_runs' docstring). Optional -- if omitted, data still streams to
    # the dataset, it just won't be organized under a Run/Asset.
    asset_rid = raw.get("asset_rid") or None

    return {
        "dataset_rid": dataset_rid,
        "dataset_name": dataset_name,
        "drivers": drivers,
        "tests": enabled_tests,
        "asset_rid": asset_rid,
    }


# ============================================================================
# Headless config -- replaces the Connect checkboxes each subsystem used to
# read via client.get_value(). There's no UI here, so these are plain
# constants: edit them to change behavior instead of clicking a checkbox.
# ============================================================================
ENABLE_CLK = False                                     # MultiCounterControl CLK output

# AIN_AOControl: which single source drives the shared TB_AO_MUX bus. One of
# AIN_AO_SOURCES' keys below, or None for "no source selected, all
# crosspoints open". Bank-relative port on the mux per source: 1H=DAQ1.AO0,
# 2H=DAQ2.AO0, 3H=DAQ3.AO0, 4H=DAQ4.AO0, 5H=cDAQ1.1.AO0 (1L=TB_AGND, not a
# source).
AIN_AO_SOURCE = None
AIN_AO_SOURCES = {
    "route_daq1": 1,
    "route_daq2": 2,
    "route_daq3": 3,
    "route_daq4": 4,
    "route_cdaq": 5,
}

MAIN_RESOURCE = Counter34980aControl.RESOURCE           # all 6 classes point at the same 34980A frame
POLL_S = 0.5


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1e9)


# ============================================================================
# Tests -- one function per test id in ALL_TESTS. Named after the id rather
# than "test1"/"test2" so they stay self-documenting and line up with the
# config file's "tests" list, TEST_REQUIRED_DRIVER, etc.
#
# No round-robin/generator scheduling: each continuous test (di_raster_scan,
# counter_totalize, multi_counter_clk, ain_ao_route, fgen_sweep) does its own
# one-time setup on first call (stashed in the `state` dict passed in) and
# then does one poll/publish step, called straight, one after the other, in
# a fixed order every pass through main()'s loop -- see CONTINUOUS_TESTS
# below for that order (fgen_sweep self-paces via a stashed "next sweep at"
# timestamp rather than actually running every pass -- see its docstring).
# Teardown for multi_counter_clk/ain_ao_route/fgen_sweep happens once,
# explicitly, in main()'s `finally` block.
#
# do_drive is a bounded one-shot (4 toggles, then done) -- see
# ONE_SHOT_TESTS below and the module docstring for why it runs once,
# before the main loop starts, instead of every pass.
# ============================================================================

# --- DI stimulus rig ---------------------------------------------------------
# di_raster_scan only reads DI2-DI6 -- with nothing driving them, those
# inputs just sit at whatever level they float to, so there's nothing real
# to see in Core. Each DI bit is wired to its own external DAQ's output line
# (confirmed from the wiring diagram, pins confirmed against real wiring):
#     DI2 <- LabJack T8  CIO1   | DI3 <- LabJack T7  CIO1   | DI4 <- LabJack T4  CIO1
#     DI5 <- NI USB-6421 DIO1   | DI6 <- NI cDAQ-9401 (cDAQ1Mod4) DIO1
# T4/T7/T8 serials reused from MultiCounterControl.LABJACKS in
# btop_test_suite.py -- same physical devices. NI's "DIO1" is expressed as
# instro's required DevN/portM/lineP physical_channel string below.
DI_STIMULUS_DEVICES = [
    # (di_alias, driver_family, device_id, physical_channel)
    ("di_2", "labjack", "480011030", "CIO1"),                       # T8        -> DI2
    ("di_3", "labjack", "470041016", "CIO1"),                       # T7        -> DI3
    ("di_4", "labjack", "440020473", "CIO1"),                       # T4        -> DI4
    ("di_5", "ni", "Dev1", "Dev1/port0/line1"),                     # USB-6421  -> DI5
    ("di_6", "ni", "cDAQ1Mod4", "cDAQ1Mod4/port0/line1"),           # NI-9401   -> DI6
]

# Round-robin: only one device drives HIGH at a time, holding for this many
# poll passes before advancing to the next -- so di_raster_scan's reads show
# each DI bit asserting on its own (proves each wire individually) rather
# than all five changing together.
DI_STIMULUS_HOLD_PASSES = 3


def test_di_raster_scan(daq, publish, state):
    """Ported from di_raster_scan.py: per-alias client.stream(...) calls
    become one batched publish() call; dio.log.info(...) calls are
    unchanged, including the real script's per-pass log line (noisy, but
    that's what the real script does).

    Also drives the DI stimulus rig (see DI_STIMULUS_DEVICES above) so the
    DI2-DI6 inputs being read here carry real, changing data instead of a
    flat/floating level -- one external DAQ's output line goes HIGH at a
    time, round-robining through all five every DI_STIMULUS_HOLD_PASSES
    passes."""
    if "di_scan" not in state:
        di_scan = diRasterScan()
        di_scan._assert_34980a(daq)
        di_scan.configure_all(daq)
        di_scan.log.info("Ready. Raster scanning DI2-DI6.")
        state["di_scan"] = di_scan

        # One InstroDAQ session per external stimulus device, opened once
        # and configured for a single digital output line each.
        stim_daqs = {}
        for di_alias, driver_family, device_id, phys_ch in DI_STIMULUS_DEVICES:
            if driver_family == "labjack":
                stim_daq = InstroDAQ(name=f"stim_{device_id}", driver=LabJackTSeriesDriver(device_id=device_id))
            elif driver_family == "ni":
                stim_daq = InstroDAQ(name=f"stim_{device_id}", driver=NIDAQDriver(device_id=device_id))
            else:
                raise ValueError(f"unknown stimulus driver family {driver_family!r}")
            stim_daq.open()
            stim_daq.configure_digital_line(
                direction=Direction.OUTPUT,
                physical_channel=phys_ch,
                alias=di_alias,
                logic=Logic.HIGH,
            )
            stim_daq.write_digital_line(channel=di_alias, data=0)  # start low
            stim_daqs[di_alias] = stim_daq
        state["di_stim_daqs"] = stim_daqs
        state["di_stim_index"] = 0
        state["di_stim_pass_count"] = 0
        di_scan.log.info(f"DI stimulus rig ready: round-robining {len(stim_daqs)} devices, "
                          f"{DI_STIMULUS_HOLD_PASSES} pass(es) each.")

    # Round-robin the stimulus: exactly one device HIGH at a time.
    stim_daqs = state["di_stim_daqs"]
    active_alias = DI_STIMULUS_DEVICES[state["di_stim_index"]][0]
    for di_alias, stim_daq in stim_daqs.items():
        stim_daq.write_digital_line(channel=di_alias, data=1 if di_alias == active_alias else 0)

    state["di_stim_pass_count"] += 1
    if state["di_stim_pass_count"] >= DI_STIMULUS_HOLD_PASSES:
        state["di_stim_pass_count"] = 0
        state["di_stim_index"] = (state["di_stim_index"] + 1) % len(DI_STIMULUS_DEVICES)

    di_scan = state["di_scan"]
    di_states = di_scan.read_inputs(daq)
    now = datetime.now()
    publish(di_states, tags={"subsystem": "di_raster_scan"})
    di_scan.log.info(f"{now.isoformat()} | published to stream={di_scan.STREAM_ID!r}: {di_states}")


# --- DO listener rig ---------------------------------------------------------
# test_do_drive commands DO0 (TB_D_OUT) from the Keysight side only -- to
# prove that signal actually reaches every external DAQ (rather than
# trusting the command alone), each device also listens for it on its own
# input line. All five devices read from CIO0 (LabJack) / DIO0 (NI) --
# confirmed wiring, and a different line than the CIO1/DIO1 lines the DI2-
# DI6 stimulus rig (test_di_raster_scan) drives as OUTPUTS, so both tests
# can run in the same session without conflicting.
DO_LISTENER_DEVICES = [
    # (listen_alias, driver_family, device_id, physical_channel)
    ("do_seen_t8", "labjack", "480011030", "CIO0"),                 # T8        listens for DO0
    ("do_seen_t7", "labjack", "470041016", "CIO0"),                 # T7        listens for DO0
    ("do_seen_t4", "labjack", "440020473", "CIO0"),                 # T4        listens for DO0
    ("do_seen_usb6421", "ni", "Dev1", "Dev1/port0/line0"),          # USB-6421  listens for DO0
    ("do_seen_cdaq9401", "ni", "cDAQ1Mod4", "cDAQ1Mod4/port0/line0"),  # NI-9401 listens for DO0
]


def test_do_drive(daq, publish):
    """One-shot square-wave toggle test on DO0 (TB_D_OUT): 1,0,1,0 with a 1s
    hold each, minus a small epsilon before each transition so the plot
    holds flat then snaps instead of ramping. Ported from
    do_send_output.py -- client.stream() calls become publish() calls; the
    four-level sequence and timing are otherwise unchanged. Runs once, not
    every pass (see module docstring): this is a bounded test that toggles
    four times and returns, not a continuous poller.

    Also opens the DO listener rig (see DO_LISTENER_DEVICES above) so each
    external DAQ's own observed level -- not just the commanded do0 level --
    gets published alongside it every step, proving the signal actually
    reaches all five devices."""
    do_drive = doDriveControl()
    do_drive.configure_all(daq)
    do_drive.log.info("Ready. Toggling DO0 between 1 and 0, twice.")

    # One InstroDAQ session per DO listener device, opened once and
    # configured as a digital INPUT on CIO0/DIO0 (see DO_LISTENER_DEVICES).
    listeners = {}
    for listen_alias, driver_family, device_id, phys_ch in DO_LISTENER_DEVICES:
        if driver_family == "labjack":
            listen_daq = InstroDAQ(name=f"stim_{device_id}", driver=LabJackTSeriesDriver(device_id=device_id))
        elif driver_family == "ni":
            listen_daq = InstroDAQ(name=f"stim_{device_id}", driver=NIDAQDriver(device_id=device_id))
        else:
            raise ValueError(f"unknown stimulus driver family {driver_family!r}")
        listen_daq.open()
        listen_daq.configure_digital_line(
            direction=Direction.INPUT,
            physical_channel=phys_ch,
            alias=listen_alias,
            logic=Logic.HIGH,
        )
        listeners[listen_alias] = listen_daq

    HOLD_S = 1.0
    EPSILON_S = 0.02   # small gap before the transition so the plot holds flat

    try:
        for level in [1, 0, 1, 0]:
            do_drive.set_drive(daq, level)
            do_drive.log.info(f"DO0 (TB_D_OUT) -> {level}")

            # Two points per plateau: one at the start, one just before the
            # next transition. Renders a flat hold followed by a sharp
            # vertical edge (square wave), instead of a diagonal ramp
            # between a single point per level. Each point also carries
            # every listener's own observed level alongside the commanded one.
            seen = {alias: float(listen_daq.read_digital_line(channel=alias).latest)
                    for alias, listen_daq in listeners.items()}
            publish({do_drive.DO_DRIVE_ALIAS: float(level), **seen}, tags={"subsystem": "do_drive"})
            time.sleep(HOLD_S - EPSILON_S)

            seen = {alias: float(listen_daq.read_digital_line(channel=alias).latest)
                    for alias, listen_daq in listeners.items()}
            publish({do_drive.DO_DRIVE_ALIAS: float(level), **seen}, tags={"subsystem": "do_drive"})
            time.sleep(EPSILON_S)
    finally:
        do_drive.safe_off(daq)
        for listen_daq in listeners.values():
            try:
                listen_daq.close()
            except Exception:
                pass


def test_counter_totalize(inst, publish, state):
    if "counter" not in state:
        counter = Counter34980aControl()
        counter.configure(inst)
        state["counter"] = counter
        state["last_count"] = None
    counter = state["counter"]
    count = counter.read_count(inst)
    if count is not None:
        if count != state["last_count"]:
            counter.log.info(f"count = {count}")
            state["last_count"] = count
        publish({"counter_8301": count}, tags={"subsystem": "counter_34980a"})


def test_multi_counter_clk(inst, publish, enable_clk, state):
    if "multi_counter" not in state:
        multi_counter = MultiCounterControl()
        if enable_clk:
            multi_counter.clk_on(inst)
            multi_counter._clk_state["on"] = True
        state["multi_counter"] = multi_counter
    multi_counter = state["multi_counter"]
    multi_counter._clk_state["on"] = enable_clk
    publish(
        {"clk_state": 1.0 if multi_counter._clk_state["on"] else 0.0},
        tags={"subsystem": "multi_counter"},
    )


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


# --- FGEN/DIFF sine rig ------------------------------------------------------
# Drives a 1 Hz, 1 Vpp-amplitude, 1 V offset sine wave out DAC0/port0 on
# T4/T7/T8/USB-6421/NI-9263, and senses on AIN1/ai1 on
# T4/T7/T8/USB-6421/NI-9204/NI-9207 -- same device list and module mapping as
# the AIN_AO analog rig below (cDAQ1Mod1=NI9263, cDAQ1Mod2=NI9204,
# cDAQ1Mod3=NI9207), just driving a sine instead of a constant.
#
# instro has no hardware-timed/buffered analog output for either LabJack or
# NI-DAQmx (confirmed from source) -- write_analog_value() is a single
# immediate write. A per-pass single sample at POLL_S=0.5s would only give
# ~2 samples/cycle at 1Hz (stair-stepped, not a real sine), so instead each
# call to this function runs a fast inner "burst" loop -- FGEN_SINE_UPDATE_HZ
# samples/sec for FGEN_SINE_BURST_S seconds (~40 samples/cycle) -- as a
# single blocking call. No real threading, just a tighter loop.
FGEN_SINE_FREQ_HZ = 1.0
FGEN_SINE_AMPLITUDE_V = 1.0
FGEN_SINE_OFFSET_V = 1.0
FGEN_SINE_UPDATE_HZ = 40.0
FGEN_SINE_BURST_S = 0.5

# (device_key, driver_family, device_id, out_channel_or_None, sense_channel_or_None)
FGEN_ANALOG_DEVICES = [
    ("t4", "labjack", "440020473", "DAC0", "AIN1"),
    ("t7", "labjack", "470041016", "DAC0", "AIN1"),
    ("t8", "labjack", "480011030", "DAC0", "AIN1"),
    ("usb6421", "ni", "Dev1", "Dev1/ao0", "Dev1/ai1"),
    ("ni9263", "ni", "cDAQ1Mod1", "cDAQ1Mod1/ao0", None),
    ("ni9204", "ni", "cDAQ1Mod2", None, "cDAQ1Mod2/ai1"),
    ("ni9207", "ni", "cDAQ1Mod3", None, "cDAQ1Mod3/ai1"),
]


def test_fgen_sweep(daq, publish, state):
    """Ported from btop_fgen_diff_control.py: repeat forever, one full
    DAC-source x port sweep per cycle, CYCLE_PAUSE_S between cycles.
    client.stream(...) calls become publish() calls via _StreamClient.

    IMPORTANT: unlike every other continuous test in this file, a single
    call to fgen.sweep() is NOT a quick per-pass step -- it holds each of
    DAC_PORTS x DEST_PORTS routes for DWELL_S seconds (5 DACs x 3 ports x
    ~6.5s/route =~ 100s per sweep, per the class's own constants). This file
    made an explicit choice to run tests straight, one after the other,
    with no threading -- so enabling "fgen_sweep" means the entire main
    loop (every other enabled test) stalls for the duration of each sweep.
    That's a real tradeoff of the no-threading design, not a bug; flagging
    it here rather than hiding it.

    Also drives the FGEN sine rig (see FGEN_ANALOG_DEVICES above) every call,
    before the sweep-cycle gate below -- so it runs every pass regardless of
    where fgen.sweep() is in its own cycle.
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

    fgen_analog_daqs = state["fgen_analog_daqs"]
    step_s = 1.0 / FGEN_SINE_UPDATE_HZ
    n_steps = max(1, round(FGEN_SINE_BURST_S * FGEN_SINE_UPDATE_HZ))
    for _ in range(n_steps):
        elapsed = time.monotonic() - state["fgen_sine_start"]
        sine_value = FGEN_SINE_OFFSET_V + FGEN_SINE_AMPLITUDE_V * math.sin(
            2 * math.pi * FGEN_SINE_FREQ_HZ * elapsed
        )
        readings = {"fgen_sine_cmd": sine_value}
        for device_key, driver_family, device_id, out_ch, sense_ch in FGEN_ANALOG_DEVICES:
            analog_daq = fgen_analog_daqs[device_key]
            if out_ch:
                analog_daq.write_analog_value(channel=f"{device_key}_ao0", value=sine_value)
            if sense_ch:
                measurement = analog_daq.read_analog()
                if isinstance(measurement, list):
                    measurement = measurement[0]
                # instro's channel_data key doesn't necessarily match the
                # alias passed to configure_analog_channel (confirmed on
                # real hardware) -- there's exactly one channel configured
                # per InstroDAQ here, so just take whichever key is there.
                (_, values), = measurement.channel_data.items()
                readings[f"{device_key}_ain1"] = float(values[-1])
        publish(readings, tags={"subsystem": "fgen_diff_analog"})
        time.sleep(step_s)

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


# --- AIN_AO analog output/sense rig -------------------------------------------
# Outputs a constant AIN_AO_CONST_VOLTAGE from DAC0/port0 on
# T4/T7/T8/USB-6421/NI-9263, and senses on AIN1/ai1 on
# T4/T7/T8/USB-6421/NI-9204/NI-9207 (NI module mapping: cDAQ1Mod1=NI9263,
# cDAQ1Mod2=NI9204, cDAQ1Mod3=NI9207 -- confirmed from rack photo).
AIN_AO_CONST_VOLTAGE = 1.0

# (device_key, driver_family, device_id, out_channel_or_None, sense_channel_or_None)
AIN_AO_ANALOG_DEVICES = [
    ("t4", "labjack", "440020473", "DAC0", "AIN1"),
    ("t7", "labjack", "470041016", "DAC0", "AIN1"),
    ("t8", "labjack", "480011030", "DAC0", "AIN1"),
    ("usb6421", "ni", "Dev1", "Dev1/ao0", "Dev1/ai1"),
    ("ni9263", "ni", "cDAQ1Mod1", "cDAQ1Mod1/ao0", None),
    ("ni9204", "ni", "cDAQ1Mod2", None, "cDAQ1Mod2/ai1"),
    ("ni9207", "ni", "cDAQ1Mod3", None, "cDAQ1Mod3/ai1"),
]


def test_ain_ao_route(daq, publish, state):

    if "ain_ao" not in state:
        tray = AIN_AOControl()
        tray._assert_34980a(daq)
        tray.startup_guard(daq)
        state["ain_ao"] = tray
        state["ain_ao_last_selected"] = None

        analog_daqs = {}
        for device_key, driver_family, device_id, out_ch, sense_ch in AIN_AO_ANALOG_DEVICES:
            if driver_family == "labjack":
                analog_daq = InstroDAQ(name=f"ainao_{device_key}", driver=LabJackTSeriesDriver(device_id=device_id))
            elif driver_family == "ni":
                analog_daq = InstroDAQ(name=f"ainao_{device_key}", driver=NIDAQDriver(device_id=device_id))
            else:
                raise ValueError(f"unknown analog driver family {driver_family!r}")
            analog_daq.open()
            if out_ch:
                analog_daq.configure_analog_channel(direction=Direction.OUTPUT, physical_channel=out_ch,
                                                     alias=f"{device_key}_ao0")
                analog_daq.write_analog_value(channel=f"{device_key}_ao0", value=AIN_AO_CONST_VOLTAGE)
            if sense_ch:
                analog_daq.configure_analog_channel(direction=Direction.INPUT, physical_channel=sense_ch,
                                                     alias=f"{device_key}_ain1")
            analog_daqs[device_key] = analog_daq
        state["ain_ao_analog_daqs"] = analog_daqs

    tray = state["ain_ao"]
    target = AIN_AO_SOURCES.get(AIN_AO_SOURCE) if AIN_AO_SOURCE else None

    if target != state["ain_ao_last_selected"]:
        if target is None:
            tray._open_all(daq)
            print("No source selected -- all crosspoints open")
        else:
            dac_ch = tray._chan(tray.BANK1_BASE, target)
            ok = tray.connect_dac(daq, dac_ch)
            print(f"Routed port {target} ({dac_ch}) -> TB_AO_MUX  [{'OK' if ok else 'FAIL'}]")
        state["ain_ao_last_selected"] = target

    analog_daqs = state["ain_ao_analog_daqs"]
    readings = {}
    for device_key, driver_family, device_id, out_ch, sense_ch in AIN_AO_ANALOG_DEVICES:
        if sense_ch:
            analog_daq = analog_daqs[device_key]
            measurement = analog_daq.read_analog()
            if isinstance(measurement, list):
                measurement = measurement[0]
            (_, values), = measurement.channel_data.items()
            readings[f"{device_key}_ain1"] = float(values[-1])
    publish(readings, tags={"subsystem": "ain_ao_analog"})


def teardown_tests(tests, state, daq, inst):
    """Run once, on the way out."""
    if "multi_counter_clk" in tests and ENABLE_CLK and "multi_counter" in state:
        state["multi_counter"].clk_off(inst)
    if "ain_ao_route" in tests and "ain_ao" in state:
        state["ain_ao"]._open_all(daq)
    if "ain_ao_route" in tests and "ain_ao_analog_daqs" in state:
        for device_key, analog_daq in state["ain_ao_analog_daqs"].items():
            try:
                if any(dk == device_key and out_ch for dk, _, _, out_ch, _ in AIN_AO_ANALOG_DEVICES):
                    analog_daq.write_analog_value(channel=f"{device_key}_ao0", value=0.0)
            except Exception:
                pass
            try:
                analog_daq.close()
            except Exception:
                pass
    if "fgen_sweep" in tests and "fgen" in state:
        state["fgen"]._open_all(daq)
    if "fgen_sweep" in tests and "fgen_analog_daqs" in state:
        for device_key, analog_daq in state["fgen_analog_daqs"].items():
            try:
                if any(dk == device_key and out_ch for dk, _, _, out_ch, _ in FGEN_ANALOG_DEVICES):
                    analog_daq.write_analog_value(channel=f"{device_key}_ao0", value=0.0)
            except Exception:
                pass
            try:
                analog_daq.close()
            except Exception:
                pass
    if "di_raster_scan" in tests and "di_stim_daqs" in state:
        for di_alias, stim_daq in state["di_stim_daqs"].items():
            try:
                stim_daq.write_digital_line(channel=di_alias, data=0)
            except Exception:
                pass
            try:
                stim_daq.close()
            except Exception:
                pass


# Continuous tests, called straight, one after the other, in THIS order,
# every pass through main()'s loop. fgen_sweep is here too, not in
# ONE_SHOT_TESTS -- it repeats forever (see its docstring for the
# main-loop-stalls-per-cycle tradeoff that comes with that).
CONTINUOUS_TESTS = [
    ("di_raster_scan", lambda daq, inst, publish, state: test_di_raster_scan(daq, publish, state)),
    ("counter_totalize", lambda daq, inst, publish, state: test_counter_totalize(inst, publish, state)),
    ("multi_counter_clk", lambda daq, inst, publish, state: test_multi_counter_clk(inst, publish, ENABLE_CLK, state)),
    ("ain_ao_route", lambda daq, inst, publish, state: test_ain_ao_route(daq, publish, state)),
    ("fgen_sweep", lambda daq, inst, publish, state: test_fgen_sweep(daq, publish, state)),
]

# One-shot tests: id -> the callable to run once, before the main loop starts.
# do_drive is here, not in CONTINUOUS_TESTS -- it's a bounded four-toggle
# test ported from do_send_output.py, not a per-pass poller (see its
# docstring); it handles its own teardown internally (finally: safe_off()).
ONE_SHOT_TESTS = {
    "do_drive": lambda daq, inst, publish: test_do_drive(daq, publish),
}


def setup_runs(client, dataset, tests, asset_rid):
    """One Run per enabled test id, tied to a persistent Asset (asset_rid)
    so every session's data lands under the same asset instead of
    scattering across disconnected objects. Returns {test_id: run}.

    ASSET_RID is what keeps things organized run after run: each session's
    dataset is attached directly to that one constant asset
    (asset.add_dataset), under the test id as its data_scope_name, and each
    Run is created via asset.create_run so it's tied to the same asset too.

    Because the asset is persistent, a prior session already registered a
    data scope under each test id, pointing at THAT session's (now stale)
    dataset -- add_dataset()'s underlying API call 400s if you try to add a
    data_scope_name that's already registered on the asset (confirmed from a
    real "POST .../asset/{rid}/data-sources" 400 on the second run against
    an already-populated asset). So any scope already present under a test
    id gets removed first, then re-added pointing at this session's dataset.

    Each run is open-ended (end=None): it represents "this test session is
    still live," not a fixed historical window. close_runs() below sets the
    end timestamp on the way out.
    """
    asset = client.get_asset(asset_rid)
    existing_scopes = {name for name, _scope_type in asset.list_data_scopes()}

    session_start = datetime.now(timezone.utc)
    created = {}
    for test_id in tests:
        # data_scope_name=test_id: lets data be resolved by scope/ref name
        # consistently, since the same test id keeps mapping to a brand new
        # dataset every time this script runs again -- see the docstring
        # above for why a stale scope under that name has to be removed
        # first rather than just re-added.
        if test_id in existing_scopes:
            asset.remove_data_scopes(names=[test_id])
        asset.add_dataset(data_scope_name=test_id, dataset=dataset)

        core_run = asset.create_run(
            name=f"{test_id} - {session_start.isoformat()}",
            start=session_start,
            end=None,
        )
        core_run.add_dataset(ref_name=test_id, dataset=dataset)
        print(f"[run] {test_id}: run={core_run.rid}", flush=True)

        created[test_id] = core_run
    return created


def close_runs(core_runs):
    """Run once, on the way out: stamp an end time on every Run this session
    created, so it stops reading as 'still live' in the app."""
    now = datetime.now(timezone.utc)
    for test_id, core_run in core_runs.items():
        try:
            core_run.update(end=now)
        except Exception as e:
            print(f"[run] failed to close out run for {test_id!r}: {e}", flush=True)


def main():
    config = load_config(CONFIG_PATH)
    tests = set(config["tests"])
    print(f"[config] dataset_name={config['dataset_name']!r} drivers={config['drivers']} "
          f"tests={sorted(tests)}", flush=True)


    daq = InstroDAQ(name="rack", driver=Keysight34980A(MAIN_RESOURCE))
    daq.open()
    inst = daq.driver._visa   # raw pyvisa handle, for the two classes that talk SCPI directly

    idn = inst.query("*IDN?").strip()
    print(f"*IDN? = {idn}", flush=True)
    if "34980A" not in idn:
        raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    # One NominalClient for the whole session: resolves the dataset below
    # (either a fixed dataset_rid, reused across runs, or a fresh one), and
    # (separately) creates the Runs in setup_runs, all bound to the same
    # asset_rid.
    core_client = NominalClient.from_profile("default")
    if config["dataset_rid"]:
        dataset = core_client.get_dataset(config["dataset_rid"])
        print(f"[dataset] using existing {dataset.rid} ({dataset.name!r})", flush=True)
    else:
        dataset = core_client.create_dataset(name=config["dataset_name"])
        print(f"[dataset] created {dataset.rid} ({config['dataset_name']!r})", flush=True)
    print(f"[dataset] view live data here: {dataset.nominal_url}", flush=True)

    publisher = NominalCorePublisher(dataset_rid=dataset.rid)
    core_runs = setup_runs(core_client, dataset, tests, config["asset_rid"]) if config["asset_rid"] else {}

    # Debug visibility: print once (not every pass) the first time each
    # subsystem tag actually streams data, and the first time each test id
    # starts running.
    seen_subsystems = set()

    def publish(channel_data: dict, tags: dict | None = None):
        subsystem = (tags or {}).get("subsystem")
        if subsystem and subsystem not in seen_subsystems:
            seen_subsystems.add(subsystem)
            print(f"[stream] {subsystem!r} is now streaming to Core", flush=True)
        ts = _now_ns()
        publisher.publish(
            Measurement(
                channel_data={name: [float(v)] for name, v in channel_data.items()},
                timestamps=[ts],
                tags=tags,
            )
        )

    started_tests = set()

    def _announce_test_start(test_id):
        if test_id not in started_tests:
            started_tests.add(test_id)
            print(f"[test] {test_id!r} starting", flush=True)

    try:
        # One-shot tests run once, before the main loop starts.
        for test_id, run in ONE_SHOT_TESTS.items():
            if test_id in tests:
                _announce_test_start(test_id)
                run(daq, inst, publish)

        # Continuous tests: no round-robin/generator scheduling -- every
        state = {}
        try:
            while True:
                for test_id, run in CONTINUOUS_TESTS:
                    if test_id in tests:
                        _announce_test_start(test_id)
                        run(daq, inst, publish, state)
                time.sleep(POLL_S)
        finally:
            teardown_tests(tests, state, daq, inst)
    finally:
        close_runs(core_runs)
        publisher.close()
        daq.close()


if __name__ == "__main__":
    main()
