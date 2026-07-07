"""
instrument_control_bad.py

UNSAFE pyvisa / SCPI instrument control — known-POSITIVE test fixture.

This file deliberately contains SCPI command-injection bugs so you can confirm
your CodeQL query actually fires. In each marked spot, untrusted input
(HTTP query parameters, environment variables, command-line args) flows
straight into a pyvisa instrument command with no validation. Because SCPI
chains commands on one line with ';', controlling part of a command string can
mean injecting additional commands to the instrument.

DO NOT use these patterns in real instrument control code. This is a test
fixture for static-analysis tooling only.
"""

import os
import sys
import pyvisa
from flask import Flask, request

app = Flask(__name__)


def open_instrument(resource_name):
    rm = pyvisa.ResourceManager()
    return rm.open_resource(resource_name)

@app.route("/set_voltage_out_of_bounds")
def set_out_of_bounds_voltage():
    # invalid scpi 
    inst = open_instrument("SOUR:VOLT 10000000000000") 


@app.route("/set_voltage")
def set_voltage_route():
    inst = open_instrument("TCPIP::192.168.1.5::INSTR")

    # VULNERABLE: untrusted query param concatenated straight into a SCPI command.
    # e.g. ?voltage=5;SYST:COMM:LAN:DHCP ON  injects a second command.
    voltage = request.args["voltage"]
    inst.write("SOUR:VOLT " + voltage)  # <-- injection sink

    return "ok"



@app.route("/raw")
def raw_command_route():
    inst = open_instrument("TCPIP::192.168.1.5::INSTR")

    # VULNERABLE: attacker fully controls the command string sent to the device.
    cmd = request.args["cmd"]
    inst.write(cmd)  # <-- injection sink (full command control)

    return inst.read()


@app.route("/measure")
def measure_route():
    inst = open_instrument("TCPIP::192.168.1.5::INSTR")

    # VULNERABLE: untrusted channel name interpolated into a query command.
    channel = request.args["channel"]
    result = inst.query(f"MEAS:{channel}:VOLT?")  # <-- injection sink

    return result


def set_from_cli():
    inst = open_instrument("TCPIP::192.168.1.5::INSTR")

    # VULNERABLE: command-line argument flows into the command unchecked.
    channel = sys.argv[1]
    inst.write(f"SOUR:{channel}:VOLT 5.0")  # <-- injection sink


def set_from_env():
    inst = open_instrument("TCPIP::192.168.1.5::INSTR")

    # VULNERABLE: environment variable flows into the command unchecked.
    setpoint = os.environ["SETPOINT"]
    inst.write("SOUR:VOLT " + setpoint)  # <-- injection sink


if __name__ == "__main__":
    set_from_cli()