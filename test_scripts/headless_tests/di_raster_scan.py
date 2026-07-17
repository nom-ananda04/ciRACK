"""di_raster_scan: raster-scans DI2-DI6, driven by its own external
stimulus rig so those inputs carry real, changing data rather than a flat/
floating level (ported from di_raster_scan.py)."""

from datetime import datetime

from instro.daq import InstroDAQ
from instro.daq.drivers.labjack import LabJackTSeriesDriver
from instro.daq.drivers.ni import NIDAQDriver
from instro.daq.types import Direction, Logic

from btop_test_suite import diRasterScan

TEST_ID = "di_raster_scan"
REQUIRED_DRIVER = "keysight_34980a"
KIND = "continuous"

DI_STIMULUS_DEVICES = [
    # (di_alias, driver_family, device_id, physical_channel)
    ("di_2", "labjack", "480011030", "CIO1"),                       # T8        -> DI2
    ("di_3", "labjack", "470041016", "CIO1"),                       # T7        -> DI3
    ("di_4", "labjack", "440020473", "CIO1"),                       # T4        -> DI4
    ("di_5", "ni", "Dev1", "Dev1/port0/line1"),                     # USB-6421  -> DI5
    ("di_6", "ni", "cDAQ1Mod4", "cDAQ1Mod4/port0/line1"),           # NI-9401   -> DI6
]

# Round-robin: only one device drives HIGH at a time, holding for this many
# poll passes before advancing to the next -- so reads show each DI bit
# asserting on its own (proves each wire individually) rather than all five
# changing together.
DI_STIMULUS_HOLD_PASSES = 3


def run(daq, inst, publish, state):
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


def teardown(state, daq, inst):
    if "di_stim_daqs" in state:
        for di_alias, stim_daq in state["di_stim_daqs"].items():
            try:
                stim_daq.write_digital_line(channel=di_alias, data=0)
            except Exception:
                pass
            try:
                stim_daq.close()
            except Exception:
                pass
