import time
from datetime import datetime
import connect_python
 
log = connect_python.get_logger(__name__)
 
POLL_S = 0.5
 
# Stream the 34980A CLK output ON/OFF state to Connect for plotting.
STREAM_ID = "dio_tray"
CLK_STATE_NAME = "clk_state"
_clk_state = {"on": False}
 
 
def stream_clk(client):
    client.stream(STREAM_ID, datetime.now(), 1.0 if _clk_state["on"] else 0.0,
                  name=CLK_STATE_NAME)
 
# --- 34980A CLK output (edge source) ---------------------------------------
RESOURCE_34980A = "USB0::0x0957::0x0507::MY44001757::INSTR"
CLK_SLOT = 8
CLK_FREQ_HZ = 1000   # clock output frequency
CLK_LEVEL_V = 3.3    # logic "1" output voltage level
 
CB_CLK = "clk_enable"
 

def clk_on(inst):
    inst.write(f"SOUR:MOD:CLOC:FREQ {CLK_FREQ_HZ},{CLK_SLOT}")
    check_err_visa(inst, "after CLK FREQ")
    inst.write(f"SOUR:MOD:CLOC:LEV {CLK_LEVEL_V},{CLK_SLOT}")
    check_err_visa(inst, "after CLK LEV")
    inst.write(f"SOUR:MOD:CLOC ON,{CLK_SLOT}")
    check_err_visa(inst, "after CLK ON")
 
def clk_off(inst):
    try:
        inst.write(f"SOUR:MOD:CLOC OFF,{CLK_SLOT}")
    except Exception:
        pass
 
 
def check_err_visa(inst, context=""):
    err = inst.query("SYST:ERR?").strip()
    log.info(f"SYST:ERR? {context} -> {err}")
    return err.startswith("+0")
 
 
# --- Checkbox app-value IDs (must match the ids in app.connect) -------------
CB_T4 = "count_t4"
CB_T7 = "count_t7"
CB_T8 = "count_t8"
CB_USB6421 = "count_usb6421"
CB_CDAQ = "count_cdaq"
 
# --- LabJack config ---------------------------------------------------------
# CIO2 == DIO18 on T4/T7/T8. DIO-EF index 7 = interrupt/edge counter.
LJ_DIO = 18
LJ_EF_INDEX = 7
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
 
 
def selected_checkbox(client):
    """Return the single selected checkbox id (first if several), or None."""
    order = [CB_T4, CB_T7, CB_T8, CB_USB6421, CB_CDAQ]
    checked = [cid for cid in order if client.get_value(cid)]
    if not checked:
        return None
    if len(checked) > 1:
        log.info(f"WARNING: multiple devices checked {checked}; using first ({checked[0]})")
    return checked[0]
 
 
# ---------------------------------------------------------------------------
# LabJack counting
# ---------------------------------------------------------------------------
def count_labjack(client, cb_id):
    from labjack import ljm
 
    dev_type, serial = LABJACKS[cb_id]
    handle = ljm.openS(dev_type, "ANY", serial)
    try:
        info = ljm.getHandleInfo(handle)
        log.info(f"Opened LabJack {dev_type} (serial {serial}); counting CIO2/DIO{LJ_DIO}")
 
        # Configure DIO-EF edge counter on the CIO2 line.
        ljm.eWriteName(handle, f"DIO{LJ_DIO}_EF_ENABLE", 0)     # disable to (re)configure
        ljm.eWriteName(handle, f"DIO{LJ_DIO}_EF_INDEX", LJ_EF_INDEX)  # 7 = counter
        ljm.eWriteName(handle, f"DIO{LJ_DIO}_EF_ENABLE", 1)     # enable
 
        log.info("Ready. Counting rising edges on LabJack CIO2.")
        last = None
        while True:
            if selected_checkbox(client) != cb_id:
                log.info("Selection changed; stopping LabJack counter.")
                return
            count = int(ljm.eReadName(handle, f"DIO{LJ_DIO}_EF_READ_A"))
            if count != last:
                log.info(f"count = {count}")
                last = count
            stream_clk(client)
            time.sleep(POLL_S)
    finally:
        try:
            ljm.eWriteName(handle, f"DIO{LJ_DIO}_EF_ENABLE", 0)
        except Exception:
            pass
        ljm.close(handle)
 
 
# ---------------------------------------------------------------------------
# NI DAQmx counting (USB-6421 and cDAQ-9401 share this path)
# ---------------------------------------------------------------------------
def count_nidaqmx(client, cb_id):
    import nidaqmx
    from nidaqmx.constants import Edge, CountDirection
 
    counter_chan, _label = NI_DEVICES[cb_id]
    source_term = NI_SOURCE[cb_id]
 
    with nidaqmx.Task() as task:
        task.ci_channels.add_ci_count_edges_chan(
            counter_chan,
            edge=Edge.RISING,
            initial_count=0,
            count_direction=CountDirection.COUNT_UP,
        )
        
        task.ci_channels[0].ci_count_edges_term = source_term
 
        task.start()
        log.info(f"Ready. Counting rising edges on {counter_chan} (source {source_term}).")
        last = None
        try:
            while True:
                if selected_checkbox(client) != cb_id:
                    log.info("Selection changed; stopping NI counter.")
                    return
                count = int(task.read())
                if count != last:
                    log.info(f"count = {count}")
                    last = count
                stream_clk(client)
                time.sleep(POLL_S)
        finally:
            task.stop()
 
 
@connect_python.main
def main(client: connect_python.Client):
    import pyvisa
    log.info("Multi-counter ready. Check ONE device box in the COUNTER TEST tab.")
 
    # Hold a 34980A session open for the whole run to drive the CLK output.
    rm = pyvisa.ResourceManager()
    inst = rm.open_resource(RESOURCE_34980A)
    inst.timeout = 5000
    inst.write("*CLS")
    log.info(f"34980A: {inst.query('*IDN?').strip()}")
 
    clk_running = False
    active = None
    try:
        while True:
            # --- CLK output: follow the enable checkbox ---
            want_clk = bool(client.get_value(CB_CLK))
            if want_clk and not clk_running:
                log.info(f"Enabling 34980A CLK output ({CLK_FREQ_HZ} Hz).")
                clk_on(inst)
                clk_running = True
            elif not want_clk and clk_running:
                log.info("Disabling 34980A CLK output.")
                clk_off(inst)
                clk_running = False
            _clk_state["on"] = clk_running
 
            # Stream the CLK ON/OFF state (1/0) so it plots in Connect.
            stream_clk(client)
 
            # --- Counter: follow the device selection ---
            cb_id = selected_checkbox(client)
            if cb_id is None:
                if active is not None:
                    log.info("No device selected.")
                    active = None
                time.sleep(POLL_S)
                continue
 
            # NOTE: the counter loop below blocks until the device selection
            # changes, so the CLK checkbox is only re-evaluated between device
            # switches (fine for the usual "enable CLK, then count" workflow).
            active = cb_id
            try:
                if cb_id in LABJACKS:
                    count_labjack(client, cb_id)
                elif cb_id in NI_DEVICES:
                    count_nidaqmx(client, cb_id)
            except Exception as e:
                log.info(f"counter error on {cb_id}: {e}")
                time.sleep(POLL_S)
    finally:
        clk_off(inst)
        inst.close()
 
 
if __name__ == "__main__":
    main()