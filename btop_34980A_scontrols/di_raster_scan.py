import time
from datetime import datetime

import connect_python
import pyvisa
from instro.daq import InstroDAQ
from instro.daq.drivers.keysight_34980a import Keysight34980A  # ADJUST PATH if needed
from instro.daq.types import Direction, Logic


# Config
RESOURCE = "USB0::0x0957::0x0507::MY44001757::INSTR"   # confirmed 34980A frame

MODULE_SLOT = 8
DIO_BANK = 201  # bank 2 -- where DIO is physically wired

DI_INPUT_BITS = [2, 3, 4, 5, 6]

# Aliases (used as channel/stream names when published)
DI_INPUT_ALIAS = {b: f"di_{b}" for b in DI_INPUT_BITS}

LOGIC_LEVEL_V = 2.5

# Timing
POLL_S = 0.20

STREAM_ID = "dio_tray"

log = connect_python.get_logger(__name__)


def _line(bit: int) -> str:
    """Keysight physical channel string for a single DIO line, e.g. '8101/0'."""
    return f"{MODULE_SLOT}{DIO_BANK}/{bit}"


class diRasterScan():
    def _create_daq(self):
        """Create and open a fresh 34980A DAQ instance."""
        daq = InstroDAQ(name="dio_tray", driver=Keysight34980A(RESOURCE))
        daq.open()
        return daq

    def _assert_34980a(self, daq):
        idn = daq.driver._visa.query("*IDN?").strip()
        log.info(f"*IDN? = {idn}")
        if "34980A" not in idn:
            raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    def configure_all(self, daq):
        """Configure DI2..DI6 per the schematic pin map as digital inputs."""
        for b in DI_INPUT_BITS:
            daq.configure_digital_line(
                direction=Direction.INPUT,
                physical_channel=_line(b),
                alias=DI_INPUT_ALIAS[b],
                logic=Logic.HIGH,
                logic_level=LOGIC_LEVEL_V,
            )
        log.info("configured: DI2-6 inputs")

    def read_inputs(self, daq) -> dict:
        """Read DI2..DI6 and return {alias: 0/1}."""
        states = {}
        for b in DI_INPUT_BITS:
            states[DI_INPUT_ALIAS[b]] = int(daq.read_digital_line(channel=DI_INPUT_ALIAS[b]).latest)
        return states


@connect_python.main
def main(client: connect_python.Client):
    print(pyvisa.ResourceManager().list_resources(), flush=True)

    dio = diRasterScan()
    daq = dio._create_daq()
    try:
        dio._assert_34980a(daq)
        dio.configure_all(daq)
        log.info("Ready. Raster scanning DI2-DI6.")

        while True:
            # continuously read + stream the digital inputs DI2..DI6
            states = dio.read_inputs(daq)
            now = datetime.now()
            for alias, val in states.items():
                client.stream(STREAM_ID, now, float(val), name=alias)
            log.info(f"{now.isoformat()} | published to stream={STREAM_ID!r}: {states}")

            time.sleep(POLL_S)
    finally:
        daq.close()


if __name__ == "__main__":
    main()
