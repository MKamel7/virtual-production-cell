"""Scenario runs, the reference controller, and the OEE arithmetic.

The controller here is not a second implementation of the control policy. It is
the executable specification of the one `plc/cell_control.st` implements, in the
same relationship the PackML model already has with the state machine, and
`test_st_matches_the_model.py` checks the ST against it. These tests verify the
specification behaves; that file verifies the ST matches it.
"""

from __future__ import annotations

import pytest

from vpc.cell import Cell
from vpc.packml import State
from vpc.process_image import Coil, ProcessImage
from vpc.scenario import (
    SCENARIOS,
    Event,
    ReferenceController,
    Result,
    Scenario,
    run,
)


def result(scans: int = 100, producing: int = 100, produced: int = 20,
           rejected: int = 0, good: int = 20) -> Result:
    return Result(scenario=SCENARIOS[0], scans=scans, producing_scans=producing,
                  produced=produced, rejected=rejected, good=good)


# --- the reference controller -------------------------------------------------
def test_the_controller_powers_on_aborted_and_stays_there_unasked() -> None:
    """The same property the ST has, and for the same reason."""
    controller = ReferenceController()
    image = ProcessImage()
    cell = Cell()

    for _ in range(20):
        image = cell.scan(controller.scan(image))

    assert controller.machine.state is State.ABORTED
    assert cell.produced == 0


def test_an_operator_walks_the_cell_up_to_execute_one_command_per_scan() -> None:
    controller = ReferenceController()
    controller.wants_to_run = True
    image, cell = ProcessImage(), Cell()

    # Nine scans to walk Aborted -> Clearing -> Stopped -> Resetting -> Idle ->
    # Starting -> Execute, then a bottle has to cross the filler and the capper
    # before anything reaches the outfeed.
    for _ in range(60):
        image = cell.scan(controller.scan(image))

    assert controller.machine.state is State.EXECUTE
    assert cell.produced > 0


def test_execute_is_not_ended_by_the_acting_timer() -> None:
    """Execute is an acting state and must not be completed on a count.

    Before this was right the cell dropped out of Execute every two scans,
    restarted itself, and reported an availability of 20% on a run where nothing
    at all had gone wrong. The symptom was a plausible number, which is the
    dangerous kind.
    """
    controller = ReferenceController()
    controller.wants_to_run = True
    image, cell = ProcessImage(), Cell()

    for _ in range(200):
        image = cell.scan(controller.scan(image))

    assert controller.machine.state is State.EXECUTE
    assert controller.machine.state.value == "Execute"


def test_opening_the_guard_aborts_the_controller() -> None:
    controller = ReferenceController()
    controller.wants_to_run = True
    image, cell = ProcessImage(), Cell()
    for _ in range(20):
        image = cell.scan(controller.scan(image))
    assert controller.machine.state is State.EXECUTE

    cell.open_guard()
    for _ in range(6):
        image = cell.scan(controller.scan(image))

    assert controller.machine.state is State.ABORTED
    assert not any(image.coils[c] for c in Coil if c is not Coil.SAFETY_RESET_REQUEST)


def test_the_controller_asks_for_torque_only_with_the_guard_closed() -> None:
    """The request is an ask. The safety channel still decides."""
    controller = ReferenceController()
    controller.wants_to_run = True
    image, cell = ProcessImage(), Cell()
    for _ in range(20):
        image = cell.scan(controller.scan(image))

    cell.open_guard()
    image = cell.scan(controller.scan(image))
    assert not image.coils[Coil.SAFETY_RESET_REQUEST], "asked while the guard was open"

    # A full cycle first, so the plant PUBLISHES the closed guard. Asking the
    # controller before that would be reading an input the plant has not sent
    # yet, which is the scan boundary this whole project is built around.
    cell.close_guard()
    image = cell.scan(controller.scan(image))

    assert controller.scan(image).coils[Coil.SAFETY_RESET_REQUEST]


def test_a_guard_cycle_recovers_only_because_somebody_asked() -> None:
    """End to end: open, close, and the cell comes back only via the request."""
    controller = ReferenceController()
    controller.wants_to_run = True
    image, cell = ProcessImage(), Cell()
    for _ in range(20):
        image = cell.scan(controller.scan(image))

    cell.open_guard()
    for _ in range(5):
        image = cell.scan(controller.scan(image))
    cell.close_guard()
    assert not cell.torque_available, "closing the guard restored torque"

    for _ in range(30):
        image = cell.scan(controller.scan(image))

    assert cell.torque_available
    assert controller.machine.state is State.EXECUTE


def test_an_operator_stopping_leaves_the_cell_where_it_is() -> None:
    controller = ReferenceController()
    controller.wants_to_run = True
    image, cell = ProcessImage(), Cell()
    for _ in range(20):
        image = cell.scan(controller.scan(image))

    controller.wants_to_run = False
    before = cell.produced
    for _ in range(20):
        image = cell.scan(controller.scan(image))

    assert cell.produced > before, "Execute is a wait state; Stop is a command"
    assert controller.machine.state is State.EXECUTE


