"""The Structured Text must agree with the model it was written from.

This is the test that makes "executable specification" a claim rather than a
comment in a docstring. The ST cannot be executed here, so the next best thing
is to parse it and check that its transition table is the one `vpc.packml`
defines and verifies exhaustively.

It caught a real defect the first time it ran: the ST declared its power on
state as 4 with a comment saying Aborted, and 4 is Idle. A PLC starting in Idle
rather than Aborted would come up ready to start on a machine nobody had reset,
which is the exact failure the Aborted power on state exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

from vpc.packml import ACTING, ON_COMPLETE, PACKTAGS_CODE, State

ST = Path(__file__).resolve().parents[1] / "plc" / "cell_control.st"
CODE_TO_STATE = {code: State(name) for name, code in PACKTAGS_CODE.items()}


def source() -> str:
    return ST.read_text()


def test_the_encoding_covers_every_state_exactly_once() -> None:
    assert set(PACKTAGS_CODE) == {s.value for s in State}
    assert len(set(PACKTAGS_CODE.values())) == len(PACKTAGS_CODE)
    assert sorted(PACKTAGS_CODE.values()) == list(range(1, 18))


def test_the_plc_powers_on_in_aborted() -> None:
    """Not Idle, and the difference matters.

    A controller that powers up Idle is one command away from running a machine
    nobody has reset or inspected. Aborted forces a deliberate Clear and Reset
    first, which is the point of it being the power on state.
    """
    match = re.search(r"PMLState\s*:\s*INT\s*:=\s*(\d+)", source())
    assert match is not None, "cannot find the power on state in the ST"
    assert CODE_TO_STATE[int(match.group(1))] is State.ABORTED


def test_every_state_complete_transition_matches_the_model() -> None:
    """The acting state transitions, parsed out of the ST and compared.

    This is the table most likely to be mistyped, because it is eleven numeric
    pairs written by hand, and a wrong one produces a machine that goes
    somewhere plausible instead of somewhere correct.
    """
    body = source()
    start = body.index("IF ActingTimer >= ACTING_SCANS THEN")
    block = body[start:body.index("END_CASE;", start)]

    found: dict[State, State] = {}
    for origin, target in re.findall(r"^\s*(\d+):\s*PMLState\s*:=\s*(\d+);",
                                     block, re.M):
        found[CODE_TO_STATE[int(origin)]] = CODE_TO_STATE[int(target)]

    expected = {s: t for s, t in ON_COMPLETE.items() if s is not State.EXECUTE}
    assert found == expected, (
        f"the ST state complete table disagrees with vpc.packml.\n"
        f"only in ST: {set(found.items()) - set(expected.items())}\n"
        f"only in model: {set(expected.items()) - set(found.items())}"
    )


def test_execute_is_not_completed_by_a_timer() -> None:
    """Execute finishes when the BATCH does, not after a fixed number of scans.

    The model has Execute -> Completing, but that transition is driven by
    production being finished. Wiring it to the acting timer would make the cell
    stop producing after a couple of scans for no reason anybody could see.
    """
    body = source()
    start = body.index("IF ActingTimer >= ACTING_SCANS THEN")
    block = body[start:body.index("END_CASE;", start)]
    execute = PACKTAGS_CODE["Execute"]
    assert not re.search(rf"^\s*{execute}:\s*PMLState", block, re.M), (
        "Execute is in the acting timer table, so production would end on a "
        "timer rather than when the batch completes"
    )


def test_the_acting_case_labels_are_exactly_the_acting_states() -> None:
    """A wait state listed as acting would walk itself out of waiting."""
    body = source()
    match = re.search(r"^\s*((?:\d+,\s*)+\d+):\s*$", body, re.M)
    assert match is not None, "cannot find the acting state CASE labels"
    labels = {CODE_TO_STATE[int(n)] for n in match.group(1).replace(" ", "").split(",")}
    assert labels == ACTING - {State.EXECUTE}, (
        f"acting labels in the ST do not match the model: "
        f"{labels ^ (ACTING - {State.EXECUTE})}"
    )


def test_abort_is_checked_before_stop() -> None:
    """Order is the whole point: transposing them decelerates on an e-stop."""
    body = source()
    assert body.index("IF CmdAbort") < body.index("ELSIF CmdStop"), (
        "Stop is evaluated before Abort, so a machine would stop politely when "
        "somebody hit the emergency button"
    )


def test_outputs_are_dropped_whenever_torque_is_withheld() -> None:
    """Unconditionally, and before the state machine runs.

    Leaving an actuator asserted while torque is restored is how a machine
    starts mid-cycle with somebody still inside it.
    """
    body = source()
    guard = body.index("IF NOT SAFETY_OK THEN")
    machine = body.index("PackML state machine")
    assert guard < machine, "the safety block runs after the state machine"
    for output in ("CONVEYOR_RUN", "FILLER_DOSE", "CAPPER_ACTUATE", "REJECT_EJECT"):
        assert re.search(rf"{output}\s*:=\s*FALSE;",
                         body[guard:body.index("END_IF;", guard)]), (
            f"{output} is not dropped when torque is withheld"
        )


def test_the_io_declarations_are_the_generated_ones() -> None:
    """Two copies of an address map is one copy plus a future defect."""
    from vpc.process_image import structured_text_declarations

    generated = structured_text_declarations()
    on_disk = (ST.parent / "io_declarations.st").read_text()
    assert on_disk.strip() == generated.strip(), (
        "plc/io_declarations.st has drifted from the address map; regenerate it"
    )
