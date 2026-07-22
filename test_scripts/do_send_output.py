import time
from datetime import datetime

import connect_python
import pyvisa
from instro.daq import InstroDAQ
from instro.daq.drivers.keysight_34980a import Keysight34980A  # ADJUST PATH if needed
from instro.daq.types import Direction, Logic
from btop_test_suite import doDriveControl

@connect_python.main
def main(client: connect_python.Client):
    print(pyvisa.ResourceManager().list_resources(), flush=True)

    dio = doDriveControl()
    daq = dio._create_daq()
    try:
        dio._assert_34980a(daq)
        dio.configure_all(daq)
        dio.log.info("Ready. Toggling DO0 between 1 and 0, twice.")

        HOLD_S = 1.0
        EPSILON_S = 0.02  # small gap before the transition so the plot holds flat

        for level in [1, 0, 1, 0]:
            dio.set_drive(daq, level)
            dio.log.info(f"DO0 (TB_D_OUT) -> {level}")

            # Two points per plateau: one at the start, one just before the
            # next transition. This makes the line plot render a flat hold
            # followed by a sharp vertical edge (square wave), instead of a
            # diagonal ramp between a single point per level.
            client.stream(dio.STREAM_ID, datetime.now(), float(level), name=dio.DO_DRIVE_ALIAS)
            time.sleep(HOLD_S - EPSILON_S)
            client.stream(dio.STREAM_ID, datetime.now(), float(level), name=dio.DO_DRIVE_ALIAS)
            time.sleep(EPSILON_S)
    finally:
        dio.safe_off(daq)
        daq.close()


if __name__ == "__main__":
    main()