# --- the OEE arithmetic -------------------------------------------------------
def test_scrap_is_in_the_denominator_of_both_performance_and_quality() -> None:
    """The easiest way to report a flattering OEE is to forget the scrap.

    Twenty rejected bottles occupied the stations and consumed the cycle. A
    quality figure that ignored them would read 100% on a line throwing away one
    bottle in eight.
    """
    measured = result(produced=100, rejected=20, good=100)

    assert measured.total_count == 120
    assert measured.quality == pytest.approx(100 / 120)


def test_performance_cannot_exceed_one() -> None:
    """A figure above 100% means the ideal cycle time is wrong.

    Reporting it as 130% would bury a broken assumption behind a good number.
    """
    measured = result(scans=10, producing=10, produced=1000, rejected=0, good=1000)

    assert measured.performance == 1.0


def test_an_idle_run_scores_zero_rather_than_dividing_by_nothing() -> None:
    measured = result(scans=100, producing=0, produced=0, rejected=0, good=0)

    assert measured.availability == 0.0
    assert measured.performance == 0.0
    assert measured.quality == 0.0
    assert measured.oee == 0.0


def test_a_run_of_no_scans_scores_zero() -> None:
    measured = result(scans=0, producing=0, produced=0, rejected=0, good=0)

    assert measured.availability == 0.0
    assert measured.oee == 0.0


def test_the_ideal_cycle_is_derived_from_the_slowest_station() -> None:
    """Chosen ideal cycle times are how OEE becomes a number you can pick."""
    from vpc.cell import CAP_SCANS, FILL_SCANS

    assert result().ideal_scans_per_product == max(FILL_SCANS, CAP_SCANS)


# --- the scenarios themselves -------------------------------------------------
def test_every_scenario_runs_and_produces_something() -> None:
    for scenario in SCENARIOS:
        measured = run(scenario)
        assert measured.total_count > 0, f"{scenario.name} produced nothing"
        assert 0.0 < measured.oee < 1.0


def test_a_guard_interruption_costs_availability_and_not_performance() -> None:
    """The shape of the loss is the finding, not the size of it.

    A cell stopped by a guard is not running slowly, it is not running. If this
    showed up as a performance loss the number would be pointing at the wrong
    remedy entirely.
    """
    baseline = run(SCENARIOS[0])
    interrupted = run(SCENARIOS[1])

    assert interrupted.availability < baseline.availability
    assert interrupted.performance == pytest.approx(baseline.performance, abs=0.05)


def test_a_starved_infeed_costs_performance_and_not_availability() -> None:
    """The complementary case, and the one a single figure would hide.

    The cell is running perfectly and producing nothing. Availability says the
    hour was fine.
    """
    baseline = run(SCENARIOS[0])
    starved = run(SCENARIOS[2])

    assert starved.performance < baseline.performance
    assert starved.availability == pytest.approx(baseline.availability, abs=0.01)


def test_runs_are_deterministic_so_a_result_is_a_fact() -> None:
    """A campaign that cannot be replayed exactly is not evidence."""
    first, second = run(SCENARIOS[1]), run(SCENARIOS[1])

    assert (first.produced, first.rejected, first.producing_scans) == \
           (second.produced, second.rejected, second.producing_scans)


def test_the_scan_hook_sees_every_scan() -> None:
    seen: list[int] = []
    scenario = Scenario(name="tiny", question="?", scans=5,
                        events={0: (Event.OPERATOR_RESUMES,)})

    run(scenario, on_scan=lambda scan, cell, controller: seen.append(scan))

    assert seen == [0, 1, 2, 3, 4]


def test_an_unknown_event_stops_the_cell_rather_than_being_ignored() -> None:
    scenario = Scenario(name="stop", question="?", scans=40,
                        events={0: (Event.OPERATOR_RESUMES,),
                                5: (Event.OPERATOR_STOPS,),
                                10: (Event.STARVE_INFEED,),
                                20: (Event.FEED_INFEED,)})

    measured = run(scenario)

    assert measured.scans == 40


def test_the_operator_has_a_way_forward_from_every_waiting_state() -> None:
    """Total over the wait set, not a lookup with a fallback.

    Every state that waits is waiting FOR something. A missing entry would be a
    state the operator cannot leave, and a lookup that returned None would hide
    that as a cell quietly sitting still, which is the hardest kind of fault to
    notice on a line that is supposed to be running.
    """
    from vpc.packml import WAIT
    from vpc.scenario import TOWARDS_EXECUTE

    assert set(TOWARDS_EXECUTE) == set(WAIT), (
        f"no way forward from {set(WAIT) - set(TOWARDS_EXECUTE)}"
    )
