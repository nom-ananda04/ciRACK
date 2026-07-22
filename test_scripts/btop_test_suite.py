import pyvisa
import time
from datetime import datetime
from types import SimpleNamespace

# connect_python only exists inside Connect's own bundled Python venv -- it's
# injected when Connect itself runs a script, not something pip-installable
# or available to headless_rack_control.py (plain system Python, no Connect
# runtime). Try it first so scripts run through Connect keep using its real
# logger (shows up in Connect's own log viewer); fall back to the stdlib
# logging module so the exact same classes still work headless.
try:
    import connect_python
    _log = connect_python.get_logger(__name__)
except ImportError:
    import logging
    _log = logging.getLogger(__name__)

from instro.daq import InstroDAQ
from instro.daq.drivers import Keysight34980A
from instro.daq.types import Direction, Logic
from instro.psu import InstroPSU
from instro.psu.drivers import BK9115, KeysightN5700
from instro.eload import InstroELoad
from instro.eload.drivers import BK85XXB
from instro.eload.types import LoadMode
from instro.lib.exceptions import FeatureNotSupportedError


# -----------------------------------------------------------
# FGEN and DIFF CONTROL FUNCTIONS
# -----------------------------------------------------------
class FGEN_DIFFControl():
    # --- configuration (class attributes so every method can read via self.) ---
    RESOURCE = "USB0::0x0957::0x0507::MY44001757::INSTR"   # confirmed 34980A frame
    MUX_SLOT = 4
    BANK1_BASE = 1          # bank 1 = channels 1..20  (DAC sources on 1..5)

    # SOURCES to sweep ("sweep the DAQs"): bank-1 DAC ports -> 4001..4005
    DAC_PORTS = [1, 2, 3, 4, 5]

    # DESTINATIONS to sweep ("sweep the ports"): bank-2 channels, ONE AT A TIME.
    #   21 = TB_FGEN, 22 = fgen copy, 23 = TB_AO_DIF+
    DEST_PORTS = [21, 22, 23]

    # Friendly names for logging (edit to match your wiring)
    DAC_NAMES = {1: "cDAQ1.1.AO0", 2: "DAQ1.AO0", 3: "DAQ2.AO0", 4: "DAQ3.AO0", 5: "DAQ4.AO0"}
    DEST_NAMES = {21: "TB_FGEN", 22: "TB_FGEN(copy)", 23: "TB_AO_DIF+"}

    # --- Internal ABus tie: joins bank-1 COM and bank-2 COM with NO external wire ---
    # 34923A ABus relays: bank1 = 911..914, bank2 = 921..924 (ABus1..4).
    # ABus1/ABus2 feed the internal DMM -> use ABus3 (913/923).
    ABUS_TIE_CHANNELS = [f"{MUX_SLOT}913", f"{MUX_SLOT}923"]   # ABus3, both banks

    RELAY_SETTLE_S = 0.50
    DWELL_S = 5.0            # hold each route long enough for the AIN stream to capture it
    CYCLE_PAUSE_S = 2.0      # pause between automatic full-sweep cycles
    STREAM_ID = "daq_tray"

    log = _log

    def _create_daq(self):
        daq = InstroDAQ(name="daq_tray", driver=Keysight34980A(self.RESOURCE))
        daq.open()
        return daq

    def _assert_34980a(self, daq):
        idn = daq.driver._visa.query("*IDN?").strip()
        self.log.info(f"*IDN? = {idn}")
        if "34980A" not in idn:
            raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    def _chan(self, base, port):
        return f"{self.MUX_SLOT}{base + port - 1:03d}"

    def _src_ch(self, port):
        return self._chan(self.BANK1_BASE, port)

    def _dest_ch(self, port):
        return f"{self.MUX_SLOT}{port:03d}"          # bank-2 port is already absolute (21->4021)

    def _safe_open(self, daq, ch):
        try:
            daq.driver.open_relay(SimpleNamespace(physical_channel=ch))
        except Exception:
            pass

    def _is_closed(self, daq, ch):
        return daq.driver._visa.query(f"ROUT:CLOS? (@{ch})").strip() == "1"

    def _all_channels(self):
        srcs = [self._src_ch(p) for p in self.DAC_PORTS]
        dests = [self._dest_ch(p) for p in self.DEST_PORTS]
        return srcs, dests

    def _open_all(self, daq):
        """Open every source, destination, and ABus tie -> no path remains."""
        srcs, dests = self._all_channels()
        for ch in srcs + dests + self.ABUS_TIE_CHANNELS:
            self._safe_open(daq, ch)
        time.sleep(self.RELAY_SETTLE_S)

    def route_and_hold(self, daq, src_port, dest_port, hold_s=None,
                       step=None, n_steps=None):
        """Close ONE source -> ABus3 -> ONE destination, verify, and hold.
        Break-before-make across ALL sources and destinations so only this
        single crosspoint is ever on the bus. Returns ok (bool)."""
        if hold_s is None:
            hold_s = self.DWELL_S
        src_ch = self._src_ch(src_port)
        dest_ch = self._dest_ch(dest_port)
        srcs, dests = self._all_channels()

        src_name = self.DAC_NAMES.get(src_port, f"DAC{src_port}")
        dest_name = self.DEST_NAMES.get(dest_port, f"port{dest_port}")
        prefix = f"[{step}/{n_steps}] " if step is not None else ""
        self.log.info(f"{prefix}SOURCE DAC{src_port}={src_name} ({src_ch})  "
                      f"-->  DEST port {dest_port}={dest_name} ({dest_ch})")

        # break: open everything that could sit on the bus
        for ch in srcs + dests + self.ABUS_TIE_CHANNELS:
            self._safe_open(daq, ch)
        time.sleep(self.RELAY_SETTLE_S)

        # make: source -> ABus tie (both banks) -> destination
        daq.driver.close_relay(SimpleNamespace(physical_channel=src_ch))
        for a in self.ABUS_TIE_CHANNELS:
            daq.driver.close_relay(SimpleNamespace(physical_channel=a))
        daq.driver.close_relay(SimpleNamespace(physical_channel=dest_ch))
        time.sleep(self.RELAY_SETTLE_S)

        need = [src_ch, dest_ch] + self.ABUS_TIE_CHANNELS
        closed = [c for c in need if self._is_closed(daq, c)]
        ok = sorted(closed) == sorted(need)
        if not ok:
            self.log.warning(f"{prefix}expected closed {sorted(need)}, got {sorted(closed)}")

        self.log.info(f"{prefix}routed via ABus{self.ABUS_TIE_CHANNELS}; holding {hold_s}s  "
                      f"[{'OK' if ok else 'FAIL'}]")
        time.sleep(hold_s)
        return ok

    def sweep(self, daq, client, src_ports, dest_ports):
        """Nested sweep: for each DAC source, route to each bank-2 port, one at a time."""
        bad_s = [p for p in src_ports if p not in self.DAC_PORTS]
        bad_d = [p for p in dest_ports if p not in self.DEST_PORTS]
        if bad_s:
            raise ValueError(f"source port(s) {bad_s} not in {self.DAC_PORTS}")
        if bad_d:
            raise ValueError(f"dest port(s) {bad_d} not in {self.DEST_PORTS}")

        total = ok_count = 0
        n_steps = len(src_ports) * len(dest_ports)
        step = 0
        self.log.info(f"=== SWEEP START: {len(src_ports)} DAC(s) x {len(dest_ports)} port(s) "
                      f"= {n_steps} routes, {self.DWELL_S}s each "
                      f"(~{n_steps * (self.DWELL_S + 1.5):.0f}s) ===")
        for sp in src_ports:
            for dp in dest_ports:
                step += 1
                ok = self.route_and_hold(daq, sp, dp, step=step, n_steps=n_steps)
                total += 1
                ok_count += int(ok)
                client.stream(self.STREAM_ID, datetime.now(), 1.0 if ok else 0.0,
                              name=f"route_dac{sp}_port{dp}")

        self._open_all(daq)
        self.log.info(f"=== SWEEP COMPLETE: {ok_count}/{total} routes verified OK, all relays open ===")
        return ok_count, total
    

