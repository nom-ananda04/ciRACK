import math
import time
from datetime import datetime
from types import SimpleNamespace
import connect_python
import pyvisa
from instro.daq import InstroDAQ
from instro.daq.drivers.keysight_34980a import Keysight34980A  # ADJUST PATH if needed
from instro.daq.types import DigitalPortWidth, Direction, Logic
from btop_test_suite import AIN_AOControl as daqTrayControl, SafeToTestControl


@connect_python.main
def main(client: connect_python.Client):
    print(pyvisa.ResourceManager().list_resources())
    tray = daqTrayControl()
    safe_ctl = SafeToTestControl()
    daq = tray._create_daq()
    print(daq.driver._visa.query(f"SYST:CTYP? {tray.MUX_SLOT}").strip())

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
            # SafeToTestControl.is_safe() -- see btop_test_suite.py -- reads
            # the rack's relay lines (already streamed by Connect's own
            # NI-DAQmx connector for the health-monitor USB-6002); ANY relay
            # energized forces every crosspoint open regardless of what's
            # checked, same gate PSUControl.apply_selection() uses in
            # btop_dc_psu.py.
            is_safe = safe_ctl.is_safe(client)
            client.stream(tray.STREAM_ID, datetime.now(), 1.0 if is_safe else 0.0, name="safe_to_test")

            # Find which checkbox is checked. Only one source may drive the
            # shared TB_AO_MUX bus at a time; if several are checked, take the
            # first and warn.
            selected = [port for cb_id, port in SOURCE_CHECKBOXES.items()
                        if client.get_value(cb_id)]

            if len(selected) > 1:
                print(f"WARNING: multiple sources checked {selected}; using first ({selected[0]})")
            target = selected[0] if selected else None
            if not is_safe:
                target = None   # NOT safe to test -- refuse to route any source, no matter what's checked

            if target != last_selected:
                if target is None:
                    tray._open_all(daq)
                    print("NOT safe to test -- all crosspoints forced open" if not is_safe
                          else "No source selected -- all crosspoints open")
                else:
                    dac_ch = tray._chan(tray.BANK1_BASE, target)
                    ok = tray.connect_dac(daq, dac_ch)
                    print(f"Routed port {target} ({dac_ch}) -> TB_AO_MUX  [{'OK' if ok else 'FAIL'}]")
                last_selected = target

            time.sleep(0.5)
    finally:
        tray._open_all(daq)
        daq.close()


if __name__ == "__main__":
    main()