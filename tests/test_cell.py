"""The plant, and the scan semantics that make it a PLC rather than an event loop.

Two groups of properties here and they are checked for different reasons.

The SCAN SEMANTICS are checked because they are what people get wrong when they
move from writing software to writing control: that the program acts on a frozen
snapshot, that its writes take effect a scan later, and that a signal which
changes twice inside a scan is seen once. A simulation that quietly gives the
program live data would let a control bug pass here and fail on the machine.

The SAFETY BEHAVIOUR is checked because it is the part with a wrong answer that
hurts somebody: torque is the safety channel's to give, not the program's to
take, and closing a guard must not by itself start the line.
"""

from __future__ import annotations

import pytest

from vpc.cell import CAP_SCANS, FILL_SCANS, Cell
from vpc.process_image import Coil, Discrete, InputRegister, ProcessImage


def run(cell: Cell, scans: int, **coils: bool) -> ProcessImage:
    """Hold a fixed set of outputs for n scans, returning the last inputs."""
    image = ProcessImage()
    for name, value in coils.items():
        image.coils[Coil[name]] = value
    for _ in range(scans):
        image = cell.scan(image)
        for name, value in coils.items():
            image.coils[Coil[name]] = value
    return image


# --- scan semantics ----------------------------------------------------------
def test_the_plant_acts_on_the_outputs_written_before_the_scan() -> None:
    """The program's writes take effect on the NEXT scan, never this one."""
    cell = Cell()
    image = ProcessImage()
    image.coils[Coil.CONVEYOR_RUN] = True

    result = cell.scan(image)
    # the conveyor ran during this scan, so a product was loaded and is visible
    # in the inputs the PLC will read next scan
    assert result.discretes[Discrete.PRODUCT_AT_FILLER]


@pytest.mark.verifies("SR-12")
def test_inputs_are_a_snapshot_and_not_a_live_view() -> None:
    """Mutating the returned image must not reach back into the plant."""
    cell = Cell()
    image = cell.scan(ProcessImage())
    before = cell.produced

    image.registers[InputRegister.PRODUCED] = 9999
    image.discretes[Discrete.SAFETY_OK] = False

    assert cell.produced == before
    assert cell.torque_available, "writing to the image changed the plant"


@pytest.mark.verifies("SR-05")
def test_a_process_image_missing_a_signal_is_refused() -> None:
    """An absent key reads as de-energised, which is the dangerous default.

    For SAFETY_OK and GUARD_CLOSED, de-energised means unsafe, so a missing
    entry would look like a healthy input on a machine with no guard.
    """
    import pytest

    with pytest.raises(ValueError, match="missing"):
        ProcessImage(discretes={Discrete.PRODUCT_AT_FILLER: False})


# --- the line runs, blocks and starves --------------------------------------
def test_a_bottle_is_filled_capped_and_completed() -> None:
    cell = Cell()
    image = ProcessImage()
    image.coils[Coil.CONVEYOR_RUN] = True

    for _ in range(40):
        image = cell.scan(image)
        image.coils[Coil.CONVEYOR_RUN] = True
        image.coils[Coil.FILLER_DOSE] = image.discretes[Discrete.PRODUCT_AT_FILLER]
        image.coils[Coil.CAPPER_ACTUATE] = image.discretes[Discrete.PRODUCT_AT_CAPPER]

    assert cell.produced > 0, "nothing reached the outfeed"
    assert all(p.filled and p.capped for p in cell.completed), (
        "a product completed without being filled and capped"
    )


def test_a_station_holds_its_product_until_its_work_is_done() -> None:
    """Which is what makes the line block, rather than behave like a queue."""
    cell = Cell()
    run(cell, 1, CONVEYOR_RUN=True)
    first = cell.at_filler
    assert first is not None

    # conveyor running, but the filler never doses, so nothing may advance
    run(cell, 10, CONVEYOR_RUN=True)
    assert cell.at_filler is first, "an unfilled bottle left the filler"
    assert cell.produced == 0


