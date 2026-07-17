

import json
import pathlib
import time
from datetime import datetime, timezone

from instro.daq import InstroDAQ
from instro.daq.drivers import Keysight34980A
from instro.lib.publishers.nominal_core import NominalCorePublisher
from instro.lib.types import Measurement
from nominal.core import NominalClient

from btop_test_suite import Counter34980aControl

from headless_tests import (
    do_drive,
    di_raster_scan,
    counter_totalize,
    multi_counter_clk,
    ain_ao_loopback,
    ain_ao_route,
    fgen_sweep,
)

# ============================================================================
# Test registry -- each test's full setup/run/teardown logic lives in its
# own file under headless_tests/ (see headless_tests/__init__.py for the
# interface every module implements). This file only orchestrates: config,
# driver connection, Run/dataset plumbing, and running each enabled test
# strictly one at a time, start to finish, before moving to the next.
#
# TEST_MODULES' order is the run order: do_drive first (a bounded one-shot
# test), then the continuous tests in this order -- same relative order
# this project has always used. Every enabled test gets its own dedicated
# time slot (see DEFAULT_TEST_DURATION_S below); none of them interleave.
# ============================================================================
TEST_MODULES = [do_drive, di_raster_scan, counter_totalize, multi_counter_clk,
                ain_ao_loopback, ain_ao_route, fgen_sweep]
TEST_MODULE_BY_ID = {m.TEST_ID: m for m in TEST_MODULES}

ALL_DRIVERS = ["keysight_34980a", "labjack", "ni_daqmx"]
ALL_TESTS = [m.TEST_ID for m in TEST_MODULES]
TEST_REQUIRED_DRIVER = {m.TEST_ID: m.REQUIRED_DRIVER for m in TEST_MODULES}


# ============================================================================
# Config file
# ============================================================================
CONFIG_PATH = pathlib.Path(__file__).with_name("headless_rack_control.config.json")


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
    # get_and_clean_asset's docstring). Optional -- if omitted, data still
    # streams to the dataset, it just won't be organized under a Run/Asset.
    asset_rid = raw.get("asset_rid") or None

    # test_duration_s: how long each continuous test runs, by itself,
    # before main() moves on to the next enabled test (see
    # DEFAULT_TEST_DURATION_S below).
    test_duration_s = float(raw.get("test_duration_s") or DEFAULT_TEST_DURATION_S)

    return {
        "dataset_rid": dataset_rid,
        "dataset_name": dataset_name,
        "drivers": drivers,
        "tests": enabled_tests,
        "asset_rid": asset_rid,
        "test_duration_s": test_duration_s,
    }


# ============================================================================
# Headless config -- replaces the Connect checkboxes each subsystem used to
# read via client.get_value(). There's no UI here, so these are plain
# constants: edit them to change behavior instead of clicking a checkbox.
# Per-test constants (ENABLE_CLK, the FGEN sine constants, the mux hold-pass
# counts, etc.) now live in each test's own file under headless_tests/ --
# only orchestration-level config stays here.
# ============================================================================
MAIN_RESOURCE = Counter34980aControl.RESOURCE           # all test files point at the same 34980A frame
POLL_S = 0.5

# How long each continuous test gets to run, by itself, before main() tears
# it down and moves on to the next enabled test. None of these tests have a
# natural end of their own except fgen_sweep (one full DAC x port sweep is
# ~100s per its own docstring -- give it a duration of at least that if you
# want full sweeps to complete rather than being cut off mid-route).
DEFAULT_TEST_DURATION_S = 60.0


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1e9)


def get_and_clean_asset(client, asset_rid, tests):
    """Fetch the one persistent Asset (asset_rid) every test's Run binds to,
    and clear out any stale data scopes left directly on it under an
    enabled test id's name.

    ASSET_RID is what keeps things organized run after run: each test's Run
    is created via asset.create_run(..., asset_rids=[asset.rid]), which is
    what ties it to the one constant asset -- that structural link is
    enough; the dataset itself is only attached to the Run
    (run.add_dataset), not separately to the asset.

    IMPORTANT: this used to *also* call asset.add_dataset(data_scope_name=
    test_id, dataset=dataset) directly on the asset, in addition to
    attaching it to the run. That 409'd on real hardware ("Scout:
    RefNamesAlreadyUsed", refNames=[test_id]) the moment run.add_dataset ran
    right after it -- a Run tied to an asset apparently shares the same
    ref-name namespace as that asset's own directly-attached data scopes, so
    registering the same name in both places at once collides. Attaching at
    the Run level only (in start_run() below) is what avoids that going
    forward -- but that old code already ran against the real asset before
    this fix, so it may have left stale data scopes directly on the asset
    under some test ids' names, which would still collide. So any leftover
    direct scope matching an enabled test id gets removed here, once, up
    front.
    """
    asset = client.get_asset(asset_rid)

    stale_scopes = {name for name, _scope_type in asset.list_data_scopes()} & set(tests)
    if stale_scopes:
        asset.remove_data_scopes(names=sorted(stale_scopes))

    return asset


