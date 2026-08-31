"""Tests for the ISA-95 information model.

The interesting ones here are not the getters. They are:

  * that the hierarchy REFUSES to be built wrong, because a model that silently
    accepts an Area under a Work Cell is a model nobody can navigate by path;
  * that `Recipe.ideal_scans_per_product` and
    `scenario.Result.ideal_scans_per_product` agree, because two ideal cycle
    times in one system produce two different performance figures for one run
    and nothing else in the suite would catch that drift;
  * that `work_unit_values()` publishes exactly `WORK_UNIT_VARIABLES`, which is
    the same class of defect `packtags.browse_names()` exists to prevent: an
    address space and a model that disagree about a name give a supervisor that
    connects successfully and reads nothing;
  * that a line which produced nothing reports no cycle time rather than a
    cycle time of zero, which a supervisor would trend as infinitely fast.
"""

from __future__ import annotations

import pytest

from vpc.cell import CAP_SCANS, FILL_SCANS, QC_FAIL_EVERY
from vpc.isa95 import (
    CELL_PATH,
    KPI,
    WORK_UNIT_VARIABLES,
    Alarm,
    EquipmentModel,
    Level,
    Node,
    Recipe,
    Severity,
    active_alarms,
    default_model,
    snapshot,
    work_unit_values,
)
from vpc.packml import PACKTAGS_CODE, State
from vpc.packtags import PackTags, StopReason, UnitMode

# --------------------------------------------------------------------------
# the hierarchy


def test_a_child_may_not_sit_at_or_above_its_parents_level() -> None:
    with pytest.raises(ValueError, match="only descends"):
        Node("Bad", Level.AREA, (Node("Worse", Level.SITE),))


def test_a_child_may_not_sit_at_the_same_level_as_its_parent() -> None:
    with pytest.raises(ValueError, match="only descends"):
        Node("Area", Level.AREA, (Node("AlsoArea", Level.AREA),))


def test_a_leaf_builds_without_complaint() -> None:
    assert Node("Filler", Level.EQUIPMENT_MODULE).children == ()


def test_walk_yields_parents_before_children_with_full_paths() -> None:
    model = EquipmentModel(
        Node("E", Level.ENTERPRISE, (
            Node("S", Level.SITE, (Node("A", Level.AREA),)),)))
    assert [p for p, _ in model.walk()] == ["E", "E/S", "E/S/A"]


def test_the_default_model_reaches_the_cell_by_its_published_path() -> None:
    model = default_model()
    assert model.find(CELL_PATH).level is Level.WORK_UNIT


def test_the_default_model_carries_every_level() -> None:
    levels = {node.level for _, node in default_model().walk()}
    assert levels == set(Level)


def test_the_equipment_modules_are_the_stations_the_plant_has() -> None:
    model = default_model()
    modules = {node.name for _, node in model.at_level(Level.EQUIPMENT_MODULE)}
    assert modules == {"Infeed", "Filler", "Capper", "QCStation", "Outfeed"}


def test_finding_nothing_raises_rather_than_returning_none() -> None:
    with pytest.raises(KeyError, match="no equipment at"):
        default_model().find("Enterprise/Site/NoSuchArea")


def test_paths_and_walk_agree() -> None:
    model = default_model()
    assert model.paths() == tuple(p for p, _ in model.walk())


# --------------------------------------------------------------------------
# recipe


def test_the_recipe_reports_the_plants_real_parameters() -> None:
    recipe = Recipe()
    assert (recipe.fill_scans, recipe.cap_scans, recipe.qc_fail_every) == (
        FILL_SCANS, CAP_SCANS, QC_FAIL_EVERY)


def test_the_ideal_cycle_time_matches_the_scenario_modules_derivation() -> None:
    """Two ideal cycle times in one system give two performance figures.

    `scenario.Result` derives this from the slowest station and so does
    `Recipe`. If either changes alone, every OEE number in the repository
    silently disagrees with every other one, and no other test compares them.
    """
    from vpc.scenario import Result, Scenario

    result = Result(
        scenario=Scenario(name="x", question="does the ideal agree?", scans=1),
        scans=1, producing_scans=1, produced=1, rejected=0, good=1)
    assert Recipe().ideal_scans_per_product == result.ideal_scans_per_product


def test_a_slower_recipe_moves_the_ideal_cycle_time() -> None:
    assert Recipe(fill_scans=9, cap_scans=2).ideal_scans_per_product == 9


