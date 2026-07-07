/**
 * @name SCPI command sent to an instrument via pyvisa
 * @description Flags SCPI command strings passed to instrument write/query
 *              calls, so risky or deprecated commands can be reviewed.
 * @kind problem
 * @problem.severity warning
 * @precision low
 * @id py/scpi-command-sent
 * @tags instrumentation
 *       maintainability
 *       security
 */

import python
import semmle.python.dataflow.new.DataFlow
import semmle.python.dataflow.new.TaintTracking
import semmle.python.dataflow.new.RemoteFlowSources

// pyvisa methods that send/receive a command. Over-broad on purpose: we flag
// the call, then inspect the command string for risky SCPI fragments.
predicate visaMethod(string name) {
  name in [
      "write", "query",
      "write_ascii_values", "query_ascii_values",
      "write_binary_values", "query_binary_values",
      "open_resource", "get_instrument", "write_binary_values" 
    ]
}

// IEEE 488.2 common commands that are worth a review when seen.
predicate ieeeSCPI(string fragment) {
  fragment in [
      "\\*CLS",     // clear status
      "\\*ESE",     // event status enable
      "\\*ESR",     // event status register query
      "\\*IDN",     // identity query
      "\\*OPC",     // operation complete
      "\\*RST",     // full instrument reset
      "\\*SRE",     // service request enable
      "\\*STB",     // status byte query
      "\\*TST",     // self test query
      "\\*WAI",     // wait to continue
      "SYST:ERR",   // error-queue read
      "OUTP\\s+ON"  // output enable
    ]
}

// Waveform / source subsystem fragments.
predicate scpiWaves(string fragment) {
  fragment in [
      "APPL", "DATA", "TRAC",
      "SOUR:FUNC", "SOUR:VOLT", "SOUR:FREQ"
    ]
}

// OVER-VOLTAGE / OVER-FREQUENCY / OVER-SAMPLERATE CHECKS -----------------------------------------------
predicate scpiLimit(string keyword, float maxValue) {
  keyword = "FREQ" and maxValue = 1000000000.0
  or
  keyword = "VOLT" and maxValue = 10.0
  or
  keyword = "RATE" and maxValue = 1000000000.0
}

bindingset[cmdText]
predicate dangerousNumber(string cmdText, string keyword, float value, float maxValue) {
  scpiLimit(keyword, maxValue) and
  exists(string numStr |
    numStr = cmdText.regexpCapture("(?i).*" + keyword + "\\s+([0-9.eE+-]+).*", 1) and
    value = numStr.toFloat() and
    value > maxValue
  )
}

// CLOCK INJECTION SECTION ---------------------------------------------------------------------------
// Match a fragment anywhere inside a call's first argument (literals, concatenations, and the literal chunks of f-strings).
bindingset[frag]
predicate argContains(Call c, string frag) {
  exists(StrConst s |
    s = c.getArg(0).getASubExpression*() and
    s.getText().regexpMatch("(?i).*" + frag + ".*")
  )
}

// Classify a pyvisa write call as a clock (CLK) or arbitrary-waveform (ARB)
// command. Extend these fragment lists to match your instrument's command set.
predicate commandKind(Call c, string kind) {
  visaMethod(c.getFunc().(Attribute).getName()) and
  (
    argContains(c, "ROSC") and kind = "CLK"            // reference-oscillator / clock
    or
    argContains(c, "SOUR:FUNC\\s+ARB") and kind = "ARB" // select arb function
    or
    argContains(c, "SOUR:ARB") and kind = "ARB"         // arb data / sample rate / etc.
  )
}

// actual check is done here
predicate clockInjection(Call send, string reason) {
  exists(StmtList block, int i, Stmt s1, Stmt s2, Call prev, string k1, string k2 |
    s1 = block.getItem(i) and
    s2 = block.getItem(i + 1) and
    s1.contains(prev) and commandKind(prev, k1) and
    s2.contains(send) and commandKind(send, k2) and
    reason = "adjacent SCPI " + k1 + " -> " + k2 + " (possible clock-glitch setup)"
  )
}