def start_run(asset, dataset, test_id):
    """Create and return one Run for exactly this test's time slot, tied to
    the persistent asset (or None if no asset is configured -- data still
    streams to the dataset either way). The run is open-ended (end=None)
    until close_run() below stamps an end the moment this test's slot is
    over -- since tests are strictly sequential, this run's start/end
    genuinely bound when this one test was executing, not the whole
    session.

    IMPORTANT (viewing this data): a workbook/chart built against the
    persistent asset shows EVERY Run ever tied to that asset, across every
    session this script has ever run. To see just THIS test's own data with
    nothing else overlaid, open the Run's own page directly (the printed
    nominal_url below), not the asset-level/workbook aggregate view.
    """
    if asset is None:
        return None
    run_start = datetime.now(timezone.utc)
    core_run = asset.create_run(
        name=f"{test_id} - {run_start.isoformat()}",
        start=run_start,
        end=None,
    )
    core_run.add_dataset(ref_name=test_id, dataset=dataset)
    print(f"[run] {test_id}: run={core_run.rid}  view just this run: {core_run.nominal_url}", flush=True)
    return core_run


def close_run(core_run, test_id):
    """Stamp an end time on this one test's Run the moment its slot is
    over, so it stops reading as 'still live' in the app."""
    if core_run is None:
        return
    try:
        core_run.update(end=datetime.now(timezone.utc))
    except Exception as e:
        print(f"[run] failed to close out run for {test_id!r}: {e}", flush=True)


def main():
    config = load_config(CONFIG_PATH)
    tests = set(config["tests"])
    print(f"[config] dataset_name={config['dataset_name']!r} drivers={config['drivers']} "
          f"tests={sorted(tests)}", flush=True)

    daq = InstroDAQ(name="rack", driver=Keysight34980A(MAIN_RESOURCE))
    daq.open()
    inst = daq.driver._visa   # raw pyvisa handle, for the test files that talk SCPI directly

    idn = inst.query("*IDN?").strip()
    print(f"*IDN? = {idn}", flush=True)
    if "34980A" not in idn:
        raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    # One NominalClient for the whole session: resolves the dataset below
    # (either a fixed dataset_rid, reused across runs, or a fresh one), and
    # (separately) creates the Runs below, all bound to the same asset_rid.
    core_client = NominalClient.from_profile("default")
    if config["dataset_rid"]:
        dataset = core_client.get_dataset(config["dataset_rid"])
        print(f"[dataset] using existing {dataset.rid} ({dataset.name!r})", flush=True)
    else:
        dataset = core_client.create_dataset(name=config["dataset_name"])
        print(f"[dataset] created {dataset.rid} ({config['dataset_name']!r})", flush=True)
    print(f"[dataset] view live data here: {dataset.nominal_url}", flush=True)

    publisher = NominalCorePublisher(dataset_rid=dataset.rid)
    asset = get_and_clean_asset(core_client, config["asset_rid"], tests) if config["asset_rid"] else None

    # Debug visibility: print once (not every pass) the first time each
    # subsystem tag actually streams data.
    seen_subsystems = set()

    # Every channel name gets prefixed with "test_<test_id>." (e.g.
    # "test_do_drive.do0") so it's obvious in Core which test a channel
    # belongs to. _current_test_id is updated right before each test starts
    # (below) and read here since publish() is shared across every test.
    _current_test_id = [None]

    def publish(channel_data: dict, tags: dict | None = None):
        subsystem = (tags or {}).get("subsystem")
        if subsystem and subsystem not in seen_subsystems:
            seen_subsystems.add(subsystem)
            print(f"[stream] {subsystem!r} is now streaming to Core", flush=True)
        prefix = f"test_{_current_test_id[0]}." if _current_test_id[0] else ""
        ts = _now_ns()
        publisher.publish(
            Measurement(
                channel_data={f"{prefix}{name}": [float(v)] for name, v in channel_data.items()},
                timestamps=[ts],
                tags=tags,
            )
        )

    try:
        for module in TEST_MODULES:
            test_id = module.TEST_ID
            if test_id not in tests:
                continue

            print(f"[test] {test_id!r} starting", flush=True)
            _current_test_id[0] = test_id
            core_run = start_run(asset, dataset, test_id)
            try:
                if module.KIND == "one_shot":
                    module.run(daq, inst, publish)
                else:
                    state = {}
                    deadline = time.monotonic() + config["test_duration_s"]
                    try:
                        while time.monotonic() < deadline:
                            module.run(daq, inst, publish, state)
                            time.sleep(POLL_S)
                    finally:
                        module.teardown(state, daq, inst)
            finally:
                close_run(core_run, test_id)
            print(f"[test] {test_id!r} complete", flush=True)
    finally:
        publisher.close()
        daq.close()


if __name__ == "__main__":
    main()
