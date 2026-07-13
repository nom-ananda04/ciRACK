import time
import pyvisa
import connect_python
 
RESOURCE = "USB0::0x0957::0x0507::MY44001757::INSTR"   # confirmed 34980A frame
 
MODULE_SLOT = 8
COUNTER_CHANNEL = f"{MODULE_SLOT}301"  # counter 1; use f"{MODULE_SLOT}302" for counter 2
 
THRESHOLD_V = 1.5   # input threshold; cDAQ 9401 drives 5V TTL so 1.5V is safe
POLL_S = 0.5
 
log = connect_python.get_logger(__name__)
 
 
def check_err(inst, context=""):
    err = inst.query("SYST:ERR?").strip()
    log.info(f"SYST:ERR? {context} -> {err}")
    return err.startswith("+0")
 
 
def safe_query(inst, cmd):
    """Query with device-clear recovery so one timeout doesn't desync the session."""
    try:
        return inst.query(cmd).strip()
    except pyvisa.errors.VisaIOError as e:
        log.info(f"query {cmd!r} failed ({e}); sending device clear and retrying once")
        try:
            inst.clear()
        except Exception:
            pass
        return inst.query(cmd).strip()
 
 
@connect_python.main
def main(client: connect_python.Client):
    rm = pyvisa.ResourceManager()
    print(rm.list_resources(), flush=True)
    inst = rm.open_resource(RESOURCE)
    inst.timeout = 5000
 
    idn = inst.query("*IDN?").strip()
    log.info(f"*IDN? = {idn}")
    if "34980A" not in idn:
        raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")
 
    # Confirm the module in the slot actually is a 34950A (counter-capable).
    ctype = inst.query(f"SYST:CTYP? {MODULE_SLOT}").strip()
    log.info(f"SYST:CTYP? {MODULE_SLOT} = {ctype}")
 
    # Clear any stale errors/output-queue desync from previous runs.
    inst.write("*CLS")
 
    try:
        # Select the totalize function on the counter channel.
        inst.write(f"COUN:FUNC TOT,(@{COUNTER_CHANNEL})")
        ok_func = check_err(inst, "after COUN:FUNC TOT")
 
        # Count rising edges.
        inst.write(f"COUN:SLOP POS,(@{COUNTER_CHANNEL})")
        check_err(inst, "after COUN:SLOP")
 
        # Gate source: INTernal so the counter free-runs after INITiate rather
        # than requiring an external gate edge. Param is {INTernal|EXTernal}
        # (NOT IMM). [SENSe:]COUNter:GATE:SOURce
        inst.write(f"COUN:GATE:SOUR INT,(@{COUNTER_CHANNEL})")
        check_err(inst, "after COUN:GATE:SOUR INT")
 
        # Gate polarity: INVerted so a LOW/floating external gate ENABLES
        # counting. The GATE H terminal is unwired; if the gate still applies
        # in totalize mode, NORMal polarity (count-while-high) would block all
        # counting -- exactly a permanent count=0. {NORMal|INVerted}
        # If you later tie GATE H physically high, switch this back to NORM.
        inst.write(f"COUN:GATE:POL INV,(@{COUNTER_CHANNEL})")
        check_err(inst, "after COUN:GATE:POL INV")
 
        # Read without resetting the count (monotonic). {READ|RRESet}
        inst.write(f"COUN:TOT:TYPE READ,(@{COUNTER_CHANNEL})")
        check_err(inst, "after COUN:TOT:TYPE READ")
 
        # Input threshold voltage (signal must cross this to register an edge).
        inst.write(f"COUN:THR:VOLT {THRESHOLD_V},(@{COUNTER_CHANNEL})")
        check_err(inst, "after COUN:THR:VOLT")
 
        # Zero the accumulated count.
        inst.write(f"COUN:TOT:CLE:IMM (@{COUNTER_CHANNEL})")
        check_err(inst, "after COUN:TOT:CLE:IMM")
 
        # START the counter. With an internal gate, INITiate triggers counting
        # immediately. Without this, a correctly-configured totalizer reads 0.
        inst.write(f"COUN:INIT (@{COUNTER_CHANNEL})")
        check_err(inst, "after COUN:INIT")
 
        if not ok_func:
            log.info("WARNING: COUN:FUNC TOT was rejected -- check module type / channel above")
 
        log.info(f"Ready. Reading totalizer on channel {COUNTER_CHANNEL}.")
 
        last_count = None
        while True:
            resp = safe_query(inst, f"COUN:TOT:DATA? (@{COUNTER_CHANNEL})")
            try:
                count = int(float(resp))
            except ValueError:
                log.info(f"unexpected totalizer response: {resp!r}")
                time.sleep(POLL_S)
                continue
 
            if count != last_count:
                log.info(f"count = {count}")
                last_count = count
            time.sleep(POLL_S)
    finally:
        inst.close()
 
 
if __name__ == "__main__":
    main()