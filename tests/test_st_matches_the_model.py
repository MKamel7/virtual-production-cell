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

import pytest

from vpc.packml import ACTING, ON_COMPLETE, PACKTAGS_CODE, State

ST = Path(__file__).resolve().parents[1] / "plc" / "cell_control.st"
CODE_TO_STATE = {code: State(name) for name, code in PACKTAGS_CODE.items()}


def source() -> str:
    return ST.read_text()


def test_the_encoding_covers_every_state_exactly_once() -> None:
    assert set(PACKTAGS_CODE) == {s.value for s in State}
    assert len(set(PACKTAGS_CODE.values())) == len(PACKTAGS_CODE)
    assert sorted(PACKTAGS_CODE.values()) == list(range(1, 18))


@pytest.mark.verifies("SR-07")
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


@pytest.mark.verifies("SR-01")
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


# --- the safety chain, parsed rather than assumed -----------------------------
# These pin properties the ST cannot be executed to demonstrate here. Each one
# fixes a defect that was actually present: the safety request was derived from
# a command the state machine consumes first, and there was no link watchdog at
# all.
def test_the_safety_reset_request_is_not_derived_from_cmdreset() -> None:
    """CmdReset is consumed and cleared by the state machine in the same scan.

    The output block runs after that, so a request built from CmdReset could
    never assert from Stopped, only from states that happen to ignore it. The
    behaviour of a safety function must not depend on which CASE branch ran.
    """
    body = source()
    match = re.search(r"SAFETY_RESET_REQUEST\s*:=\s*(.+?);", body, re.S)
    assert match is not None, "the safety reset request is not assigned"
    expression = match.group(1)
    assert "CmdSafetyReset" in expression
    assert "CmdReset" not in expression.replace("CmdSafetyReset", ""), (
        "the safety reset request is still derived from CmdReset, which the "
        "state machine clears before this line runs"
    )


@pytest.mark.verifies("SR-06")
def test_a_link_watchdog_exists_and_takes_the_abort_path() -> None:
    """A cell that cannot tell a dead link from a quiet one is not industrial.

    Zeroing inputs on a channel error only covers failures the MASTER notices.
    A plant that hangs with its socket open is invisible to that, and
    SCAN_COUNT is already a heartbeat, so watching it costs nothing.
    """
    body = source()
    assert "LinkFault" in body, "there is no link watchdog"
    stall = body.index("IF SCAN_COUNT = LastScanCount THEN")
    assert stall < body.index("PackML state machine"), (
        "the link watchdog runs after the state machine, so a scan can act on "
        "input it already knows is stale"
    )
    fault = body.index("IF LinkFault THEN")
    assert re.search(r"CmdAbort\s*:=\s*TRUE;",
                     body[fault:body.index("END_IF;", fault)]), (
        "a link fault does not abort; a controller that cannot see the plant "
        "cannot know it is safe to stop politely"
    )


@pytest.mark.verifies("SR-07")
def test_the_link_starts_faulted() -> None:
    """Fail safe on power up, and the same reasoning as the Aborted power on
    state: a link that has never delivered a scan is not a healthy link, it is
    an unproven one."""
    body = source()
    assert re.search(r"LinkFault\s*:\s*BOOL\s*:=\s*TRUE;", body), (
        "LinkFault does not initialise TRUE, so the cell trusts a link that has "
        "never produced a heartbeat"
    )


def test_outputs_are_gated_on_the_link_as_well_as_on_torque() -> None:
    body = source()
    match = re.search(r"IF PMLState = 6 AND (.+?) THEN", body)
    assert match is not None
    assert "SAFETY_OK" in match.group(1)
    assert "NOT LinkFault" in match.group(1), (
        "actuators can run while the link is faulted"
    )


@pytest.mark.verifies("SR-08")
def test_every_command_is_cleared_at_the_end_of_the_scan() -> None:
    """One shot, so a command that arrives in a state which ignores it is
    discarded rather than latched.

    Found on the running cell: CmdReset and CmdStart both sat TRUE in Execute,
    written while the machine was in a state that ignored them. Nothing would
    ever have cleared them, so the next Stop would have walked the cell back to
    Idle with nobody asking. A machine that resumes unbidden is what the wait
    states exist to prevent.
    """
    body = source()
    tail = body[body.index("COMMANDS ARE ONE SHOT"):]

    declared = set(re.findall(r"^\s*(Cmd\w+)\s*:\s*BOOL;", body, re.M))
    assert declared, "no commands found in the declaration"

    for command in declared:
        assert re.search(rf"^{command}\s*:=\s*FALSE;", tail, re.M), (
            f"{command} is never cleared, so it latches and fires later"
        )


@pytest.mark.verifies("SR-08")
def test_the_clearing_block_is_the_last_thing_the_program_does() -> None:
    """Anything reading a command after it is cleared would read FALSE always.

    In particular SAFETY_RESET_REQUEST is built from CmdSafetyReset, and this
    is the ordering bug that already bit once when the request was derived from
    CmdReset.
    """
    body = source()
    assert body.index("SAFETY_RESET_REQUEST :=") < body.index("COMMANDS ARE ONE SHOT"), (
        "commands are cleared before the outputs that read them are computed"
    )


@pytest.mark.verifies("SR-09")
def test_every_state_except_execute_leaves_the_cell_still() -> None:
    """Stricter than PackML requires, and deliberately so.

    A machine that keeps doing things while it is Holding has a Held state that
    means nothing. The ELSE branch is the one that matters: it is what runs in
    all sixteen other states, including every acting state.
    """
    body = source()
    start = body.index("IF PMLState = 6 AND")
    otherwise = body.index("ELSE", start)
    block = body[otherwise:body.index("END_IF;", otherwise)]

    for output in ("CONVEYOR_RUN", "FILLER_DOSE", "CAPPER_ACTUATE", "REJECT_EJECT"):
        assert re.search(rf"{output}\s*:=\s*FALSE;", block), (
            f"{output} is not dropped outside Execute, so the cell can act in a "
            f"state that is supposed to be still"
        )


@pytest.mark.verifies("SR-10")
def test_a_guard_opening_aborts_once_on_the_edge() -> None:
    """Once, not every scan while it stays open.

    Re-triggering continuously would make the abort indistinguishable from a
    fault storm, and would stop the operator ever clearing it: every Clear would
    be overridden by the next scan's abort before it could take effect.
    """
    body = source()
    assert re.search(r"IF\s+GuardWasClosed\s+AND\s+NOT\s+GUARD_CLOSED\s+THEN", body), (
        "the guard abort is not edge triggered"
    )
    edge = body.index("IF GuardWasClosed AND NOT GUARD_CLOSED THEN")
    assert re.search(r"GuardWasClosed\s*:=\s*GUARD_CLOSED;", body[edge:]), (
        "GuardWasClosed is never updated, so the edge can never be seen again"
    )
