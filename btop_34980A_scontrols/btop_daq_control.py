import math
import time 
from types import SimpleNamespace
import connect_python
import pyvisa
from instro.daq import InstroDAQ
from instro.daq.drivers.keysight_34980a import Keysight34980A  # ADJUST PATH if needed
from instro.daq.types import DigitalPortWidth, Direction, Logic


# Config
RESOURCE = "USB0::2391::1287::MY44001757::0::INSTR"

MUX_SLOT = 4

BANK1_BASE = 1
DAC_PORTS = [1, 2, 3, 4, 5]

BANK_TIE = "external"
ABUS_TIE_CHANNELS = []

RELAY_SETTLE_S = 0.50
DWELL_S = 3.0
SETTLE_S = 1.0

# Constants referenced by your original three methods (add only if not defined elsewhere)
RELAY_CHANNEL = f"{MUX_SLOT}003"
HAS_INTERNAL_DMM = True
KNOWN_SOURCE_WIRED = False
EXPECTED_VOLTAGE = 1.0
VOLTAGE_TOLERANCE_V = 0.05

# connect app init
log = connect_python.get_logger(__name__)
STREAM_ID = "daq_tray"
COMMAND_TOPIC = "script/daq_tray"


class daqTrayControl():
    def _create_daq(self):
        """Create and open a fresh 34980A DAQ instance (open() issues *RST)."""
        daq = InstroDAQ(name="daq_tray", driver=Keysight34980A(RESOURCE))
        daq.open()
        return daq

    def _assert_34980a(self, daq):
        """Confirm the connected instrument is a 34980A."""
        idn = daq.driver._visa.query("*IDN?").strip()
        print(f"         *IDN? = {idn}")
        if "34980A" not in idn:
            raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    # ---- ADDED (required): bank-relative port -> absolute channel address ----
    def _chan(self, bank_base, port):
        """e.g. slot 1, bank2 port 1 -> '1021'."""
        return f"{MUX_SLOT}{bank_base + port - 1:03d}"

    def _is_closed(self, daq, ch):
        return daq.driver._visa.query(f"ROUT:CLOS? (@{ch})").strip() == "1"

    def connect_dac(self, daq, dac_ch, verify=True):
        """ Connect one DAC to output"""
        # break: open all sources + taps
        for p in DAC_PORTS:
            daq.driver.open_relay(SimpleNamespace(physical_channel=self._chan(BANK1_BASE, p)))

        time.sleep(RELAY_SETTLE_S)

        # make: close the chosen crosspoint
        daq.driver.close_relay(SimpleNamespace(physical_channel=dac_ch))
        if BANK_TIE == "abus":
            for ch in ABUS_TIE_CHANNELS:
                daq.driver.close_relay(SimpleNamespace(physical_channel=ch))
        time.sleep(RELAY_SETTLE_S)

        if not verify:
            return True
        # EXCLUSIVITY CHECK: confirm ONLY dac_ch is closed across all sources.
        # If a reed failed to open, a second channel is still on the common (a
        # parallel 100 ohm leg) -- exactly what divides the source level down.
        closed = [self._chan(BANK1_BASE, p) for p in DAC_PORTS
                  if self._is_closed(daq, self._chan(BANK1_BASE, p))]
        if closed != [dac_ch]:
            print(f"         WARNING: expected only {dac_ch} closed, got {closed}")
        return closed == [dac_ch]

    def _open_all(self, daq):
        """Open every DAC + AIN channel (and any ABus tie) -> no live path remains."""
        for p in DAC_PORTS:
            try:
                daq.driver.open_relay(SimpleNamespace(physical_channel=self._chan(BANK1_BASE, p)))
            except Exception:
                pass
        for ch in ABUS_TIE_CHANNELS:
            try:
                daq.driver.open_relay(SimpleNamespace(physical_channel=ch))
            except Exception:
                pass
        time.sleep(RELAY_SETTLE_S)

    def route_all_dac(self):
        """Walk every DAC -> every AIN, connecting one path at a time."""
        daq = self._create_daq()
        total = ok_count = 0
        try:
            self._assert_34980a(daq)
            self._open_all(daq)
            for dp in DAC_PORTS:
                    dac_ch = self._chan(BANK1_BASE, dp)
                    ok = self.connect_dac(daq, dac_ch)
                    total += 1
                    ok_count += int(ok)
                    time.sleep(DWELL_S)
            self._open_all(daq)
            print(f"done: {ok_count}/{total} connections verified, all relays open")
        finally:
            self._open_all(daq)
            daq.close()

    # =====================================================================
    # ================  added: logic to connect the ports  ================
    # =====================================================================
    def connect_pair(self, dac_port, hold=True):
        """Connect a single DAC port to a single AIN port (by bank-relative port #).

        Opens its own frame session and makes just this one connection using
        connect_dac(). With hold=True (default) it leaves the crosspoint
        CLOSED so signal keeps passing and returns the open `daq`; command your
        source DAC / read your destination AIN, then call disconnect(daq).
        With hold=False it opens everything and closes the session before return.
        """
        daq = self._create_daq()
        self._assert_34980a(daq)
        dac_ch = self._chan(BANK1_BASE, dac_port)
        ok = self.connect_dac(daq, dac_ch)
        time.sleep(SETTLE_S)

        print(f"DAC{dac_port} ({dac_ch})  [{'OK' if ok else 'FAIL'}]")
        if hold:
            return daq
        self._open_all(daq)
        daq.close()
        return None

    def disconnect(self, daq):
        """Open all crosspoints and close a session returned by connect_pair(hold=True)."""
        self._open_all(daq)
        daq.close()

@connect_python.main
def main(client: connect_python.Client):
    print(pyvisa.ResourceManager().list_resources())
    tray = daqTrayControl()
    daq = tray._create_daq()
    try:
        tray._assert_34980a(daq)
        for dac_port in DAC_PORTS:
                dac_ch = tray._chan(BANK1_BASE, dac_port)
                ok = tray.connect_dac(daq, dac_ch)
                print(f"DAC{dac_port} ({dac_ch})  [{'OK' if ok else 'FAIL'}]")
    finally:
        tray._open_all(daq)
        daq.close()


if __name__ == "__main__":
    main()