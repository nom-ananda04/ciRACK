"""
diag_relay_clear.py

Opens any DAC crosspoint currently CLOSED on the 34923A mux (slot MUX_SLOT),
then verifies each is open via read-back. Drives nothing, does not *RST.
Use to clear a stuck/left-closed reed (e.g. DAC5 / channel 4005) that's
tying a source onto the shared COM bus.
"""

import time

import pyvisa

RESOURCE = "USB0::2391::1287::MY44001757::0::INSTR"
MUX_SLOT = 4
BANK1_BASE = 1
DAC_PORTS = [1, 2, 3, 4, 5]
RELAY_SETTLE_S = 0.5


def _chan(port):
    return f"{MUX_SLOT}{BANK1_BASE + port - 1:03d}"


rm = pyvisa.ResourceManager()
inst = rm.open_resource(RESOURCE)
inst.timeout = 5000

print("IDN:", inst.query("*IDN?").strip())
print()

# Open every DAC crosspoint that is currently closed.
for p in DAC_PORTS:
    ch = _chan(p)
    if inst.query(f"ROUT:CLOS? (@{ch})").strip() == "1":
        print(f"DAC{p} ({ch}) is closed -> opening...")
        inst.write(f"ROUT:OPEN (@{ch})")

time.sleep(RELAY_SETTLE_S)

# Verify.
print("\nPost-open state:")
all_open = True
for p in DAC_PORTS:
    ch = _chan(p)
    closed = inst.query(f"ROUT:CLOS? (@{ch})").strip() == "1"
    all_open = all_open and not closed
    print(f"DAC{p}  channel {ch}:  {'STILL CLOSED <<<' if closed else 'open'}")

print()
if all_open:
    print("All DAC crosspoints open. COM is clear -- safe to drive the cDAQ.")
else:
    print("A crosspoint is STILL closed after ROUT:OPEN -- likely a physically")
    print("stuck reed. Try *RST, or the relay/module may need service.")

inst.close()