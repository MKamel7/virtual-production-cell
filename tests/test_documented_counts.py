"""Every test count the documents claim must be the real one.

WHY THIS EXISTS. This repository already gates its traceability matrix and its
generated diagrams against the code, and both work. Its README nonetheless said
144 tests while the suite collected 236, because nothing checked that
particular kind of claim. The nearby sentence was historical and has been
reworded so it no longer carries a number that can go stale.

`fault-injection-harness` has a gate like this and it caught three stale counts
in one afternoon. The repositories without one drifted. That is the whole
argument: a number a human has to remember to update is a number that will be
wrong, and the fix is not more care, it is a check.

WHY COLLECTION RATHER THAN A RUN. It is fast, it needs no threshold to be met,
and the count is exactly what the documents claim. Collection does not execute
tests, so this cannot recurse.

A number that is deliberately historical should not be written as "N tests" at
all. Reword it instead of exempting it: an exemption is a second place for the
truth to live.

THE GATE HAD A HOLE, found 2026-08-31 and closed here. The first version matched
`(\\d+)\\s+tests` against raw markdown, which cannot see either place the README
actually states its count: the summary table writes `| **223** | tests, ...`,
where emphasis and a table pipe sit between the number and the word, and the run
instructions write `Expect **223 passed**`, which does not contain "tests" at
all. So the headline number on the front page sat at 223 against a real 239 with
this gate green the whole time, and only the count in `docs/SAFETY_ARGUMENT.md`
was ever genuinely guarded.

The fix is to normalise before matching rather than to write a cleverer regex:
strip markdown emphasis and table pipes, collapse whitespace, then look for both
"N tests" and "N passed". A gate that cannot see the claim it exists to guard is
the same failure as no gate, wearing a green tick.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = ("README.md", "docs/SAFETY_ARGUMENT.md")


def collected_tests() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "--no-cov", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, check=False)
    found = re.search(r"^(\d+) tests collected", result.stdout, re.M)
    assert found is not None, (
        f"could not count the suite:\n{result.stdout[-1500:]}")
    return int(found.group(1))


def normalise(markdown: str) -> str:
    """Strip the markup that hides a count from a plain regex.

    Emphasis markers and table pipes are removed and whitespace collapsed, so
    `| **285** | tests,` and `Expect **285 passed**` both reduce to something
    `(\\d+)\\s+(tests|passed)` can actually see. Without this the gate reads a
    document it cannot parse and reports success.
    """
    return re.sub(r"\s+", " ", markdown.replace("*", " ").replace("|", " "))


#: Both ways this repository states its suite size. "passed" is here because the
#: README's run instructions use it and nothing else would catch that one.
CLAIM = re.compile(r"(\d+)\s+(?:tests|passed)\b")


def claimed_counts(markdown: str) -> set[int]:
    return {int(m.group(1)) for m in CLAIM.finditer(normalise(markdown))}


@pytest.mark.parametrize("document", DOCUMENTS)
def test_every_stated_test_count_is_the_real_one(document: str) -> None:
    path = ROOT / document
    if not path.is_file():
        pytest.skip(f"{document} is not in this repository")

    actual = collected_tests()
    wrong = sorted(n for n in claimed_counts(path.read_text(encoding="utf-8"))
                   if n != actual)
    assert not wrong, (
        f"{document} says {wrong} tests; the suite collects {actual}. "
        f"Update the document, or reword the claim if it is historical.")


def test_the_documents_state_the_count_at_all() -> None:
    """A gate over a claim nobody makes passes for the wrong reason.

    If both documents stopped mentioning the suite size this file would go
    green forever while saying nothing, which is the shape of check this
    repository is otherwise careful to avoid.
    """
    stated = [d for d in DOCUMENTS
              if (ROOT / d).is_file()
              and claimed_counts((ROOT / d).read_text(encoding="utf-8"))]

    assert stated, "no document states a test count, so this gate guards nothing"


def test_the_gate_can_see_a_count_inside_a_markdown_table() -> None:
    """The exact shape that slipped past the first version of this file.

    Kept as a test rather than a comment because the failure was invisible: the
    gate was green, the README was wrong, and nothing pointed at the regex.
    """
    assert claimed_counts("| **223** | tests, 100% branch coverage |") == {223}


def test_the_gate_can_see_a_count_written_as_passed() -> None:
    assert claimed_counts("Expect **223 passed**. Coverage is gated.") == {223}


def test_normalising_does_not_invent_a_count() -> None:
    """Prose with no claim in it must stay empty, or the gate cries wolf."""
    assert claimed_counts("The 5 hazards map to 14 requirements.") == set()