# --------------------------------------------------------------------------
# alarms


@pytest.mark.parametrize("reason, severity", [
    (StopReason.GUARD_OPEN, Severity.HIGH),
    (StopReason.SAFETY_CHANNEL, Severity.CRITICAL),
    (StopReason.LINK_LOST, Severity.CRITICAL),
    (StopReason.STARVED, Severity.WARNING),
    (StopReason.BLOCKED, Severity.WARNING),
])
def test_each_faulting_stop_reason_raises_its_alarm(
        reason: StopReason, severity: Severity) -> None:
    tags = PackTags()
    tags.status.state_current = State.STOPPED
    tags.status.stop_reason = reason
    alarms = active_alarms(tags)
    assert [a.identifier for a in alarms] == [f"Stop.{reason.name}"]
    assert alarms[0].severity is severity


@pytest.mark.parametrize("reason", [StopReason.NONE, StopReason.OPERATOR])
def test_a_stop_without_a_fault_raises_no_alarm(reason: StopReason) -> None:
    """An operator pressing stop is not a fault, and neither is not stopping."""
    tags = PackTags()
    tags.status.state_current = State.STOPPED
    tags.status.stop_reason = reason
    assert active_alarms(tags) == ()


def test_aborted_is_itself_an_alarm() -> None:
    tags = PackTags()
    tags.status.state_current = State.ABORTED
    assert [a.identifier for a in active_alarms(tags)] == ["State.Aborted"]


def test_held_is_a_warning_not_a_fault() -> None:
    tags = PackTags()
    tags.status.state_current = State.HELD
    alarms = active_alarms(tags)
    assert [a.identifier for a in alarms] == ["State.Held"]
    assert alarms[0].severity is Severity.WARNING


def test_a_running_machine_has_no_alarms() -> None:
    tags = PackTags()
    tags.status.state_current = State.EXECUTE
    assert active_alarms(tags) == ()


def test_alarms_come_back_worst_first() -> None:
    tags = PackTags()
    tags.status.state_current = State.ABORTED
    tags.status.stop_reason = StopReason.STARVED
    severities = [a.severity for a in active_alarms(tags)]
    assert severities == [Severity.CRITICAL, Severity.WARNING]


# --------------------------------------------------------------------------
# KPIs


def _kpi(**over: object) -> KPI:
    base: dict[str, object] = {
        "good_count": 90, "reject_count": 10, "producing_scans": 300,
        "planned_scans": 400, "ideal_scans_per_product": 3}
    base.update(over)
    return KPI(**base)  # type: ignore[arg-type]


def test_the_counts_add_up() -> None:
    assert _kpi().total_count == 100


def test_availability_is_producing_over_planned() -> None:
    assert _kpi().availability == pytest.approx(0.75)


def test_availability_of_a_run_that_never_started_is_zero_not_an_error() -> None:
    assert _kpi(planned_scans=0).availability == 0.0


def test_performance_compares_ideal_against_actual() -> None:
    assert _kpi().performance == pytest.approx(1.0)


def test_performance_is_capped_at_one() -> None:
    """Above 100% means the ideal cycle time is wrong, not that the line flew."""
    assert _kpi(ideal_scans_per_product=99).performance == 1.0


def test_performance_without_producing_scans_is_zero() -> None:
    assert _kpi(producing_scans=0).performance == 0.0


def test_quality_is_good_over_everything_processed() -> None:
    assert _kpi().quality == pytest.approx(0.9)


def test_quality_of_a_line_that_made_nothing_is_zero() -> None:
    assert _kpi(good_count=0, reject_count=0).quality == 0.0


def test_oee_is_the_product_of_the_three() -> None:
    kpi = _kpi()
    assert kpi.oee == pytest.approx(
        kpi.availability * kpi.performance * kpi.quality)


def test_cycle_time_is_measured_scans_per_product() -> None:
    assert _kpi().cycle_time == pytest.approx(3.0)


def test_a_line_that_made_nothing_has_no_cycle_time() -> None:
    """None rather than 0.0: zero would trend as an infinitely fast line."""
    assert _kpi(good_count=0, reject_count=0).cycle_time is None


# --------------------------------------------------------------------------
# the published snapshot


