"""btop_dc_psu: fully interlocked control for the rack's three bench
sources -- BK9115, Keysight N5745A, and the BK 8514B eLoad (see
PSUControl in btop_test_suite.py) -- with a single-select checkbox per
device (PSUControl.bk9115().cb_id / .n5745a().cb_id / .eload_8514b().cb_id)
so exactly ONE of the three is ever driving/drawing at a time. The other
two are always held safe_off() -- never both/all three enabled together.

PSUControl.apply_selection() enforces this with break-before-make: every
non-selected device gets safe_off() BEFORE the newly-selected device gets
configure()'d, so there's never a moment where two are commanded on at
once. If the eLoad is the selected device, its operating mode is ALSO
checkbox-driven (PSUControl.CB_MODE_CC/CV/CR/CP), and each mode has its own
level text box (PSUControl.LEVEL_FIELD_CC/CV/CR/CP -- Amps/Volts/Ohms/Watts
respectively, validated against PSUControl.LEVEL_RANGE_BY_MODE's reasonable
per-mode caps for this rig) -- changing the mode checkbox or the current
mode's level text box while the eLoad is already selected disables its
output, applies the new mode/level, and re-enables it (same
break-before-make principle, one level down).

All three devices are sensed (voltage/current read + streamed to Connect)
every pass regardless of which one is actively selected -- the two
deselected devices will just read ~0V/0A since their outputs are off.

This file's name predates the eLoad/N5745A interlock (it started as a
BK9115-only script); kept for import/script-name continuity. Matches this
project's existing pattern of a thin driver script calling into a shared
*Control class (see do_send_output.py/doDriveControl,
DAQ_counter.py/MultiCounterControl).

Note: rack-wide relay safety monitoring lives in its own standalone
script (btop_safe_to_test.py / SafeToTestControl) -- it does not gate this
script; run it alongside for a live safe-to-test indicator + notification."""

from datetime import datetime

import connect_python

from btop_test_suite import PSUControl


@connect_python.main
def main(client: connect_python.Client):
    bk_ctl = PSUControl.bk9115()
    n5745a_ctl = PSUControl.n5745a()
    eload_ctl = PSUControl.eload_8514b()
    group = [bk_ctl, n5745a_ctl, eload_ctl]

    sessions = {}
    for ctl in group:
        instrument = ctl.create_instrument()
        instrument.open()
        instrument.start()   # required before get_channel() -- see PSUControl.bk9115()'s docstring for why
        ctl.safe_off(instrument)   # make sure nothing is left enabled from a previous run
        sessions[ctl] = instrument

    # apply_selection() owns this dict across calls -- empty means "no
    # selection applied yet," so the first pass always acts (everything
    # starts/stays off if nothing is checked).
    interlock_state = {}
    # Tracks the eLoad's own last-applied mode/level separately, so a
    # mode-only or level-only change while the eLoad stays selected still
    # gets applied (apply_selection() only reacts to a DEVICE change, not a
    # mode/level change within the same selected device).
    last_eload_mode = None
    last_eload_level = None

    try:
        bk_ctl.log.info(
            "Ready. Check ONE device checkbox in the SOURCE SELECT tab "
            f"({bk_ctl.cb_id!r} / {n5745a_ctl.cb_id!r} / {eload_ctl.cb_id!r}); "
            f"if eLoad is selected, also check ONE mode checkbox "
            f"({eload_ctl.CB_MODE_CC!r} / {eload_ctl.CB_MODE_CV!r} / "
            f"{eload_ctl.CB_MODE_CR!r} / {eload_ctl.CB_MODE_CP!r}) and set its "
            f"matching level text box ({eload_ctl.LEVEL_FIELD_CC!r} / "
            f"{eload_ctl.LEVEL_FIELD_CV!r} / {eload_ctl.LEVEL_FIELD_CR!r} / "
            f"{eload_ctl.LEVEL_FIELD_CP!r} -- see PSUControl.LEVEL_RANGE_BY_MODE "
            f"for this rig's reasonable per-mode caps)."
        )
        while True:
            try:
                # Determine the eLoad's checkbox-selected mode AND its
                # matching level field BEFORE running the interlock, and
                # set both on the instance now -- if apply_selection()
                # below ends up selecting/reselecting the eLoad, its own
                # configure() call already uses this fresh mode/level,
                # instead of whatever was set last time (which would
                # otherwise mean briefly enabling with the WRONG mode/
                # level, then immediately having to disable and
                # reconfigure again to fix it). selected_level() validates
                # against LEVEL_RANGE_BY_MODE and raises ValueError (loud,
                # not silently clamped) if the text box holds something
                # outside this rig's reasonable cap for that mode.
                mode = eload_ctl.selected_mode(client)
                level = eload_ctl.selected_level(client, mode)
                mode_changed = mode != last_eload_mode
                level_changed = level != last_eload_level
                eload_ctl.mode = mode
                eload_ctl.level = level

                was_selected = interlock_state.get("last_selected") is eload_ctl
                selected = PSUControl.apply_selection(client, group, sessions, interlock_state)

                if selected is eload_ctl:
                    # apply_selection() only reacts to a DEVICE change, not
                    # a mode/level change within the same already-selected
                    # device -- if the eLoad was ALREADY selected and
                    # either the mode or the level changed, reconfigure it
                    # here (break-before-make within the eLoad itself, same
                    # principle as the device-level interlock above). If
                    # the eLoad was just newly selected this pass,
                    # apply_selection() already configured it with the
                    # fresh mode/level above -- nothing more to do.
                    if was_selected and (mode_changed or level_changed):
                        eload_ctl.log.info(
                            f"eLoad mode/level changed: "
                            f"{last_eload_mode.value if last_eload_mode else None}/{last_eload_level} -> "
                            f"{mode.value}/{level}")
                        eload_ctl.safe_off(sessions[eload_ctl])
                        eload_ctl.configure(sessions[eload_ctl])
                    last_eload_mode = mode
                    last_eload_level = level
                else:
                    last_eload_mode = None   # re-selecting eLoad later always re-applies the current checkbox mode/level fresh
                    last_eload_level = None

                for ctl, instrument in sessions.items():
                    voltage, current = ctl.read_channel(instrument)
                    now = datetime.now()
                    client.stream(PSUControl.STREAM_ID, now, voltage, name=f"{ctl.name}_voltage")
                    client.stream(PSUControl.STREAM_ID, now, current[-1], name=f"{ctl.name}_current")
                    print(f"{ctl.name}: Voltage={voltage} Current={current}")
            except KeyboardInterrupt:
                print("Stopping...")
                break
    finally:
        for ctl, instrument in sessions.items():
            ctl.safe_off(instrument)
            ctl.shutdown(instrument)


if __name__ == "__main__":
    main()
