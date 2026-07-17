"""multi_counter_clk: reports the CLK output's on/off state, and turns it
on/off per ENABLE_CLK below -- there's no UI checkbox in headless mode, so
edit this constant directly to change behavior.

Also has all five counting DAQs -- T4/T7/T8, USB-6421, NI-9401 -- each
independently count rising edges of that same CLK pulse train on their own
DIO2/CIO2 line, proving the pulse train actually reaches each device (not
just trusting the Keysight's own reported CLK state). Ported from the
already-working reference implementation in btop_test_suite.py's
MultiCounterControl (count_labjack/count_nidaqmx) -- the exact register
names and NI source-terminal strings below are taken straight from there,
not re-derived or guessed."""

from instro.daq import InstroDAQ
from instro.daq.drivers.labjack import LabJackTSeriesDriver
from labjack import ljm
import nidaqmx
from nidaqmx.constants import Edge, CountDirection

from btop_test_suite import MultiCounterControl

TEST_ID = "multi_counter_clk"
REQUIRED_DRIVER = "keysight_34980a"
KIND = "continuous"

ENABLE_CLK = False   # MultiCounterControl CLK output

# CIO2 == DIO18 on T4/T7/T8 (confirmed in btop_test_suite.py's
# MultiCounterControl.LJ_DIO) -- LJM's DIO-EF (extended feature) registers
# need the literal DIO number; "CIO2_EF_INDEX" is NOT a valid register name
# even though plain "CIO2" works fine as a pin alias elsewhere in this
# project for simple digital I/O. This is a real, already-hit bug (see
# btop_test_suite.py's own comment): an earlier version used "CIO2_EF_..."
# directly and it silently never counted anything.
#
# DIO_EF_INDEX=8 is the Interrupt Counter feature (counts rising edges) --
# index 7 is a different feature ("High-Speed Counter") that needs extra
# clock setup and isn't valid on every line; using 7 would silently arm the
# wrong feature and never count (also already hit once, per the same
# comment). Register sequence: ENABLE=0 (can't change index while enabled),
# INDEX=8, ENABLE=1, then read READ_A for the accumulated count. instro has
# no counter abstraction (confirmed from source) so this talks to the raw
# LJM handle directly via InstroDAQ.driver._handle -- same "reach into the
# raw driver handle" pattern the rest of this project already uses for the
# Keysight's raw pyvisa handle (see headless_rack_control.py's `inst`).
LJ_DIO = 18
LJ_EF_INDEX = 8
LABJACK_COUNTER_DEVICES = [
    # (device_key, device_id)
    ("t4", "440020473"),
    ("t7", "470041016"),
    ("t8", "480011030"),
]

# (device_key, counter_channel, source_terminal) -- both confirmed working
# in btop_test_suite.py's MultiCounterControl.NI_DEVICES/NI_SOURCE. NI-9401
# has no counter hardware of its own; it borrows the parent cDAQ chassis's
# counter, addressed through the module's own namespace/PFI terminal.
NI_COUNTER_DEVICES = [
    ("usb6421", "Dev1/ctr0", "/Dev1/PFI2"),
    ("ni9401", "cDAQ1Mod4/ctr0", "/cDAQ1Mod4/PFI5"),
]


def run(daq, inst, publish, state):
    if "multi_counter" not in state:
        multi_counter = MultiCounterControl()
        if ENABLE_CLK:
            multi_counter.clk_on(inst)
            multi_counter._clk_state["on"] = True
        state["multi_counter"] = multi_counter

        # LabJack counters: one InstroDAQ session per device, each with a
        # hardware edge-counter enabled on DIO18 (CIO2).
        counter_daqs = {}
        for device_key, device_id in LABJACK_COUNTER_DEVICES:
            counter_daq = InstroDAQ(name=f"counter_{device_key}", driver=LabJackTSeriesDriver(device_id=device_id))
            counter_daq.open()
            handle = counter_daq.driver._handle
            ljm.eWriteName(handle, f"DIO{LJ_DIO}_EF_ENABLE", 0)
            ljm.eWriteName(handle, f"DIO{LJ_DIO}_EF_INDEX", LJ_EF_INDEX)
            ljm.eWriteName(handle, f"DIO{LJ_DIO}_EF_ENABLE", 1)
            counter_daqs[device_key] = counter_daq
        state["counter_daqs"] = counter_daqs

        # NI counters: one CI Count Edges task per device.
        ni_tasks = {}
        for device_key, counter_chan, source_term in NI_COUNTER_DEVICES:
            task = nidaqmx.Task()
            task.ci_channels.add_ci_count_edges_chan(
                counter_chan,
                edge=Edge.RISING,
                initial_count=0,
                count_direction=CountDirection.COUNT_UP,
            )
            task.ci_channels[0].ci_count_edges_term = source_term
            task.start()
            ni_tasks[device_key] = task
        state["ni_tasks"] = ni_tasks

    multi_counter = state["multi_counter"]
    multi_counter._clk_state["on"] = ENABLE_CLK
    readings = {"clk_state": 1.0 if multi_counter._clk_state["on"] else 0.0}

    for device_key, counter_daq in state["counter_daqs"].items():
        handle = counter_daq.driver._handle
        count = ljm.eReadName(handle, f"DIO{LJ_DIO}_EF_READ_A")
        readings[f"{device_key}_pulse_count"] = float(count)

    for device_key, task in state["ni_tasks"].items():
        readings[f"{device_key}_pulse_count"] = float(task.read())

    publish(readings, tags={"subsystem": "multi_counter"})


def teardown(state, daq, inst):
    if ENABLE_CLK and "multi_counter" in state:
        state["multi_counter"].clk_off(inst)
    if "counter_daqs" in state:
        for counter_daq in state["counter_daqs"].values():
            try:
                ljm.eWriteName(counter_daq.driver._handle, f"DIO{LJ_DIO}_EF_ENABLE", 0)
            except Exception:
                pass
            try:
                counter_daq.close()
            except Exception:
                pass
    if "ni_tasks" in state:
        for task in state["ni_tasks"].values():
            try:
                task.stop()
            except Exception:
                pass
            try:
                task.close()
            except Exception:
                pass