def test_the_line_starves_when_the_infeed_has_nothing() -> None:
    cell = Cell()
    cell.infeed_starved = True
    run(cell, 10, CONVEYOR_RUN=True)
    assert cell.at_filler is None
    assert cell.produced == 0


def test_filling_takes_the_scans_it_is_supposed_to() -> None:
    cell = Cell()
    run(cell, 1, CONVEYOR_RUN=True)
    for _ in range(FILL_SCANS - 1):
        run(cell, 1, FILLER_DOSE=True)
        assert cell.at_filler is not None and not cell.at_filler.filled
    run(cell, 1, FILLER_DOSE=True)
    assert cell.at_filler is not None and cell.at_filler.filled


def test_capping_takes_the_scans_it_is_supposed_to() -> None:
    cell = Cell()
    run(cell, 1, CONVEYOR_RUN=True)
    run(cell, FILL_SCANS, FILLER_DOSE=True)
    run(cell, 1, CONVEYOR_RUN=True)
    assert cell.at_capper is not None
    for _ in range(CAP_SCANS - 1):
        run(cell, 1, CAPPER_ACTUATE=True)
        assert not cell.at_capper.capped
    run(cell, 1, CAPPER_ACTUATE=True)
    assert cell.at_capper.capped


# --- QC and the reject path --------------------------------------------------
def test_an_unfinished_bottle_fails_qc() -> None:
    """QC judges what actually happened to the bottle, not a coin flip."""
    cell = Cell()
    run(cell, 1, CONVEYOR_RUN=True)
    # push it through without ever filling or capping
    cell.at_qc = cell.at_filler
    cell.at_filler = None
    image = cell.scan(ProcessImage())
    assert image.discretes[Discrete.QC_FAIL]


def test_the_reject_path_removes_the_product_and_counts_it() -> None:
    cell = Cell()
    run(cell, 1, CONVEYOR_RUN=True)
    cell.at_qc = cell.at_filler
    cell.at_filler = None

    run(cell, 1, REJECT_EJECT=True)
    assert cell.at_qc is None
    assert cell.rejected == 1
    assert cell.produced == 0, "a rejected bottle was counted as produced"


def test_qc_reports_nothing_when_the_station_is_empty() -> None:
    cell = Cell()
    image = cell.scan(ProcessImage())
    assert not image.discretes[Discrete.QC_FAIL]


# --- the safety channel, which the PLC cannot overrule -----------------------
@pytest.mark.verifies("SR-01")
def test_the_conveyor_cannot_move_while_torque_is_withheld() -> None:
    """The program asking is not the same as the machine being allowed."""
    cell = Cell()
    cell.open_guard()

    run(cell, 20, CONVEYOR_RUN=True)

    assert cell.at_filler is None, "the line moved with the guard open"
    assert cell.produced == 0


@pytest.mark.verifies("SR-02")
def test_closing_the_guard_does_not_restart_the_line() -> None:
    """A machine that moves the moment a door shuts moves while somebody is inside.

    Restoring torque has to be a separate deliberate act, which is why
    close_guard and reset_safety are two calls and not one.
    """
    cell = Cell()
    cell.open_guard()
    cell.close_guard()

    image = run(cell, 5, CONVEYOR_RUN=True)

    assert not image.discretes[Discrete.SAFETY_OK]
    assert cell.at_filler is None, "the line restarted when the guard closed"


@pytest.mark.verifies("SR-03")
def test_safety_cannot_be_reset_while_the_guard_is_open() -> None:
    cell = Cell()
    cell.open_guard()
    assert not cell.reset_safety()
    assert not cell.torque_available


def test_safety_resets_once_the_guard_is_closed_and_the_line_runs_again() -> None:
    cell = Cell()
    cell.open_guard()
    cell.close_guard()
    assert cell.reset_safety()

    image = run(cell, 3, CONVEYOR_RUN=True)
    assert image.discretes[Discrete.SAFETY_OK]
    assert cell.at_filler is not None


