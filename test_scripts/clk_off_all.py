"""clk_off_all: one-shot utility to force every 34980A module's CLK output
(SOURce:MODule:CLOCk) OFF, and report what state each was in before/after.

Why this exists: SOUR:MOD:CLOC:STAT is instrument-side persistent state --
independent of any Python process. Any script that ever called clk_on() (e.g.
multi_counter_clk.py, DAQ_counter.py) leaves it running on that slot until
something explicitly sends STAT OFF, even after the script exits or crashes.
Run this before testing Counter34980aControl to rule out CLK bleed-through
as a source of unexplained counts.

Usage: python3 clk_off_all.py
"""

import pyvisa

RESOURCE = "USB0::0x0957::0x0507::MY44001757::INSTR"   # confirmed 34980A frame
SLOTS_TO_CHECK = range(1, 9)   # 34980A has up to 8 module slots


def main():
    rm = pyvisa.ResourceManager()
    inst = rm.open_resource(RESOURCE)
    inst.timeout = 5000

    idn = inst.query("*IDN?").strip()
    print(f"*IDN? = {idn}")
    if "34980A" not in idn:
        raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    try:
        for slot in SLOTS_TO_CHECK:
            # Only touch slots that actually have a module installed --
            # querying CLOC:STAT on an empty slot just errors out.
            ctyp = inst.query(f"SYST:CTYP? {slot}").strip()
            if ctyp.strip('"') in ("", "0"):
                continue
            print(f"slot {slot}: {ctyp}")

            try:
                before = inst.query(f"SOUR:MOD:CLOC:STAT? {slot}").strip()
            except pyvisa.errors.VisaIOError as e:
                print(f"  SOUR:MOD:CLOC:STAT? {slot} not supported on this module ({e}); skipping")
                continue

            print(f"  CLK state before: {before!r}")
            if before.strip() in ("0", "OFF"):
                print(f"  already off, nothing to do")
                continue

            inst.write(f"SOUR:MOD:CLOC:STAT OFF,{slot}")
            err = inst.query("SYST:ERR?").strip()
            print(f"  SOUR:MOD:CLOC:STAT OFF,{slot} -> SYST:ERR? {err}")

            after = inst.query(f"SOUR:MOD:CLOC:STAT? {slot}").strip()
            print(f"  CLK state after:  {after!r}")
            if after.strip() not in ("0", "OFF"):
                print(f"  WARNING: slot {slot} still reports CLK on ({after!r}) after turning it off")
    finally:
        inst.close()


if __name__ == "__main__":
    main()
