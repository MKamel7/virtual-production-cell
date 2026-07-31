"""Exhaustive verification of the PackML state model.

This file is the reason the state model exists in Python before it exists in
Structured Text. The ST cannot be executed on this machine, so an untested
implementation would be carried straight into the PLC, and a seventeen state
machine with a partial transition function is exactly the kind of thing that
looks right and is not.

So the properties below are checked over the WHOLE state and command space
rather than on a few paths somebody thought of. Where a test enumerates, it
enumerates everything.
"""

from __future__ import annotations

import itertools

import pytest

from vpc.packml import (
    ACTING,
    ON_COMMAND,
    ON_COMPLETE,
    WAIT,
    Command,
    IllegalCommand,
    PackML,
    State,
)


# --- the model's own shape ---------------------------------------------------
def test_every_state_is_either_acting_or_waiting_and_never_both() -> None:
    """The distinction drives everything else, so it is checked first."""
    assert set(State) == ACTING | WAIT
    assert not (ACTING & WAIT)
    assert len(State) == 17, "PackML defines seventeen states"


def test_every_acting_state_knows_where_it_goes_when_it_finishes() -> None:
    """An acting state without a completion target is a machine that hangs.

    This is the single most likely omission in a hand written implementation,
    because the acting states are the ones nobody exercises by hand.
    """
    missing = ACTING - set(ON_COMPLETE)
    assert not missing, f"acting states with no state complete transition: {missing}"


def test_no_wait_state_has_a_completion_transition() -> None:
    """A wait state exists to wait. If it can complete, it does not wait."""
    assert not (WAIT & set(ON_COMPLETE))


def test_every_command_transition_starts_somewhere_legal() -> None:
    for (origin, command), target in ON_COMMAND.items():
        assert origin in State and target in State
        assert origin != target, f"{command.value} in {origin.value} goes nowhere"


# --- reachability, which is where these models actually fail -----------------
def test_every_state_is_reachable_from_power_on() -> None:
    """A state nobody can reach is either dead code or a missing transition.

    Explored by breadth first search over commands AND state complete, from the
    power on state, rather than by walking the paths the author had in mind.
    """
    seen = {State.ABORTED}
    frontier = [State.ABORTED]
    while frontier:
        current = frontier.pop()
        for command in Command:
            machine = PackML(state=current)
            try:
                nxt = machine.send(command)
            except IllegalCommand:
                continue
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
        if current in ACTING:
            nxt = PackML(state=current).complete()
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)

    unreachable = set(State) - seen
    assert not unreachable, f"unreachable states: {unreachable}"


def test_the_machine_can_always_get_back_to_producing() -> None:
    """From every state there must be a path to Execute.

    A cell that can enter a state it cannot productively leave is a cell that
    needs a power cycle on the line, which is how people end up bypassing
    interlocks.
    """
    for start in State:
        seen = {start}
        frontier = [start]
        reached = False
        while frontier and not reached:
            current = frontier.pop()
            if current is State.EXECUTE:
                reached = True
                break
            for command in Command:
                try:
                    nxt = PackML(state=current).send(command)
                except IllegalCommand:
                    continue
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
            if current in ACTING:
                nxt = PackML(state=current).complete()
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        assert reached, f"Execute is not reachable from {start.value}"


# --- the emergency paths, which must work from everywhere --------------------
@pytest.mark.parametrize("start", list(State), ids=lambda s: s.value)
def test_abort_is_reachable_from_every_state(start: State) -> None:
    """A safety path that some state cannot reach is not a safety path.

    Aborting and Aborted are the exceptions, and only because the machine is
    already going where the command would send it.
    """
    machine = PackML(state=start)
    if start in {State.ABORTING, State.ABORTED}:
        with pytest.raises(IllegalCommand):
            machine.send(Command.ABORT)
        return
    assert machine.send(Command.ABORT) is State.ABORTING


@pytest.mark.parametrize("start", list(State), ids=lambda s: s.value)
def test_stop_is_reachable_from_every_state_that_is_not_already_going_there(
    start: State,
) -> None:
    machine = PackML(state=start)
    already = {State.STOPPING, State.STOPPED, State.ABORTING, State.ABORTED,
               State.CLEARING}
    if start in already:
        with pytest.raises(IllegalCommand):
            machine.send(Command.STOP)
        return
    assert machine.send(Command.STOP) is State.STOPPING


