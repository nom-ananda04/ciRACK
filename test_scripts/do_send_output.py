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

DO_DRIVE_BIT = 0

# Aliases (used as channel/stream names when published)
DO_DRIVE_ALIAS = "do_drive"

# Timing
POLL_S = 0.20

STREAM_ID = "dio_tray"

# UI app-value IDs (set these as the ID on the matching Form widgets in Connect)
DRIVE_LEVEL_ID = "drive_level"

DRIVE_LEVEL_DEFAULT = 0

log = connect_python.get_logger(__name__)


def _line(bit: int) -> str:
    """Keysight physical channel string for a single DIO line, e.g. '8101/0'."""
    return f"{MODULE_SLOT}{DIO_BANK}/{bit}"


class doDriveControl():
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
        """Configure DO0 per the schematic pin map as a digital output.

        DO0 -> output (drive the DAQs)
        """
        daq.configure_digital_line(
            direction=Direction.OUTPUT,
            physical_channel=_line(DO_DRIVE_BIT),
            alias=DO_DRIVE_ALIAS,
            logic=Logic.HIGH,
        )
        # Start in a known-safe state: output low.
        daq.write_digital_line(channel=DO_DRIVE_ALIAS, data=0)
        log.info("configured: DO0 drive")

    def set_drive(self, daq, level: int):
        """Drive DO0 (TB_D_OUT) high or low to the DAQ modules."""
        daq.write_digital_line(channel=DO_DRIVE_ALIAS, data=1 if level else 0)

    def safe_off(self, daq):
        """Drive the output low."""
        try:
            daq.write_digital_line(channel=DO_DRIVE_ALIAS, data=0)
        except Exception:
            pass


@connect_python.main
def main(client: connect_python.Client):
    print(pyvisa.ResourceManager().list_resources(), flush=True)

    dio = doDriveControl()
    daq = dio._create_daq()
    try:
        dio._assert_34980a(daq)
        dio.configure_all(daq)
        log.info("Ready. Toggling DO0 between 1 and 0, twice.")

        HOLD_S = 1.0
        EPSILON_S = 0.02  # small gap before the transition so the plot holds flat

        for level in [1, 0, 1, 0]:
            dio.set_drive(daq, level)
            log.info(f"DO0 (TB_D_OUT) -> {level}")

            # Two points per plateau: one at the start, one just before the
            # next transition. This makes the line plot render a flat hold
            # followed by a sharp vertical edge (square wave), instead of a
            # diagonal ramp between a single point per level.
            client.stream(STREAM_ID, datetime.now(), float(level), name=DO_DRIVE_ALIAS)
            time.sleep(HOLD_S - EPSILON_S)
            client.stream(STREAM_ID, datetime.now(), float(level), name=DO_DRIVE_ALIAS)
            time.sleep(EPSILON_S)
    finally:
        dio.safe_off(daq)
        daq.close()


if __name__ == "__main__":
    main()
