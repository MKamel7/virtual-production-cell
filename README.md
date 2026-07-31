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

## Running it

```sh
uv run --group dev pytest -q
```

100% statement and branch coverage is gated, along with `ruff` and
`mypy --strict`.
