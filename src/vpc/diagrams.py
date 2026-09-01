"""Two pictures of the things prose is worst at: a scan cycle and a state machine.

WHY THESE TWO. The process image semantics are the most sophisticated part of
this project and the easiest to skim past. The whole point is that the PLC
executes against a SNAPSHOT taken at the scan boundary, not against live plant
state, so an input changing mid-scan is not seen until the next one. That is
one sentence in the README and a diagram in every PLC textbook, for the good
reason that the ordering is the content. The PackML state machine has the same
problem in a different shape: seventeen states and two kinds of transition,
which is a table nobody reads and a picture anybody can.

WHY THEY ARE GENERATED. Both are drawn from the code they describe: the scan
cycle from `vpc.process_image`, the state machine from `vpc.packml`. A drawn
diagram is a second place for the same facts and it drifts silently, because
unlike a number in a README nobody diffs a picture. CI regenerates these and
fails on a diff, which is the same rule the traceability matrix already lives
under.

The state diagram in particular is worth generating rather than copying from
the standard: this project's claim is that its implementation IS PackML, and a
picture traced from the specification would be a picture of the specification,
proving nothing about the code. Drawing it from `ON_COMPLETE` and `COMMANDS`
means a wrong transition shows up in the diagram.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from vpc import packml
from vpc.process_image import Coil, Discrete, InputRegister

INK = "#e8eaee"
MUTED = "#9aa3af"
LINE = "#39404d"
PAPER = "#11141a"
PANEL = "#1b1f27"

#: The five stages of one scan, in the order they happen. The order IS the
#: content: the PLC runs against a snapshot, so anything the plant does during
#: stage 2 is invisible until the next stage 1.
SCAN_STAGES = (
    ("1. Input snapshot", "the plant's inputs are frozen",
     "ProcessImage.copy()"),
    ("2. PLC program", "runs against the snapshot only",
     "cell logic + PackML"),
    ("3. Output image", "coils written, not yet applied",
     "ProcessImage.coils"),
    ("4. Modbus exchange", "the only wire between the two",
     "server.py, port 502"),
    ("5. Plant step", "physics advances one period",
     "Cell.step()"),
)


def _header(width: int, title: str, subtitle: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{{height}}" viewBox="0 0 {width} {{height}}" '
        f'font-family="Segoe UI, Helvetica, Arial, sans-serif">',
        f'<rect width="100%" height="100%" fill="{PAPER}"/>',
        f'<text x="24" y="32" fill="{INK}" font-size="17" font-weight="600">'
        f'{escape(title)}</text>',
        f'<text x="24" y="52" fill="{MUTED}" font-size="12">'
        f'{escape(subtitle)}</text>',
    ]


def render_scan_cycle() -> str:
    """One scan, and why the snapshot is the whole idea."""
    width, box_w, box_h, gap = 980, 172, 84, 20
    top = 96
    height = top + box_h + 190

    out = _header(width, "One scan of the cell",
                  "Generated from vpc.process_image. The PLC never sees the "
                  "plant directly; it sees a snapshot taken at stage 1.")

    x = 24
    for index, (title, why, where) in enumerate(SCAN_STAGES):
        out.append(f'<rect x="{x}" y="{top}" width="{box_w}" height="{box_h}" '
                   f'rx="6" fill="{PANEL}" stroke="{LINE}"/>')
        out.append(f'<text x="{x + 12}" y="{top + 24}" fill="{INK}" '
                   f'font-size="12" font-weight="600">{escape(title)}</text>')
        out.append(f'<text x="{x + 12}" y="{top + 44}" fill="{MUTED}" '
                   f'font-size="10">{escape(why)}</text>')
        out.append(f'<text x="{x + 12}" y="{top + 64}" fill="#6f7784" '
                   f'font-size="10" font-style="italic">{escape(where)}</text>')
        if index < len(SCAN_STAGES) - 1:
            mid = top + box_h // 2
            out.append(f'<path d="M{x + box_w} {mid} L{x + box_w + gap - 4} {mid}" '
                       f'stroke="{MUTED}" stroke-width="1.5" '
                       f'marker-end="url(#arrow)"/>')
        x += box_w + gap

    # The wrap-around, which is the part that makes it a cycle rather than a
    # pipeline, and the reason a mid-scan input change is not an input change.
    last_x = 24 + (len(SCAN_STAGES) - 1) * (box_w + gap)
    loop_y = top + box_h + 42
    out.append(f'<path d="M{last_x + box_w // 2} {top + box_h} '
               f'L{last_x + box_w // 2} {loop_y} L{24 + box_w // 2} {loop_y} '
               f'L{24 + box_w // 2} {top + box_h}" fill="none" stroke="{MUTED}" '
               f'stroke-width="1.5" stroke-dasharray="4 3" '
               f'marker-end="url(#arrow)"/>')
    out.append(f'<text x="{width // 2 - 150}" y="{loop_y + 18}" fill="{MUTED}" '
               f'font-size="11">next scan. An input that changes during stages '
               f'2 to 5 is not seen until here.</text>')

    counts = (f"{len(Coil)} coils out, {len(Discrete)} discrete inputs, "
              f"{len(InputRegister)} input registers")
    out.append(f'<text x="24" y="{loop_y + 54}" fill="{MUTED}" font-size="11">'
               f'{escape(counts)}, exchanged over Modbus TCP each scan.</text>')
    out.append(f'<text x="24" y="{loop_y + 74}" fill="#6f7784" font-size="10">'
               f'Signal names and addresses come from vpc.process_image, so this '
               f'count cannot disagree with the address map.</text>')

    out.append(_ARROW_DEFS)
    out.append("</svg>")
    return "\n".join(out).replace("{height}", str(height)) + "\n"


_ARROW_DEFS = (
    '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
    'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
    f'<path d="M0 0 L10 5 L0 10 z" fill="{MUTED}"/></marker></defs>'
)


def render_packml() -> str:
    """The seventeen states, and the two different ways they are left.

    Command transitions are what an operator asks for. State-complete
    transitions are what the machine reports when its work is done, and PackML
    is commonly got wrong by modelling the second as a command. Drawing them
    differently is the point of the picture.
    """
    columns = {
        "wait": [s for s in packml.State if s in packml.WAIT],
        "acting": [s for s in packml.State if s in packml.ACTING],
    }
    rows = max(len(v) for v in columns.values())
    width, height = 900, 120 + rows * 46 + 90
    col_x = {"wait": 60, "acting": 520}
    box_w, box_h = 250, 32

    place: dict[packml.State, tuple[int, int]] = {}
    for kind, states in columns.items():
        for row, state in enumerate(states):
            place[state] = (col_x[kind], 120 + row * 46)

    # A state belonging to neither ACTING nor WAIT would simply not be drawn,
    # and the transitions touching it would silently vanish with it, leaving a
    # picture that looks complete and is not. Loud beats guarding each edge.
    unplaced = set(packml.State) - set(place)
    if unplaced:
        raise ValueError(
            f"{sorted(s.value for s in unplaced)} are in neither packml.ACTING "
            f"nor packml.WAIT, so they cannot be drawn and every transition "
            f"through them would disappear from the diagram")

    out = _header(width, "PackML, as implemented",
                  "Generated from vpc.packml. Solid arrows are commands "
                  "somebody sends; dashed arrows are the machine reporting its "
                  "own work complete.")
    out.append('<text x="60" y="106" fill="#6f7784" font-size="11" '
               'letter-spacing="0.08em">WAIT STATES</text>')
    out.append('<text x="520" y="106" fill="#6f7784" font-size="11" '
               'letter-spacing="0.08em">ACTING STATES</text>')

    def edge(a: packml.State, b: packml.State, dashed: bool, label: str) -> str:
        ax, ay = place[a]
        bx, by = place[b]
        start_x = ax + box_w if ax < bx else ax
        end_x = bx if ax < bx else bx + box_w
        y1, y2 = ay + box_h // 2, by + box_h // 2
        mid = (start_x + end_x) / 2
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        return (f'<path d="M{start_x} {y1} C{mid} {y1} {mid} {y2} {end_x} {y2}" '
                f'fill="none" stroke="{LINE}" stroke-width="1"{dash}>'
                f'<title>{escape(label)}</title></path>')

    for state, target in packml.ON_COMPLETE.items():
        out.append(edge(state, target, True, f"{state.value} state complete"))

    # ABORT and STOP are deliberately not in ON_COMMAND: they are legal from
    # almost everywhere and are handled by exemption sets, so drawing an arrow
    # from every state would bury the diagram in fifteen identical lines. The
    # note below the picture says so rather than the picture pretending they
    # do not exist.
    for (state, command), target in packml.ON_COMMAND.items():
        out.append(edge(state, target, False,
                        f"{command.value} in {state.value}"))

    for state, (x, y) in place.items():
        code = packml.PACKTAGS_CODE[state.value]
        acting = state in packml.ACTING
        fill = "#12313f" if acting else "#1b1f27"
        stroke = "#3f7f9c" if acting else LINE
        out.append(f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" '
                   f'rx="5" fill="{fill}" stroke="{stroke}"/>')
        out.append(f'<text x="{x + 12}" y="{y + 21}" fill="{INK}" '
                   f'font-size="12">{escape(state.value)}</text>')
        out.append(f'<text x="{x + box_w - 12}" y="{y + 21}" fill="{MUTED}" '
                   f'font-size="10" text-anchor="end">PackTags {code}</text>')

    note = (f"{len(packml.State)} states, {len(packml.ON_COMMAND)} explicit "
            f"command transitions, {len(packml.ON_COMPLETE)} state-complete "
            f"transitions. Execute is both: it acts, and it is where product "
            f"is made.")
    out.append(f'<text x="60" y="{height - 62}" fill="{MUTED}" font-size="11">'
               f'{escape(note)}</text>')
    out.append(f'<text x="60" y="{height - 44}" fill="#6f7784" font-size="10">'
               f'ABORT and STOP are not drawn: both are legal from almost every '
               f'state and are handled by exemption sets, so fifteen identical '
               f'arrows would bury the rest.</text>')
    out.append(f'<text x="60" y="{height - 26}" fill="#6f7784" font-size="10">'
               f'There is no "state complete" command, deliberately: completion '
               f'is reported by the machine, never requested.</text>')

    out.append(_ARROW_DEFS)
    out.append("</svg>")
    return "\n".join(out).replace("{height}", str(height)) + "\n"


#: Output file name -> the function that builds it.
DIAGRAMS = {
    "scan-cycle.svg": render_scan_cycle,
    "packml-states.svg": render_packml,
}
