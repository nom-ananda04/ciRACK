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

    def startup_guard(self, daq):
        """Clear + verify ALL crosspoints before doing anything else.

        A previous run that was hard-killed can leave a reed CLOSED, tying a
        source onto the shared COM bus (this is what produced phantom
        staircase readings). This runs first: it opens every DAC crosspoint,
        settles, then reads back. If any crosspoint refuses to open it raises,
        so we never route on top of a stuck/left-closed path.
        """
        stuck = []
        for p in DAC_PORTS:
            ch = self._chan(BANK1_BASE, p)
            if self._is_closed(daq, ch):
                print(f"         startup_guard: {ch} was CLOSED -> opening")
                try:
                    daq.driver.open_relay(SimpleNamespace(physical_channel=ch))
                except Exception:
                    pass
        time.sleep(RELAY_SETTLE_S)

        # verify everything is now open
        for p in DAC_PORTS:
            ch = self._chan(BANK1_BASE, p)
            if self._is_closed(daq, ch):
                stuck.append(ch)

        if stuck:
            raise RuntimeError(
                f"startup_guard: crosspoints still CLOSED after open: {stuck}. "
                f"Likely a physically stuck reed -- try *RST or service the module. "
                f"Refusing to route on a dirty bus."
            )
        print("         startup_guard: all crosspoints open, COM is clear")

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
            self.startup_guard(daq)
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
        self.startup_guard(daq)
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
    print(daq.driver._visa.query(f"SYST:CTYP? {MUX_SLOT}").strip())

    # Checkbox app-value IDs -> bank-relative port on the mux.
    # 1H=DAQ1.AO0, 2H=DAQ2.AO0, 3H=DAQ3.AO0, 4H=DAQ4.AO0, 5H=cDAQ1.1.AO0
    # (1L=TB_AGND, not a source)
    SOURCE_CHECKBOXES = {
        "route_daq1": 1,
        "route_daq2": 2,
        "route_daq3": 3,
        "route_daq4": 4,
        "route_cdaq": 5,
    }

    try:
        tray._assert_34980a(daq)
        tray.startup_guard(daq)

        last_selected = None
        while True:
            # Find which checkbox is checked. Only one source may drive the
            # shared TB_AO_MUX bus at a time; if several are checked, take the
            # first and warn.
            selected = [port for cb_id, port in SOURCE_CHECKBOXES.items()
                        if client.get_value(cb_id)]

            if len(selected) > 1:
                print(f"WARNING: multiple sources checked {selected}; using first ({selected[0]})")
            target = selected[0] if selected else None

            if target != last_selected:
                if target is None:
                    tray._open_all(daq)
                    print("No source selected -- all crosspoints open")
                else:
                    dac_ch = tray._chan(BANK1_BASE, target)
                    ok = tray.connect_dac(daq, dac_ch)
                    print(f"Routed port {target} ({dac_ch}) -> TB_AO_MUX  [{'OK' if ok else 'FAIL'}]")
                last_selected = target

            time.sleep(0.5)
    finally:
        tray._open_all(daq)
        daq.close()


if __name__ == "__main__":
    main()