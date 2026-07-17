"""do_drive: one-shot square-wave toggle test on DO0 (TB_D_OUT), ported
from do_send_output.py. Runs once, to completion, then returns -- not a
continuous poller, so this module has no teardown() (it handles its own
cleanup internally in a try/finally)."""

import time

from instro.daq import InstroDAQ
from instro.daq.drivers.labjack import LabJackTSeriesDriver
from instro.daq.drivers.ni import NIDAQDriver
from instro.daq.types import Direction, Logic

from btop_test_suite import doDriveControl

TEST_ID = "do_drive"
REQUIRED_DRIVER = "keysight_34980a"
KIND = "one_shot"

# test_do_drive commands DO0 (TB_D_OUT) from the Keysight side only -- to
# prove that signal actually reaches every external DAQ (rather than
# trusting the command alone), each device also listens for it on its own
# input line. All five devices read from CIO0 (LabJack) / DIO0 (NI) --
# confirmed wiring, and a different line than the CIO1/DIO1 lines
# di_raster_scan.py's stimulus rig drives as OUTPUTS, so both tests can run
# in the same session without conflicting.
DO_LISTENER_DEVICES = [
    # (listen_alias, driver_family, device_id, physical_channel)
    ("do_seen_t8", "labjack", "480011030", "CIO0"),                    # T8        listens for DO0
    ("do_seen_t7", "labjack", "470041016", "CIO0"),                    # T7        listens for DO0
    ("do_seen_t4", "labjack", "440020473", "CIO0"),                    # T4        listens for DO0
    ("do_seen_usb6421", "ni", "Dev1", "Dev1/port0/line0"),             # USB-6421  listens for DO0
    ("do_seen_cdaq9401", "ni", "cDAQ1Mod4", "cDAQ1Mod4/port0/line0"),  # NI-9401   listens for DO0
]


def _with_retry(fn, *, attempts=3, delay_s=1.0, label=""):
    """Call fn() (a zero-arg callable) and return its result, retrying on
    exception up to `attempts` times with a delay_s pause in between.

    LabJack (LJM) connections over USB can throw a transient error --
    confirmed on real hardware as "LJME_RECONNECT_FAILED" (error code 1239)
    from a read_digital_line() call right after a successful open() -- even
    when the device and wiring are fine, just from a USB/driver-level
    hiccup. Without this, one flaky read/write crashes the entire script;
    with it, a transient failure gets a couple of retries before actually
    giving up and re-raising.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < attempts:
                print(f"[retry] {label or 'call'} failed (attempt {attempt}/{attempts}): {e} "
                      f"-- retrying in {delay_s}s", flush=True)
                time.sleep(delay_s)
    raise last_exc


def run(daq, inst, publish):
    """One-shot square-wave toggle test on DO0 (TB_D_OUT): 1,0,1,0 with a 1s
    hold each, minus a small epsilon before each transition so the plot
    holds flat then snaps instead of ramping. Ported from
    do_send_output.py -- client.stream() calls become publish() calls; the
    four-level sequence and timing are otherwise unchanged.

    Also opens the DO listener rig (see DO_LISTENER_DEVICES above) so each
    external DAQ's own observed level -- not just the commanded do0 level --
    gets published alongside it every step, proving the signal actually
    reaches all five devices."""
    do_drive = doDriveControl()
    do_drive.configure_all(daq)
    do_drive.log.info("Ready. Toggling DO0 between 1 and 0, twice.")

    # One InstroDAQ session per DO listener device, opened once and
    # configured as a digital INPUT on CIO0/DIO0 (see DO_LISTENER_DEVICES).
    listeners = {}
    for listen_alias, driver_family, device_id, phys_ch in DO_LISTENER_DEVICES:
        if driver_family == "labjack":
            listen_daq = InstroDAQ(name=f"stim_{device_id}", driver=LabJackTSeriesDriver(device_id=device_id))
        elif driver_family == "ni":
            listen_daq = InstroDAQ(name=f"stim_{device_id}", driver=NIDAQDriver(device_id=device_id))
        else:
            raise ValueError(f"unknown stimulus driver family {driver_family!r}")
        _with_retry(lambda d=listen_daq: d.open(), label=f"{listen_alias}.open()")
        listen_daq.configure_digital_line(
            direction=Direction.INPUT,
            physical_channel=phys_ch,
            alias=listen_alias,
            logic=Logic.HIGH,
        )
        listeners[listen_alias] = listen_daq

    HOLD_S = 1.0
    EPSILON_S = 0.02   # small gap before the transition so the plot holds flat

    def _read_all_listeners():
        # Wrapped in _with_retry per-alias: a real hardware run hit
        # "LJME_RECONNECT_FAILED" here (a transient LJM/USB hiccup, not a
        # wiring or sequencing problem -- confirmed do_drive runs alone,
        # first, before anything else touches these devices) -- retrying a
        # couple of times before giving up avoids one flaky read crashing
        # the whole script.
        return {
            alias: float(_with_retry(
                lambda a=alias, d=listen_daq: d.read_digital_line(channel=a).latest,
                label=f"{alias}.read_digital_line()",
            ))
            for alias, listen_daq in listeners.items()
        }

    try:
        for level in [1, 0, 1, 0]:
            do_drive.set_drive(daq, level)
            do_drive.log.info(f"DO0 (TB_D_OUT) -> {level}")

            # Two points per plateau: one at the start, one just before the
            # next transition. Renders a flat hold followed by a sharp
            # vertical edge (square wave), instead of a diagonal ramp
            # between a single point per level. Each point also carries
            # every listener's own observed level alongside the commanded one.
            seen = _read_all_listeners()
            publish({do_drive.DO_DRIVE_ALIAS: float(level), **seen}, tags={"subsystem": "do_drive"})
            time.sleep(HOLD_S - EPSILON_S)

            seen = _read_all_listeners()
            publish({do_drive.DO_DRIVE_ALIAS: float(level), **seen}, tags={"subsystem": "do_drive"})
            time.sleep(EPSILON_S)
    finally:
        do_drive.safe_off(daq)
        for listen_daq in listeners.values():
            try:
                listen_daq.close()
            except Exception:
                pass
