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
    ctype = inst.query(f"SYST:CTYP? {counter.MODULE_SLOT}").strip()
    counter.log.info(f"SYST:CTYP? {counter.MODULE_SLOT} = {ctype}")

    # Clear any stale errors/output-queue desync from previous runs.
    inst.write("*CLS")

    try:
        counter.configure(inst)
        counter.log.info(f"Ready. Reading totalizer on channel {counter.COUNTER_CHANNEL}.")

        last_count = None
        while True:
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