def test_the_safety_signals_are_reported_positively() -> None:
    """De-energise to trip: a lost input must read as unsafe.

    Both are named for the safe condition, SAFETY_OK and GUARD_CLOSED, so that
    a broken wire or a dead link reads False and stops the machine. Naming them
    the other way round would make a disconnected input look like a closed guard.
    """
    from vpc.process_image import FAIL_SAFE_LOW

    cell = Cell()
    healthy = cell.scan(ProcessImage())
    for signal in FAIL_SAFE_LOW:
        assert healthy.discretes[signal], (
            f"{signal.name} is False on a healthy cell, so its polarity is "
            f"inverted and a lost input would read as safe"
        )

    cell.open_guard()
    unsafe = cell.scan(ProcessImage())
    for signal in FAIL_SAFE_LOW:
        assert not unsafe.discretes[signal]


# --- counters ----------------------------------------------------------------
def test_counters_are_published_for_the_supervisory_layer() -> None:
    cell = Cell()
    image = run(cell, 5, CONVEYOR_RUN=True)
    assert image.registers[InputRegister.SCAN_COUNT] == 5
    assert image.registers[InputRegister.PRODUCED] == cell.produced
    assert image.registers[InputRegister.REJECTED] == cell.rejected


# --- the address map, which both sides are written against -------------------
def test_the_address_map_is_generated_from_one_source() -> None:
    """Two copies of an address map is one copy plus a future defect.

    The PLC end is configured with raw Modbus addresses and the plant end uses
    names. If those ever disagree, both halves are individually correct and
    jointly useless, and the symptom is a machine that does the wrong thing for
    no visible reason.
    """
    from vpc.process_image import signal_map

    mapping = signal_map()
    assert mapping["coils"]["CONVEYOR_RUN"] == int(Coil.CONVEYOR_RUN)
    assert mapping["discrete_inputs"]["SAFETY_OK"] == int(Discrete.SAFETY_OK)
    assert mapping["input_registers"]["PRODUCED"] == int(InputRegister.PRODUCED)

    for group, enum_type in (("coils", Coil), ("discrete_inputs", Discrete),
                             ("input_registers", InputRegister)):
        assert set(mapping[group]) == {m.name for m in enum_type}
        assert len(set(mapping[group].values())) == len(mapping[group]), (
            f"two {group} share an address"
        )


def test_the_structured_text_declarations_cover_every_signal() -> None:
    """Generated for the PLC side, so a drift shows up as a diff.

    The declarations are emitted from the same enums the plant uses, which is
    the only way to keep one address map rather than two.
    """
    from vpc.process_image import structured_text_declarations

    text = structured_text_declarations()
    for signal in (*Coil, *Discrete, *InputRegister):
        assert signal.name in text, f"{signal.name} is missing from the ST declarations"

    assert text.startswith("VAR_GLOBAL")
    assert text.rstrip().endswith("END_VAR")
    # outputs at %Q, inputs at %I, which is the IEC 61131-3 convention and the
    # thing a PLC engineer will check first
    assert f"CONVEYOR_RUN AT %QX0.{int(Coil.CONVEYOR_RUN)}" in text
    assert f"SAFETY_OK AT %IX0.{int(Discrete.SAFETY_OK)}" in text
    assert f"PRODUCED AT %IW{int(InputRegister.PRODUCED)}" in text


# --- the safety channel answering the PLC's reset request ---------------------
# The PLC can ASK for torque back. Until this existed the coil went nowhere: the
# program computed SAFETY_RESET_REQUEST and the plant read four coils and
# ignored the fifth, so the documented recovery path could not be exercised at
# all and the safety story was untestable on a running cell.
def scan_with(cell: Cell, image: ProcessImage, **coils: bool) -> ProcessImage:
    for name, value in coils.items():
        image.coils[Coil[name]] = value
    return cell.scan(image)


