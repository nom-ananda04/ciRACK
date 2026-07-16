"""Headless rack control: runs the btop_test_suite control classes with no
Connect UI, against ONE shared VISA session to the 34980A, and streams
live channel data to Nominal Core via instro's NominalCorePublisher.

Concurrency model (explicit choice, matches the earlier discussion about
merging all the per-script main()s): a single shared VISA session, single
process, single thread, no round-robin/generator scheduling -- every pass
through the main loop just calls each enabled continuous test straight,
one after the other, in a fixed order (see CONTINUOUS_TESTS). Nothing
blocks on its own infinite while-loop the way the individual Connect
scripts do, so they can all share the one physical instrument connection
safely.

Not everything fits a per-pass step, and this file does NOT paper over
that:

  * diRasterScan, doDriveControl, Counter34980aControl -- genuinely
    continuous pollers/writers. These run every pass below.

  * MultiCounterControl -- clk_on()/clk_off() are quick single calls and
    run every pass. Its count_labjack()/count_nidaqmx(), however, are each
    their own blocking `while True` loop written for a dedicated script
    (they poll a checkbox to know when to stop). Calling either one here
    would hijack the whole loop, so they are NOT wired in. Turning
    per-device LabJack/NI counting into a headless, single-shot-per-pass
    read is a real refactor of those methods, not just a wiring change --
    flagging it rather than quietly reimplementing behavior that wasn't
    asked for.

  * doDriveControl -- test_do_drive() is NOT a continuous poller. Ported
    from do_send_output.py, it's a bounded one-shot: toggle DO0 1,0,1,0
    (1s hold each) and return. Runs once, before the main loop starts (see
    ONE_SHOT_TESTS), and handles its own teardown internally.

  * FGEN_DIFFControl.sweep() -- ported from btop_fgen_diff_control.py:
    repeats forever, but each individual sweep() call is a multi-second,
    multi-step relay procedure (DWELL_S hold per route, ~100s per full
    sweep). It's wired into CONTINUOUS_TESTS like the other pollers, but
    unlike them it does NOT return quickly -- when its pause-between-cycles
    window elapses, the call to test_fgen_sweep() blocks the entire main
    loop for the duration of that sweep. That's the real behavior of the
    ported script (a dedicated `while True` loop with nothing else sharing
    its process), not a bug in the port -- see test_fgen_sweep()'s docstring.

  * AIN_AOControl -- test_ain_ao_route() is NOT AIN_AOControl.route_all_dac()
    (that's still the multi-second full sweep, unused here). It's the
    headless port of a separate Connect script that polls a single-source
    selection (AIN_AO_SOURCE below) and re-routes on change, same shape as
    the other continuous tests -- runs every pass, does its own one-time
    setup on first call.

Nominal Core: a brand new, empty dataset is created every time this script
starts (NominalClient.create_dataset(name=config["dataset_name"])) --
there's no dataset_rid in the config anymore, since a fresh dataset is made
each run rather than reused. Auth goes through
NominalClient.from_profile("default") both for that dataset creation and,
separately, inside instro's NominalCorePublisher for the raw channel-data
stream -- so a ~/.nominal profile needs to exist before this will actually
connect.

Runs and workbooks: since every run creates a brand new dataset (a new RID
each time), "the same dataset" isn't what ties one session's data to the
next -- it's the Workbook. Each enabled test that has a template_rid
configured in "workbook_templates" gets its own Run (an open-ended,
start-now/end-None window) and its own Workbook instantiated from that
test's template against that run, with the fresh dataset attached under
ref_name=test_id. Pointing every run at the same template_rid is what makes
each new session look like "the same workbook" -- even though a new
dataset and a new Workbook object both get created every time. A test with
no template_rid configured still streams data into the dataset as normal --
it just has no dedicated run/workbook, and load_config() prints a note when
this happens rather than staying silent.

Config file (headless_rack_control.config.json, alongside this script):

    {
        "dataset_name": "Hardware CI RACK stream",
        "drivers": "all" | ["keysight_34980a", "labjack", "ni_daqmx"],
        "tests": "all" | ["counter_totalize", "di_raster_scan", "do_drive",
                           "multi_counter_clk", "fgen_sweep", "ain_ao_route"],
        "workbook_templates": {
            "counter_totalize": "ri.workbook_template...",
            "di_raster_scan": "ri.workbook_template...",
            ...
        }
    }

"dataset_name" is optional (defaults to "Hardware CI RACK stream" if
omitted). "workbook_templates" is optional and per-test-id -- omit a test
id (or the whole field) to skip run/workbook creation for it while still
streaming its data.

"drivers" and "tests" are two different axes (confirmed this mapping
explicitly rather than guessing): "drivers" is which hardware family is in
play (Keysight 34980A / LabJack / NI-DAQmx), "tests" is which of the 6
classes' actions actually runs. Every test currently wired into the
round-robin below talks to the 34980A only (counter/DI/DO/FGEN/AIN-AO are
all Keysight-side; MultiCounterControl's per-device LabJack/NI counting
isn't wired in at all -- see the note above) -- so right now
TEST_REQUIRED_DRIVER maps every test to "keysight_34980a", and picking only
"labjack" or "ni_daqmx" in "drivers" will legitimately skip every test
until per-device counting gets added. That's flagged at startup rather
than silently doing nothing.
"""

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

    # No dataset_rid anymore -- a fresh dataset gets created every run (see
    # main()). Just a display name, defaulted if omitted.
    dataset_name = raw.get("dataset_name") or DEFAULT_DATASET_NAME

    drivers = _resolve_all(raw.get("drivers", "all"), ALL_DRIVERS, "drivers")
    tests = _resolve_all(raw.get("tests", "all"), ALL_TESTS, "tests")

    # A test only actually runs if its required driver is also enabled.
    enabled_tests = [t for t in tests if TEST_REQUIRED_DRIVER[t] in drivers]
    skipped = [t for t in tests if t not in enabled_tests]
    if skipped:
        print(f"[config] skipping test(s) {skipped}: required driver not in "
              f"enabled drivers {drivers}", flush=True)

    # Optional, per-test-id -- see module docstring for why this is one
    # template per test id rather than one shared template.
    workbook_templates = raw.get("workbook_templates", {}) or {}
    unknown = [t for t in workbook_templates if t not in ALL_TESTS]
    if unknown:
        raise ValueError(f"{path}: \"workbook_templates\" has unknown test id(s) {unknown}; "
                          f"valid ids are {ALL_TESTS}")
    no_template = [t for t in enabled_tests if not workbook_templates.get(t)]
    if no_template:
        print(f"[config] no workbook_templates entry for {no_template}: data still streams to "
              f"the dataset, but no Run/Workbook will be created for these tests", flush=True)

    return {
        "dataset_name": dataset_name,
        "drivers": drivers,
        "tests": enabled_tests,
        "workbook_templates": workbook_templates,
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


# --- FGEN/DIFF analog output/sense rig ---------------------------------------
# Same device roles and rig structure as the ain_ao_route analog rig above
# ("do the same thing"), but driving a sine wave instead of a flat constant:
# FGEN_SINE_FREQ_HZ Hz, amplitude FGEN_SINE_AMPLITUDE_V, offset
# FGEN_SINE_OFFSET_V. Runs every pass through the main loop, independent of
# the sweep-cycle pacing below (sweep() itself still only fires every
# CYCLE_PAUSE_S / after each ~100s sweep completes).
#
# CAVEAT: test_fgen_sweep only gets called once every POLL_S (0.5s default)
# except while an actual sweep is running -- that's ~2 samples/second, right
# at the Nyquist limit for a 1Hz sine and nowhere near enough to reconstruct
# a smooth waveform. This is a software-timed point-by-point value (same as
# every other output in this file -- no hardware-timed waveform generation
# is implemented for LabJack/NI here), so expect a coarse, stair-stepped
# approximation of the sine in Core, not a clean curve. Flagging this rather
# than pretending it's a real analog sine output.
FGEN_SINE_FREQ_HZ = 1.0
FGEN_SINE_AMPLITUDE_V = 1.0
FGEN_SINE_OFFSET_V = 1.0

# instro has no hardware-timed/buffered analog OUTPUT for either driver as
# of this writing (confirmed directly from source: NI's configure_ao_channel
# says "until hardware timed analog output is implemented"; LabJack's just
# calls write_analog_value straight through to ljm.eWriteName) -- so a
# "real" sine has to come from stepping the value fast enough in software,
# not from a true hardware waveform generator. FGEN_SINE_UPDATE_HZ steps
# well above the 1Hz signal's Nyquist rate (40 samples/cycle), and
# FGEN_SINE_BURST_S is how long each call to test_fgen_sweep spends doing
# that stepping before returning control to the main loop -- still a single
# blocking call, no real threads, consistent with the rest of this file
# (fgen.sweep() itself already blocks for ~100s per cycle). The tradeoff:
# while this test is enabled, every pass through the main loop takes
# roughly FGEN_SINE_BURST_S longer, on top of the usual time.sleep(POLL_S).
FGEN_SINE_UPDATE_HZ = 40.0
FGEN_SINE_BURST_S = 0.5

FGEN_ANALOG_DEVICES = [
    # (device_key, driver_family, device_id, output_channel or None, sense_channel or None)
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

    Also drives the FGEN/DIFF analog output/sense rig (see
    FGEN_ANALOG_DEVICES above): a FGEN_SINE_FREQ_HZ sine wave out of
    DAC0/port0 on every output-capable device, and a fresh AIN1/ai1 reading
    on every sense-capable device, published every pass."""
    if "fgen" not in state:
        fgen = FGEN_DIFFControl()
        fgen._assert_34980a(daq)
        fgen._open_all(daq)
        fgen.log.info("Starting automatic sweep (no trigger).")
        state["fgen"] = fgen
        state["fgen_stream_client"] = _StreamClient(publish)
        state["fgen_next_sweep_at"] = 0.0   # run the first sweep immediately

        # One InstroDAQ session per unique analog device, configured with
        # whichever of an AO channel / AI channel apply to it.
        fgen_analog_daqs = {}
        for device_key, driver_family, device_id, out_ch, sense_ch in FGEN_ANALOG_DEVICES:
            if driver_family == "labjack":
                a_daq = InstroDAQ(name=f"fgen_analog_{device_id}", driver=LabJackTSeriesDriver(device_id=device_id))
            elif driver_family == "ni":
                a_daq = InstroDAQ(name=f"fgen_analog_{device_id}", driver=NIDAQDriver(device_id=device_id))
            else:
                raise ValueError(f"unknown analog driver family {driver_family!r}")
            a_daq.open()
            if out_ch:
                a_daq.configure_analog_channel(direction=Direction.OUTPUT, physical_channel=out_ch, alias=f"{device_key}_ao0")
            if sense_ch:
                a_daq.configure_analog_channel(direction=Direction.INPUT, physical_channel=sense_ch, alias=f"{device_key}_ain1")
            fgen_analog_daqs[device_key] = a_daq
        state["fgen_analog_daqs"] = fgen_analog_daqs
        state["fgen_sine_start"] = time.monotonic()

    # Sine output + sense: step FGEN_SINE_UPDATE_HZ times per call for
    # FGEN_SINE_BURST_S (see the constants above for why this is a fast
    # blocking burst rather than one value per pass) -- independent of the
    # sweep-cycle pause below.
    fgen_analog_daqs = state["fgen_analog_daqs"]
    step_s = 1.0 / FGEN_SINE_UPDATE_HZ
    n_steps = max(1, round(FGEN_SINE_BURST_S * FGEN_SINE_UPDATE_HZ))
    for _ in range(n_steps):
        elapsed = time.monotonic() - state["fgen_sine_start"]
        sine_value = FGEN_SINE_OFFSET_V + FGEN_SINE_AMPLITUDE_V * math.sin(2 * math.pi * FGEN_SINE_FREQ_HZ * elapsed)
        readings = {"fgen_sine_cmd": sine_value}
        for device_key, driver_family, device_id, out_ch, sense_ch in FGEN_ANALOG_DEVICES:
            if out_ch:
                fgen_analog_daqs[device_key].write_analog_value(channel=f"{device_key}_ao0", value=sine_value)
            if sense_ch:
                measurement = fgen_analog_daqs[device_key].read_analog()
                sense_alias = f"{device_key}_ain1"
                readings[sense_alias] = float(measurement.channel_data[sense_alias][-1])
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


# --- Analog output/sense rig -------------------------------------------------
# Beyond the TB_AO_MUX crosspoint routing above, this also drives a constant
# value out of DAC0/port0 on every output-capable device and senses AIN1 on
# every sense-capable device -- confirmed module list and roles:
#   T4, T7, T8, USB-6421          -- both output (DAC0/ao0) AND sense (AIN1/ai1)
#   NI-9263 (cDAQ1Mod1)           -- output only (ao0)
#   NI-9204 (cDAQ1Mod2), NI-9207 (cDAQ1Mod3) -- sense only (ai1)
# One InstroDAQ session per unique physical device (not per role) -- T4/T7/
# T8/USB-6421 each get exactly one session with both an AO and an AI channel
# configured on it.
AIN_AO_CONST_VOLTAGE = 1.0   # constant value driven on every output device's DAC0/port0

AIN_AO_ANALOG_DEVICES = [
    # (device_key, driver_family, device_id, output_channel or None, sense_channel or None)
    ("t4", "labjack", "440020473", "DAC0", "AIN1"),
    ("t7", "labjack", "470041016", "DAC0", "AIN1"),
    ("t8", "labjack", "480011030", "DAC0", "AIN1"),
    ("usb6421", "ni", "Dev1", "Dev1/ao0", "Dev1/ai1"),
    ("ni9263", "ni", "cDAQ1Mod1", "cDAQ1Mod1/ao0", None),
    ("ni9204", "ni", "cDAQ1Mod2", None, "cDAQ1Mod2/ai1"),
    ("ni9207", "ni", "cDAQ1Mod3", None, "cDAQ1Mod3/ai1"),
]


def test_ain_ao_route(daq, publish, state):
    """Route whichever single source AIN_AO_SOURCE names onto the shared
    TB_AO_MUX bus. This is the headless port of a Connect script that polled
    a checkbox per source (route_daq1..route_daq4, route_cdaq) every 0.5s and
    re-routed on change -- the only thing that changed is where the
    selection comes from: AIN_AO_SOURCE (a plain constant below) instead of
    client.get_value(cb_id), since there's no Connect client headless. Uses
    the shared `daq` session passed in rather than calling
    AIN_AOControl._create_daq() itself -- that would open a second InstroDAQ
    session and *RST the instrument on top of whatever the other tests just
    configured (see the module docstring).

    Also drives the analog output/sense rig (see AIN_AO_ANALOG_DEVICES
    above): a constant AIN_AO_CONST_VOLTAGE out of DAC0/port0 on every
    output-capable device, and a fresh AIN1/ai1 reading on every
    sense-capable device published every pass."""
    if "ain_ao" not in state:
        tray = AIN_AOControl()
        tray._assert_34980a(daq)
        tray.startup_guard(daq)
        state["ain_ao"] = tray
        state["ain_ao_last_selected"] = None

        # One InstroDAQ session per unique analog device, configured with
        # whichever of an AO channel / AI channel apply to it.
        analog_daqs = {}
        for device_key, driver_family, device_id, out_ch, sense_ch in AIN_AO_ANALOG_DEVICES:
            if driver_family == "labjack":
                a_daq = InstroDAQ(name=f"analog_{device_id}", driver=LabJackTSeriesDriver(device_id=device_id))
            elif driver_family == "ni":
                a_daq = InstroDAQ(name=f"analog_{device_id}", driver=NIDAQDriver(device_id=device_id))
            else:
                raise ValueError(f"unknown analog driver family {driver_family!r}")
            a_daq.open()
            if out_ch:
                out_alias = f"{device_key}_ao0"
                a_daq.configure_analog_channel(direction=Direction.OUTPUT, physical_channel=out_ch, alias=out_alias)
                a_daq.write_analog_value(channel=out_alias, value=AIN_AO_CONST_VOLTAGE)
            if sense_ch:
                sense_alias = f"{device_key}_ain1"
                a_daq.configure_analog_channel(direction=Direction.INPUT, physical_channel=sense_ch, alias=sense_alias)
            analog_daqs[device_key] = a_daq
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

    # Read every sense-capable device's AIN1/ai1 every pass and publish.
    analog_daqs = state["ain_ao_analog_daqs"]
    readings = {}
    for device_key, driver_family, device_id, out_ch, sense_ch in AIN_AO_ANALOG_DEVICES:
        if sense_ch:
            sense_alias = f"{device_key}_ain1"
            measurement = analog_daqs[device_key].read_analog()
            readings[sense_alias] = float(measurement.channel_data[sense_alias][-1])
    publish(readings, tags={"subsystem": "ain_ao_analog"})


def teardown_tests(tests, state, daq, inst):
    """Run once, on the way out."""
    if "multi_counter_clk" in tests and ENABLE_CLK and "multi_counter" in state:
        state["multi_counter"].clk_off(inst)
    if "ain_ao_route" in tests and "ain_ao" in state:
        state["ain_ao"]._open_all(daq)
    if "ain_ao_route" in tests and "ain_ao_analog_daqs" in state:
        for device_key, driver_family, device_id, out_ch, sense_ch in AIN_AO_ANALOG_DEVICES:
            a_daq = state["ain_ao_analog_daqs"][device_key]
            if out_ch:
                try:
                    a_daq.write_analog_value(channel=f"{device_key}_ao0", value=0.0)
                except Exception:
                    pass
            try:
                a_daq.close()
            except Exception:
                pass
    if "fgen_sweep" in tests and "fgen" in state:
        state["fgen"]._open_all(daq)
    if "fgen_sweep" in tests and "fgen_analog_daqs" in state:
        for device_key, driver_family, device_id, out_ch, sense_ch in FGEN_ANALOG_DEVICES:
            a_daq = state["fgen_analog_daqs"][device_key]
            if out_ch:
                try:
                    a_daq.write_analog_value(channel=f"{device_key}_ao0", value=0.0)
                except Exception:
                    pass
            try:
                a_daq.close()
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


# def setup_runs_and_workbooks(client, dataset, tests, workbook_templates):
#     """One Run + one Workbook per enabled test id that has a template_rid in
#     workbook_templates (see module docstring). Returns {test_id: (run,
#     workbook)} for whatever actually got created -- tests with no
#     template_rid configured are simply absent from the returned dict.

#     Each run is open-ended (end=None): it represents "this test session is
#     still live," not a fixed historical window. close_runs() below sets the
#     end timestamp on the way out.
#     """
#     session_start = datetime.now(timezone.utc)
#     created = {}
#     for test_id in tests:
#         template_rid = workbook_templates.get(test_id)
#         if not template_rid:
#             continue  # already logged by load_config()

#         core_run = client.create_run(
#             name=f"{test_id} - {session_start.isoformat()}",
#             start=session_start,
#             end=None,
#         )
#         # ref_name=test_id: templates bind their charts to a dataset by
#         # ref_name (see Run.add_dataset's docstring), and using the test id
#         # itself as the ref_name is what lets the same template keep
#         # resolving correctly against a brand new run (and a brand new
#         # dataset) every time this script runs again.
#         core_run.add_dataset(ref_name=test_id, dataset=dataset)

#         template = client.get_workbook_template(template_rid)
#         workbook = template.create_workbook(run=core_run)
#         print(f"[workbook] {test_id}: run={core_run.rid} workbook={workbook.nominal_url}", flush=True)

#         created[test_id] = (core_run, workbook)
#     return created


def close_runs(core_runs):
    """Run once, on the way out: stamp an end time on every Run this session
    created, so it stops reading as 'still live' in the app."""
    now = datetime.now(timezone.utc)
    for test_id, (core_run, _workbook) in core_runs.items():
        try:
            core_run.update(end=now)
        except Exception as e:
            print(f"[workbook] failed to close out run for {test_id!r}: {e}", flush=True)


def main():
    config = load_config(CONFIG_PATH)
    tests = set(config["tests"])
    print(f"[config] dataset_name={config['dataset_name']!r} drivers={config['drivers']} "
          f"tests={sorted(tests)}", flush=True)

    # --- one shared InstroDAQ session for the whole process ----------------
    # InstroDAQ.open() issues *RST, so this must happen exactly once, before
    # any subsystem touches the instrument. Letting each subsystem create its
    # own InstroDAQ the way the individual per-script versions did would
    # *RST the instrument every time and wipe out whatever the others had
    # just configured.
    daq = InstroDAQ(name="rack", driver=Keysight34980A(MAIN_RESOURCE))
    daq.open()
    inst = daq.driver._visa   # raw pyvisa handle, for the two classes that talk SCPI directly

    idn = inst.query("*IDN?").strip()
    print(f"*IDN? = {idn}", flush=True)
    if "34980A" not in idn:
        raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    # One NominalClient for the whole session: creates the fresh dataset
    # below, and (separately) the Runs/Workbooks in setup_runs_and_workbooks.
    core_client = NominalClient.from_profile("default")
    dataset = core_client.create_dataset(name=config["dataset_name"])
    print(f"[dataset] created {dataset.rid} ({config['dataset_name']!r})", flush=True)
    print(f"[dataset] view live data here: {dataset.nominal_url}", flush=True)

    publisher = NominalCorePublisher(dataset_rid=dataset.rid)
    # core_runs = setup_runs_and_workbooks(core_client, dataset, tests, config["workbook_templates"])

    # Debug visibility: announce the first time each subsystem tag actually
    # streams data (every publish() call in this file passes
    # tags={"subsystem": ...}, so this catches every DAQ/test's first real
    # write to Core), and the first time each test id actually starts
    # running. Both print exactly once per subsystem/test id per session --
    # not a per-pass log, just "this one is live now."
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
        # pass through the loop just calls each enabled test straight, one
        # after the other, in CONTINUOUS_TESTS' order. Each test does its
        # own one-time setup on its first call (see `state`, a dict shared
        # across passes and tests).
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
        # close_runs(core_runs)  # no-op while setup_runs_and_workbooks() is disabled above
        publisher.close()
        daq.close()


if __name__ == "__main__":
    main()