class AIN_AOControl():
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
    log = _log
    STREAM_ID = "daq_tray"
    COMMAND_TOPIC = "script/daq_tray"
    def _create_daq(self):
        """Create and open a fresh 34980A DAQ instance (open() issues *RST)."""
        daq = InstroDAQ(name="daq_tray", driver=Keysight34980A(self.RESOURCE))
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
        return f"{self.MUX_SLOT}{bank_base + port - 1:03d}"

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
        for p in self.DAC_PORTS:
            ch = self._chan(self.BANK1_BASE, p)
            if self._is_closed(daq, ch):
                print(f"         startup_guard: {ch} was CLOSED -> opening")
                try:
                    daq.driver.open_relay(SimpleNamespace(physical_channel=ch))
                except Exception:
                    pass
        time.sleep(self.RELAY_SETTLE_S)

        # verify everything is now open
        for p in self.DAC_PORTS:
            ch = self._chan(self.BANK1_BASE, p)
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
        for p in self.DAC_PORTS:
            daq.driver.open_relay(SimpleNamespace(physical_channel=self._chan(self.BANK1_BASE, p)))

        time.sleep(self.RELAY_SETTLE_S)

        # make: close the chosen crosspoint
        daq.driver.close_relay(SimpleNamespace(physical_channel=dac_ch))
        if self.BANK_TIE == "abus":
            for ch in self.ABUS_TIE_CHANNELS:
                daq.driver.close_relay(SimpleNamespace(physical_channel=ch))
        time.sleep(self.RELAY_SETTLE_S)

        if not verify:
            return True
        # EXCLUSIVITY CHECK: confirm ONLY dac_ch is closed across all sources.
        # If a reed failed to open, a second channel is still on the common (a
        # parallel 100 ohm leg) -- exactly what divides the source level down.
        closed = [self._chan(self.BANK1_BASE, p) for p in self.DAC_PORTS
                  if self._is_closed(daq, self._chan(self.BANK1_BASE, p))]
        if closed != [dac_ch]:
            print(f"         WARNING: expected only {dac_ch} closed, got {closed}")
        return closed == [dac_ch]

    def _open_all(self, daq):
        """Open every DAC + AIN channel (and any ABus tie) -> no live path remains."""
        for p in self.DAC_PORTS:
            try:
                daq.driver.open_relay(SimpleNamespace(physical_channel=self._chan(self.BANK1_BASE, p)))
            except Exception:
                pass
        for ch in self.ABUS_TIE_CHANNELS:
            try:
                daq.driver.open_relay(SimpleNamespace(physical_channel=ch))
            except Exception:
                pass
        time.sleep(self.RELAY_SETTLE_S)

    def route_all_dac(self):
        """Walk every DAC -> every AIN, connecting one path at a time."""
        daq = self._create_daq()
        total = ok_count = 0
        try:
            self._assert_34980a(daq)
            self.startup_guard(daq)
            self._open_all(daq)
            for dp in self.DAC_PORTS:
                    dac_ch = self._chan(self.BANK1_BASE, dp)
                    ok = self.connect_dac(daq, dac_ch)
                    total += 1
                    ok_count += int(ok)
                    time.sleep(self.DWELL_S)
            self._open_all(daq)
            print(f"done: {ok_count}/{total} connections verified, all relays open")
        finally:
            self._open_all(daq)
            daq.close()

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
        dac_ch = self._chan(self.BANK1_BASE, dac_port)
        ok = self.connect_dac(daq, dac_ch)
        time.sleep(self.SETTLE_S)

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


