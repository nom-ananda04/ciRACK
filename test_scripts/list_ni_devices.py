"""Quick sanity check after a rewire: confirms what NI-DAQmx currently calls
each connected device/module, so we can check that against what
btop_test_suite.py hardcodes (NI_DEVICES / NI_SOURCE: "Dev1" for the
USB-6421, "cDAQ1Mod4" for the NI-9401). Device/chassis names are assigned by
the driver, not fixed to a physical port -- a USB unplug/replug or a module
moved to a different chassis slot can silently change them, which would
explain a task that creates fine and just always reads 0.

Run with: python list_ni_devices.py
"""

import nidaqmx.system

system = nidaqmx.system.System.local()

print(f"NI-DAQmx version: {system.driver_version}\n")

for device in system.devices:
    print(f"Device name : {device.name}")
    print(f"  Product   : {device.product_type}")
    print(f"  Is chassis : {getattr(device, 'is_simulated', 'n/a')}")
    try:
        print(f"  CI physical channels: {[c.name for c in device.ci_physical_chans]}")
    except Exception as e:
        print(f"  CI physical channels: <error: {e}>")
    try:
        print(f"  Terminals : {list(device.terminals)}")
    except Exception as e:
        print(f"  Terminals: <error: {e}>")
    print()

print("--- Check against btop_test_suite.py's hardcoded names ---")
print("Expected USB-6421 name : Dev1")
print("Expected cDAQ module   : cDAQ1Mod4")
print("If either isn't in the device list above (or the product_type doesn't")
print("match), that's the bug -- update NI_DEVICES/NI_SOURCE in")
print("btop_test_suite.py's MultiCounterControl class to the real names.")
