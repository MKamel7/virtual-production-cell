"""The ISA-95 information model: equipment hierarchy, KPIs, alarms, recipe.

WHY A HIERARCHY, when the cell already publishes PackTags. PackTags answers
"what is this machine doing". It does not answer "which machine", and on a real
site that is the harder question. A flat address space works for exactly one
machine; the second one arrives with a `StateCurrent` of its own and the two
collide, so every supervisor above them grows a per-machine translation layer,
which is the thing PackTags existed to remove. IEC 62264 fixes it by making the
equipment ADDRESS structural: a work unit is identified by where it sits, not by
a tag prefix somebody agreed in a meeting.

WHAT THE LEVELS ACTUALLY ARE, because "enterprise, site, area, line, cell,
equipment" is the shop-floor phrasing and not quite the standard's. IEC 62264-1
defines five levels, Enterprise, Site, Area, Work Center, Work Unit. Production
Line is a SPECIALIZATION of Work Center, and Work Cell is a specialization of
Work Unit, which is why both appear here with two names. Equipment Module is not
an IEC 62264 term at all; it comes from ISA-88 (IEC 61512) and is borrowed
deliberately, because the filler and the capper are real addressable things and
pretending they do not exist would be a worse model than crossing a standard
boundary and saying so.

WHAT IS POPULATED, stated as a subset in the same way `packtags.py` states its
own. The KPI set is the ISO 22400 trio plus OEE. Cycle time is measured, not
nominal. Recipe carries the three parameters this plant actually has. Alarms are
derived from conditions the cell genuinely reports, and there is no alarm
history, no acknowledgement model and no shelving, because none of those exist
here and naming them would invite a supervisor to read an empty list and believe
the line has never faulted.

TIME IS SCANS throughout, exactly as in `cell.py`. Every duration here is a scan
count, and `CycleTime` is scans per product rather than seconds. A wall clock
would make a campaign unreproducible, and the whole repository is built the
other way.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum

from vpc.cell import CAP_SCANS, FILL_SCANS, QC_FAIL_EVERY
from vpc.packml import PACKTAGS_CODE, State
from vpc.packtags import PackTags, StopReason, UnitMode


class Level(IntEnum):
    """Where a node sits in the equipment hierarchy.

    Levels 1 to 5 are IEC 62264-1. `EQUIPMENT_MODULE` is ISA-88 and sits below
    the ISA-95 model rather than inside it; it is numbered 6 so that ordering
    still works, not because the standard defines a sixth level.
    """

    ENTERPRISE = 1
    SITE = 2
    AREA = 3
    WORK_CENTER = 4
    WORK_UNIT = 5
    EQUIPMENT_MODULE = 6


#: The shop-floor synonym for each level, where one exists. Kept because a
#: reviewer who knows the plant floor and a reviewer who knows the standard use
#: different words for the same box, and the model should answer to both.
SYNONYM: dict[Level, str] = {
    Level.WORK_CENTER: "Production Line",
    Level.WORK_UNIT: "Work Cell",
}


@dataclass(frozen=True)
class Node:
    """One element of the equipment hierarchy.

    Frozen because an equipment model that changes shape at runtime is a
    different plant, and the supervisor holding browse paths into it would not
    be told. Reconfiguration means building a new model.
    """

    name: str
    level: Level
    children: tuple[Node, ...] = ()

    def __post_init__(self) -> None:
        for child in self.children:
            if child.level <= self.level:
                raise ValueError(
                    f"{child.name} is {child.level.name}, which cannot sit "
                    f"under {self.name} at {self.level.name}: the hierarchy "
                    f"only descends")


@dataclass(frozen=True)
class EquipmentModel:
    """The tree, plus the path lookups a supervisor needs."""

    root: Node

    def walk(self) -> Iterator[tuple[str, Node]]:
        """Every node with its full browse path, depth first, parents first."""

        def _walk(node: Node, prefix: str) -> Iterator[tuple[str, Node]]:
            path = f"{prefix}/{node.name}" if prefix else node.name
            yield path, node
            for child in node.children:
                yield from _walk(child, path)

        yield from _walk(self.root, "")

    def paths(self) -> tuple[str, ...]:
        return tuple(path for path, _ in self.walk())

    def find(self, path: str) -> Node:
        """The node at a browse path, or `KeyError`.

        `KeyError` rather than `None`: a supervisor that asked for a work unit
        by path and got nothing back will otherwise carry the None into its next
        decision, which is the failure this whole module exists to avoid.
        """
        for candidate, node in self.walk():
            if candidate == path:
                return node
        raise KeyError(f"no equipment at {path!r}")

    def at_level(self, level: Level) -> tuple[tuple[str, Node], ...]:
        return tuple((p, n) for p, n in self.walk() if n.level is level)


def default_model() -> EquipmentModel:
    """The hierarchy this cell actually is.

    The names above the work unit are this project's, not a real customer's, and
    they are here because the SHAPE is the point: a supervisor written against
    this address space works unchanged against a site with four lines, which is
    the claim a flat tag list cannot make.

    The equipment modules are not decoration. Filler, Capper and QC are the
    three stations `cell.py` advances, and Infeed and Outfeed are where the
    starved and blocked conditions come from, so every module below the cell
    corresponds to something the plant model genuinely has.
    """
    return EquipmentModel(
        Node("Enterprise", Level.ENTERPRISE, (
            Node("Site", Level.SITE, (
                Node("Packaging", Level.AREA, (
                    Node("BottlingLine1", Level.WORK_CENTER, (
                        Node("PackagingCell", Level.WORK_UNIT, (
                            Node("Infeed", Level.EQUIPMENT_MODULE),
                            Node("Filler", Level.EQUIPMENT_MODULE),
                            Node("Capper", Level.EQUIPMENT_MODULE),
                            Node("QCStation", Level.EQUIPMENT_MODULE),
                            Node("Outfeed", Level.EQUIPMENT_MODULE),
                        )),
                    )),
                )),
            )),
        ))
    )


#: The browse path of the one work unit this repository simulates. Defined once
#: so the OPC UA layer and the tests cannot drift apart on it.
CELL_PATH = "Enterprise/Site/Packaging/BottlingLine1/PackagingCell"


@dataclass(frozen=True)
class Recipe:
    """The parameters that define what the line is making.

    Three, because three is what the plant has. `cell.py` holds a bottle at the
    filler for `FILL_SCANS`, at the capper for `CAP_SCANS`, and fails one in
    `QC_FAIL_EVERY` at inspection. A recipe listing a fill volume or a cap torque
    would be inventing process values this simulation does not model, and a
    supervisor would trend them.
    """

    fill_scans: int = FILL_SCANS
    cap_scans: int = CAP_SCANS
    qc_fail_every: int = QC_FAIL_EVERY
    name: str = "Default"

    @property
    def ideal_scans_per_product(self) -> int:
        """The best this recipe can do, set by the slower station.

        The same derivation as `scenario.Result.ideal_scans_per_product`, and it
        has to stay the same: two different ideal cycle times in one system
        produce two different performance figures for one run.
        """
        return max(self.fill_scans, self.cap_scans)


class Severity(IntEnum):
    """OPC UA condition severity, banded.

    OPC UA gives severity a 1 to 1000 range and does not say what the numbers
    mean, so these bands are a choice made here rather than a quotation, and
    they are named so a reviewer can disagree with the mapping rather than
    reverse engineer it from integers.
    """

    INFO = 100
    WARNING = 400
    HIGH = 700
    CRITICAL = 900


@dataclass(frozen=True)
class Alarm:
    """One active condition.

    There is no acknowledgement flag and no timestamp. Acknowledgement needs an
    operator interface this project does not have, and a timestamp needs a
    clock this project deliberately does not use.
    """

    identifier: str
    severity: Severity
    message: str


#: Stop reasons that are genuinely alarms, with how bad each one is. Reasons
#: absent from this map are stops without a fault: OPERATOR is somebody pressing
#: stop, and NONE is not a stop at all.
_ALARM_FOR_STOP: dict[StopReason, tuple[Severity, str]] = {
    StopReason.GUARD_OPEN: (Severity.HIGH, "Guard open, torque removed"),
    StopReason.SAFETY_CHANNEL: (
        Severity.CRITICAL, "Safety channel tripped, torque removed"),
    StopReason.LINK_LOST: (Severity.CRITICAL, "Fieldbus link lost"),
    StopReason.STARVED: (Severity.WARNING, "Infeed starved, no product"),
    StopReason.BLOCKED: (Severity.WARNING, "Outfeed blocked, cannot discharge"),
}


def active_alarms(tags: PackTags) -> tuple[Alarm, ...]:
    """Everything currently wrong, worst first.

    Derived rather than stored. An alarm list the machine has to remember to
    clear is an alarm list that eventually disagrees with the machine, and the
    conditions here are all directly readable from the current scan.
    """
    alarms: list[Alarm] = []
    reason = tags.status.stop_reason
    if reason in _ALARM_FOR_STOP:
        severity, message = _ALARM_FOR_STOP[reason]
        alarms.append(Alarm(f"Stop.{reason.name}", severity, message))
    if tags.status.state_current is State.ABORTED:
        alarms.append(Alarm("State.Aborted", Severity.CRITICAL,
                            "Machine aborted, reset required"))
    elif tags.status.state_current is State.HELD:
        alarms.append(Alarm("State.Held", Severity.WARNING,
                            "Machine held"))
    return tuple(sorted(alarms, key=lambda a: -a.severity))


@dataclass(frozen=True)
class KPI:
    """The ISO 22400 set, computed from counts rather than asserted.

    ISO 22400-2 is the standard that actually defines these; ISA-95 gives the
    hierarchy they hang on. Keeping the two straight matters, because OEE is
    routinely called an ISA-95 KPI and is not one.

    Every figure is a ratio in [0, 1] except the counts and the cycle time.
    `None` for cycle time when nothing was produced, rather than zero: a line
    that made nothing has no cycle time, and zero would read as infinitely fast.
    """

    good_count: int
    reject_count: int
    producing_scans: int
    planned_scans: int
    ideal_scans_per_product: int

    @property
    def total_count(self) -> int:
        return self.good_count + self.reject_count

    @property
    def availability(self) -> float:
        return self.producing_scans / self.planned_scans if self.planned_scans else 0.0

    @property
    def performance(self) -> float:
        """How fast it ran while it was running.

        Capped at 1.0 for the same reason `scenario.Result.performance` is: a
        figure above 100% means the ideal cycle time is wrong, and reporting it
        as performance hides the modelling error behind a flattering number.
        """
        if not self.producing_scans:
            return 0.0
        ideal = self.total_count * self.ideal_scans_per_product
        return min(ideal / self.producing_scans, 1.0)

    @property
    def quality(self) -> float:
        return self.good_count / self.total_count if self.total_count else 0.0

    @property
    def oee(self) -> float:
        return self.availability * self.performance * self.quality

    @property
    def cycle_time(self) -> float | None:
        """Measured scans per product, or None if nothing was produced."""
        if not self.total_count:
            return None
        return self.producing_scans / self.total_count


@dataclass(frozen=True)
class WorkUnitSnapshot:
    """Everything the model publishes for the cell at one scan."""

    path: str
    state: State
    mode: UnitMode
    stop_reason: StopReason
    kpi: KPI
    recipe: Recipe
    alarms: tuple[Alarm, ...] = ()

    @property
    def is_faulted(self) -> bool:
        return any(a.severity >= Severity.HIGH for a in self.alarms)


def snapshot(tags: PackTags, *, producing_scans: int, planned_scans: int,
             good_count: int, reject_count: int,
             recipe: Recipe | None = None,
             path: str = CELL_PATH) -> WorkUnitSnapshot:
    """Assemble the work unit's published view from the machine's own tags.

    Counts are passed rather than read off `Admin`, because `Admin` holds
    processed and defective, and good is their difference. Deriving it in one
    place and passing it keeps the arithmetic out of three callers.
    """
    recipe = recipe or Recipe()
    return WorkUnitSnapshot(
        path=path,
        state=tags.status.state_current,
        mode=tags.status.unit_mode_current,
        stop_reason=tags.status.stop_reason,
        kpi=KPI(good_count=good_count, reject_count=reject_count,
                producing_scans=producing_scans, planned_scans=planned_scans,
                ideal_scans_per_product=recipe.ideal_scans_per_product),
        recipe=recipe,
        alarms=active_alarms(tags),
    )


#: The variables published under a work unit, in browse order. Held here for the
#: same reason `packtags.browse_names()` is: an address space and a model that
#: disagree about a name give a supervisor that connects and reads nothing.
WORK_UNIT_VARIABLES: tuple[str, ...] = (
    "MachineState", "MachineStateCode", "UnitMode", "StopReason",
    "GoodCount", "RejectCount", "TotalCount",
    "Availability", "Performance", "Quality", "OEE", "CycleTime",
    "RecipeName", "AlarmCount", "HighestSeverity",
)


def work_unit_values(snap: WorkUnitSnapshot) -> dict[str, object]:
    """The published values, keyed exactly by `WORK_UNIT_VARIABLES`.

    `CycleTime` publishes -1.0 when there is nothing to report, because OPC UA
    has no null for a Double and a supervisor reading 0.0 would trend it as an
    infinitely fast line. -1.0 is out of range for a duration and therefore
    cannot be mistaken for one.
    """
    kpi = snap.kpi
    return {
        "MachineState": snap.state.value,
        "MachineStateCode": PACKTAGS_CODE[snap.state.value],
        "UnitMode": snap.mode.name,
        "StopReason": snap.stop_reason.name,
        "GoodCount": kpi.good_count,
        "RejectCount": kpi.reject_count,
        "TotalCount": kpi.total_count,
        "Availability": kpi.availability,
        "Performance": kpi.performance,
        "Quality": kpi.quality,
        "OEE": kpi.oee,
        "CycleTime": -1.0 if kpi.cycle_time is None else kpi.cycle_time,
        "RecipeName": snap.recipe.name,
        "AlarmCount": len(snap.alarms),
        "HighestSeverity": max((a.severity for a in snap.alarms),
                               default=Severity.INFO).value,
    }
