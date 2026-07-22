"""btop_safe_to_test: standalone relay-safety watcher for the rack.

Continuously reads the six digital lines (dc_panel_daq/port0/line0-5) that
Connect's own built-in NI-DAQmx device connector already streams for the
health-monitor USB-6002 (see
'CI Rack Config/health-monitor-usb-6002.ni-daqmx.ni-daqmx.json'), via
SafeToTestControl (btop_test_suite.py) -- NANDs them together (safe only
if ALL SIX read 0; any line reading 1 means NOT safe), streams a
safe_to_test boolean indicator, and pops up a Connect notification (toast)
plus a log line on every transition between safe and NOT safe -- once per
transition, not every poll, so it doesn't spam.

This script does NOT gate or control anything else on the rig -- it is a
read-only, standalone monitor. It opens no hardware session of its own: it
only reads Connect's already-published telemetry via
client.get_channel_values(), the exact same call SafeToTestControl has
always used, so there's no risk of a competing DAQmx session on the
USB-6002 (which Connect's own device connector already owns exclusively).

Run this alongside whatever other scripts are active on the rack. Matches
this project's existing pattern of a thin driver script calling into a
shared *Control class (see do_send_output.py/doDriveControl,
DAQ_counter.py/MultiCounterControl, btop_dc_psu.py/PSUControl)."""

import time
from datetime import datetime

import connect_python

from btop_test_suite import SafeToTestControl

POLL_S = 0.5


@connect_python.main
def main(client: connect_python.Client):
    safe_ctl = SafeToTestControl()
    safe_ctl.log.info(
        f"Safe-to-test watcher ready -- monitoring {safe_ctl.RELAY_LINE_CHANNELS} "
        f"on stream {safe_ctl.HEALTH_MONITOR_STREAM_ID!r}."
    )

    # None (not True/False) so the very first pass always fires a
    # transition -- both to log the starting state and to prime last_safe.
    last_safe = None

    try:
        while True:
            is_safe = safe_ctl.is_safe(client)
            client.stream(safe_ctl.STREAM_ID, datetime.now(), 1.0 if is_safe else 0.0, name="safe_to_test")

            if is_safe != last_safe:
                if is_safe:
                    safe_ctl.log.info("Safe to test -- all relay lines clear.")
                    client.send_notification(
                        "Safe to test -- all relay lines clear.", level="info", duration_seconds=5
                    )
                else:
                    safe_ctl.log.info("NOT safe to test -- a relay line is energized.")
                    client.send_notification(
                        "NOT safe to test -- a relay line is energized.", level="error"
                    )
                last_safe = is_safe

            time.sleep(POLL_S)
    except KeyboardInterrupt:
        print("Stopping...")


if __name__ == "__main__":
    main()
