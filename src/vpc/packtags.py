"""PackTags: the data interface PackML actually specifies.

ISA-TR88.00.02 is two things and the state model is only the first. The second
is a NAMED TAG STRUCTURE, so that a line built from six vendors' machines can be
supervised by one system without a translation layer per machine. A project that
implements the seventeen states and invents its own tag names has done the
interesting half and skipped the useful one.

The structure is three groups and the split is about ownership, not tidiness:

  Command   written BY the supervisor, read by the machine
  Status    written BY the machine, read by the supervisor
  Admin     written by the machine, for reporting rather than control

Getting that direction wrong is the classic PackTags mistake, and it shows up as
a supervisor writing to Status because it seemed convenient, at which point two
things own one value and the machine's own view of itself can be overwritten
from outside.

WHAT IS IMPLEMENTED, and this is a subset stated as a subset. The standard
defines far more, including remote interfaces for machine to machine
interlocking, per-mode accumulated timers and a full alarm history. What is here
is the part this cell can populate honestly. Naming a tag the machine cannot
fill would be worse than leaving it out, because a supervisor would read zero
and believe it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum

from vpc.packml import PACKTAGS_CODE, WAIT, State


class UnitMode(IntEnum):
    """The three base modes PackML defines.

    A mode is not a state. The state model describes where the machine is in its
    cycle; the mode describes which cycle it is running at all, and each mode has
    its own permitted state set. Conflating them gives a machine that can be put
    into Manual while it is producing.
    """

    PRODUCING = 1
    MAINTENANCE = 2
    MANUAL = 3


#: States each mode permits. Producing is the full model. Maintenance and Manual
#: drop the states that only mean something during a production batch: you
#: cannot Suspend a machine that is not running product, and Complete describes
#: a batch finishing.
#:
#: The standard leaves the exact per-mode sets to the implementer, so this is a
#: choice rather than a quotation, and it is written down here where a reviewer
#: can disagree with it rather than left implicit in the code.
MODE_STATES: dict[UnitMode, frozenset[State]] = {
    UnitMode.PRODUCING: frozenset(State),
    UnitMode.MAINTENANCE: frozenset(State) - {
        State.SUSPENDED, State.SUSPENDING, State.UNSUSPENDING,
        State.COMPLETE, State.COMPLETING},
    UnitMode.MANUAL: frozenset(State) - {
        State.SUSPENDED, State.SUSPENDING, State.UNSUSPENDING,
        State.COMPLETE, State.COMPLETING},
}


class StopReason(IntEnum):
    """Why the machine is not producing.

    A machine that has stopped and cannot say why is a machine somebody has to
    stand in front of to diagnose. This is the smallest thing that turns a stop
    into a diagnosis, and it is what an OEE availability loss needs attributing
    to something.
    """

    NONE = 0
    OPERATOR = 1
    GUARD_OPEN = 2
    SAFETY_CHANNEL = 3
    LINK_LOST = 4
    STARVED = 5
    BLOCKED = 6


class ModeChangeRefused(ValueError):
    """A mode change the standard does not permit here.

    Raised rather than ignored, for the same reason an illegal command is: a
    supervisor that believed the machine changed mode and is wrong will make
    every subsequent decision on that belief.
    """


@dataclass
class Command:
    """Written by the supervisor. The machine reads these and never writes them."""

    unit_mode_change_request: bool = False
    unit_mode_requested: UnitMode = UnitMode.PRODUCING
    #: Requested throughput as a percentage of design speed. Present because
    #: PackTags defines it and a supervisor expects it; this cell runs at one
    #: speed, so `Status.mach_speed` reports design speed and this is recorded
    #: rather than obeyed. Said plainly instead of pretending to a rate control
    #: the plant does not have.
    mach_speed: int = 100


@dataclass
class Status:
    """Written by the machine. The supervisor reads these and never writes them."""

    state_current: State = State.ABORTED
    unit_mode_current: UnitMode = UnitMode.PRODUCING
    stop_reason: StopReason = StopReason.NONE
    #: True when the machine is ready to be commanded, which is not the same as
    #: running: a machine can be perfectly ready and stopped.
    sys_ready: bool = False
    #: The two loss conditions PackTags names separately, because they point
    #: upstream and downstream and a supervisor needs to know which.
    equipment_blocked: bool = False
    equipment_starved: bool = False
    mach_speed: int = 100
    cur_mach_speed: int = 0

    @property
    def state_code(self) -> int:
        """The numeric encoding, which is what actually goes on the wire."""
        return PACKTAGS_CODE[self.state_current.value]


@dataclass
class Admin:
    """Counters and history. Reporting rather than control."""

    prod_processed_count: int = 0
    prod_defective_count: int = 0
    acc_time_since_reset: int = 0
    #: Scans spent in each state. This is where an availability figure comes
    #: from on a real line, rather than from somebody timing it.
    states_duration_acc: dict[State, int] = field(default_factory=dict)

    def record(self, state: State) -> None:
        self.acc_time_since_reset += 1
        self.states_duration_acc[state] = self.states_duration_acc.get(state, 0) + 1


@dataclass
class PackTags:
    """The three groups together, plus the mode rule that governs changes."""

    command: Command = field(default_factory=Command)
    status: Status = field(default_factory=Status)
    admin: Admin = field(default_factory=Admin)

    def request_mode(self, mode: UnitMode, state: State) -> UnitMode:
        """Change mode, if the current state permits it.

        **Only from a wait state.** A machine in the middle of Starting or
        Aborting is doing something, and changing the rules underneath it means
        the acting state completes into a state its new mode may not even have.
        The standard restricts mode changes for exactly this reason, and it
        falls out neatly here: a wait state is one where the machine is already
        waiting for a person, which is who is asking.
        """
        if state not in WAIT:
            raise ModeChangeRefused(
                f"cannot change mode in {state.value}, which is an acting state")
        if state not in MODE_STATES[mode]:
            raise ModeChangeRefused(
                f"{mode.name} has no {state.value} state to change into")
        self.status.unit_mode_current = mode
        self.command.unit_mode_change_request = False
        return mode

    def update(self, state: State, stop_reason: StopReason, *,
               produced: int, rejected: int, blocked: bool, starved: bool,
               speed: int) -> None:
        """Publish one scan's worth of machine state.

        Everything written here is Status or Admin, never Command, because the
        machine does not get to decide what it was asked for.
        """
        self.status.state_current = state
        self.status.stop_reason = stop_reason
        self.status.sys_ready = state in WAIT and stop_reason is StopReason.NONE
        self.status.equipment_blocked = blocked
        self.status.equipment_starved = starved
        self.status.cur_mach_speed = speed
        self.admin.prod_processed_count = produced + rejected
        self.admin.prod_defective_count = rejected
        self.admin.record(state)


class TagGroup(str, Enum):
    COMMAND = "Command"
    STATUS = "Status"
    ADMIN = "Admin"


def browse_names() -> dict[TagGroup, tuple[str, ...]]:
    """The tag names, for anything that has to expose them by name.

    Kept here rather than in the OPC UA server so the names have one home. An
    OPC UA address space and a PackTags structure that disagree about a name is
    the same defect as two copies of an address map, and it produces a
    supervisor that connects successfully and reads nothing.
    """
    return {
        TagGroup.COMMAND: ("UnitModeChangeRequest", "UnitModeRequested",
                           "MachSpeed"),
        TagGroup.STATUS: ("StateCurrent", "UnitModeCurrent", "StopReason",
                          "SysReady", "EquipmentBlocked", "EquipmentStarved",
                          "MachSpeed", "CurMachSpeed"),
        TagGroup.ADMIN: ("ProdProcessedCount", "ProdDefectiveCount",
                         "AccTimeSinceReset"),
    }
