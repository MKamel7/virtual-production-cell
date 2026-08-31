"""The diagrams describe the code, and are checked against it.

A generated diagram is only worth more than a drawn one if something compares
the two. These tests assert the pictures agree with `vpc.packml` and
`vpc.process_image`, so a transition added to the state machine and forgotten
in the picture is a test failure rather than a picture that has silently
stopped being true.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from vpc import packml
from vpc.diagrams import DIAGRAMS, render_packml, render_scan_cycle
from vpc.process_image import Coil, Discrete, InputRegister

SVG = "{http://www.w3.org/2000/svg}"


def texts(svg: str) -> list[str]:
    return [e.text or "" for e in ET.fromstring(svg).iter(f"{SVG}text")]


@pytest.mark.parametrize("name,build", sorted(DIAGRAMS.items()))
def test_every_diagram_is_well_formed_svg(name, build):
    root = ET.fromstring(build())
    assert root.tag == f"{SVG}svg"
    assert int(root.get("width")) > 0
    assert int(root.get("height")) > 0


@pytest.mark.parametrize("name,build", sorted(DIAGRAMS.items()))
def test_rendering_is_deterministic(name, build):
    """The output is committed and gated, so two runs must agree byte for byte."""
    assert build() == build()


# ---- the scan cycle ---------------------------------------------------------

def test_the_scan_cycle_states_the_signal_counts_from_the_address_map():
    """Hardcoding these would let the picture and the process image disagree."""
    body = render_scan_cycle()

    assert f"{len(Coil)} coils out" in body
    assert f"{len(Discrete)} discrete inputs" in body
    assert f"{len(InputRegister)} input registers" in body


def test_the_scan_cycle_names_all_five_stages_in_order():
    """The ordering is the content: the PLC runs against a snapshot, so an
    input that changes mid-scan is not an input change until the next one."""
    found = [t for t in texts(render_scan_cycle()) if t[:2] in
             ("1.", "2.", "3.", "4.", "5.")]

    assert [t.split(". ")[1] for t in found] == [
        "Input snapshot", "PLC program", "Output image",
        "Modbus exchange", "Plant step"]


# ---- the state machine ------------------------------------------------------

def test_every_packml_state_is_drawn():
    drawn = set(texts(render_packml()))
    for state in packml.State:
        assert state.value in drawn, f"{state.value} is missing from the diagram"


def test_every_state_carries_its_packtags_code():
    """The number the PLC actually holds, so the picture matches the wire."""
    drawn = set(texts(render_packml()))
    for state in packml.State:
        assert f"PackTags {packml.PACKTAGS_CODE[state.value]}" in drawn


def test_both_kinds_of_transition_are_drawn_and_told_apart():
    """Modelling state-complete as a command is the usual way to get PackML
    wrong, so the picture has to distinguish them or it teaches the mistake."""
    root = ET.fromstring(render_packml())
    paths = [p for p in root.iter(f"{SVG}path") if p.get("stroke")]

    dashed = [p for p in paths if p.get("stroke-dasharray")]
    solid = [p for p in paths if not p.get("stroke-dasharray")]

    assert len(dashed) == len(packml.ON_COMPLETE)
    assert len(solid) == len(packml.ON_COMMAND)


def test_the_transition_counts_in_the_caption_are_the_real_ones():
    body = render_packml()

    assert f"{len(packml.State)} states" in body
    assert f"{len(packml.ON_COMMAND)} explicit command transitions" in body
    assert f"{len(packml.ON_COMPLETE)} state-complete transitions" in body


def test_acting_and_wait_states_are_both_placed():
    """A column that lost its states would render as an empty half."""
    body = render_packml()
    for state in packml.WAIT | packml.ACTING:
        assert state.value in body
    assert len(packml.WAIT) + len(packml.ACTING) == len(packml.State)


def test_a_state_in_neither_column_is_refused_rather_than_dropped(monkeypatch):
    """The failure mode this replaced a per-edge guard to catch.

    Guarding each edge meant a state in neither ACTING nor WAIT would simply
    not be drawn, and every transition through it would vanish with it, leaving
    a picture that looks complete and is not. Refusing to render is the only
    honest answer.
    """
    monkeypatch.setattr(packml, "WAIT",
                        packml.WAIT - {packml.State.IDLE})

    with pytest.raises(ValueError, match="neither packml.ACTING"):
        render_packml()


def test_the_caption_says_abort_and_stop_are_not_drawn():
    """They are legal almost everywhere and would bury the diagram. Leaving
    them out silently would make the picture look like they do not exist."""
    body = render_packml()

    assert "ABORT and STOP are not drawn" in body
