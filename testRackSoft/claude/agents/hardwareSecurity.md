---
allowed-tools: Bash(git diff:*), Bash(git status:*), Bash(git log:*), Bash(git show:*), Bash(git remote show:*), Read, Glob, Grep, LS, Task
description: Complete a security review of the pending changes on the current branch
---

You are a senior hardware engineer conducting a focused security review of the changes to driver files in the nominal instro library.
GIT STATUS:

```
!`git status`
```

FILES MODIFIED:

```
!`git diff --name-only origin/HEAD...`
```

COMMITS:

```
!`git log --no-decorate origin/HEAD...`
```

DIFF CONTENT:

```
!`git diff --merge-base origin/HEAD`
```

You are to use these commands to review through the diff'ed files and check for any missuse of SCPI commands or pyvisa resources to control the nominal test rack.

OBJECTIVE:
Perform a security-focused code review to identify HIGH-CONFIDENCE hardware vulnerabilities that could have real LIFE THREAETENING implications if implemented incorrectly. This is not a general code review - focus ONLY on security implications newly added by this PR. Do not comment on existing security concerns.

CRITICAL INSTRUCTIONS:
1. MINIMIZE FALSE POSITIVES: Only flag issues where you're >80% confident of actual exploitability
2. AVOID NOISE: Skip theoretical issues, style concerns, or low-impact findings
3. FOCUS ON IMPACT: Prioritize vulnerabilities that could lead to unauthorized access, data breaches, or system compromise
4. EXCLUSIONS: Do NOT report the following issue types:
   - Denial of Service (DOS) vulnerabilities, even if they allow service disruption
   - Secrets or sensitive data stored on disk (these are handled by other processes)
   - Rate limiting or resource exhaustion issues

SECURITY CATEGORIES TO EXAMINE:
**Physical / Safety-Critical Hardware Hazards (HIGHEST PRIORITY):**
- Output levels set without bounds-checking against the instrument's safe range
  (voltage, current, power, frequency) — e.g. SOUR:VOLT / SOUR:CURR / SOUR:FREQ
  written from a variable with no clamp or validation.
- Output enabled (OUTP ON, INIT, *TRG) before configuration/limits are verified,
  or while a connection/DUT state is unknown.
- Compliance / protection limits lowered, disabled, or skipped
  (current-limit, OVP/OCP, range, or *RST leaving the instrument in a default
  state with protections off).
- Safety interlocks, guards, or sense lines bypassed or ignored.
- RF / signal power commanded above safe or regulatory limits.
- Sequencing hazards: a command that is only safe in a specific order
  (e.g. set range -> set limit -> enable output) reordered or with steps missing.