class diRasterScan():
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

    log = _log

    def _line(self, bit: int) -> str:
        return f"{self.MODULE_SLOT}{self.DIO_BANK}/{bit}"

    def _create_daq(self):
        """Create and open a fresh 34980A DAQ instance."""
        daq = InstroDAQ(name="dio_tray", driver=Keysight34980A(self.RESOURCE))
        daq.open()
        return daq

    def _assert_34980a(self, daq):
        idn = daq.driver._visa.query("*IDN?").strip()
        self.log.info(f"*IDN? = {idn}")
        if "34980A" not in idn:
            raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    def configure_all(self, daq):
        """Configure DI2..DI6 per the schematic pin map as digital inputs."""
        for b in self.DI_INPUT_BITS:
            daq.configure_digital_line(
                direction=Direction.INPUT,
                physical_channel=self._line(b),
                alias=self.DI_INPUT_ALIAS[b],
                logic=Logic.HIGH,
                logic_level=self.LOGIC_LEVEL_V,
            )
        self.log.info("configured: DI2-6 inputs")

    def read_inputs(self, daq) -> dict:
        """Read DI2..DI6 and return {alias: 0/1}."""
        states = {}
        for b in self.DI_INPUT_BITS:
            states[self.DI_INPUT_ALIAS[b]] = int(daq.read_digital_line(channel=self.DI_INPUT_ALIAS[b]).latest)
        return states



class doDriveControl():
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

    log = _log

    def _line(self, bit: int) -> str:
        """Keysight physical channel string for a single DIO line, e.g. '8101/0'."""
        return f"{self.MODULE_SLOT}{self.DIO_BANK}/{bit}"

    def _create_daq(self):
        """Create and open a fresh 34980A DAQ instance."""
        daq = InstroDAQ(name="dio_tray", driver=Keysight34980A(self.RESOURCE))
        daq.open()
        return daq

    def _assert_34980a(self, daq):
        idn = daq.driver._visa.query("*IDN?").strip()
        self.log.info(f"*IDN? = {idn}")
        if "34980A" not in idn:
            raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    def configure_all(self, daq):
        """Configure DO0 per the schematic pin map as a digital output.

        DO0 -> output (drive the DAQs)
        """
        daq.configure_digital_line(
            direction=Direction.OUTPUT,
            physical_channel=self._line(self.DO_DRIVE_BIT),
            alias=self.DO_DRIVE_ALIAS,
            logic=Logic.HIGH,
        )
        # Start in a known-safe state: output low.
        daq.write_digital_line(channel=self.DO_DRIVE_ALIAS, data=0)
        self.log.info("configured: DO0 drive")

    def set_drive(self, daq, level: int):
        """Drive DO0 (TB_D_OUT) high or low to the DAQ modules."""
        daq.write_digital_line(channel=self.DO_DRIVE_ALIAS, data=1 if level else 0)

    def safe_off(self, daq):
        """Drive the output low."""
        try:
            daq.write_digital_line(channel=self.DO_DRIVE_ALIAS, data=0)
        except Exception:
            pass

class Counter34980aControl():
    RESOURCE = "USB0::0x0957::0x0507::MY44001757::INSTR"   # confirmed 34980A frame

    MODULE_SLOT = 8
    COUNTER_CHANNEL = f"{MODULE_SLOT}301"  # counter 1; use f"{MODULE_SLOT}302" for counter 2

    # Fixed input threshold, in volts. Per LabJack's own T-series datasheet
    # (Appendix A-2), CIO/EIO output impedance is 180 ohms (a fairly weak
    # driver) and their own worked example shows a 180 ohm load pulling the
    # output HIGH down to ~1.65V (vs. 3.3V unloaded) -- Output High Voltage
    # is only guaranteed down to 2.6V typical at 5mA and drops further under
    # heavier loading. Output LOW stays low regardless (0.01-0.75V across
    # the sinking range in that same table). NI's counter/PFI driver doesn't
    # sag like this under the same load, which is the real reason 5V TTL
    # (NI) clears a threshold that 3.3V CMOS (LabJack) may not -- it's a
    # drive-strength/loading difference, not just a nominal voltage one.
    # Dropped from 1.5V to 1.0V for more margin above LOW's worst case
    # (0.75V) while staying safely below even a badly-drooped HIGH like the
    # 1.65V worked example above.
    THRESHOLD_V = 1.0
    POLL_S = 0.5

    log = _log

    def check_err(self, inst, context=""):
        err = inst.query("SYST:ERR?").strip()
        self.log.info(f"SYST:ERR? {context} -> {err}")
        return err.startswith("+0")

    def safe_query(self, inst, cmd):
        """Query with device-clear recovery so one timeout doesn't desync the session."""
        try:
            return inst.query(cmd).strip()
        except pyvisa.errors.VisaIOError as e:
            self.log.info(f"query {cmd!r} failed ({e}); sending device clear and retrying once")
            try:
                inst.clear()
            except Exception:
                pass
            return inst.query(cmd).strip()

    def configure(self, inst):
        """Select totalize mode on COUNTER_CHANNEL, zero it, and start counting."""
        # Select the totalize function on the counter channel.
        inst.write(f"COUN:FUNC TOT,(@{self.COUNTER_CHANNEL})")
        ok_func = self.check_err(inst, "after COUN:FUNC TOT")

        # Count rising edges.
        inst.write(f"COUN:SLOP POS,(@{self.COUNTER_CHANNEL})")
        self.check_err(inst, "after COUN:SLOP")

        # Gate source: INTernal so the counter free-runs after INITiate rather
        # than requiring an external gate edge. Param is {INTernal|EXTernal}
        # (NOT IMM). [SENSe:]COUNter:GATE:SOURce
        inst.write(f"COUN:GATE:SOUR INT,(@{self.COUNTER_CHANNEL})")
        self.check_err(inst, "after COUN:GATE:SOUR INT")

        # Gate polarity: INVerted so a LOW/floating external gate ENABLES
        # counting. The GATE H terminal is unwired; if the gate still applies
        # in totalize mode, NORMal polarity (count-while-high) would block all
        # counting -- exactly a permanent count=0. {NORMal|INVerted}
        # If you later tie GATE H physically high, switch this back to NORM.
        inst.write(f"COUN:GATE:POL INV,(@{self.COUNTER_CHANNEL})")
        self.check_err(inst, "after COUN:GATE:POL INV")

        # Read without resetting the count (monotonic). {READ|RRESet}
        inst.write(f"COUN:TOT:TYPE READ,(@{self.COUNTER_CHANNEL})")
        self.check_err(inst, "after COUN:TOT:TYPE READ")

        # Input threshold voltage (signal must cross this to register an edge).
        inst.write(f"COUN:THR:VOLT {self.THRESHOLD_V},(@{self.COUNTER_CHANNEL})")
        self.check_err(inst, "after COUN:THR:VOLT")

        # Read the threshold straight back from the instrument rather than
        # trusting that the write succeeded just because SYST:ERR? was clean --
        # a clamped/rounded value would still report no error but wouldn't
        # match what we asked for. Flag loudly if it doesn't match.
        readback = self.safe_query(inst, f"COUN:THR:VOLT? (@{self.COUNTER_CHANNEL})")
        try:
            readback_v = float(readback)
            if abs(readback_v - self.THRESHOLD_V) > 0.01:
                self.log.error(
                    f"Threshold readback mismatch: asked for {self.THRESHOLD_V}V, "
                    f"instrument reports {readback_v}V on channel {self.COUNTER_CHANNEL}. "
                    f"The card is NOT actually configured at the level we intended."
                )
            else:
                self.log.info(f"Threshold readback confirmed: {readback_v}V on channel {self.COUNTER_CHANNEL}.")
        except ValueError:
            self.log.info(f"unexpected COUN:THR:VOLT? response: {readback!r}")

        # Zero the accumulated count.
        inst.write(f"COUN:TOT:CLE:IMM (@{self.COUNTER_CHANNEL})")
        self.check_err(inst, "after COUN:TOT:CLE:IMM")

        # START the counter. With an internal gate, INITiate triggers counting
        # immediately. Without this, a correctly-configured totalizer reads 0.
        inst.write(f"COUN:INIT (@{self.COUNTER_CHANNEL})")
        self.check_err(inst, "after COUN:INIT")

        if not ok_func:
            raise RuntimeError(
                f"COUN:FUNC TOT was rejected on channel {self.COUNTER_CHANNEL} -- the module in "
                f"slot {self.MODULE_SLOT} likely doesn't support counting on this channel (wrong "
                f"module type, or the wrong channel number for a 34950A). The totalizer will read "
                f"0 forever until this succeeds -- check SYST:CTYP? above before re-running."
            )

    def read_count(self, inst):
        """Read the totalizer once. Returns an int count, or None if the response was unparseable."""
        resp = self.safe_query(inst, f"COUN:TOT:DATA? (@{self.COUNTER_CHANNEL})")
        try:
            return int(float(resp))
        except ValueError:
            self.log.info(f"unexpected totalizer response: {resp!r}")
            return None
        
