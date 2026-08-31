# The safety argument for this cell

What is claimed, what supports it, and what it does not cover. The order matters:
the limitations are not an appendix, they are half the document.

## 1. What this is, and what it is not

**It is not a functional safety assessment.** There is no Performance Level and
no SIL here, and none can be derived from what is in this repository. Both come
from a risk assessment that judges severity, frequency of exposure and
possibility of avoidance for a *real* machine, made by people who have stood in
front of one. This is a simulation on a laptop.

**It is not a validated design.** Nothing here has been checked against a
physical cell. The plant is a model of a bottling line written by the same person
who wrote the controller, which means the controller has never met a plant that
behaved in a way its author did not anticipate.

**What it is:** a hazard derived requirement set where every requirement traces
up to a safety goal and a hazard, and down to at least one test, with a gate that
fails the build on a gap in either direction. That is a useful and checkable
thing. It is not a safety rating and this document never calls it one.

## 2. The claim

> For the hazards catalogued in `safety/hazards.yaml`, the control program does
> not command hazardous motion, and does not resume motion without a deliberate
> human action.

Narrow on purpose. It says nothing about hazards nobody thought of, nothing about
the physical guard, nothing about the safety channel's own integrity, and nothing
about what the machine does mechanically once torque is removed.

## 3. The evidence

| Claim | Where | Why it is evidence |
|---|---|---|
| Every requirement is verified | `report/traceability.md` | Generated from the analysis and the tests, rebuilt in CI, fails on a gap either way |
| The gate itself works | `tests/test_traceability_gate.py` | Each direction is deliberately broken and watched to fail |
| The ST matches the verified model | `tests/test_st_matches_the_model.py` | The program cannot be executed here, so it is parsed and compared against the exhaustively verified Python model |
| The wire layer is exhaustive | `tests/test_modbus.py` | Every function code, exception path, boundary, malformed frame and split point, with no socket |
| It works against a real client | `pymodbus` interop | A server checked only by the client that shares its assumptions proves self consistency, not a wire format |
| 239 tests, 100% branch coverage, ruff and mypy strict | `.github/workflows/verify.yml` | The harness is not the weak link |

### The traceability gate runs in both directions

A requirement with no test is a hole: something was asserted and never checked.
A test claiming a requirement that does not exist is the quieter failure, because
it runs, it passes, and it makes the matrix look complete while verifying nothing
that was asked for. `SR-O1` for `SR-01` is one character.

Claims are read by parsing the test files with `ast` rather than by searching
them. A marker inside a docstring or behind a comment is not a claim, and a grep
would count both.

## 4. The findings that came from RUNNING it

This is the section that justifies the project existing, and it is worth reading
before the requirement list. **Every defect below survived a suite that was at
100% branch coverage at the time**, and every one was found within minutes of the
cell being driven by a real controller.

**The reset request was wired to nothing.** The program computed
`SAFETY_RESET_REQUEST` and the plant read four coils and ignored the fifth. The
documented recovery from a guard opening was unreachable, and no test noticed
because both halves were individually correct.

**The safety request depended on which branch of a `CASE` ran first.** It was
derived from `CmdReset`, which the state machine consumes and clears in the same
scan, before the output block runs. It could never assert from Stopped.

**Commands latched.** A command arriving in a state that ignores it was never
cleared, so it fired the next time the machine entered a state that consumed it.
Observed live with `CmdReset` and `CmdStart` both true in Execute: the cell was
one Stop away from resetting itself.

**The plant died when the controller reset the connection.** The graceful
disconnect was handled and the abrupt one was not, which is backwards for a rig
whose normal condition is being downloaded to repeatedly.

**The controller never reconnected.** `AutoReconnect` defaults off, so losing the
plant once required a full download to recover.

The general lesson, which generalises past this project: **coverage measures
which lines ran, not which situations were imagined.** Every one of these was a
situation nobody had imagined, and the cheapest way to imagine them turned out to
be running the thing.

### The result the cell now demonstrates

With the controller producing, the plant was killed and restarted, and nothing
was touched in the IDE:

| | Link lost | Link restored |
|---|---|---|
| PackML state | Execute → **Aborted** | stays Aborted |
| actuators | all off | all off |
| connection | dropped | **reconnected by itself** |
| production | stopped | **stays stopped** |

> The cell recovers the connection automatically and refuses to recover the
> machine automatically.

Both halves are deliberate. A dropped socket is an infrastructure event, and a
controller that needs a human to reconnect one is useless. A machine that stopped
for a reason the controller could not see is a different thing entirely, and the
only correct response is to wait for somebody who can see it.

## 5. What is NOT verified, and cannot be from here

**The safety channel itself.** Torque availability is an input. Whether the thing
providing it is a relay, a safety PLC or a piece of string is outside this
model, and every requirement here assumes it works. In a real cell that channel
is where the Performance Level actually lives.

**Anything mechanical.** STO here is state logic: the program stops commanding
motion. What the machine physically does while it coasts, whether a vertical axis
falls, whether the capper is still descending, is not modelled. A cell that stops
commanding is not the same as a cell that has stopped.

**Timing in seconds.** Everything is measured in scans. The plant advances in
discrete steps and the relationship between a scan and a millisecond is a
simulation parameter. No latency here converts to a reaction time.

**The completeness of the hazard list.** Five hazards were considered. There is
no argument anywhere in this repository that five is all of them, and there is no
method here that would find a sixth.

**Common cause.** Every requirement is verified against one thing going wrong.
The link failing *and* the guard being open, or a stuck sensor *and* a stalled
heartbeat, are not covered. The fault injection harness this project sits
alongside does exactly that analysis for its own device and finds that the
answers change; the same is very likely true here and is simply not done.

**Security.** Modbus TCP has no authentication and no integrity protection.
Anything that can reach port 502 can command every actuator, and the only reason
this is acceptable is that the plant binds loopback on a laptop. On a real
network this belongs in a segregated cell zone with a conduit through a firewall,
per IEC 62443. That is stated rather than solved.

## 6. What would make this stronger

In the order a real project would do them.

1. **A second independent review.** The fault injection harness found that its
   most impressive result was partly self fulfilling, and it took an outside
   reader noticing two documents disagreeing to see it. Nothing here has had
   that.
2. **Common cause and dual point analysis**, importing the method already built
   next door.
3. **The safety channel carried over PROFIsafe framing**, which exists and is
   tested in the fault injection harness, giving a real protected channel
   instead of a plain coil.
4. **Scenario runs with OEE**, which would let the requirements be argued against
   production data rather than against unit tests.
5. **A physical cell**, which is the only thing that turns any of this from
   verification into validation.
