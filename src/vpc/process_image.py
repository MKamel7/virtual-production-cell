"""The process image: the contract between the PLC and the plant.

This is the most important file in the project, and it is deliberately the
dullest. Everything either side of the boundary is written against it: the
Structured Text reads these inputs and writes these outputs, the plant
simulation does the reverse, and the Modbus server is only a way of moving the
bytes. Get the addresses wrong and both halves are individually correct and
jointly useless.

WHY A PROCESS IMAGE AT ALL, rather than function calls between objects. Because
that is what makes this a PLC rather than an event loop. A real PLC does not
observe the plant continuously: it copies every input into a buffer, runs the
whole program against that frozen snapshot, then writes every output at once.
Consequences that fall out of it and that a callback based simulation would
hide:

  A signal that changes twice within a scan is seen once, or not at all.
  Nothing the program writes takes effect until the scan ends.
  The program always acts on data that is already up to one scan old.

Those are the semantics people get wrong when they move from writing software to
writing control, so the simulation reproduces them rather than papering over
them.

ADDRESSING follows Modbus, because that is the carrier and the PLC end is
configured with raw addresses. Names live here so nothing else has to hardcode a
number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Coil(IntEnum):
    """Outputs. The PLC writes these; the plant obeys them.

    Modbus coils, read/write from the PLC's point of view.
    """

    CONVEYOR_RUN = 0
    FILLER_DOSE = 1
    CAPPER_ACTUATE = 2
    REJECT_EJECT = 3
    #: Requests the safety channel release the drives. Not itself a safety
    #: function: the safety channel decides, and this is only the ask.
    SAFETY_RESET_REQUEST = 4


class Discrete(IntEnum):
    """Inputs. The plant writes these; the PLC reads them.

    Modbus discrete inputs, read only from the PLC's point of view.
    """

    PRODUCT_AT_FILLER = 0
    PRODUCT_AT_CAPPER = 1
    PRODUCT_AT_QC = 2
    FILLER_BUSY = 3
    CAPPER_BUSY = 4
    #: The QC verdict for whatever is currently at the QC station. Only
    #: meaningful while PRODUCT_AT_QC is set.
    QC_FAIL = 5
    #: Safety channel healthy. Low means torque is not available, whatever the
    #: PLC asks for. The PLC may read it but cannot clear it.
    SAFETY_OK = 6
    #: Guard door closed and locked, per ISO 14119.
    GUARD_CLOSED = 7


class InputRegister(IntEnum):
    """Counters and measurements the plant reports."""

    PRODUCED = 0
    REJECTED = 1
    #: Scans elapsed. Exposed so the supervisory layer can compute rates without
    #: assuming a wall clock the simulation does not have.
    SCAN_COUNT = 2


@dataclass
class ProcessImage:
    """One scan's worth of IO, frozen at the scan boundary.

    Held as booleans and integers rather than raw words on purpose. The Modbus
    layer converts; nothing else should have to think about bit packing, and a
    bug in packing should not be able to masquerade as a control bug.
    """

    coils: dict[Coil, bool] = field(
        default_factory=lambda: dict.fromkeys(Coil, False))
    discretes: dict[Discrete, bool] = field(
        default_factory=lambda: dict.fromkeys(Discrete, False))
    registers: dict[InputRegister, int] = field(
        default_factory=lambda: dict.fromkeys(InputRegister, 0))

    def copy(self) -> ProcessImage:
        """A snapshot, which is what the PLC actually executes against."""
        return ProcessImage(dict(self.coils), dict(self.discretes),
                            dict(self.registers))

    def __post_init__(self) -> None:
        # Every signal must be present. A missing key would read as False
        # through .get and look like a de-energised input, which is the most
        # dangerous possible default for a safety signal: it fails towards
        # "everything is fine" for GUARD_CLOSED and SAFETY_OK.
        for enum_type, mapping in ((Coil, self.coils), (Discrete, self.discretes),
                                   (InputRegister, self.registers)):
            missing = set(enum_type) - set(mapping)
            if missing:
                raise ValueError(
                    f"process image is missing {sorted(m.name for m in missing)}; "
                    f"an absent signal reads as de-energised and would look like "
                    f"a healthy input for the safety signals")


def signal_map() -> dict[str, dict[str, int]]:
    """The address map, for generating PLC side declarations and documentation.

    Emitted from the enums rather than maintained separately, because two copies
    of an address map is one copy plus a future defect.
    """
    return {
        "coils": {member.name: int(member) for member in Coil},
        "discrete_inputs": {member.name: int(member) for member in Discrete},
        "input_registers": {member.name: int(member) for member in InputRegister},
    }


def structured_text_declarations() -> str:
    """The IO declarations for the PLC program, generated from the same enums.

    Generated rather than written by hand for the same reason: the address map
    exists once. A reviewer can diff this against the ST in the repository and a
    drift shows up immediately.
    """
    lines = ["VAR_GLOBAL", "    (* Outputs: written by the PLC *)"]
    for coil in Coil:
        lines.append(f"    {coil.name} AT %QX0.{int(coil)} : BOOL;")
    lines.append("    (* Inputs: written by the plant *)")
    for discrete in Discrete:
        lines.append(f"    {discrete.name} AT %IX0.{int(discrete)} : BOOL;")
    for register in InputRegister:
        lines.append(f"    {register.name} AT %IW{int(register)} : WORD;")
    lines.append("END_VAR")
    return "\n".join(lines)


#: Signals whose de-energised state must mean "unsafe", so that a broken wire,
#: a dead PLC or a lost network link fails towards stopping the machine.
#:
#: This is the de-energise to trip principle and it is why both are named
#: positively: GUARD_CLOSED rather than GUARD_OPEN, SAFETY_OK rather than
#: SAFETY_FAULT. Naming them the other way round would make a disconnected input
#: read as a closed guard on a healthy machine.
FAIL_SAFE_LOW: frozenset[Discrete] = frozenset({
    Discrete.SAFETY_OK, Discrete.GUARD_CLOSED,
})