class MultiCounterControl():
    POLL_S = 0.5

    # Stream the 34980A CLK output ON/OFF state to Connect for plotting.
    STREAM_ID = "dio_tray"
    CLK_STATE_NAME = "clk_state"

    # --- 34980A CLK output (edge source) ---------------------------------------
    RESOURCE_34980A = "USB0::0x0957::0x0507::MY44001757::INSTR"
    CLK_SLOT = 8
    CLK_FREQ_HZ = 1000   # clock output frequency

    # Fixed output logic level. LabJacks (T4/T7/T8) are NOT 5V tolerant on
    # their digital inputs, so this stays at the LabJack-safe 3.3V for all
    # sources (including cDAQ/USB-6421, which read 3.3V TTL fine).
    CLK_LEVEL_V = 3.3    # logic "1" output voltage level

    CB_CLK = "clk_enable"

    # --- Checkbox app-value IDs (must match the ids in app.connect) -------------
    CB_T4 = "count_t4"
    CB_T7 = "count_t7"
    CB_T8 = "count_t8"
    CB_USB6421 = "count_usb6421"
    CB_CDAQ = "count_cdaq"

    # --- LabJack config ---------------------------------------------------------
    # CIO2 == DIO18 on T4/T7. Index 8 ("Interrupt Counter") is NOT valid on
    # DIO18 for any of these models -- its capable-pin list is DIO4-9 (T4),
    # DIO0/1/2/3/6/7 (T7), DIO0-15 (T8). Using it here caused LJM error 2553
    # EF_PIN_TYPE_MISMATCH on the T4 (and would fail the same way on T7/T8).
    # Index 7 ("High-Speed Counter") is the correct feature for DIO18/CIO2: it
    # needs no clock-source setup, and DIO18 IS in its capable-pin list for the
    # T4 (shared with async-serial, unused here) and T7 ("always available").
    # See LabJack's DIO-EF table:
    # https://support.labjack.com/docs/13-2-dio-extended-features-t-series-datasheet
    # and https://support.labjack.com/docs/configuring-reading-a-counter
    #
    # T8 exception: the T8's index-7 capable list is DIO6/7/8/10/13/14/15 --
    # DIO18 is not in it (nor in index 8's 0-15 range), so the T8 cannot
    # hardware-count on CIO2/DIO18 at all (confirmed on real hardware: LJM
    # error 2550 EF_DIO_HAS_NO_TNC_FEATURES). Requires a physical rewire of
    # the T8's sense line to a capable pin -- DIO6 (FIO6) is used here since
    # it's free elsewhere in this project and valid for index 7. Move the
    # signal on the rack from the T8's CIO2 terminal to its FIO6 terminal to
    # match. LJ_DIO_OVERRIDES lets a device use a different pin than the
    # LJ_DIO default without touching count_labjack's shared logic.
    LJ_DIO = 18
    LJ_EF_INDEX = 7
    LJ_DIO_OVERRIDES = {
        CB_T8: 6,   # T8 rewired to FIO6/DIO6 -- see note above
    }
    LABJACKS = {
        CB_T4: ("T4", "440020473"),
        CB_T7: ("T7", "470041016"),
        CB_T8: ("T8", "480011030"),
    }

    # --- NI DAQmx config --------------------------------------------------------
    # CountEdges counter task: (counter_channel, source_terminal)
    NI_DEVICES = {
        CB_USB6421: ("Dev1/ctr0", "/Dev1/PFI... (DIO2)"),   # source terminal string below
        CB_CDAQ: ("cDAQ1Mod4/ctr0", "cDAQ1Mod4 (DIO5)"),
    }

    NI_SOURCE = {
        CB_USB6421: "/Dev1/PFI2",
        CB_CDAQ: "/cDAQ1Mod4/PFI5",
    }

    log = _log

    def __init__(self):
        self._clk_state = {"on": False}

    def stream_clk(self, client):
        client.stream(self.STREAM_ID, datetime.now(), 1.0 if self._clk_state["on"] else 0.0,
                      name=self.CLK_STATE_NAME)

    def clk_on(self, inst):
        inst.write(f"SOUR:MOD:CLOC:FREQ {self.CLK_FREQ_HZ},{self.CLK_SLOT}")
        self.check_err_visa(inst, "after CLK FREQ")
        inst.write(f"SOUR:MOD:CLOC:LEV {self.CLK_LEVEL_V},{self.CLK_SLOT}")
        self.check_err_visa(inst, "after CLK LEV")
        # Real mnemonic is SOURce:MODule:CLOCk:STATe -- "CLOC ON" alone (no
        # :STATe) isn't a valid command per the Keysight 34980A Programmer's
        # Reference and was silently doing nothing.
        inst.write(f"SOUR:MOD:CLOC:STAT ON,{self.CLK_SLOT}")
        self.check_err_visa(inst, "after CLK STATe ON")

    def clk_off(self, inst):
        try:
            inst.write(f"SOUR:MOD:CLOC:STAT OFF,{self.CLK_SLOT}")
        except Exception:
            pass

    def check_err_visa(self, inst, context=""):
        err = inst.query("SYST:ERR?").strip()
        self.log.info(f"SYST:ERR? {context} -> {err}")
        return err.startswith("+0")

    def selected_checkbox(self, client):
        """Return the single selected checkbox id (first if several), or None."""
        order = [self.CB_T4, self.CB_T7, self.CB_T8, self.CB_USB6421, self.CB_CDAQ]
        checked = [cid for cid in order if client.get_value(cid)]
        if not checked:
            return None
        if len(checked) > 1:
            self.log.info(f"WARNING: multiple devices checked {checked}; using first ({checked[0]})")
        return checked[0]

    # -------------------------------------------------------------------
    # LabJack counting
    # -------------------------------------------------------------------
    def count_labjack(self, client, cb_id):
        from labjack import ljm

        dev_type, serial = self.LABJACKS[cb_id]
        dio = self.LJ_DIO_OVERRIDES.get(cb_id, self.LJ_DIO)
        handle = ljm.openS(dev_type, "ANY", serial)
        try:
            info = ljm.getHandleInfo(handle)
            self.log.info(f"Opened LabJack {dev_type} (serial {serial}); counting DIO{dio}")

            # Configure DIO-EF edge counter on this device's counting line.
            ljm.eWriteName(handle, f"DIO{dio}_EF_ENABLE", 0)     # disable to (re)configure
            ljm.eWriteName(handle, f"DIO{dio}_EF_INDEX", self.LJ_EF_INDEX)  # 7 = high-speed counter
            ljm.eWriteName(handle, f"DIO{dio}_EF_ENABLE", 1)     # enable

            self.log.info(f"Ready. Counting rising edges on LabJack DIO{dio}.")
            last = None
            while True:
                if self.selected_checkbox(client) != cb_id:
                    self.log.info("Selection changed; stopping LabJack counter.")
                    return
                count = int(ljm.eReadName(handle, f"DIO{dio}_EF_READ_A"))
                if count != last:
                    self.log.info(f"count = {count}")
                    last = count
                self.stream_clk(client)
                time.sleep(self.POLL_S)
        finally:
            try:
                ljm.eWriteName(handle, f"DIO{dio}_EF_ENABLE", 0)
            except Exception:
                pass
            ljm.close(handle)

    # -------------------------------------------------------------------
    # NI DAQmx counting (USB-6421 and cDAQ-9401 share this path)
    # -------------------------------------------------------------------
    def count_nidaqmx(self, client, cb_id):
        import nidaqmx
        from nidaqmx.constants import Edge, CountDirection

        counter_chan, _label = self.NI_DEVICES[cb_id]
        source_term = self.NI_SOURCE[cb_id]

        with nidaqmx.Task() as task:
            task.ci_channels.add_ci_count_edges_chan(
                counter_chan,
                edge=Edge.RISING,
                initial_count=0,
                count_direction=CountDirection.COUNT_UP,
            )

            task.ci_channels[0].ci_count_edges_term = source_term

            task.start()
            self.log.info(f"Ready. Counting rising edges on {counter_chan} (source {source_term}).")
            last = None
            try:
                while True:
                    if self.selected_checkbox(client) != cb_id:
                        self.log.info("Selection changed; stopping NI counter.")
                        return
                    count = int(task.read())
                    if count != last:
                        self.log.info(f"count = {count}")
                        last = count
                    self.stream_clk(client)
                    time.sleep(self.POLL_S)
            finally:
                task.stop()