// infinite loop of SCPI CMD check -------------------------------------------------------

// A SCPI command emitted inside a loop whose iteration count is unreasonable, untrusted, or unbounded can flood / abuse the instrument.
int reasonableMax() { result = 10 }
 
Call rangeCallOf(For loop) {
  result = loop.getIter() and
  result.getFunc().(Name).getId() = "range"
}
 
// Untrusted-input -> loop-bound taint configuration (boolean flow form).
module LoopBoundConfig implements DataFlow::ConfigSig {
  predicate isSource(DataFlow::Node source) { source instanceof RemoteFlowSource }
 
  predicate isSink(DataFlow::Node sink) {
    exists(For loop |
      sink.asExpr() = loop.getIter()
      or
      sink.asExpr() = loop.getIter().(Call).getArg(_)
    )
  }
}
 
module LoopBoundFlow = TaintTracking::Global<LoopBoundConfig>;
 
predicate infiniteLoop(Call send, string reason) {
  exists(For loop, Call r, IntegerLiteral lit |
    loop.contains(send) and
    r = rangeCallOf(loop) and
    lit = r.getArg(0) and
    lit.getValue() > reasonableMax() and
    reason =
      "SCPI command in for-loop with literal count " + lit.getValue().toString() +
        " exceeding reasonable max " + reasonableMax().toString()
  )
  or
  // CASE 2: literal NOT found -> untrusted-input check
  exists(For loop, DataFlow::Node sink |
    loop.contains(send) and
    (
      sink.asExpr() = loop.getIter()
      or
      sink.asExpr() = loop.getIter().(Call).getArg(_)
    ) and
    not loop.getIter().(Call).getArg(0) instanceof IntegerLiteral and
    LoopBoundFlow::flow(_, sink) and
    reason = "SCPI command in for-loop whose count derives from untrusted input"
  )
  or
  // CASE 3: while-loop with no break -> potentially unbounded
  exists(While loop |
    loop.contains(send) and
    not exists(Break b | loop.contains(b)) and
    reason = "SCPI command in while-loop with no break (potentially unbounded)"
  )
}

// Unauthorized FLASHING via SCPI COMM command -------------------------------------------------------
predicate bannedFlashCmd(string fragment) {
  fragment in [
      "SYST:FIRM",   // firmware update / flash
      "SYST:UPD",    // system update
      "SYST:COMM",   // comm-port (re)configuration
      "MMEM",        // mass-memory store / delete / load
      "DIAG",        // diagnostic / service mode
      "PROG",        // program subsystem
      "\\*PUD"       // protected user data write
    ]
}

// ---------------------------------------------------------------------------

from Call send, StrConst cmd, string method, string reason
where
  method = send.getFunc().(Attribute).getName() and
  visaMethod(method) and
  cmd = send.getArg(0).getASubExpression*() and
  (
    exists(string fragment |
      ieeeSCPI(fragment) and
      cmd.getText().regexpMatch("(?i).*" + fragment + ".*") and
      reason = "risky command fragment: " + fragment
    )
    or
    exists(string fragment |
      scpiWaves(fragment) and
      cmd.getText().regexpMatch("(?i).*" + fragment + ".*") and
      reason = "waveform fragment: " + fragment
    )
    or
    exists(string keyword, float value, float maxValue |
      dangerousNumber(cmd.getText(), keyword, value, maxValue) and
      reason =
        keyword + " setpoint " + value.toString() + " exceeds limit " + maxValue.toString()
    )
    or 
    (bannedFlashCmd(cmd.getText()) and reason = "banned flash / firmware command")
    or
    (infiniteLoop(send, reason) and reason = "SCPI command in loop with untrusted or unbounded iteration count")
    or 
    clockInjection(send, reason) and reason = "adjacent SCPI clock / arb commands (possible clock-glitch setup)"
  )
select send, "PyVISA ." + method + "(): " + reason + " in string '" + cmd.getText() + "'"
