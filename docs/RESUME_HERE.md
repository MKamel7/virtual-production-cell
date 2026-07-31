# Resume here

Written on 31 July 2026, at the point where work moved from Linux to Windows.
If you are reading this after a reboot, this is the handover.

## Where everything lives

| Repo | Visibility | State |
|---|---|---|
| [virtual-production-cell](https://github.com/MKamel7/virtual-production-cell) | **private** | P4, this repo, 132 tests |
| [fault-injection-harness](https://github.com/MKamel7/fault-injection-harness) | public | P2, 27 faults, independently reviewed |
| [embedded-test-automation](https://github.com/MKamel7/embedded-test-automation) | public | P1, v3.1, 80 tests |

All four repos including the vault were committed and pushed before the reboot.
Nothing was left uncommitted anywhere.

## First thing to run on Windows

```
git clone https://github.com/MKamel7/virtual-production-cell
cd virtual-production-cell
uv run --group dev pytest -q
```

Expect **105 passed**, 100% branch coverage.

Do this **before installing any PLC toolchain.** It is the cheap check, and it
tells you the environment is sane before something harder fails. A toolchain
problem and a program problem look identical when you meet them together.

If `uv` is missing: `winget install --id=astral-sh.uv -e`

The repo is private, so you will need to be signed in to `gh` or use a token on
that machine.

## Then follow WINDOWS_SETUP.md

It has the full sequence, including a table of state transitions you can force
from the IDE watch window to verify the control program **before wiring any IO**.
That check needs no plant and no network, and it is the most useful early
confidence you can get.

## Two decisions still open

**CODESYS or TwinCAT.** Both are Windows only for authoring. CODESYS has a Linux
runtime and TwinCAT does not, so CODESYS keeps open the option of eventually
running the whole cell on one machine where the plant already lives. **Check the
current licence terms yourself** rather than trusting any summary, since the free
tiers have real constraints on runtime duration and they change.

**Whether your reviewer does a second pass.** Their first review found four high
severity defects, including the one that mattered most in P2: the overload
channel had no current sensor, so it read plant truth while the temperature
channels could be made to lie, which made the headline diversity result partly
self-fulfilling. All findings are closed and recorded in
`fault-injection-harness/docs/REVIEW.md`.

## Built since this note was written (31 July 2026, on the Windows side)

**The socket server and the scan loop exist.** `uv run python -m vpc.server`
binds port 502 and scans the plant every 50 ms, so the PLC and the plant can now
exchange IO and `WINDOWS_SETUP.md` step 8 is a real integration. Single threaded
on a selector, so a coil write cannot land in the middle of a scan; verified
against pymodbus as well as its own tests.

## What is NOT built yet, so you do not go looking for it

Scenario runs with OEE, OPC UA with real certificates rather than security
`None`, and the safety channel carrying guard and reset over the PROFIsafe
framing already built and tested in P2. None of them block bringing the control
program up on a runtime.

## Two things to keep straight when you come back

**`plc/io_declarations.st` is generated.** Never hand edit it. If an address
changes, change the enum in `src/vpc/process_image.py` and regenerate:

```
uv run python -c "from vpc.process_image import structured_text_declarations; print(structured_text_declarations())" > plc/io_declarations.st
uv run --group dev pytest -q tests/test_st_matches_the_model.py
```

The whole point is that the address map exists once. Two copies of an address map
is one copy plus a future defect.

**`src/vpc/packml.py` is the authority, not the Structured Text.** The Python
model is verified exhaustively and the ST implements it. If the two disagree, the
ST is wrong. `tests/test_st_matches_the_model.py` parses the ST and checks it,
and that test earned itself on its first run: the program declared its power on
state as `4` with a comment saying Aborted, and 4 is Idle. A controller powering
up Idle is one command away from running a machine nobody has reset.

## The constraint that shaped all of this

You dual boot, and the PLC runtimes worth using are Windows only to author. So
the project is split: the plant, the state model, the Modbus layer and the tests
are built and **executed** on Linux, and only the Structured Text crosses.

That is not a workaround. It is how a virtual commissioning rig is normally
arranged, with the controller on its own runtime and the plant model elsewhere.
It is also why the Modbus layer was implemented rather than imported, and why the
PackML model exists in Python first: anything that cannot be executed has to be
checked some other way, and the cheapest other way is to keep the untestable
surface small and put a parser on it.