# -----------------------------------------------------------
# UNIFIED BENCH SOURCE CONTROL (PSUs + eLoad + interlock)
# -----------------------------------------------------------
class PSUControl():
    """Unified control for every regulated current-capable bench source on
    this rack -- the two PSUs (BK9115, Keysight N5745A) and the BK 8514B
    eLoad -- combined into ONE class per request, replacing what used to be
    four separate classes (the old PSUControl/DCPSUControl, N5745AControl,
    PSUInterlockControl, ELoadControl). The name PSUControl predates the
    eLoad merge and is kept so btop_dc_psu.py's existing import keeps
    working -- read "PSU" here loosely as "one of the rack's interlocked
    bench sources," not literally "power supply only."

    One instance = one physical device, built via one of the three factory
    classmethods below (bk9115(), n5745a(), eload_8514b()) rather than
    __init__ directly, since each device's config (VISA resource, default
    setpoint, etc.) differs. PSU instances (kind=KIND_PSU) wrap InstroPSU
    (BK9115/KeysightN5700 drivers, both single-channel -- confirmed from
    source); the eLoad instance (kind=KIND_ELOAD) wraps InstroELoad
    (BK85XXB driver). These two instro base classes expose DIFFERENT
    methods for conceptually the same operations (set_voltage/
    set_current_limit/output_enable vs set_mode/set_level/output_enable),
    so configure()/read_channel() branch on self.kind rather than
    pretending the two are identical.

    Also owns the shared Phoenix Contact relay current safety ceiling
    (SAFE_CURRENT_CEILING_A) and a generic mutual-exclusion interlock
    (selected()/apply_selection()) -- see their own docstrings below.

    IMPORTANT physical-safety note on the interlock: apply_selection()
    works on any group of instances you pass it. On THIS rig, all three
    devices (BK9115, N5745A, eLoad) go in the SAME group and are fully
    mutually exclusive -- exactly one of the three may ever be driving/
    drawing at a time (see btop_dc_psu.py, which interlocks all three).
    An earlier version of this docstring assumed a PSU + eLoad running
    together (source + sink) was the normal case and should be exempt from
    the interlock -- that assumption was wrong for this rig; all three are
    interlocked here.
    """

    KIND_PSU = "psu"
    KIND_ELOAD = "eload"

    # --- shared relay current safety ceiling ------------------------------
    # The rack's Phoenix Contact relays are rated for 5A. Every current-
    # related setpoint (PSU current_limit_a, eLoad CC-mode level) is
    # checked against SAFE_CURRENT_CEILING_A before being written -- raises
    # ValueError rather than silently clamping (a silent clamp could mask a
    # real test-config mistake; a loud failure can't). 5A is the relays'
    # absolute rating, not a target operating point -- derating by 20%
    # (4A) leaves headroom for overshoot/measurement error/transients
    # before ever reaching the relays' actual limit. Lower it further for
    # more margin; don't raise it above the relay rating.
    PHOENIX_RELAY_MAX_CURRENT_A = 5.0
    SAFE_CURRENT_CEILING_A = 4.0

    # Stream id for any driver script that wants to publish an instance's
    # read_channel() output to Connect via client.stream(STREAM_ID, ...,
    # name=f"{instance.name}_voltage"/f"{instance.name}_current") -- same
    # "class owns STREAM_ID, script owns the actual client.stream() call"
    # split as doDriveControl/diRasterScan/MultiCounterControl above.
    STREAM_ID = "psu_tray"

    log = _log

    def __init__(self, *, kind, cb_id, name, driver_cls, visa_resource, channel=1,
                 voltage_setpoint_v=None, current_limit_a=None,
                 mode=None, level=None, current_read_length=5):
        if kind not in (self.KIND_PSU, self.KIND_ELOAD):
            raise ValueError(f"unknown kind {kind!r}; expected {self.KIND_PSU!r} or {self.KIND_ELOAD!r}")
        self.kind = kind
        self.cb_id = cb_id            # checkbox app-value id, used by apply_selection()
        self.name = name
        self.driver_cls = driver_cls
        self.visa_resource = visa_resource
        self.channel = channel
        self.voltage_setpoint_v = voltage_setpoint_v
        self.current_limit_a = current_limit_a
        self.mode = mode
        self.level = level
        self.current_read_length = current_read_length
        self.voltage_channel_name = f"{name}.ch{channel}.voltage"
        self.current_channel_name = f"{name}.ch{channel}.current"

    # ---- factory classmethods for the three real devices on this rack ----
    @classmethod
    def bk9115(cls):
        """B&K Precision 9115 bench PSU (single-channel). Fixes two real
        bugs found in the original btop_dc_psu.py script this was ported
        from: it never called psu.start() before psu.get_channel()
        (Instrument.get_channel() unconditionally raises RuntimeError
        without a running background daemon -- open() alone does NOT
        start it), and it passed num_channels=2 to a single-channel-only
        driver (every BK9115 method calls _check_channel(), which raises
        ValueError for anything but channel=1).

        visa_resource confirmed directly against the connected unit
        (USB0::0xFFFF::0x9115::800422020766920015::INSTR -> "BK PRECISION,
        9115, 800422020766920015, 0.02-0.02") -- this replaces the
        original loose script's resource string
        (USB0::0xF4EC::0x1430::SPD3XJGQ806726::INSTR), which was for a
        DIFFERENT serial and never actually matched grabVisaIDN/
        idn_output.txt's confirmed BK9115 (serial 800422020766920015)."""
        return cls(
            kind=cls.KIND_PSU,
            cb_id="dev_select_bk9115",
            name="myPSU",
            driver_cls=BK9115,
            visa_resource="USB0::0xFFFF::0x9115::800422020766920015::INSTR",
            voltage_setpoint_v=12.0,
            current_limit_a=0.5,
        )

    @classmethod
    def n5745a(cls):
        """Keysight N5745A bench PSU (single-channel), via instro's
        KeysightN5700 driver (an N5700-series compatibility driver that
        talks the TDK Lambda Genesys SCPI dialect -- confirmed from
        source). visa_resource confirmed directly against the connected
        unit (USB0::0x0957::0x0807::US25D3814E::INSTR -> "Agilent
        Technologies,N5745A,US25D3814E,A.01.08,REV:B"), matching
        grabVisaIDN/idn_output.txt's serial (US25D3814E)."""
        return cls(
            kind=cls.KIND_PSU,
            cb_id="dev_select_n5745a",
            name="n5745a",
            driver_cls=KeysightN5700,
            visa_resource="USB0::0x0957::0x0807::US25D3814E::INSTR",
            voltage_setpoint_v=12.0,
            current_limit_a=0.5,
        )

    @classmethod
    def eload_8514b(cls):
        """B&K Precision 8514B electronic load, via instro's BK85XXB
        driver -- specifically hardware-validated by instro against the
        8514B (confirmed from source). visa_resource confirmed directly
        against the connected unit (ASRL4::INSTR -> "B&K Precision, 8514B,
        803328011797140029, 1.57"), matching grabVisaIDN/idn_output.txt's
        serial (803328011797140029) -- it's a serial/RS232 connection
        (ASRL), same connection type as the instro driver's own worked
        example ("ASRL19::INSTR")."""
        return cls(
            kind=cls.KIND_ELOAD,
            cb_id="dev_select_eload_8514b",
            name="eload_8514b",
            driver_cls=BK85XXB,
            visa_resource="ASRL4::INSTR",
            mode=LoadMode.CC,
            level=1.0,
        )

    # --- eLoad mode-select checkboxes (only meaningful for kind=KIND_ELOAD,
    # same single-select-checkbox idiom as selected()/apply_selection()
    # below -- must match the ids in app.connect) --------------------------
    CB_MODE_CC = "eload_mode_cc"
    CB_MODE_CV = "eload_mode_cv"
    CB_MODE_CR = "eload_mode_cr"
    CB_MODE_CP = "eload_mode_cp"
    MODE_CHECKBOX_ORDER = [CB_MODE_CC, CB_MODE_CV, CB_MODE_CR, CB_MODE_CP]
    MODE_BY_CHECKBOX = {
        CB_MODE_CC: LoadMode.CC,
        CB_MODE_CV: LoadMode.CV,
        CB_MODE_CR: LoadMode.CR,
        CB_MODE_CP: LoadMode.CP,
    }

    def selected_mode(self, client) -> LoadMode:
        """eLoad only: return the LoadMode for whichever mode checkbox is
        checked (first if several, same warn-and-pick-first idiom as
        selected() below), or self.mode (this instance's default) if none
        are checked -- so headless/no-UI callers still get a sane mode."""
        checked = [cid for cid in self.MODE_CHECKBOX_ORDER if client.get_value(cid)]
        if not checked:
            return self.mode
        if len(checked) > 1:
            self.log.info(f"WARNING: multiple eLoad modes checked {checked}; "
                          f"using first ({checked[0]})")
        return self.MODE_BY_CHECKBOX[checked[0]]

    # ---- construction/session ---------------------------------------------
    def create_instrument(self):
        """Construct the underlying InstroPSU/InstroELoad instance. Caller
        still must call .open() then .start() before configure()/
        read_channel() -- kept separate (like every DAQ class's
        _create_daq() elsewhere in this file) so callers control ordering
        and can wrap shutdown() in a try/finally."""
        if self.kind == self.KIND_PSU:
            return InstroPSU(
                name=self.name,
                driver=self.driver_cls(visa_resource=self.visa_resource),
                num_channels=1,   # every PSU driver used here is single-channel
            )
        return InstroELoad(
            name=self.name,
            driver=self.driver_cls(visa_resource=self.visa_resource),
        )

    # ---- configure/read/safe_off/shutdown (branch on self.kind) -----------
    def configure(self, instrument, voltage_v=None, current_limit_a=None,
                  mode=None, level=None):
        """Configure and enable this device. PSU args (voltage_v,
        current_limit_a) are ignored for an eLoad instance, and eLoad args
        (mode, level) are ignored for a PSU instance."""
        if self.kind == self.KIND_PSU:
            self._configure_psu(instrument, voltage_v, current_limit_a)
        else:
            self._configure_eload(instrument, mode, level)

    def _configure_psu(self, psu, voltage_v, current_limit_a):
        """Set the current limit BEFORE voltage (so a stale/too-high limit
        is never briefly in effect at the new voltage), then enable
        output. Checks current_limit_a against SAFE_CURRENT_CEILING_A
        first -- raises ValueError rather than silently clamping. Also
        attempts to arm this PSU's own hardware OCP as a backstop (see
        _try_enable_hardware_ocp) -- not every driver supports it."""
        channel = self.channel
        voltage_v = self.voltage_setpoint_v if voltage_v is None else voltage_v
        current_limit_a = self.current_limit_a if current_limit_a is None else current_limit_a

        self._enforce_relay_safe_current(current_limit_a, f"{self.name}.configure current_limit_a")

        psu.set_current_limit(current_limit_a, channel=channel)
        self._try_enable_hardware_ocp(psu, current_limit_a, channel)
        psu.set_voltage(voltage_v, channel=channel)
        psu.output_enable(True, channel=channel)
        self.log.info(f"{self.name} ch{channel}: {voltage_v}V, {current_limit_a}A limit, output ON")

    def _configure_eload(self, eload, mode, level):
        """Set mode BEFORE level -- InstroELoad.set_level()/set_range()
        raise ValueError if no mode has been set yet (confirmed from
        source) -- then enable the input draw. In CC mode, level IS the
        current the load will sink in amperes, checked against
        SAFE_CURRENT_CEILING_A -- raises ValueError rather than silently
        clamping. CV/CP/CR modes don't expose the drawn current as a
        direct parameter here (it depends on the source's own voltage/
        resistance) -- protection there comes from the SOURCE PSU's own
        current_limit_a, already capped by _configure_psu() above."""
        channel = self.channel
        mode = self.mode if mode is None else mode
        level = self.level if level is None else level

        if mode is LoadMode.CC:
            self._enforce_relay_safe_current(level, f"{self.name}.configure level (CC mode)")

        eload.set_mode(mode, channel=channel)
        eload.set_level(level, channel=channel)
        eload.output_enable(True, channel=channel)
        self.log.info(f"{self.name} ch{channel}: mode={mode.value}, level={level}, input ON")

    def read_channel(self, instrument):
        """One voltage sample (blocks for a fresh sample from the
        background daemon) and the last current_read_length current
        samples (returns immediately with whatever's already buffered) --
        same shape for both PSU and eLoad instances (both extend the same
        Instrument base, confirmed from source).

        Returns (voltage: float, current: list[float]) -- unwrapped from
        the Measurement objects get_channel() actually returns (confirmed
        from instro.lib.publishers.channel_buffer.DequeInMemoryPublisher.
        get(), which packages values as
        Measurement(channel_data={channel_name: [...]})). An earlier
        version of this method returned the raw Measurement objects
        instead -- harmless for a bare print() (which just prints its
        repr), but broken for anything that needs an actual number, like
        client.stream()."""
        voltage_measurement = instrument.get_channel(
            channel_name=self.voltage_channel_name, length=1, wait_for_new_samples=True)
        current_measurement = instrument.get_channel(
            channel_name=self.current_channel_name, length=self.current_read_length,
            wait_for_new_samples=False)
        voltage = float(voltage_measurement.channel_data[self.voltage_channel_name][-1])
        current = [float(v) for v in current_measurement.channel_data[self.current_channel_name]]
        return voltage, current

    def safe_off(self, instrument):
        """Disable output/input draw. Always call this before shutdown()
        so the device isn't left powered/drawing if the script exits
        unexpectedly."""
        try:
            instrument.output_enable(False, channel=self.channel)
        except Exception:
            pass

    def shutdown(self, instrument):
        """Stop the background sampling daemon and close the VISA
        session."""
        try:
            instrument.stop()
        except Exception:
            pass
        try:
            instrument.close()
        except Exception:
            pass

    # ---- shared relay current safety ceiling ------------------------------
    def _enforce_relay_safe_current(self, requested_a, context):
        """Raise ValueError if `requested_a` risks exceeding the rack's
        Phoenix Contact relay rating (see SAFE_CURRENT_CEILING_A's comment
        above for the ceiling's derivation). `context` is prepended to the
        error so it's obvious which call site tripped it."""
        if requested_a > self.SAFE_CURRENT_CEILING_A:
            raise ValueError(
                f"{context}: requested {requested_a}A exceeds the software safety "
                f"ceiling of {self.SAFE_CURRENT_CEILING_A}A (derated from the Phoenix "
                f"Contact relays' {self.PHOENIX_RELAY_MAX_CURRENT_A}A rating) -- refusing "
                f"to configure a level that could damage the relays. Lower the "
                f"requested current, or if you're certain this specific circuit "
                f"never routes through those relays, change SAFE_CURRENT_CEILING_A "
                f"deliberately rather than bypassing this check at the call site."
            )

    def _try_enable_hardware_ocp(self, psu, level_a, channel):
        """Best-effort: arm this PSU's own hardware overcurrent protection
        as a backstop behind the software ceiling above. Not every driver
        supports this -- BK9115 supports neither a settable OCP level nor
        an OCP enable (confirmed from source: both raise
        FeatureNotSupportedError); KeysightN5700/TDKLambdaGenesys supports
        enabling OCP but not setting its trip level (confirmed from
        source). Logs and continues rather than failing configure() over a
        hardware feature the specific model doesn't have -- the software
        ceiling above is enforced either way."""
        try:
            psu.set_overcurrent_protection_level(level_a, channel=channel)
            self.log.info(f"Hardware OCP level set to {level_a}A on channel {channel}.")
        except FeatureNotSupportedError:
            self.log.info("This PSU's driver has no settable OCP level -- relying on the "
                          "software current-limit ceiling + set_current_limit() only.")
        except Exception as e:
            self.log.info(f"Unexpected error setting hardware OCP level: {e}")

        try:
            psu.set_overcurrent_protection_enabled(True, channel=channel)
            self.log.info(f"Hardware OCP enabled on channel {channel}.")
        except FeatureNotSupportedError:
            self.log.info("This PSU's driver has no OCP enable at all.")
        except Exception as e:
            self.log.info(f"Unexpected error enabling hardware OCP: {e}")

    # ---- interlock across an arbitrary group of instances ------------------
    @classmethod
    def selected(cls, client, group):
        """Return whichever instance in `group` has its checkbox checked
        (first if several, warning and picking the first -- same idiom as
        MultiCounterControl.selected_checkbox() elsewhere in this file), or
        None if none are checked."""
        checked = [dev for dev in group if client.get_value(dev.cb_id)]
        if not checked:
            return None
        if len(checked) > 1:
            cls.log.info(f"WARNING: multiple devices checked "
                         f"({[d.cb_id for d in checked]}); using first ({checked[0].cb_id})")
        return checked[0]

    @classmethod
    def apply_selection(cls, client, group, sessions, state):
        """Read the checkbox group and, ONLY if the selection changed since
        the last call (tracked in state['last_selected'] -- same "small
        dict this call owns" idiom every continuous headless_tests/*.py
        test uses), enforce it: every OTHER device in `group` gets
        safe_off() BEFORE the newly-selected device gets configure()'d
        (break-before-make), so there's never a moment where two devices in
        the SAME group are both enabled. `sessions` maps each device
        instance -> its already open()+start()'d instrument. Safe to call
        every poll; a no-op once the current selection has already been
        applied. Returns the selected device instance (or None).

        IMPORTANT: only put devices in the SAME `group` call that must
        never run together (see class docstring's physical-safety note --
        e.g. the two PSUs, but NOT a PSU + the eLoad, which normally run
        together on purpose)."""
        selected = cls.selected(client, group)
        if selected is state.get("last_selected", "__unset__"):
            return selected

        prev_name = getattr(state.get("last_selected"), "name", state.get("last_selected"))
        new_name = getattr(selected, "name", None)
        cls.log.info(f"Device selection changed: {prev_name!r} -> {new_name!r}")

        for dev in group:
            if dev is not selected:
                dev.safe_off(sessions[dev])
        if selected is not None:
            selected.configure(sessions[selected])

        state["last_selected"] = selected
        return selected
