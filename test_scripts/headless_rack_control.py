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

Nominal Core: the dataset RID comes from the config file below (see
CONFIG_PATH), not a hardcoded constant -- nothing is configured yet
(explicitly stubbed per instruction: headless_rack_control.config.json
ships with a placeholder RID). Auth goes through
NominalClient.from_profile("default") inside instro's NominalCorePublisher,
so a ~/.nominal profile needs to exist before this will actually connect.

Config file (headless_rack_control.config.json, alongside this script):

    {
        "dataset_rid": "...",
        "drivers": "all" | ["keysight_34980a", "labjack", "ni_daqmx"],
        "tests": "all" | ["counter_totalize", "di_raster_scan", "do_drive",
                           "multi_counter_clk", "fgen_sweep", "ain_ao_route"]
    }

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

from http import client
import json
import pathlib
import time
from datetime import datetime, timezone

from instro.daq import InstroDAQ
from instro.daq.drivers import Keysight34980A
from instro.daq.drivers.labjack import LabJackTSeriesDriver
from instro.daq.drivers.ni import NIDAQDriver
from instro.lib.publishers.nominal_core import NominalCorePublisher
from instro.lib.types import Measurement

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


def load_config(path: pathlib.Path) -> dict:
    with open(path) as f:
        raw = json.load(f)

    # dataset_rid = raw.get("dataset_rid")
    # if not dataset_rid:
    #     raise ValueError(f"{path}: \"dataset_rid\" is required")

    drivers = _resolve_all(raw.get("drivers", "all"), ALL_DRIVERS, "drivers")
    tests = _resolve_all(raw.get("tests", "all"), ALL_TESTS, "tests")

    # A test only actually runs if its required driver is also enabled.
    enabled_tests = [t for t in tests if TEST_REQUIRED_DRIVER[t] in drivers]
    skipped = [t for t in tests if t not in enabled_tests]
    if skipped:
        print(f"[config] skipping test(s) {skipped}: required driver not in "
              f"enabled drivers {drivers}", flush=True)

    return {
        # "dataset_rid": dataset_rid,
        "drivers": drivers,
        "tests": enabled_tests,
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

def test_di_raster_scan(daq, publish, state):
    """Ported from di_raster_scan.py: per-alias client.stream(...) calls
    become one batched publish() call; dio.log.info(...) calls are
    unchanged, including the real script's per-pass log line (noisy, but
    that's what the real script does)."""
    if "di_scan" not in state:
        di_scan = diRasterScan()
        di_scan._assert_34980a(daq)
        di_scan.configure_all(daq)
        di_scan.log.info("Ready. Raster scanning DI2-DI6.")
        state["di_scan"] = di_scan

    di_scan = state["di_scan"]
    di_states = di_scan.read_inputs(daq)
    now = datetime.now()
    publish(di_states, tags={"subsystem": "di_raster_scan"})
    di_scan.log.info(f"{now.isoformat()} | published to stream={di_scan.STREAM_ID!r}: {di_states}")


def test_do_drive(daq, publish):
    """One-shot square-wave toggle test on DO0 (TB_D_OUT): 1,0,1,0 with a 1s
    hold each, minus a small epsilon before each transition so the plot
    holds flat then snaps instead of ramping. Ported from
    do_send_output.py -- client.stream() calls become publish() calls; the
    four-level sequence and timing are otherwise unchanged. Runs once, not
    every pass (see module docstring): this is a bounded test that toggles
    four times and returns, not a continuous poller."""
    do_drive = doDriveControl()
    do_drive.configure_all(daq)
    do_drive.log.info("Ready. Toggling DO0 between 1 and 0, twice.")

    HOLD_S = 1.0
    EPSILON_S = 0.02   # small gap before the transition so the plot holds flat

    try:
        for level in [1, 0, 1, 0]:
            do_drive.set_drive(daq, level)
            do_drive.log.info(f"DO0 (TB_D_OUT) -> {level}")

            # Two points per plateau: one at the start, one just before the
            # next transition. Renders a flat hold followed by a sharp
            # vertical edge (square wave), instead of a diagonal ramp
            # between a single point per level.
            publish({do_drive.DO_DRIVE_ALIAS: float(level)}, tags={"subsystem": "do_drive"})
            time.sleep(HOLD_S - EPSILON_S)
            publish({do_drive.DO_DRIVE_ALIAS: float(level)}, tags={"subsystem": "do_drive"})
            time.sleep(EPSILON_S)
    finally:
        do_drive.safe_off(daq)


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
    """
    if "fgen" not in state:
        fgen = FGEN_DIFFControl()
        fgen._assert_34980a(daq)
        fgen._open_all(daq)
        fgen.log.info("Starting automatic sweep (no trigger).")
        state["fgen"] = fgen
        state["fgen_stream_client"] = _StreamClient(publish)
        state["fgen_next_sweep_at"] = 0.0   # run the first sweep immediately

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


def test_ain_ao_route(daq, state):
    """Route whichever single source AIN_AO_SOURCE names onto the shared
    TB_AO_MUX bus. This is the headless port of a Connect script that polled
    a checkbox per source (route_daq1..route_daq4, route_cdaq) every 0.5s and
    re-routed on change -- the only thing that changed is where the
    selection comes from: AIN_AO_SOURCE (a plain constant below) instead of
    client.get_value(cb_id), since there's no Connect client headless. Uses
    the shared `daq` session passed in rather than calling
    AIN_AOControl._create_daq() itself -- that would open a second InstroDAQ
    session and *RST the instrument on top of whatever the other tests just
    configured (see the module docstring)."""
    if "ain_ao" not in state:
        tray = AIN_AOControl()
        tray._assert_34980a(daq)
        tray.startup_guard(daq)
        state["ain_ao"] = tray
        state["ain_ao_last_selected"] = None

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


def teardown_tests(tests, state, daq, inst):
    """Run once, on the way out."""
    if "multi_counter_clk" in tests and ENABLE_CLK and "multi_counter" in state:
        state["multi_counter"].clk_off(inst)
    if "ain_ao_route" in tests and "ain_ao" in state:
        state["ain_ao"]._open_all(daq)
    if "fgen_sweep" in tests and "fgen" in state:
        state["fgen"]._open_all(daq)


# Continuous tests, called straight, one after the other, in THIS order,
# every pass through main()'s loop. fgen_sweep is here too, not in
# ONE_SHOT_TESTS -- it repeats forever (see its docstring for the
# main-loop-stalls-per-cycle tradeoff that comes with that).
CONTINUOUS_TESTS = [
    ("di_raster_scan", lambda daq, inst, publish, state: test_di_raster_scan(daq, publish, state)),
    ("counter_totalize", lambda daq, inst, publish, state: test_counter_totalize(inst, publish, state)),
    ("multi_counter_clk", lambda daq, inst, publish, state: test_multi_counter_clk(inst, publish, ENABLE_CLK, state)),
    ("ain_ao_route", lambda daq, inst, publish, state: test_ain_ao_route(daq, state)),
    ("fgen_sweep", lambda daq, inst, publish, state: test_fgen_sweep(daq, publish, state)),
]

# One-shot tests: id -> the callable to run once, before the main loop starts.
# do_drive is here, not in CONTINUOUS_TESTS -- it's a bounded four-toggle
# test ported from do_send_output.py, not a per-pass poller (see its
# docstring); it handles its own teardown internally (finally: safe_off()).
ONE_SHOT_TESTS = {
    "do_drive": lambda daq, inst, publish: test_do_drive(daq, publish),
}


def main():
    config = load_config(CONFIG_PATH)
    tests = set(config["tests"])
    print(f"[config] drivers={config['drivers']} "
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

    dataset = client.create_dataset(name="Hardware CI RACK stream")
    publisher = NominalCorePublisher(dataset_rid=dataset.rid)

    def publish(channel_data: dict, tags: dict | None = None):
        ts = _now_ns()
        publisher.publish(
            Measurement(
                channel_data={name: [float(v)] for name, v in channel_data.items()},
                timestamps=[ts],
                tags=tags,
            )
        )

    try:
        # One-shot tests run once, before the main loop starts.
        for test_id, run in ONE_SHOT_TESTS.items():
            if test_id in tests:
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
                        run(daq, inst, publish, state)
                time.sleep(POLL_S)
        finally:
            teardown_tests(tests, state, daq, inst)
    finally:
        publisher.close()
        daq.close()


if __name__ == "__main__":
    main()
