import time
from datetime import datetime
from types import SimpleNamespace

import connect_python
import pyvisa
from instro.daq import InstroDAQ
from instro.daq.drivers.keysight_34980a import Keysight34980A  # ADJUST PATH if needed
from btop_test_suite import FGEN_DIFFControl as daqTrayControl, SafeToTestControl


@connect_python.main
def main(client: connect_python.Client):
    tray = daqTrayControl()
    safe_ctl = SafeToTestControl()
    tray.log.info(f"VISA resources: {pyvisa.ResourceManager().list_resources()}")

    daq = tray._create_daq()
    try:
        tray._assert_34980a(daq)
        tray._open_all(daq)
        tray.log.info("Starting automatic sweep (no trigger).")

        # Run the full source x port sweep automatically, on repeat.
        while True:
            # SafeToTestControl.is_safe() -- see btop_test_suite.py -- same
            # relay-line gate as btop_dc_psu.py's PSUControl.apply_selection().
            # If ANY relay is energized, skip the sweep entirely and keep
            # every crosspoint open instead of routing anything.
            is_safe = safe_ctl.is_safe(client)
            client.stream(tray.STREAM_ID, datetime.now(), 1.0 if is_safe else 0.0, name="safe_to_test")
            if not is_safe:
                tray._open_all(daq)
                tray.log.info(
                    f"NOT safe to test -- skipping sweep, all crosspoints open. "
                    f"Rechecking in {tray.CYCLE_PAUSE_S}s."
                )
                time.sleep(tray.CYCLE_PAUSE_S)
                continue

            try:
                tray.sweep(daq, client, tray.DAC_PORTS, tray.DEST_PORTS)
            except Exception as e:
                tray.log.error(f"Sweep failed: {e}")
                tray._open_all(daq)
            tray.log.info(f"Sweep cycle done; restarting in {tray.CYCLE_PAUSE_S}s.")
            time.sleep(tray.CYCLE_PAUSE_S)
    finally:
        tray._open_all(daq)
        daq.close()


if __name__ == "__main__":
    main()