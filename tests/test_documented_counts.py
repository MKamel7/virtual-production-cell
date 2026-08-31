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


@pytest.mark.parametrize("document", DOCUMENTS)
def test_every_stated_test_count_is_the_real_one(document: str) -> None:
    path = ROOT / document
    if not path.is_file():
        pytest.skip(f"{document} is not in this repository")

    actual = collected_tests()
    claimed = {int(n) for n in re.findall(r"(\d+)\s+tests\b",
                                          path.read_text(encoding="utf-8"))}

    wrong = sorted(n for n in claimed if n != actual)
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
              and re.search(r"\d+\s+tests\b", (ROOT / d).read_text(encoding="utf-8"))]

    assert stated, "no document states a test count, so this gate guards nothing"
