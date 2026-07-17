"""ain_ao_loopback: drives a constant AIN_AO_CONST_VOLTAGE on T4/T7/T8/
USB-6421's own self-loop -- DAC1->AIN0 (LabJack) / ao1->ai0 (NI), confirmed
wiring. This is the always-on, mux-INDEPENDENT signal path: each device
drives and senses its own voltage directly on its own box, with no
Keysight switch involved at all. See ain_ao_route.py for the separate
SW_AO_MUX round-robin test (same constant voltage, but routed through the
switch one port at a time)."""

from instro.daq import InstroDAQ
from instro.daq.drivers.labjack import LabJackTSeriesDriver
from instro.daq.drivers.ni import NIDAQDriver
from instro.daq.types import Direction

TEST_ID = "ain_ao_loopback"
REQUIRED_DRIVER = "keysight_34980a"
KIND = "continuous"

AIN_AO_CONST_VOLTAGE = 1.0

# (device_key, driver_family, device_id, out_channel, sense_channel)
# Only the four devices with a real self-loop of their own belong here --
# NI-9263/NI-9204/NI-9207 have no self-loop (they're mux-only, see
# ain_ao_route.py / mux_rig.py) so they're intentionally absent from this
# list.
AIN_AO_LOOPBACK_DEVICES = [
    ("t4", "labjack", "440020473", "DAC1", "AIN0"),
    ("t7", "labjack", "470041016", "DAC1", "AIN0"),
    ("t8", "labjack", "480011030", "DAC1", "AIN0"),
    ("usb6421", "ni", "Dev1", "Dev1/ao1", "Dev1/ai0"),
]


def run(daq, inst, publish, state):
    if "analog_daqs" not in state:
        analog_daqs = {}
        for device_key, driver_family, device_id, out_ch, sense_ch in AIN_AO_LOOPBACK_DEVICES:
            if driver_family == "labjack":
                analog_daq = InstroDAQ(name=f"loop_{device_key}", driver=LabJackTSeriesDriver(device_id=device_id))
            elif driver_family == "ni":
                analog_daq = InstroDAQ(name=f"loop_{device_key}", driver=NIDAQDriver(device_id=device_id))
            else:
                raise ValueError(f"unknown analog driver family {driver_family!r}")
            analog_daq.open()
            analog_daq.configure_analog_channel(direction=Direction.OUTPUT, physical_channel=out_ch,
                                                 alias=f"{device_key}_ao0")
            analog_daq.write_analog_value(channel=f"{device_key}_ao0", value=AIN_AO_CONST_VOLTAGE)
            analog_daq.configure_analog_channel(direction=Direction.INPUT, physical_channel=sense_ch,
                                                 alias=f"{device_key}_ain1")
            analog_daqs[device_key] = analog_daq
        state["analog_daqs"] = analog_daqs

    analog_daqs = state["analog_daqs"]
    readings = {}
    for device_key, driver_family, device_id, out_ch, sense_ch in AIN_AO_LOOPBACK_DEVICES:
        analog_daq = analog_daqs[device_key]
        measurement = analog_daq.read_analog()
        if isinstance(measurement, list):
            measurement = measurement[0]
        readings[f"{device_key}_ain1"] = float(
            measurement.channel_data[f"{analog_daq.name}.{device_key}_ain1"][-1])
    publish(readings, tags={"subsystem": "ain_ao_loopback"})


def teardown(state, daq, inst):
    if "analog_daqs" not in state:
        return
    for device_key, analog_daq in state["analog_daqs"].items():
        try:
            analog_daq.write_analog_value(channel=f"{device_key}_ao0", value=0.0)
        except Exception:
            pass
        try:
            analog_daq.close()
        except Exception:
            pass
