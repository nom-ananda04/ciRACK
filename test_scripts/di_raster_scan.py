import time
from datetime import datetime

import connect_python
import pyvisa
from instro.daq import InstroDAQ
from instro.daq.drivers.keysight_34980a import Keysight34980A  # ADJUST PATH if needed
from instro.daq.types import Direction, Logic
from btop_test_suite import diRasterScan, SafeToTestControl


@connect_python.main
def main(client: connect_python.Client):
    print(pyvisa.ResourceManager().list_resources(), flush=True)

    dio = diRasterScan()
    safe_ctl = SafeToTestControl()
    daq = dio._create_daq()
    try:
        dio._assert_34980a(daq)
        dio.configure_all(daq)
        dio.log.info("Ready. Raster scanning DI2-DI6.")

        while True:
            # This script only ever READS DI2-DI6 -- it never drives an
            # output, so there's nothing for SafeToTestControl to actively
            # gate. Still stream it for visibility/consistency with every
            # other script on this rig (see btop_test_suite.py).
            is_safe = safe_ctl.is_safe(client)
            now = datetime.now()
            client.stream(dio.STREAM_ID, now, 1.0 if is_safe else 0.0, name="safe_to_test")

            # continuously read + stream the digital inputs DI2..DI6
            states = dio.read_inputs(daq)
            now = datetime.now()
            for alias, val in states.items():
                client.stream(dio.STREAM_ID, now, float(val), name=alias)
            dio.log.info(f"{now.isoformat()} | published to stream={dio.STREAM_ID!r}: {states}")

            time.sleep(dio.POLL_S)
    finally:
        daq.close()


if __name__ == "__main__":
    main()
