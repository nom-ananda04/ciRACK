"""multi_counter_clk: reports the CLK output's on/off state, and turns it
on/off per ENABLE_CLK below -- there's no UI checkbox in headless mode, so
edit this constant directly to change behavior.

Also has hardware pulse counters -- T4, T7, USB-6421, NI-9401 -- each
independently counting rising edges of that same CLK pulse train on their
own DIO2/CIO2 line, proving the pulse train actually reaches each device
(not just trusting the Keysight's own reported CLK state). T8 is currently
EXCLUDED: LabJack's own datasheet confirms CIO2 (DIO18) can't do hardware
edge-counting on the T8 with any DIO-EF feature (see LABJACK_COUNTER_DEVICES
below) -- needs a rewire or a software-polling fallback if T8 counting is
required. Loosely based on btop_test_suite.py's MultiCounterControl
(count_labjack/count_nidaqmx), but the DIO-EF register index/name for the
LabJack side was corrected after two real-hardware failures -- see the
comment above LABJACK_COUNTER_DEVICES. NI source-terminal strings are still
taken straight from the reference implementation, not re-derived."""

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

# Two real-hardware failures, in order, both now explained by LabJack's own
# DIO-EF datasheet (https://support.labjack.com/docs/13-2-dio-extended-features-t-series-datasheet):
#
# 1. btop_test_suite.py's MultiCounterControl (and this file's first attempt)
#    used DIO18_EF_INDEX=8 (Interrupt Counter) -> LJM error 2553
#    EF_PIN_TYPE_MISMATCH. Turns out index 8's capable-pin list is DIO4-9 on
#    the T4, DIO0/1/2/3/6/7 on the T7, and DIO0-15 on the T8 -- DIO18 (CIO2)
#    is not in ANY of those lists. Index 8 on CIO2 was never valid on this
#    project's actual hardware, on any of the three models; the reference
#    file's "CIO2 == DIO18, index 8" claim was simply never true.
# 2. This file's second attempt switched to the literal alias "CIO2_EF_..."
#    -> LJM error 1294 LJME_INVALID_NAME. DIO-EF registers are only ever
#    named using the numbered "DIO#_EF_..." form -- "CIOx_EF_..." is not a
#    recognized name at all (unlike plain digital I/O reads/writes, where
#    "CIO0"/"CIO1" work fine elsewhere in this project, e.g.
#    DI_STIMULUS_DEVICES/DO_LISTENER_DEVICES).
#
# The fix: use index 7 (High-Speed Counter) instead of 8, with the numbered
# "DIO18" name. Per the same datasheet, DIO18/CIO2 IS in index 7's
# capable-pin list for the T4 and T7 ("Always available" on T7; on T4 it's
# shared with the async-serial feature, which nothing else in this project
# currently uses, so it's free). The T8's index-7 capable list is DIO6, 7,
# 8, 10, 13, 14, 15 -- DIO18 is not in it, and DIO18 is also out of range
# for index 8 (0-15). So the T8 genuinely cannot hardware-count edges on
# CIO2 with any DIO-EF feature; it's excluded from LABJACK_COUNTER_DEVICES
# below pending a decision to either rewire its sense line to one of its
# actual capable pins, or add a software-polling fallback (much less
# accurate, tied to the 0.5s poll rate).
#
# Register sequence: ENABLE=0 (can't change index while enabled), INDEX=7,
# ENABLE=1, then read READ_A for the accumulated count. instro has no
# counter abstraction (confirmed from source) so this talks to the raw LJM
# handle directly via InstroDAQ.driver._handle -- same "reach into the raw
# driver handle" pattern the rest of this project already uses for the
# Keysight's raw pyvisa handle (see headless_rack_control.py's `inst`).
LJ_DIO = 18   # CIO2, same numbering on both T4 and T7
LJ_EF_INDEX = 7
LABJACK_COUNTER_DEVICES = [
    # (device_key, device_id) -- T8 excluded, see comment above.
    ("t4", "440020473"),
    ("t7", "470041016"),
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
        # hardware edge-counter enabled on CIO2.
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
