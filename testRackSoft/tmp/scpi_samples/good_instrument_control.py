"""
instrument_control_good.py

SAFE pyvisa / SCPI instrument control — known-NEGATIVE test fixture.

Every value that ends up in a SCPI command is validated against an allowlist
or coerced to a bounded numeric type before it is written, so there is no flow
from untrusted input into an instrument command string. A taint-tracking query
for SCPI command injection should produce ZERO results on this file.
"""

import sys
import pyvisa
from flask import Flask, request

app = Flask(__name__)

# Allowlist of channels we are willing to talk to.
VALID_CHANNELS = {"CH1", "CH2", "CH3", "CH4"}

VOLTAGE_MIN = 0.0
VOLTAGE_MAX = 30.0


def open_instrument(resource_name: str):
    """Open a VISA resource. resource_name is a fixed, trusted address."""
    rm = pyvisa.ResourceManager()
    return rm.open_resource(resource_name)


def _validate_channel(channel: str) -> str:
    if channel not in VALID_CHANNELS:
        raise ValueError(f"Unknown channel: {channel!r}")
    return channel


def _validate_voltage(voltage) -> float:
    # Coerce to float first: a malicious string can't survive float().
    v = float(voltage)
    if not (VOLTAGE_MIN <= v <= VOLTAGE_MAX):
        raise ValueError(f"Voltage {v} out of allowed range")
    return v


def set_voltage(inst, channel: str, voltage) -> None:
    """Set a channel voltage from validated, bounded inputs."""
    ch = _validate_channel(channel)
    v = _validate_voltage(voltage)
    # ch is from a fixed allowlist; v is a bounded float formatted with a
    # fixed specifier. Nothing attacker-controlled reaches the command verbatim.
    inst.write(f"SOUR:{ch}:VOLT {v:.3f}")


def read_measurement(inst, channel: str) -> str:
    """Query a measurement using only an allowlisted channel token."""
    ch = _validate_channel(channel)
    return inst.query(f"MEAS:{ch}:VOLT?")


@app.route("/set_voltage")
def set_voltage_route():
    """Web entry point — request data is validated before it touches SCPI."""
    inst = open_instrument("TCPIP::192.168.1.5::INSTR")

    # request.args is untrusted (a RemoteFlowSource), but we sanitize before use.
    channel = request.args.get("channel", "")
    voltage = request.args.get("voltage", "")

    try:
        set_voltage(inst, channel, voltage)
    except ValueError as exc:
        return f"rejected: {exc}", 400

    return "ok"


def set_from_cli() -> None:
    """CLI entry point — argv is validated against the allowlist before use."""
    inst = open_instrument("TCPIP::192.168.1.5::INSTR")
    channel = _validate_channel(sys.argv[1])
    inst.write(f"SOUR:{channel}:VOLT 5.000")


if __name__ == "__main__":
    set_from_cli()