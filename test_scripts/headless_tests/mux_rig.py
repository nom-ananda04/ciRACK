"""Keysight SW_AO_MUX wiring truth -- shared by fgen_sweep.py and
ain_ao_route.py since both round-robin the SAME physical switch. This file
holds ONLY data (the port/channel mapping), no logic -- each test file's
own round-robin loop is still written out independently in that file (no
shared control-flow code between rigs); this is just the one physical
wiring table kept in one place so a future wiring change doesn't have to
be made twice and risk drifting out of sync between the two files.

Confirmed wiring (schematic + RefDes/model table): the 34980A hosts a
single-select crosspoint switch, SW_AO_MUX (34923A w/001 block) -- only ONE
port can be connected at a time. Each of the 5 MUX_PORT_SEQUENCE devices'
AO0/DAC0 output feeds one mux port (bank-relative ports 1H-5H; 1L-5L=
TB_AGND, not a source). Port order is exactly: USB6421=1, T4=2, T7=3,
T8=4, NI-9263=5.
"""

# (device_key, driver_family, device_id, mux_ao_alias, mux_ao_physical_channel_or_None, port_num)
# mux_ao_physical_channel is None for ni9263: it has no self-loop of its
# own, so its existing "ni9263_ao0" channel (configured in fgen_sweep.py/
# ain_ao_route.py's own analog-device list) IS its mux-drive channel
# already -- no separate channel needed.
MUX_PORT_SEQUENCE = [
    ("usb6421", "ni",      "Dev1",      "usb6421_mux_ao", "Dev1/ao0", 1),
    ("t4",      "labjack", "440020473", "t4_mux_ao",      "DAC0",     2),
    ("t7",      "labjack", "470041016", "t7_mux_ao",      "DAC0",     3),
    ("t8",      "labjack", "480011030", "t8_mux_ao",      "DAC0",     4),
    ("ni9263",  "ni",      "cDAQ1Mod1", "ni9263_ao0",     None,       5),
]

# Read and publish AIN1 on ALL SIX of these every pass, regardless of which
# port is currently routed -- only the currently-routed device's reading
# should track the driven signal; the other five are on a disconnected mux
# bus and read whatever's floating there. T4/T7/T8/USB-6421 use a NEW
# alias/channel here: their AIN0/ai0 is already used by that test's own
# self-loop rig, under an alias literally named "..._ain1" for historical
# reasons -- so this uses a distinct "_mux_ain1" alias to avoid colliding
# with that. NI9204/NI9207 have no self-loop of their own -- confirmed
# wiring: the switch's output lands on their Analog Input 0 (ai0) -- so
# their mux-sense channel is configured entirely from this table.
#
# (device_key, driver_family, device_id, mux_ain1_alias, mux_ain1_physical_channel)
MUX_SENSE_DEVICES = [
    ("t4",      "labjack", "440020473", "t4_mux_ain1",      "AIN1"),
    ("t7",      "labjack", "470041016", "t7_mux_ain1",      "AIN1"),
    ("t8",      "labjack", "480011030", "t8_mux_ain1",      "AIN1"),
    ("usb6421", "ni",      "Dev1",      "usb6421_mux_ain1", "Dev1/ai1"),
    ("ni9207",  "ni",      "cDAQ1Mod3", "ni9207_ain1",      "cDAQ1Mod3/ai0"),
    ("ni9204",  "ni",      "cDAQ1Mod2", "ni9204_ain1",      "cDAQ1Mod2/ai0"),
]
