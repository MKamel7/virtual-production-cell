"""The traceability gate, broken deliberately in every direction it guards.

A gate nobody has watched fail is an assumption rather than a control. This file
is the difference between having a traceability check and believing you have
one, and it is the same discipline the fault injection harness applies to its
own gate.

It does not verify a safety requirement itself, so it carries no `verifies`
marker. It verifies the thing that verifies them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_traceability import (  # noqa: E402
    claims_in,
    collect_claims,
    find_problems,
    load_analysis,
)


def analysis(requirements: list[str], goal: str = "SG-01",
             hazard: str = "HAZ-01") -> dict[str, list[dict[str, object]]]:
    return {
        "hazards": [{"id": "HAZ-01", "text": "h", "harm": "x", "cause": "y"}],
        "goals": [{"id": "SG-01", "text": "g", "hazards": [hazard]}],
        "requirements": [{"id": r, "goal": goal, "text": "t", "rationale": "r"}
                         for r in requirements],
    }


# --- the two directions the gate exists for ----------------------------------
def test_a_requirement_nobody_tests_fails_the_build() -> None:
    """A hole in the argument. Something was asserted and never checked."""
    problems = find_problems(analysis(["SR-01"]), claimed={})

    assert problems == ["SR-01 is not verified by any test"]


def test_a_test_claiming_a_requirement_that_does_not_exist_fails_the_build() -> None:
    """The quieter direction, and the one that inflates a matrix.

    A test claiming SR-O1 instead of SR-01 runs, passes, and leaves SR-01
    looking covered by nothing while the report gains a row nobody asked for.
    One mistyped character.
    """
    problems = find_problems(analysis(["SR-01"]),
                             claimed={"SR-O1": [("test_x.py", "test_y")]})

    assert any("SR-O1" in p and "not a requirement" in p for p in problems)
    assert any("SR-01 is not verified" in p for p in problems)


def test_a_complete_argument_reports_nothing() -> None:
    """The control case. Without it the two above pass on a gate that always
    complains."""
    problems = find_problems(analysis(["SR-01"]),
                             claimed={"SR-01": [("test_x.py", "test_y")]})

    assert problems == []


# --- the analysis has to hang together too -----------------------------------
def test_a_requirement_under_a_goal_that_does_not_exist_fails() -> None:
    problems = find_problems(analysis(["SR-01"], goal="SG-99"),
                             claimed={"SR-01": [("f", "t")]})

    assert any("SG-99" in p and "does not exist" in p for p in problems)


def test_a_goal_covering_a_hazard_that_does_not_exist_fails() -> None:
    """A matrix that reads as complete and traces to nothing."""
    problems = find_problems(analysis(["SR-01"], hazard="HAZ-99"),
                             claimed={"SR-01": [("f", "t")]})

    assert any("HAZ-99" in p and "does not exist" in p for p in problems)


# --- how claims are read -----------------------------------------------------
def test_claims_are_parsed_and_not_grepped(tmp_path: Path) -> None:
    """Parsing beats searching, and this is the case that shows why.

    A marker inside a docstring or a comment is not a claim, and a grep would
    count both. The gate would then report a requirement as verified by a test
    that mentions it while checking something else entirely.
    """
    module = tmp_path / "test_sample.py"
    module.write_text(
        'import pytest\n'
        '\n'
        '\n'
        '@pytest.mark.verifies("SR-01")\n'
        'def test_real() -> None:\n'
        '    """This one mentions SR-99 in prose, which is not a claim."""\n'
        '\n'
        '\n'
        'def test_commented() -> None:\n'
        '    # @pytest.mark.verifies("SR-98")\n'
        '    pass\n',
        encoding="utf-8")

    claims = claims_in(module)

    assert claims == {"test_real": ["SR-01"]}


def test_collecting_walks_every_test_module(tmp_path: Path) -> None:
    for name in ("test_a.py", "test_b.py"):
        (tmp_path / name).write_text(
            'import pytest\n\n\n@pytest.mark.verifies("SR-01")\n'
            f'def test_{name[5]}() -> None:\n    pass\n', encoding="utf-8")
    (tmp_path / "helper.py").write_text(
        'import pytest\n\n\n@pytest.mark.verifies("SR-77")\n'
        'def test_ignored() -> None:\n    pass\n', encoding="utf-8")

    claimed = collect_claims(tmp_path)

    assert sorted(claimed) == ["SR-01"], "a non test_ module was collected"
    assert len(claimed["SR-01"]) == 2


# --- and the real analysis has to be internally consistent -------------------
def test_the_real_analysis_has_no_gaps() -> None:
    """The same check CI runs, so a broken argument fails here too rather than
    only in the workflow."""
    assert find_problems(load_analysis(), collect_claims(Path(__file__).parent)) == []


def test_every_requirement_carries_a_rationale() -> None:
    """A requirement without a stated reason is one nobody can argue with, and
    therefore one nobody can correct."""
    for requirement in load_analysis()["requirements"]:
        assert str(requirement.get("rationale", "")).strip(), (
            f"{requirement['id']} has no rationale"
        )


@pytest.mark.parametrize("section", ["hazards", "goals", "requirements"])
def test_ids_are_unique(section: str) -> None:
    ids = [str(entry["id"]) for entry in load_analysis()[section]]
    assert len(ids) == len(set(ids)), f"duplicate id in {section}"