def test_abort_outranks_stop() -> None:
    """Both are legal in Execute, and abort must win if both are wired.

    Checked because the two are easy to transpose, and transposing them means a
    machine that politely decelerates when somebody hit the emergency button.
    """
    stopping = PackML(state=State.EXECUTE)
    stopping.send(Command.STOP)
    assert stopping.state is State.STOPPING
    assert stopping.send(Command.ABORT) is State.ABORTING


# --- commands must be refused where PackML does not permit them --------------
def test_every_illegal_command_is_refused_rather_than_ignored() -> None:
    """The whole state and command space, not a sample.

    A machine that silently swallows a command the operator believed it accepted
    is worse than one that refuses audibly, and on a line the operator is
    standing next to it.
    """
    legal = set(ON_COMMAND)
    for state, command in itertools.product(State, Command):
        machine = PackML(state=state)
        emergency = (
            (command is Command.ABORT and state not in {State.ABORTING, State.ABORTED})
            or (command is Command.STOP and state not in {
                State.STOPPING, State.STOPPED, State.ABORTING, State.ABORTED,
                State.CLEARING})
        )
        if (state, command) in legal or emergency:
            machine.send(command)          # must not raise
        else:
            with pytest.raises(IllegalCommand):
                machine.send(command)


def test_state_complete_is_refused_in_every_wait_state() -> None:
    for state in WAIT:
        with pytest.raises(IllegalCommand, match="wait state"):
            PackML(state=state).complete()


# --- the ordinary production cycle -------------------------------------------
def test_a_full_production_cycle_walks_the_expected_path() -> None:
    """Power on to product made and back to ready, with nothing skipped."""
    machine = PackML()
    assert machine.state is State.ABORTED

    machine.send(Command.CLEAR)
    assert machine.state is State.CLEARING
    machine.complete()
    assert machine.state is State.STOPPED

    machine.send(Command.RESET)
    machine.complete()
    assert machine.state is State.IDLE

    machine.send(Command.START)
    machine.complete()
    assert machine.state is State.EXECUTE

    machine.complete()                      # production finished
    assert machine.state is State.COMPLETING
    machine.complete()
    assert machine.state is State.COMPLETE

    machine.send(Command.RESET)
    machine.complete()
    assert machine.state is State.IDLE

    assert [step[1] for step in machine.history] == [
        "Clear", "SC", "Reset", "SC", "Start", "SC", "SC", "SC", "Reset", "SC",
    ]


def test_hold_and_unhold_return_to_execute() -> None:
    """A held machine resumes production rather than restarting the batch."""
    machine = PackML(state=State.EXECUTE)
    machine.send(Command.HOLD)
    machine.complete()
    assert machine.state is State.HELD
    machine.send(Command.UNHOLD)
    machine.complete()
    assert machine.state is State.EXECUTE


def test_suspend_and_unsuspend_return_to_execute() -> None:
    """Suspend is the starved or blocked case, and it also resumes.

    Held and Suspended differ by cause, not by shape: Held is commanded by a
    person, Suspended is caused by the process, typically upstream starvation or
    downstream blocking. Both must return to Execute.
    """
    machine = PackML(state=State.EXECUTE)
    machine.send(Command.SUSPEND)
    machine.complete()
    assert machine.state is State.SUSPENDED
    machine.send(Command.UNSUSPEND)
    machine.complete()
    assert machine.state is State.EXECUTE


def test_history_records_every_transition_with_its_trigger() -> None:
    """A PackML implementation is usually wrong in its paths, not its states."""
    machine = PackML(state=State.IDLE)
    machine.send(Command.START)
    machine.complete()
    assert machine.history == [
        (State.IDLE, "Start", State.STARTING),
        (State.STARTING, "SC", State.EXECUTE),
    ]


def test_acting_and_waiting_are_reported_correctly() -> None:
    assert PackML(state=State.EXECUTE).is_acting
    assert not PackML(state=State.EXECUTE).is_waiting
    assert PackML(state=State.IDLE).is_waiting
    assert not PackML(state=State.IDLE).is_acting
