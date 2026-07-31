# Moving the PLC side to Windows

Everything in this repository except the Structured Text runs anywhere. The ST
needs a runtime, and the runtimes worth using are Windows only, so the project is
split: the plant, the state model and the tests are developed and verified on
Linux, and the controller runs on Windows.

That split is not a workaround. It is how a virtual commissioning rig is
normally arranged, with the controller on its own runtime and the plant model
elsewhere.

## What is ready to move, and what is not

**Ready.** The PackML state model and its exhaustive verification, the plant
simulation with PLC scan semantics, the process image and address map, the
generated IO declarations, and `plc/cell_control.st` itself. All of it is
tested at 100% branch coverage on Linux and none of it depends on the platform.

**Not ready.** The Modbus TCP bridge that carries the process image between the
runtime and the plant. Until that exists the two halves cannot talk, so the
sequence below gets the toolchain installed and the program compiling, and the
bridge is the next thing built.

Being explicit about that because a setup guide that quietly assumes a missing
component wastes an afternoon before anybody notices.

## Choosing the runtime

Both realistic options are Windows only for authoring. The difference that
matters later:

| | CODESYS | TwinCAT 3 |
|---|---|---|
| Authoring | Windows only | Windows only |
| Runtime | Windows **and Linux** | Windows only, a real time extension to it |
| In industry | the engine under many vendors' tools, so broad | very strong in machine building, especially in Bavaria |
| Later option | the finished cell could run entirely on Linux | it could not |

**CODESYS keeps a door open that TwinCAT closes.** Because a Linux runtime
exists, the whole cell could eventually run on one machine where the plant
simulation already lives, which also means it could be run and debugged
together rather than across a dual boot.

Licence terms for both change, and the free tiers have real constraints on
runtime duration. **Check the current terms before committing time**, rather
than trusting a summary written months earlier.

## Before you start

You are dual booting, so the two halves cannot run at once on this machine. For
the first integration that is fine: the plant simulation is Python and runs on
Windows too, so everything sits on one side while you get it working. Splitting
across two machines is a refinement, not a requirement.

## Sequence

### 1. Get the repository onto Windows

```
git clone https://github.com/MKamel7/virtual-production-cell
```

It is not pushed yet, so until it is, copy the folder across or push it first.

### 2. Install Python and uv on Windows

The plant simulation, the tests and later the OPC UA server all need it.

```
winget install --id=astral-sh.uv -e
uv run --group dev pytest -q
```

If that passes on Windows, the whole verified half has moved intact. **Do this
before touching the PLC toolchain**, because it is the cheap check and it tells
you the environment is sane before anything harder fails.

### 3. Install the PLC toolchain

Whichever you chose. Install the IDE and whatever runtime it ships with, and
confirm you can create an empty project, download it and see it running before
introducing this program. A toolchain problem and a program problem look
identical when you meet them together.

### 4. Bring in the program

Two files, and they belong together:

- `plc/io_declarations.st` is **generated** from `src/vpc/process_image.py`. Do
  not hand edit it. If an address needs changing, change the enum and
  regenerate, because the whole point is that the address map exists once.
- `plc/cell_control.st` is the program.

To regenerate the declarations after any address change:

```
uv run python -c "from vpc.process_image import structured_text_declarations; print(structured_text_declarations())" > plc/io_declarations.st
uv run --group dev pytest -q tests/test_st_matches_the_model.py
```

That test fails if the file on disk has drifted from the address map, which is
the cheapest possible protection against the two halves disagreeing.

### 5. Expect the ST to need small edits, and keep them portable

The program is written to plain IEC 61131-3 with no vendor extensions, but
runtimes differ in practice. Things likely to need attention:

- **Direct addressing syntax.** `%QX0.0` and `%IW0` are standard, but how a
  runtime maps them to a Modbus slave is vendor specific and is configured in
  the IDE, not in the code.
- **`ACTING_SCANS` is declared in `VAR`.** Some toolchains want constants in
  `VAR CONSTANT`. Moving it is fine.
- **Program versus function block.** Some runtimes expect the main program in a
  specific POU type or a specific task assignment.

If a change is vendor specific, keep it out of the shared file or note it, so
the program stays portable. The value of it being plain ST disappears the first
time a vendor extension is committed without comment.

### 6. Verify the state machine before wiring any IO

The most useful early check needs no plant at all. Force the command variables
from the IDE's watch window and confirm the state numbers follow the model:

| From | Force | Expect |
|---|---|---|
| 9 Aborted | `CmdClear` | 1 Clearing, then 2 Stopped |
| 2 Stopped | `CmdReset` | 15 Resetting, then 4 Idle |
| 4 Idle | `CmdStart` | 3 Starting, then 6 Execute |
| 6 Execute | `CmdHold` | 10 Holding, then 11 Held |
| 11 Held | `CmdUnhold` | 12 Unholding, then 6 Execute |
| anywhere | `CmdAbort` | 8 Aborting, then 9 Aborted |

The acting states pass through after `ACTING_SCANS` scans, so at a fast task
rate they will look instant. If a transition goes somewhere not in this table,
the ST has drifted from `src/vpc/packml.py`, and that module is the authority.

**The power on state must be 9, Aborted.** If the controller comes up at 4,
Idle, it is one command away from running a machine nobody has reset. That exact
defect was in this program and was caught by
`tests/test_st_matches_the_model.py`.

### 7. Safety behaviour, which is worth forcing by hand

Two properties matter more than the sequencing and are quick to check:

- Force `GUARD_CLOSED` low. The program must abort **once**, on the edge, not
  repeatedly while the guard stays open.
- Force `SAFETY_OK` low. Every actuator output must drop immediately regardless
  of state, and must **not** come back when `SAFETY_OK` returns until a reset
  has been commanded. A machine that restarts the moment a door shuts is a
  machine that restarts while somebody is still inside it.

## What comes after this

1. The Modbus TCP bridge, so the runtime and the plant exchange the process image
2. Scenario runs: bottleneck, station failure, changeover, with OEE per scenario
3. The OPC UA server, with certificates and sign and encrypt rather than
   security `None`
4. The safety channel proper, carrying the guard and reset over the PROFIsafe
   framing already built and tested in the fault injection harness

## If something does not work

The useful thing to send back is the same as always: what you ran, what you
expected, and what actually happened, with the state number and the IO values at
the moment it went wrong. The state machine is deterministic, so any disagreement
with the table above is reproducible and therefore findable.
