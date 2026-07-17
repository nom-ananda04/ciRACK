"""multi_counter_clk: reports the CLK output's on/off state, and turns it
on/off per ENABLE_CLK below -- there's no UI checkbox in headless mode, so
edit this constant directly to change behavior.

Also has T4/T7/T8 (LabJack) each independently count rising edges of that
same CLK pulse train on their own CIO2 line -- proving the pulse train
actually reaches each device (not just trusting the Keysight's own
reported CLK state), same "external listener" idea as do_drive.py's
DO_LISTENER_DEVICES, but counting accumulated edges instead of reading an
instantaneous level.

USB-6421 and NI-9401 are NOT counting yet (see COUNTER_LISTENER_DEVICES'
comment below) -- their hardware edge-counters need a PFI-designated
source terminal (confirmed from NI's own docs), and "DIO2" doesn't tell me
which PFI terminal that is on the USB-6421, or whether NI-9401 (which has
no counter hardware of its own) can even route its DIO2 to the cDAQ
chassis's counter. Rather than guess wrong on real hardware, this is
flagged here as a real open question, not implemented."""

from instro.daq import InstroDAQ
from instro.daq.drivers.labjack import LabJackTSeriesDriver
from labjack import ljm

from btop_test_suite import MultiCounterControl

TEST_ID = "multi_counter_clk"
REQUIRED_DRIVER = "keysight_34980a"
KIND = "continuous"

ENABLE_CLK = False   # MultiCounterControl CLK output

# (device_key, device_id) -- all count rising edges on CIO2 via the LabJack
# T-series "Interrupt Counter" DIO-EF feature (DIO_EF_INDEX=8): a real
# hardware edge counter, not just a level read. Register sequence confirmed
# from LabJack's own docs (support.labjack.com/docs/configuring-reading-a-counter):
#   CIO2_EF_ENABLE = 0   (can't change index while enabled)
#   CIO2_EF_INDEX  = 8   (8 = Interrupt Counter -- counts rising edges)
#   CIO2_EF_ENABLE = 1
# then read CIO2_EF_READ_A for the accumulated count. instro has no
# counter abstraction (confirmed from source) so this talks to the raw LJM
# handle directly via InstroDAQ.driver._handle -- same "reach into the raw
# driver handle" pattern the rest of this project already uses for the
# Keysight's raw pyvisa handle (see headless_rack_control.py's `inst`).
COUNTER_LISTENER_DEVICES = [
    ("t4", "440020473"),
    ("t7", "470041016"),
    ("t8", "480011030"),
]


def run(daq, inst, publish, state):
    if "multi_counter" not in state:
        multi_counter = MultiCounterControl()
        if ENABLE_CLK:
            multi_counter.clk_on(inst)
            multi_counter._clk_state["on"] = True
        state["multi_counter"] = multi_counter

        counter_daqs = {}
        for device_key, device_id in COUNTER_LISTENER_DEVICES:
            counter_daq = InstroDAQ(name=f"counter_{device_key}", driver=LabJackTSeriesDriver(device_id=device_id))
            counter_daq.open()
            handle = counter_daq.driver._handle
            ljm.eWriteName(handle, "CIO2_EF_ENABLE", 0)
            ljm.eWriteName(handle, "CIO2_EF_INDEX", 8)
            ljm.eWriteName(handle, "CIO2_EF_ENABLE", 1)
            counter_daqs[device_key] = counter_daq
        state["counter_daqs"] = counter_daqs

    multi_counter = state["multi_counter"]
    multi_counter._clk_state["on"] = ENABLE_CLK
    readings = {"clk_state": 1.0 if multi_counter._clk_state["on"] else 0.0}

    for device_key, counter_daq in state["counter_daqs"].items():
        handle = counter_daq.driver._handle
        count = ljm.eReadName(handle, "CIO2_EF_READ_A")
        readings[f"{device_key}_pulse_count"] = float(count)

    publish(readings, tags={"subsystem": "multi_counter"})


def teardown(state, daq, inst):
    if ENABLE_CLK and "multi_counter" in state:
        state["multi_counter"].clk_off(inst)
    if "counter_daqs" in state:
        for counter_daq in state["counter_daqs"].values():
            try:
                ljm.eWriteName(counter_daq.driver._handle, "CIO2_EF_ENABLE", 0)
            except Exception:
                pass
            try:
                counter_daq.close()
            except Exception:
                pass