@pytest.mark.verifies("SR-03")
def test_a_reset_request_restores_torque_once_the_guard_is_closed() -> None:
    cell = Cell()
    image = ProcessImage()
    cell.open_guard()
    cell.close_guard()
    assert not cell.torque_available, "closing the guard restored torque by itself"

    image = scan_with(cell, image, SAFETY_RESET_REQUEST=True)

    assert cell.torque_available


@pytest.mark.verifies("SR-03")
def test_a_reset_request_with_the_guard_still_open_does_nothing() -> None:
    """The safety channel checks the guard itself rather than trusting the PLC.

    The control program already refuses to ask while the guard is open, so this
    is the second of two independent checks. A safety function that relies on
    the thing it is protecting against to behave is not a safety function.
    """
    cell = Cell()
    image = ProcessImage()
    cell.open_guard()

    image = scan_with(cell, image, SAFETY_RESET_REQUEST=True)

    assert not cell.torque_available


@pytest.mark.verifies("SR-03")
def test_a_held_reset_does_not_restart_the_machine_when_the_guard_closes() -> None:
    """The property that makes this a manual reset rather than an interlock.

    An operator who wedges the reset button and then closes the door has
    performed one action, and a restart needs two: closing the guard and a
    deliberate reset. Level triggering would collapse those into one and hand
    back torque the instant the door shut, which is the exact behaviour
    close_guard() refuses to have.
    """
    cell = Cell()
    image = ProcessImage()
    cell.open_guard()

    # request goes and stays high while the guard is still open
    image = scan_with(cell, image, SAFETY_RESET_REQUEST=True)
    assert not cell.torque_available

    cell.close_guard()
    image = scan_with(cell, image, SAFETY_RESET_REQUEST=True)

    assert not cell.torque_available, "a held reset restarted the machine"


@pytest.mark.verifies("SR-03")
def test_releasing_and_pressing_again_does_restore_torque() -> None:
    """The control case. Without it the test above passes on a cell that has
    simply broken its reset."""
    cell = Cell()
    image = ProcessImage()
    cell.open_guard()
    image = scan_with(cell, image, SAFETY_RESET_REQUEST=True)
    cell.close_guard()
    image = scan_with(cell, image, SAFETY_RESET_REQUEST=True)
    assert not cell.torque_available

    image = scan_with(cell, image, SAFETY_RESET_REQUEST=False)
    image = scan_with(cell, image, SAFETY_RESET_REQUEST=True)

    assert cell.torque_available


@pytest.mark.verifies("SR-11")
def test_a_failing_product_must_be_ejected_in_the_scan_it_is_at_qc() -> None:
    """The dwell at QC is exactly one scan, and that is the whole requirement.

    A controller that computes REJECT_EJECT from inputs it read one scan ago has
    already lost: the product advanced to the outfeed at the boundary. The
    failure is silent, because the product ships, the reject counter does not
    move and nothing reports an error.

    Measured both ways when this was found: an in-scan controller rejects 2 of
    18 products, and the same logic driven slower than the scan rejected 3 of
    191 and looked, from every counter available to it, like excellent quality.
    """
    cell = Cell()
    image = ProcessImage()

    # a controller that acts within the scan, as the PLC task does
    for _ in range(60):
        image.coils[Coil.CONVEYOR_RUN] = True
        image.coils[Coil.FILLER_DOSE] = image.discretes[Discrete.PRODUCT_AT_FILLER]
        image.coils[Coil.CAPPER_ACTUATE] = image.discretes[Discrete.PRODUCT_AT_CAPPER]
        image.coils[Coil.REJECT_EJECT] = (image.discretes[Discrete.PRODUCT_AT_QC]
                                          and image.discretes[Discrete.QC_FAIL])
        image = cell.scan(image)

    assert cell.rejected > 0, "no failing product was ever ejected"
    assert cell.ejected, "the reject path produced no ejected product"
    for product in cell.completed:
        assert product.good, "a defective product reached the outfeed"
