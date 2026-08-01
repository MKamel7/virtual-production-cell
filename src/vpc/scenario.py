"""Scenario runs, and the OEE that comes out of them.

A cell that runs proves it works. A cell that runs a bottleneck, a guard
interruption and a starved infeed, and reports what each cost, is an
engineering result. This module is the second thing.

THE CONTROLLER PROBLEM, and it is worth being explicit because the obvious
solution is the wrong one. Scenarios have to run in CI, and CI has no PLC, so
something has to drive the plant. Writing a second control policy in Python
would give this project exactly the defect it refuses everywhere else: two
implementations of one thing, one verified and one executing.

So `ReferenceController` is not a second controller. It is the executable
specification of the policy `plc/cell_control.st` implements, in the same
relationship the PackML model already has with the state machine, and
`tests/test_st_matches_the_model.py` parses the ST and checks its four output
expressions against the ones written here. If they diverge the build fails.
The ST remains the thing that runs on a real machine.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from vpc.cell import CAP_SCANS, FILL_SCANS, Cell
from vpc.packml import Command, IllegalCommand, PackML, State
from vpc.process_image import Coil, Discrete, InputRegister, ProcessImage

#: Scans an acting state takes, matching ACTING_SCANS in the ST.
ACTING_SCANS = 2

#: The output policy, as expressions over the inputs. Written once, here,
#: because the ST is checked against these strings. Changing a rule means
#: changing it in one place and watching the parse test fail until the ST
#: agrees.
OUTPUT_POLICY: dict[str, str] = {
    "CONVEYOR_RUN": "NOT FILLER_BUSY AND NOT CAPPER_BUSY",
    "FILLER_DOSE": "PRODUCT_AT_FILLER AND FILLER_BUSY",
    "CAPPER_ACTUATE": "PRODUCT_AT_CAPPER AND CAPPER_BUSY",
    "REJECT_EJECT": "PRODUCT_AT_QC AND QC_FAIL",
}


#: What an operator presses in each state that is waiting for them, to get the
#: cell back to Execute. TOTAL over the wait states rather than a lookup with a
#: fallback: every state that waits is waiting FOR something, so a missing entry
#: is a state where the operator has no way forward, and a `.get` returning None
#: would hide that as a cell that quietly sits still. `test_scenario.py` pins
#: that this covers exactly the wait set, so adding a state fails the build.
TOWARDS_EXECUTE: dict[State, Command] = {
    State.ABORTED: Command.CLEAR,
    State.STOPPED: Command.RESET,
    State.IDLE: Command.START,
    State.HELD: Command.UNHOLD,
    State.SUSPENDED: Command.UNSUSPEND,
    State.COMPLETE: Command.RESET,
}


@dataclass
class ReferenceController:
    """The control policy, in the order `cell_control.st` runs it.

    Safety first and unconditionally, then the state machine, then outputs.
    Commands are one shot: anything not consumed in the scan it arrives is
    discarded rather than latched.
    """

    machine: PackML = field(default_factory=PackML)
    acting_timer: int = 0
    guard_was_closed: bool = True
    #: Set by an operator deciding the cell should be producing. Drives one
    #: command per scan towards Execute, which is what a person at an HMI does.
    wants_to_run: bool = False

    def scan(self, inputs: ProcessImage) -> ProcessImage:
        """One controller cycle: read the frozen inputs, write the outputs."""
        outputs = inputs.copy()
        safety_ok = inputs.discretes[Discrete.SAFETY_OK]
        guard_closed = inputs.discretes[Discrete.GUARD_CLOSED]

        # --- safety first, and nothing below can override the result
        if self.guard_was_closed and not guard_closed:
            self._abort()
        self.guard_was_closed = guard_closed

        # --- the state machine, advanced one step per scan
        self._advance_state(safety_ok)

        # --- outputs, only in Execute and only with torque
        running = self.machine.state is State.EXECUTE and safety_ok
        outputs.coils[Coil.CONVEYOR_RUN] = running and not (
            inputs.discretes[Discrete.FILLER_BUSY]
            or inputs.discretes[Discrete.CAPPER_BUSY])
        outputs.coils[Coil.FILLER_DOSE] = running and (
            inputs.discretes[Discrete.PRODUCT_AT_FILLER]
            and inputs.discretes[Discrete.FILLER_BUSY])
        outputs.coils[Coil.CAPPER_ACTUATE] = running and (
            inputs.discretes[Discrete.PRODUCT_AT_CAPPER]
            and inputs.discretes[Discrete.CAPPER_BUSY])
        outputs.coils[Coil.REJECT_EJECT] = running and (
            inputs.discretes[Discrete.PRODUCT_AT_QC]
            and inputs.discretes[Discrete.QC_FAIL])

        # --- the reset request is an ASK. The safety channel decides.
        outputs.coils[Coil.SAFETY_RESET_REQUEST] = (
            self.wants_to_run and guard_closed and not safety_ok)
        return outputs

    def _abort(self) -> None:
        # Already aborting or aborted is not an error, it is the desired end
        # state arriving early, so the refusal is suppressed rather than
        # handled. The state machine raises rather than ignoring illegal
        # commands everywhere else, and that is right: this is the one caller
        # that genuinely does not care.
        with contextlib.suppress(IllegalCommand):
            self.machine.send(Command.ABORT)
        self.acting_timer = 0

    def _advance_state(self, safety_ok: bool) -> None:
        # Execute is an acting state in the model and is deliberately NOT
        # driven by the timer: it completes when the BATCH does, not after a
        # fixed number of scans. Wiring it to the timer makes the cell stop
        # producing every couple of scans for no reason anybody can see, which
        # is exactly what it did before this line existed. The ST excludes it
        # from the timer table for the same reason, and a test pins that.
        if self.machine.is_acting and self.machine.state is not State.EXECUTE:
            self.acting_timer += 1
            if self.acting_timer >= ACTING_SCANS:
                self.acting_timer = 0
                self.machine.complete()
            return
        if self.machine.state is State.EXECUTE:
            return

        if not self.wants_to_run or not safety_ok:
            return

        # One command per scan towards Execute, which is what an operator at an
        # HMI actually does: press the thing the machine is currently waiting
        # for, and wait for it to get there.
        self.machine.send(TOWARDS_EXECUTE[self.machine.state])
        self.acting_timer = 0


class Event(str, Enum):
    """Things that happen to the cell, rather than things it decides."""

    OPEN_GUARD = "guard opened"
    CLOSE_GUARD = "guard closed"
    STARVE_INFEED = "infeed starved"
    FEED_INFEED = "infeed restored"
    OPERATOR_RESUMES = "operator restarts the cell"
    OPERATOR_STOPS = "operator stops the cell"


@dataclass(frozen=True)
class Scenario:
    name: str
    question: str
    scans: int
    #: scan number -> what happens at the start of it
    events: dict[int, tuple[Event, ...]] = field(default_factory=dict)


@dataclass
class Result:
    scenario: Scenario
    scans: int
    producing_scans: int
    produced: int
    rejected: int
    good: int

    #: The theoretical best this cell can do, in scans per product. Derived
    #: from the slowest station rather than chosen: the filler holds a bottle
    #: for FILL_SCANS and the capper for CAP_SCANS, so the line cannot beat the
    #: larger of the two however it is driven.
    @property
    def ideal_scans_per_product(self) -> int:
        return max(FILL_SCANS, CAP_SCANS)

    @property
    def availability(self) -> float:
        """Producing time over planned time.

        Planned time is the whole run. A cell stopped by a guard opening is
        unavailable, which is the point of measuring it.
        """
        return self.producing_scans / self.scans if self.scans else 0.0

    @property
    def total_count(self) -> int:
        """Everything the line processed, scrap included.

        The rejected bottles occupied the stations, consumed the cycle and were
        then thrown away. Leaving them out of the denominator would credit the
        cell with capacity it spent producing waste, which is the single easiest
        way to report a flattering OEE.
        """
        return self.produced + self.rejected

    @property
    def performance(self) -> float:
        """Total output over what the run time could theoretically have made.

        Total rather than good, because performance asks how fast the machine
        ran and quality asks how much of that was worth keeping. Conflating them
        hides a fast line making scrap.

        Capped at 1.0: a figure above 100% means the ideal cycle time is wrong,
        and reporting it as a number above one buries that behind a good result.
        """
        if not self.producing_scans:
            return 0.0
        ideal = self.producing_scans / self.ideal_scans_per_product
        return min(self.total_count / ideal, 1.0) if ideal else 0.0

    @property
    def quality(self) -> float:
        """Good output over everything produced, scrap included."""
        return self.good / self.total_count if self.total_count else 0.0

    @property
    def oee(self) -> float:
        return self.availability * self.performance * self.quality


def run(scenario: Scenario,
        on_scan: Callable[[int, Cell, ReferenceController], None] | None = None,
        ) -> Result:
    """Play a scenario and measure it. Deterministic, so a result is a fact."""
    cell = Cell()
    controller = ReferenceController()
    image = ProcessImage()
    producing = 0

    for scan in range(scenario.scans):
        for event in scenario.events.get(scan, ()):
            _apply(event, cell, controller)

        image = controller.scan(image)
        image = cell.scan(image)

        if controller.machine.state is State.EXECUTE and cell.torque_available:
            producing += 1
        if on_scan is not None:
            on_scan(scan, cell, controller)

    good = sum(1 for product in cell.completed if product.good)
    return Result(scenario=scenario, scans=scenario.scans,
                  producing_scans=producing,
                  produced=image.registers[InputRegister.PRODUCED],
                  rejected=image.registers[InputRegister.REJECTED],
                  good=good)


def _apply(event: Event, cell: Cell, controller: ReferenceController) -> None:
    if event is Event.OPEN_GUARD:
        cell.open_guard()
    elif event is Event.CLOSE_GUARD:
        cell.close_guard()
    elif event is Event.STARVE_INFEED:
        cell.infeed_starved = True
    elif event is Event.FEED_INFEED:
        cell.infeed_starved = False
    elif event is Event.OPERATOR_RESUMES:
        controller.wants_to_run = True
    else:
        controller.wants_to_run = False


#: The three questions worth asking of this cell. Each one isolates a single
#: loss so the OEE difference against the baseline is attributable, which is
#: what makes the number an argument rather than a statistic.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="baseline",
        question="What does the cell do when nothing goes wrong?",
        scans=600,
        events={0: (Event.OPERATOR_RESUMES,)},
    ),
    Scenario(
        name="guard interruption",
        question="What does a person opening the guard cost, including the "
                 "restart nobody can skip?",
        scans=600,
        events={
            0: (Event.OPERATOR_RESUMES,),
            200: (Event.OPEN_GUARD,),
            260: (Event.CLOSE_GUARD,),
            300: (Event.OPERATOR_RESUMES,),
        },
    ),
    Scenario(
        name="starved infeed",
        question="What does an upstream supply failure cost, when the cell "
                 "itself is working perfectly?",
        scans=600,
        events={
            0: (Event.OPERATOR_RESUMES,),
            200: (Event.STARVE_INFEED,),
            350: (Event.FEED_INFEED,),
        },
    ),
)
