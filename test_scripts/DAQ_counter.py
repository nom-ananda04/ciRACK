import time
from datetime import datetime
import connect_python
import pyvisa
from btop_test_suite import MultiCounterControl


@connect_python.main
def main(client: connect_python.Client):

    counter = MultiCounterControl()
    counter.log.info("Multi-counter ready. Check ONE device box in the COUNTER TEST tab.")

    # Hold a 34980A session open for the whole run to drive the CLK output.
    rm = pyvisa.ResourceManager()
    inst = rm.open_resource(counter.RESOURCE_34980A)
    inst.timeout = 5000
    inst.write("*CLS")
    counter.log.info(f"34980A: {inst.query('*IDN?').strip()}")

    clk_running = False
    active = None
    try:
        while True:
            # --- CLK output: follow the enable checkbox ---
            want_clk = bool(client.get_value(counter.CB_CLK))
            if want_clk and not clk_running:
                counter.log.info(f"Enabling 34980A CLK output ({counter.CLK_FREQ_HZ} Hz).")
                counter.clk_on(inst)
                clk_running = True
            elif not want_clk and clk_running:
                counter.log.info("Disabling 34980A CLK output.")
                counter.clk_off(inst)
                clk_running = False
            counter._clk_state["on"] = clk_running

            # Stream the CLK ON/OFF state (1/0) so it plots in Connect.
            counter.stream_clk(client)

            # --- Counter: follow the device selection ---
            cb_id = counter.selected_checkbox(client)
            if cb_id is None:
                if active is not None:
                    counter.log.info("No device selected.")
                    active = None
                time.sleep(counter.POLL_S)
                continue

            # NOTE: the counter loop below blocks until the device selection
            # changes, so the CLK checkbox is only re-evaluated between device
            # switches (fine for the usual "enable CLK, then count" workflow).
            active = cb_id
            try:
                if cb_id in counter.LABJACKS:
                    counter.count_labjack(client, cb_id)
                elif cb_id in counter.NI_DEVICES:
                    counter.count_nidaqmx(client, cb_id)
            except Exception as e:
                counter.log.info(f"counter error on {cb_id}: {e}")
                time.sleep(counter.POLL_S)
    finally:
        counter.clk_off(inst)
        inst.close()


if __name__ == "__main__":
    main()
