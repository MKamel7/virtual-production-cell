"""The PackML state model, per ISA-TR88.00.02 (OMAC).

This exists in Python for a specific reason, and it is not that the machine
control belongs here. It does not: PackML is the state model a PLC runs, and
this one will be written in Structured Text.

It is here first because **the Structured Text cannot be executed on this
machine**, and an untested state machine is where this kind of project quietly
goes wrong. Seventeen states with a partial transition function is exactly the
shape of thing that looks right and is not. So the model is built and tested
exhaustively here, and the ST is then written to match it. This module is the
executable specification, and the ST is the implementation of it.

That ordering also gives the eventual port something to be checked against,
which is worth more than either half alone.

WHY PACKML AT ALL. It is the state model for packaging machinery and the
vocabulary the industry actually uses, so a cell that implements it is legible
to anybody from that world without explanation. Krones, twenty minutes from
Regensburg, is a packaging company.

The model has three kinds of state:

  ACTING states are transient. The machine is doing something, and it leaves the
  state on its own when the work finishes. That completion is the "state
  complete" transition, SC below, and it is NOT a command: nobody sends it.

  WAIT states are steady. The machine sits there until an operator or a
  supervisor sends a command.

  DUAL state, Execute, is where production actually happens.

Getting that distinction right matters, because the commonest way to implement
PackML wrongly is to let a command drive a transition that only SC may drive, or
to let SC fire from a wait state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    """The seventeen PackML states."""

    # wait states: the machine rests here until commanded
    IDLE = "Idle"
    COMPLETE = "Complete"
    HELD = "Held"
    SUSPENDED = "Suspended"
    STOPPED = "Stopped"
    ABORTED = "Aborted"

    # acting states: transient, left on state complete
    STARTING = "Starting"
    COMPLETING = "Completing"
    RESETTING = "Resetting"
    HOLDING = "Holding"
    UNHOLDING = "Unholding"
    SUSPENDING = "Suspending"
    UNSUSPENDING = "Unsuspending"
    ABORTING = "Aborting"
    CLEARING = "Clearing"
    STOPPING = "Stopping"

    # dual: acting, and where product is made
    EXECUTE = "Execute"


#: The numeric encoding PackTags uses on the wire, which is what the PLC holds
#: and what OPC UA will publish. Kept here so the Structured Text can be checked
#: against this model rather than trusted.
PACKTAGS_CODE: dict[str, int] = {
    "Clearing": 1, "Stopped": 2, "Starting": 3, "Idle": 4, "Suspended": 5,
    "Execute": 6, "Stopping": 7, "Aborting": 8, "Aborted": 9, "Holding": 10,
    "Held": 11, "Unholding": 12, "Suspending": 13, "Unsuspending": 14,
    "Resetting": 15, "Completing": 16, "Complete": 17,
}


class Command(str, Enum):
    """The commands an operator or supervisor may send.

    Note what is absent: there is no "state complete" command. Completion of an
    acting state is something the machine reports, not something anybody asks
    for, and modelling it as a command is a common way to get PackML wrong.
    """

    RESET = "Reset"
    START = "Start"
    STOP = "Stop"
    HOLD = "Hold"
    UNHOLD = "Unhold"
    SUSPEND = "Suspend"
    UNSUSPEND = "Unsuspend"
    ABORT = "Abort"
    CLEAR = "Clear"


#: States the machine leaves by itself, once its work is done.
ACTING: frozenset[State] = frozenset({
    State.STARTING, State.COMPLETING, State.RESETTING, State.HOLDING,
    State.UNHOLDING, State.SUSPENDING, State.UNSUSPENDING, State.ABORTING,
    State.CLEARING, State.STOPPING, State.EXECUTE,
})

#: States the machine rests in until commanded.
WAIT: frozenset[State] = frozenset({
    State.IDLE, State.COMPLETE, State.HELD, State.SUSPENDED, State.STOPPED,
    State.ABORTED,
})

#: Where an acting state goes when its work completes. This is the SC transition
#: and it is the machine's own, never a command.
ON_COMPLETE: dict[State, State] = {
    State.RESETTING: State.IDLE,
    State.STARTING: State.EXECUTE,
    State.EXECUTE: State.COMPLETING,
    State.COMPLETING: State.COMPLETE,
    State.HOLDING: State.HELD,
    State.UNHOLDING: State.EXECUTE,
    State.SUSPENDING: State.SUSPENDED,
    State.UNSUSPENDING: State.EXECUTE,
    State.STOPPING: State.STOPPED,
    State.ABORTING: State.ABORTED,
    State.CLEARING: State.STOPPED,
}

#: Command transitions, keyed by the state they are legal in.
#:
#: ABORT and STOP are deliberately absent here and handled separately, because
#: they are legal from almost everywhere and listing them per state would invite
#: exactly the omission that makes an emergency path unreachable from one
#: forgotten state.
ON_COMMAND: dict[tuple[State, Command], State] = {
    (State.STOPPED, Command.RESET): State.RESETTING,
    (State.COMPLETE, Command.RESET): State.RESETTING,
    (State.IDLE, Command.START): State.STARTING,
    (State.EXECUTE, Command.HOLD): State.HOLDING,
    (State.HELD, Command.UNHOLD): State.UNHOLDING,
    (State.EXECUTE, Command.SUSPEND): State.SUSPENDING,
    (State.SUSPENDED, Command.UNSUSPEND): State.UNSUSPENDING,
    (State.ABORTED, Command.CLEAR): State.CLEARING,
}

#: ABORT is legal everywhere except where the machine is already going there.
#: A safety path that cannot be reached from some state is not a safety path.
_ABORT_EXEMPT: frozenset[State] = frozenset({State.ABORTING, State.ABORTED})

#: STOP is legal everywhere except where the machine is already stopping, or
#: where an abort outranks it.
_STOP_EXEMPT: frozenset[State] = frozenset({
    State.STOPPING, State.STOPPED, State.ABORTING, State.ABORTED,
    State.CLEARING,
})


class IllegalCommand(ValueError):
    """A command that PackML does not permit in the current state.

    Raised rather than ignored. A machine that silently swallows a command an
    operator believed it accepted is worse than one that refuses audibly, and on
    a real line the operator is standing next to it.
    """


@dataclass
class PackML:
    """The state machine. Deterministic, and with no notion of time.

    Time is deliberately absent. How long an acting state takes is a property of
    the equipment, not of the state model, so this object is advanced by
    `complete()` when the caller decides the work is done. That keeps the model
    exhaustively testable and keeps the equipment's timing where it belongs.
    """

    state: State = State.ABORTED
    #: Every transition taken, as (from, trigger, to). Kept because a PackML
    #: implementation is usually wrong in its PATHS rather than its states, and
    #: a path is only reviewable if it was recorded.
    history: list[tuple[State, str, State]] = field(default_factory=list)

    @property
    def is_acting(self) -> bool:
        return self.state in ACTING

    @property
    def is_waiting(self) -> bool:
        return self.state in WAIT

    def send(self, command: Command) -> State:
        """Apply an operator command, or raise IllegalCommand."""
        if command is Command.ABORT and self.state not in _ABORT_EXEMPT:
            return self._go(self.state, command.value, State.ABORTING)
        if command is Command.STOP and self.state not in _STOP_EXEMPT:
            return self._go(self.state, command.value, State.STOPPING)

        target = ON_COMMAND.get((self.state, command))
        if target is None:
            raise IllegalCommand(
                f"{command.value} is not permitted in {self.state.value}")
        return self._go(self.state, command.value, target)

    def complete(self) -> State:
        """Report that the current acting state has finished its work.

        Refused in a wait state, because there is no work in progress to
        complete and accepting it would let the machine walk itself out of a
        state that exists precisely to wait for a human.
        """
        target = ON_COMPLETE.get(self.state)
        if target is None:
            raise IllegalCommand(
                f"{self.state.value} is a wait state and has nothing to complete")
        return self._go(self.state, "SC", target)

    def _go(self, previous: State, trigger: str, target: State) -> State:
        self.state = target
        self.history.append((previous, trigger, target))
        return target