def _tags(state: State = State.EXECUTE,
          reason: StopReason = StopReason.NONE) -> PackTags:
    tags = PackTags()
    tags.status.state_current = state
    tags.status.stop_reason = reason
    tags.status.unit_mode_current = UnitMode.PRODUCING
    return tags


def test_the_snapshot_publishes_under_the_cells_path_by_default() -> None:
    snap = snapshot(_tags(), producing_scans=300, planned_scans=400,
                    good_count=90, reject_count=10)
    assert snap.path == CELL_PATH


def test_the_snapshot_carries_the_machines_own_state_and_mode() -> None:
    snap = snapshot(_tags(State.HELD), producing_scans=1, planned_scans=2,
                    good_count=1, reject_count=0)
    assert snap.state is State.HELD
    assert snap.mode is UnitMode.PRODUCING


def test_a_high_severity_alarm_marks_the_unit_faulted() -> None:
    snap = snapshot(_tags(State.STOPPED, StopReason.GUARD_OPEN),
                    producing_scans=1, planned_scans=2,
                    good_count=1, reject_count=0)
    assert snap.is_faulted


def test_a_warning_alone_does_not_mark_the_unit_faulted() -> None:
    snap = snapshot(_tags(State.STOPPED, StopReason.STARVED),
                    producing_scans=1, planned_scans=2,
                    good_count=1, reject_count=0)
    assert snap.alarms and not snap.is_faulted


def test_a_supplied_recipe_drives_the_performance_figure() -> None:
    snap = snapshot(_tags(), producing_scans=300, planned_scans=400,
                    good_count=90, reject_count=10,
                    recipe=Recipe(fill_scans=1, cap_scans=1, name="Fast"))
    assert snap.recipe.name == "Fast"
    assert snap.kpi.ideal_scans_per_product == 1


def test_the_snapshot_can_be_published_at_another_path() -> None:
    snap = snapshot(_tags(), producing_scans=1, planned_scans=1,
                    good_count=1, reject_count=0, path="E/S/A/L/Cell2")
    assert snap.path == "E/S/A/L/Cell2"


# --------------------------------------------------------------------------
# the published variable set


def test_the_published_values_are_exactly_the_declared_variables() -> None:
    """The address space and the model must not drift apart on a name."""
    snap = snapshot(_tags(), producing_scans=300, planned_scans=400,
                    good_count=90, reject_count=10)
    assert tuple(work_unit_values(snap)) == WORK_UNIT_VARIABLES


def test_the_state_code_published_is_the_packtags_encoding() -> None:
    snap = snapshot(_tags(State.EXECUTE), producing_scans=1, planned_scans=1,
                    good_count=1, reject_count=0)
    values = work_unit_values(snap)
    assert values["MachineStateCode"] == PACKTAGS_CODE[State.EXECUTE.value]


def test_no_cycle_time_publishes_the_out_of_range_sentinel() -> None:
    """OPC UA has no null for a Double, and 0.0 would read as a real duration."""
    snap = snapshot(_tags(), producing_scans=0, planned_scans=10,
                    good_count=0, reject_count=0)
    assert work_unit_values(snap)["CycleTime"] == -1.0


def test_a_real_cycle_time_is_published_as_itself() -> None:
    snap = snapshot(_tags(), producing_scans=300, planned_scans=400,
                    good_count=90, reject_count=10)
    assert work_unit_values(snap)["CycleTime"] == pytest.approx(3.0)


def test_a_quiet_machine_publishes_no_alarms_and_the_lowest_severity() -> None:
    snap = snapshot(_tags(), producing_scans=1, planned_scans=1,
                    good_count=1, reject_count=0)
    values = work_unit_values(snap)
    assert values["AlarmCount"] == 0
    assert values["HighestSeverity"] == Severity.INFO.value


def test_an_alarming_machine_publishes_its_worst_severity() -> None:
    snap = snapshot(_tags(State.STOPPED, StopReason.SAFETY_CHANNEL),
                    producing_scans=1, planned_scans=1,
                    good_count=1, reject_count=0)
    values = work_unit_values(snap)
    assert values["AlarmCount"] == 1
    assert values["HighestSeverity"] == Severity.CRITICAL.value


def test_the_alarm_record_is_immutable() -> None:
    """An alarm a consumer can edit is an alarm the machine no longer owns."""
    alarm = Alarm("X", Severity.INFO, "text")
    with pytest.raises(AttributeError):
        alarm.severity = Severity.CRITICAL  # type: ignore[misc]