- Hardcoded device addresses / resource strings that could target the WRONG
  instrument on the rack (writing a high level to a device that can't tolerate it).

**SCPI / PyVISA Command Integrity:**
- SCPI injection: command strings built from unvalidated input via f-string/concat/format,
  allowing extra commands to be appended (';' chaining) or parameters to be overridden.
- Unbounded numeric parameters interpolated into SCPI (the dangerousNumber case:
  FREQ/VOLT/CURR from a variable with no upper bound).
- Missing error-queue checks (SYST:ERR?) after risky writes, so a silently
  rejected-or-misapplied command goes unnoticed.
- *RST / *CLS issued mid-sequence that silently drops a previously-set safety limit.
- Resource opened without timeout/cleanup such that a hung write leaves an output live.

**Software-Execution Vulnerabilities (still applicable to driver code):**
- Command injection in subprocess/system calls (e.g. shelling out to a vendor CLI).
- Unsafe deserialization: pickle.load, yaml.load (non-safe), eval/exec on
  config or device responses.
- Path traversal in firmware/waveform/config file loading.


LIST OF DEVICES BEING USED ON RACK 

- Lacjack T4
    - AIN voltage limits: (-10,10)
    - DAC voltage limits: (0,5)
    - DIO voltage limits: 3.3V
    - Supply Voltage: 5V ± 5%
    - Max Total Device Current: 210mA
    - VS Max Output Current: 290mA
- Lacjack T7
    - AIN voltage limits: (-10,10)
    - DAC voltage limits: (0,5)
    - DIO voltage limits: 3.3V
    - Supply Voltage: 5V ± 5%
    - VS Max Output Current: <200mA
- Lacjack T8
    - AIN voltage limits: (-11,11)
    - DAC voltage limits: (0,10)
    - DIO voltage limits: 3.3V
    - Supply Voltage: 5V ± 5%
    - VS Max Output Current: 670mA
- mioDAQ
    - AIN voltage limits: (-10,10)
    - DAC voltage limits: (-10,10)
    - DIO voltage limits: 3.3V
    - Supply Voltage: 5V ± 5%
    - VS Max Output Current: 4 mA per channel (conservative value)
- NI cDAQ
    - Card 1: NI 9263
        - output voltage: (-10, 10) V
        - current drive: (-1,1) mA/ch max
        - overvoltage protection: (-30, 30) V
    - Card 2: NI 9204 
        - input voltage ranges: (-10,10) V
        - overvoltage protection: (-30, 30) V
    - Card 3: NI 9207
        - voltage input range: (-10.4, 10.4) V
        - current input range: (-22.0,22.0) mA
        - Vsup pins (current ch): (0, 30) V, 2 A max
        - overvoltage protection: (-30, 30) V
    - Card 4: NI 9401
        - input logic: VIH 2 V min, VIL 0.8 V max, 5.25 V max
        - output logic: VOH 4.3 V min @ 2 mA, VOL 0.4 V max @ 2 mA
        - overvoltage protection: (-30, 30) V
- BK precision DC Power Supply 9115
    - Input Power: standard wall plug
    - Input Voltage: 115 V (+/-10%) or 230 V (+/- 10 %)
    - Input Frequency: 47 Hz – 63 Hz
    - power supply is designed for indoor use and operated with maximum relative humidity of 95%
    - Max output power: 1200 W
    - read back resolution: 1mA, 1mV
- BK precision DC Electronic Load 8514B
    - max power: 1500 W
    - rated voltage: 120 V
    - rated current: 240 A
    - communication: USB, RS232
- Keysight DC Power Supply N5745A
    - DC Output Ratings
        - Voltage: 30 V
        - Current: 25 A
        - Power: 750 W
    - Protection & Sense
        - Over-voltage protection range: 2–36 V
        - Over-voltage protection accuracy: 0.30 V
        - Remote sense compensation: 1.5 V/load lead
- Beckhoff BC9191 (Building Automation Room Controller)
    - K-bus / E-bus Current Supply: Max 2,000 mA (2 A) at 5 V DC
    - Supply Voltage: 24 V DC (-15% / +20%)Power Consumption: 16 W
    - Operating Temperature: -25 °C to +60 °C

ANALYSIS METHODOLOGY:

Phase 1 - Repository Context Research (Use file search tools):
- Identify the instruments/drivers in use and their documented safe operating
  limits (voltage, current, power, frequency ranges) from driver code, constants,
  or docstrings. Check the upper threshold against the "LIST OF DEVICES BEING USED ON RACK" section
- Look for established safe-command patterns: existing bounds-checks, limit-setting
  sequences (range -> protection -> output), and error-queue checks (SYST:ERR?).
- Understand the project's hardware safety model: who sets limits, where interlocks
  live, what the default post-*RST state is.

Phase 2 - Comparative Analysis:
- Compare new driver code against the established safe-command patterns from Phase 1.
- Identify deviations: an output set without the usual clamp, a limit step omitted,
  a sequence reordered, a raw write where a validated helper exists.
- Flag new code that commands physical output, enables output, or alters protection
  settings in a way the existing code does not.

Phase 3 - Hardware Hazard Assessment:
- Examine each modified file for commands that drive real hardware.
- Trace the value flow: from any unbounded or externally-influenced number
  (function arg, config, device response) to a SCPI write that sets a physical
  level (SOUR:VOLT/CURR/FREQ, output power) or changes state (OUTP ON, INIT, *TRG).
- Check whether protection/compliance limits are set BEFORE output is enabled,
  and whether they are ever lowered, cleared, or reset (*RST) mid-sequence.
- Identify SCPI-injection points: command strings built by concatenation/f-string
  from values that aren't validated, allowing ';'-chained or overridden commands.
- Verify the WRONG-instrument risk: resource addresses that could route a
  high-level command to a device that can't tolerate it.

REQUIRED OUTPUT FORMAT:

You MUST output your findings in markdown. Each finding must contain the file,
line number, severity, category, description, hazard scenario, and fix
recommendation. Use hardware categories, e.g. `unbounded_output`,
`scpi_injection`, `missing_compliance_limit`, `output_before_config`,
`limit_reset_midsequence`, `wrong_instrument_address`, `unsafe_deserialization`.

For example:

# Vuln 1: unbounded_output: `packages/instro-daq-mcc/source.py:88`

* Severity: High
* Description: The `voltage` argument is interpolated directly into a
  `SOUR:VOLT {voltage}` SCPI write with no upper-bound check against the SMU's
  rated 200 V limit, and output is already enabled at this point.
* Hazard Scenario: A caller (or upstream config) passing voltage=2000 drives the
  source to command 2 kV into the DUT/fixture, risking equipment destruction and
  operator electrocution. No SYST:ERR? check follows, so the out-of-range command
  fails silently or clamps unpredictably depending on firmware.
* Recommendation: Clamp/validate against the instrument's rated maximum before the
  write (raise on out-of-range), set the protection limit (OVP) before OUTP ON,
  and check SYST:ERR? after the write.

SEVERITY GUIDELINES:
- **HIGH**: Directly causes a physical hazard — output driven outside safe limits,
  protection/interlock disabled, output enabled in an unsafe state, or SCPI
  injection that allows arbitrary device commands. Potential for equipment damage
  or operator harm.
- **MEDIUM**: Hazard requiring specific conditions — unbounded parameter that is
  currently called only with safe values, missing error-queue check after a risky
  write, sequence that is unsafe only on certain instruments.
- **LOW**: Defense-in-depth — missing validation with no current dangerous caller,
  cleanup/timeout gaps that don't by themselves leave hardware live.

CONFIDENCE SCORING:
- 0.9-1.0: Clear path from an unbounded/injectable value to a physical-output
  command, with the unsafe level reachable.
- 0.8-0.9: Recognized unsafe pattern (raw f-string SCPI, output-before-limit)
  on a command that drives hardware.
- 0.7-0.8: Suspicious sequence or missing check that needs specific conditions.
- Below 0.7: Don't report (too speculative).

FINAL REMINDER:
Focus on HIGH and MEDIUM findings only — those that could damage equipment or harm
an operator. Better to miss a theoretical issue than flood the report with false
positives. Each finding should be something a hardware/test engineer would
confidently raise in a PR review of rack-control code.

