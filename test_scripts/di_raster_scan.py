import time
from datetime import datetime

import connect_python
import pyvisa
from instro.daq import InstroDAQ
from instro.daq.drivers.keysight_34980a import Keysight34980A  # ADJUST PATH if needed
from instro.daq.types import Direction, Logic
from btop_test_suite import diRasterScan


@connect_python.main
def main(client: connect_python.Client):
    print(pyvisa.ResourceManager().list_resources(), flush=True)

    dio = diRasterScan()
    daq = dio._create_daq()
    try:
        dio._assert_34980a(daq)
        dio.configure_all(daq)
        dio.log.info("Ready. Raster scanning DI2-DI6.")

        while True:
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
