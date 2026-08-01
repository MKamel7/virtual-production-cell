"""The plant: a conveyor with three stations, advanced one PLC scan at a time.

This is the thing the PLC program is tested against. It is deliberately not
clever. Its job is to be DETERMINISTIC and to reproduce the timing semantics of
real IO, so that a control program which works here fails here too, for the same
reasons it would fail on a machine.

The cell is a bottling line in miniature:

    infeed -> [filler] -> [capper] -> [QC] -> outfeed
                                        \\-> reject

Products advance one position per scan while the conveyor is running. A station
holds the product in front of it until its own work is done, which is what makes
the line block and starve rather than behaving like a queue that never fills.

WHAT IS AND IS NOT MODELLED. Positions are discrete and a product occupies one
station at a time; there is no continuous belt position, no product length, no
acceleration. Fill volume is a scan count rather than a litre. None of that
changes the control problem, which is sequencing, interlocking and handling the
cases where the line does not behave.

The one piece of physics that IS modelled carefully is the safety chain, because
that is the part with a wrong answer that matters: torque availability is
decided by the safety channel and not by the PLC, and the conveyor cannot move
while it is withheld regardless of what the program asks for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vpc.process_image import Coil, Discrete, InputRegister, ProcessImage

#: Scans a station needs to do its work. Chosen, not measured: these stand in
#: for equipment timing and exist so the line can block and starve, which is the
#: behaviour the control program has to handle.
FILL_SCANS = 3
CAP_SCANS = 2

#: One product in this many fails QC. Deterministic rather than random, because
#: a campaign that cannot be reproduced exactly is not evidence. A rate of 7
#: means the reject path is exercised often enough to matter and rarely enough
#: that the line is not mostly rejecting.
QC_FAIL_EVERY = 7


@dataclass
class Product:
    """One bottle. Carries what happened to it, so QC has something to judge."""

    serial: int
    filled: bool = False
    capped: bool = False

    @property
    def good(self) -> bool:
        return self.filled and self.capped


@dataclass
class Cell:
    """The plant. Advanced by `scan()`, never by a clock.

    Time is scans. There is no wall clock anywhere in this class, which is what
    lets a scenario be replayed exactly and a failure be reduced to a minimal
    reproduction.
    """

    #: Station slots, in flow order. None means empty.
    at_filler: Product | None = None
    at_capper: Product | None = None
    at_qc: Product | None = None

    fill_progress: int = 0
    cap_progress: int = 0

    produced: int = 0
    rejected: int = 0
    scans: int = 0
    next_serial: int = 1

    #: Set by the safety channel, never by the PLC. False means torque is not
    #: available whatever the program asks for.
    torque_available: bool = True
    guard_closed: bool = True

    #: Products that reached the outfeed, for assertions in tests.
    completed: list[Product] = field(default_factory=list)
    ejected: list[Product] = field(default_factory=list)

    #: Set while the infeed has no product to offer, so the line starves.
    infeed_starved: bool = False

    #: Last state of the PLC's reset request, for edge detection. See
    #: `_service_reset_request` for why the edge matters rather than the level.
    reset_was_requested: bool = False

    def scan(self, image: ProcessImage) -> ProcessImage:
        """Advance one PLC scan and return the inputs for the next one.

        The order here is the order a real cycle runs in: the plant acts on the
        outputs the PLC wrote LAST scan, then publishes fresh inputs. Nothing
        the PLC writes this scan is visible to it until the next one, which is
        the property the process image exists to preserve.
        """
        self.scans += 1
        self._act_on_outputs(image)
        return self._publish_inputs(image)

    # ---- the plant reacts to what the PLC asked for last scan --------------
    def _act_on_outputs(self, image: ProcessImage) -> None:
        # The safety channel reads the reset REQUEST before anything else, since
        # whether torque is available decides what the rest of this scan can do.
        self._service_reset_request(image.coils[Coil.SAFETY_RESET_REQUEST])

        # Torque next. The safety channel outranks the program, so a conveyor
        # command is simply ignored when torque is withheld rather than being
        # allowed to move the line and be corrected afterwards.
        moving = image.coils[Coil.CONVEYOR_RUN] and self.torque_available

        if image.coils[Coil.FILLER_DOSE] and self.at_filler is not None:
            self.fill_progress += 1
            if self.fill_progress >= FILL_SCANS:
                self.at_filler.filled = True

        if image.coils[Coil.CAPPER_ACTUATE] and self.at_capper is not None:
            self.cap_progress += 1
            if self.cap_progress >= CAP_SCANS:
                self.at_capper.capped = True

        if image.coils[Coil.REJECT_EJECT] and self.at_qc is not None:
            self.ejected.append(self.at_qc)
            self.rejected += 1
            self.at_qc = None

        if moving:
            self._advance()

    def _advance(self) -> None:
        """Shift products one station along, downstream first.

        Downstream first so a product only moves into a slot that has already
        been vacated this scan. Iterating the other way would let two products
        occupy one station for an instant, which is the kind of thing that never
        shows up until a test asserts a count.
        """
        if self.at_qc is not None:
            self.completed.append(self.at_qc)
            self.produced += 1
            self.at_qc = None

        if self.at_capper is not None and self._capper_done():
            self.at_qc = self.at_capper
            self.at_capper = None
            self.cap_progress = 0

        if self.at_filler is not None and self._filler_done() and self.at_capper is None:
            self.at_capper = self.at_filler
            self.at_filler = None
            self.fill_progress = 0

        if self.at_filler is None and not self.infeed_starved:
            self.at_filler = Product(serial=self.next_serial)
            self.next_serial += 1

    def _filler_done(self) -> bool:
        return self.at_filler is not None and self.at_filler.filled

    def _capper_done(self) -> bool:
        return self.at_capper is not None and self.at_capper.capped

    # ---- the plant publishes what the PLC will read next scan --------------
    def _publish_inputs(self, image: ProcessImage) -> ProcessImage:
        nxt = image.copy()
        nxt.discretes[Discrete.PRODUCT_AT_FILLER] = self.at_filler is not None
        nxt.discretes[Discrete.PRODUCT_AT_CAPPER] = self.at_capper is not None
        nxt.discretes[Discrete.PRODUCT_AT_QC] = self.at_qc is not None
        nxt.discretes[Discrete.FILLER_BUSY] = (
            self.at_filler is not None and not self.at_filler.filled)
        nxt.discretes[Discrete.CAPPER_BUSY] = (
            self.at_capper is not None and not self.at_capper.capped)
        nxt.discretes[Discrete.QC_FAIL] = self._qc_verdict()
        nxt.discretes[Discrete.SAFETY_OK] = self.torque_available
        nxt.discretes[Discrete.GUARD_CLOSED] = self.guard_closed

        nxt.registers[InputRegister.PRODUCED] = self.produced
        nxt.registers[InputRegister.REJECTED] = self.rejected
        nxt.registers[InputRegister.SCAN_COUNT] = self.scans
        return nxt

    def _qc_verdict(self) -> bool:
        """True means the product at QC has FAILED.

        Judged on what actually happened to the bottle, plus a deterministic
        every-nth failure standing in for the defects a real inspection catches
        that the model does not simulate.
        """
        if self.at_qc is None:
            return False
        if not self.at_qc.good:
            return True
        return self.at_qc.serial % QC_FAIL_EVERY == 0

    def _service_reset_request(self, requested: bool) -> None:
        """The safety channel's answer to the PLC asking for torque back.

        Torque is restored on a RISING edge and never on a level, and that is a
        safety requirement rather than a style preference. A reset that is held
        down must not re-enable the machine the instant a guard closes: ISO
        13849 wants a manual reset to be a separate deliberate action, and one
        held button covering both the closing of the door and the restart is one
        action, not two. So an operator who wedges the reset gets nothing until
        they release it and press again.

        The request is still only ever an ASK. This method is the safety channel
        deciding, which is why it lives here and not in the control program, and
        why it checks the guard itself rather than trusting the PLC to have.
        """
        rising = requested and not self.reset_was_requested
        self.reset_was_requested = requested
        if rising and self.guard_closed:
            self.torque_available = True

    # ---- the safety channel, which the PLC cannot overrule -----------------
    def open_guard(self) -> None:
        """Guard door opened, per ISO 14119. Torque goes with it."""
        self.guard_closed = False
        self.torque_available = False

    def close_guard(self) -> None:
        """Closing the guard does NOT restore torque.

        Deliberate, and it is the whole point of a reset being a separate act:
        a machine that starts moving again the moment a door shuts is a machine
        that starts moving while somebody is still inside it.
        """
        self.guard_closed = True

    def reset_safety(self) -> bool:
        """Restore torque, if it is safe to do so. Returns whether it worked."""
        if not self.guard_closed:
            return False
        self.torque_available = True
        return True
