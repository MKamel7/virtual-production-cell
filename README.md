# Virtual production cell

A packaging cell simulated in enough detail to run **real IEC 61131-3 control
against it**, which is what virtual commissioning means: the PLC program is the
thing under test, and the plant is a model it drives.

Work in progress. This README describes what exists, not what is planned.

## Architecture

```
PLC program, Structured Text          the thing under test
   |  process image over Modbus TCP
   v
plant simulation, Python              deterministic, exhaustively tested
   |
   +-- OPC UA server                  supervisory interface
   +-- safety channel                 imports the protection layer from P2
```

The split matters. The PLC runs on a vendor runtime under Windows; everything
below it is Python and runs anywhere. That is also how a real virtual
commissioning rig is arranged, with the controller on its own runtime and the
plant model elsewhere.

## Moving the PLC side to Windows

The controller needs a vendor runtime and those are Windows only, so the project
splits: plant, state model and tests are developed and verified on Linux, the
controller runs on Windows. That is how a virtual commissioning rig is normally
arranged anyway. See `docs/WINDOWS_SETUP.md`, which is explicit about what is
ready to move and what is not.

## What exists

**`src/vpc/packml.py`** implements the PackML state model from ISA-TR88.00.02:
seventeen states, the acting and wait distinction, and the state complete
transition that belongs to the machine rather than to any operator.

It is in Python **first, deliberately.** PackML belongs in the PLC and will be
written in Structured Text, but that ST cannot be executed on the machine this
was developed on, and a seventeen state machine with a partial transition
function is exactly the kind of thing that looks correct and is not. So the
model is built and verified here, exhaustively, and the ST is then written to
match it. This module is the executable specification; the ST is an
implementation of it.

The tests enumerate rather than sample:

- every state is reachable from power on, by breadth first search over commands
  and state complete, not by walking paths the author had in mind
- **Execute is reachable from every state**, so the cell can never enter
  something it cannot productively leave
- **Abort is reachable from every state** that is not already going there, since
  a safety path one state cannot reach is not a safety path
- every one of the 17 x 9 state and command combinations either transitions or
  raises, so a command an operator believed was accepted is never silently
  swallowed
- Abort outranks Stop, checked because transposing them means a machine that
  politely decelerates when somebody hit the emergency button

**`src/vpc/process_image.py`** is the contract between the two halves: the
address map, held once, from which the PLC side declarations are generated. Two
copies of an address map is one copy plus a future defect.

It also carries the reason the whole thing is scan based rather than event
driven. A real PLC copies every input into a buffer, runs the program against
that frozen snapshot, then writes every output at once, so a signal that changes
twice in a scan is seen once and nothing the program writes takes effect until
the scan ends. Those are the semantics people get wrong moving from software to
control, and a callback based simulation would hide them.

**`src/vpc/cell.py`** is the plant: a conveyor with a filler, a capper and a QC
reject, advanced one scan at a time and never by a clock. Deterministic, so a
scenario replays exactly. The one part modelled carefully is the safety chain,
because it is the part with a wrong answer that hurts somebody: torque is the
safety channel's to give, not the program's to take, and closing a guard does
not by itself restore it.

**`plc/cell_control.st`** is the program under test, written to plain IEC 61131-3.
`tests/test_st_matches_the_model.py` parses it and checks its transition table
against the verified Python model, because the ST cannot be executed here. That
test earned itself immediately: the program declared its power on state as 4,
with a comment saying Aborted, and 4 is Idle. A controller powering up Idle is
one command away from running a machine nobody reset.

## Running it

```sh
uv run --group dev pytest -q
```

100% statement and branch coverage is gated, along with `ruff` and
`mypy --strict`.
