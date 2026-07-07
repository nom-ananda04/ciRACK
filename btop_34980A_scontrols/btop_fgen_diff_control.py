import time
from datetime import datetime
from types import SimpleNamespace

import connect_python
import pyvisa
from instro.daq import InstroDAQ
from instro.daq.drivers.keysight_34980a import Keysight34980A  # ADJUST PATH if needed


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
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

log = connect_python.get_logger(__name__)


class daqTrayControl():
    def _create_daq(self):
        daq = InstroDAQ(name="daq_tray", driver=Keysight34980A(RESOURCE))
        daq.open()
        return daq

    def _assert_34980a(self, daq):
        idn = daq.driver._visa.query("*IDN?").strip()
        log.info(f"*IDN? = {idn}")
        if "34980A" not in idn:
            raise RuntimeError(f"Connected device is not a 34980A: {idn!r}")

    def _chan(self, base, port):
        return f"{MUX_SLOT}{base + port - 1:03d}"

    def _src_ch(self, port):
        return self._chan(BANK1_BASE, port)

    def _dest_ch(self, port):
        return f"{MUX_SLOT}{port:03d}"          # bank-2 port is already absolute (21->4021)

    def _safe_open(self, daq, ch):
        try:
            daq.driver.open_relay(SimpleNamespace(physical_channel=ch))
        except Exception:
            pass

    def _is_closed(self, daq, ch):
        return daq.driver._visa.query(f"ROUT:CLOS? (@{ch})").strip() == "1"

    def _all_channels(self):
        srcs = [self._src_ch(p) for p in DAC_PORTS]
        dests = [self._dest_ch(p) for p in DEST_PORTS]
        return srcs, dests

    def _open_all(self, daq):
        """Open every source, destination, and ABus tie -> no path remains."""
        srcs, dests = self._all_channels()
        for ch in srcs + dests + ABUS_TIE_CHANNELS:
            self._safe_open(daq, ch)
        time.sleep(RELAY_SETTLE_S)

    def route_and_hold(self, daq, src_port, dest_port, hold_s=DWELL_S,
                       step=None, n_steps=None):
        """Close ONE source -> ABus3 -> ONE destination, verify, and hold.
        Break-before-make across ALL sources and destinations so only this
        single crosspoint is ever on the bus. Returns ok (bool)."""
        src_ch = self._src_ch(src_port)
        dest_ch = self._dest_ch(dest_port)
        srcs, dests = self._all_channels()

        src_name = DAC_NAMES.get(src_port, f"DAC{src_port}")
        dest_name = DEST_NAMES.get(dest_port, f"port{dest_port}")
        prefix = f"[{step}/{n_steps}] " if step is not None else ""
        log.info(f"{prefix}SOURCE DAC{src_port}={src_name} ({src_ch})  "
                 f"-->  DEST port {dest_port}={dest_name} ({dest_ch})")

        # break: open everything that could sit on the bus
        for ch in srcs + dests + ABUS_TIE_CHANNELS:
            self._safe_open(daq, ch)
        time.sleep(RELAY_SETTLE_S)

        # make: source -> ABus tie (both banks) -> destination
        daq.driver.close_relay(SimpleNamespace(physical_channel=src_ch))
        for a in ABUS_TIE_CHANNELS:
            daq.driver.close_relay(SimpleNamespace(physical_channel=a))
        daq.driver.close_relay(SimpleNamespace(physical_channel=dest_ch))
        time.sleep(RELAY_SETTLE_S)

        need = [src_ch, dest_ch] + ABUS_TIE_CHANNELS
        closed = [c for c in need if self._is_closed(daq, c)]
        ok = sorted(closed) == sorted(need)
        if not ok:
            log.warning(f"{prefix}expected closed {sorted(need)}, got {sorted(closed)}")

        log.info(f"{prefix}routed via ABus{ABUS_TIE_CHANNELS}; holding {hold_s}s  "
                 f"[{'OK' if ok else 'FAIL'}]")
        time.sleep(hold_s)
        return ok

    def sweep(self, daq, client, src_ports, dest_ports):
        """Nested sweep: for each DAC source, route to each bank-2 port, one at a time."""
        bad_s = [p for p in src_ports if p not in DAC_PORTS]
        bad_d = [p for p in dest_ports if p not in DEST_PORTS]
        if bad_s:
            raise ValueError(f"source port(s) {bad_s} not in {DAC_PORTS}")
        if bad_d:
            raise ValueError(f"dest port(s) {bad_d} not in {DEST_PORTS}")

        total = ok_count = 0
        n_steps = len(src_ports) * len(dest_ports)
        step = 0
        log.info(f"=== SWEEP START: {len(src_ports)} DAC(s) x {len(dest_ports)} port(s) "
                 f"= {n_steps} routes, {DWELL_S}s each (~{n_steps * (DWELL_S + 1.5):.0f}s) ===")
        for sp in src_ports:
            for dp in dest_ports:
                step += 1
                ok = self.route_and_hold(daq, sp, dp, step=step, n_steps=n_steps)
                total += 1
                ok_count += int(ok)
                client.stream(STREAM_ID, datetime.now(), 1.0 if ok else 0.0,
                              name=f"route_dac{sp}_port{dp}")

        self._open_all(daq)
        log.info(f"=== SWEEP COMPLETE: {ok_count}/{total} routes verified OK, all relays open ===")
        return ok_count, total


@connect_python.main
def main(client: connect_python.Client):
    log.info(f"VISA resources: {pyvisa.ResourceManager().list_resources()}")

    tray = daqTrayControl()
    daq = tray._create_daq()
    try:
        tray._assert_34980a(daq)
        tray._open_all(daq)
        log.info("Starting automatic sweep (no trigger).")

        # Run the full source x port sweep automatically, on repeat.
        while True:
            try:
                tray.sweep(daq, client, DAC_PORTS, DEST_PORTS)
            except Exception as e:
                log.error(f"Sweep failed: {e}")
                tray._open_all(daq)
            log.info(f"Sweep cycle done; restarting in {CYCLE_PAUSE_S}s.")
            time.sleep(CYCLE_PAUSE_S)
    finally:
        tray._open_all(daq)
        daq.close()


if __name__ == "__main__":
    main()