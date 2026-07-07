"""
instrument_control_attacks.py

Known-POSITIVE test fixture for the SCPI CodeQL query. Each function below
deliberately triggers one of the three attack checks, so you can confirm the
query fires on a known-bad input.

This file is for STATIC ANALYSIS (build a CodeQL database from it). It is NOT
meant to be executed — the while-True loop would hang. CodeQL extracts every
function whether or not it is called, so the findings appear regardless.

  1. clock injection  -> adjacent CLK then ARB writes
  2. unbounded loop    -> SCPI command in (a) a for-loop with a literal count
                          above 10, (b) a while-True with no break, and
                          (c) a loop whose count comes from untrusted input
  3. banned flashing   -> SYST:FIRM / MMEM / SYST:COMM commands
"""

import pyvisa
from flask import Flask, request

app = Flask(__name__)


def open_instrument(resource_name: str):
    rm = pyvisa.ResourceManager()
    return rm.open_resource(resource_name)


# 1. CLOCK INJECTION -- clock source set, immediately followed by ARB select.
def attack_clock_injection(inst):
    inst.write("ROSC:SOUR EXT")          # CLK
    inst.write("SOUR:FUNC ARB")          # ARB  -> adjacency CLK -> ARB flagged


# 2a. UNBOUNDED LOOP -- literal iteration count above the reasonable max (10).
def attack_loop_literal(inst):
    for i in range(1000):                # 1000 > 10  -> flagged
        inst.write("SOUR:VOLT 5")


# 2b. UNBOUNDED LOOP -- while-True with no break (potentially infinite).
def attack_loop_infinite(inst):
    while True:                          # no break  -> flagged
        inst.write("SOUR:FUNC ARB")


# 2c. UNBOUNDED LOOP -- iteration count derived from untrusted input.
@app.route("/burst")
def attack_loop_untrusted():
    inst = open_instrument("TCPIP::192.168.1.5::INSTR")
    n = int(request.args["count"])       # untrusted source (RemoteFlowSource)
    for i in range(n):                   # tainted loop bound  -> flagged
        inst.write("SOUR:VOLT 5")
    return "ok"


# 3. BANNED FLASHING -- firmware / memory / comm-port reconfiguration.
def attack_flashing(inst):
    inst.write("SYST:FIRM:UPD")          # firmware flash     -> banned
    inst.write("MMEM:STOR:STAT 1")       # mass-memory write  -> banned
    inst.write("SYST:COMM:LAN:DHCP ON")  # comm-port reconfig -> banned

def overvolt():
    inst.write("SOUR:VOLT 10000000000000")  # overvoltage -> flagged


if __name__ == "__main__":
    inst = open_instrument("TCPIP::192.168.1.5::INSTR")
    attack_clock_injection(inst)
    attack_flashing(inst)
    # attack_loop_literal / attack_loop_infinite / attack_loop_untrusted are
    # analyzed statically; not invoked here because the infinite loop hangs.