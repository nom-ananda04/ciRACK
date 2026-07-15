import time
import pyvisa
import connect_python
from btop_test_suite import Counter34980aControl


@connect_python.main
def main(client: connect_python.Client):
    counter = Counter34980aControl()

    rm = pyvisa.ResourceManager()
    print(rm.list_resources(), flush=True)
    inst = rm.open_resource(counter.RESOURCE)
    inst.timeout = 5000

    idn = inst.query("*IDN?").strip()
    counter.log.info(f"*IDN? = {idn}")
    if "34980A" not in idn:
        raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    # Confirm the module in the slot actually is a 34950A (counter-capable).
    # This used to be logged and nothing else -- if the wrong module (or slot)
    # is configured, COUN:FUNC TOT gets silently rejected downstream and the
    # totalizer reads 0 forever with no obvious error, which is exactly the
    # kind of "stuck at 0" symptom that's hard to tell apart from a wiring
    # issue. Fail loudly here instead.
    ctype = inst.query(f"SYST:CTYP? {counter.MODULE_SLOT}").strip()
    counter.log.info(f"SYST:CTYP? {counter.MODULE_SLOT} = {ctype}")
    if "34950" not in ctype:
        raise RuntimeError(
            f"Module in slot {counter.MODULE_SLOT} is not a 34950A (counter-capable): {ctype!r}. "
            f"COUNTER_CHANNEL={counter.COUNTER_CHANNEL} will never count on the wrong module -- "
            f"check MODULE_SLOT / physical slot wiring."
        )

    # Clear any stale errors/output-queue desync from previous runs.
    inst.write("*CLS")

    try:
        counter.configure(inst)
        counter.log.info(f"Ready. Reading totalizer on channel {counter.COUNTER_CHANNEL}.")

        # Follow the TTL level checkbox (checked = 5V, unchecked = 3.3V, the
        # default). ttl_level starts as None so the first loop iteration always
        # applies whatever the checkbox currently reads, instead of silently
        # trusting a hardcoded default.
        ttl_level = None
        last_count = None
        while True:
            want_5v = bool(client.get_value(counter.CB_TTL_5V))
            new_level = "5V" if want_5v else "3.3V"
            if new_level != ttl_level:
                counter.set_logic_level(inst, new_level)
                ttl_level = new_level

            count = counter.read_count(inst)
            if count is None:
                time.sleep(counter.POLL_S)
                continue

            if count != last_count:
                counter.log.info(f"count = {count}")
                last_count = count
            time.sleep(counter.POLL_S)
    finally:
        inst.close()


if __name__ == "__main__":
    main()
