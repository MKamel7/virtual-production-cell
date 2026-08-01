"""Push the repository's ST into an existing CODESYS project, touching nothing else.

Run with the project CLOSED in the IDE, from the repository root:

    "E:\\CODESYS\\CODESYS\\Common\\CODESYS.exe" ^
        --profile="CODESYS V3.5 SP22 Patch 3" --noUI ^
        --runscript="plc\\codesys\\sync_program.py"

WHY THIS EXISTS, and it is the tension at the centre of this project.

`cell.project` holds a COPY of the control program. `build_project.py` puts it
there once, and from then on editing `plc/cell_control.st` changes the file the
tests parse and does NOT change the program that runs on the PLC. Two copies of
the program, one of which is verified and one of which executes. That is exactly
the defect this repository refuses to have anywhere else.

Rebuilding the project would fix it and destroy the Modbus channel configuration,
which is not scriptable and was entered by hand. So this script does the narrow
thing instead: it replaces the text of the POU and the global variable list from
the files that own them, and leaves the device tree, the channels, the mapping
and the task configuration exactly as they are.

The rule that falls out: **`plc/cell_control.st` is the source, the project is a
cache of it.** Edit the file, run this, download. Never edit the POU in the IDE,
because `tests/test_st_matches_the_model.py` parses the file and would happily
pass while the PLC ran something else entirely.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTDIR = os.path.join(ROOT, "plc", "codesys")
PROJ = os.path.join(OUTDIR, "cell.project")

log = open(os.path.join(OUTDIR, "sync.log"), "w")

def say(*bits):
    log.write(" ".join(str(b) for b in bits) + "\n")
    log.flush()

if not os.path.exists(PROJ):
    say("REFUSED: %s does not exist. Run build_project.py first." % PROJ)
    log.close()
    raise SystemExit(1)

st = open(os.path.join(ROOT, "plc", "cell_control.st")).read()
end_var = st.index("END_VAR") + len("END_VAR")
declaration = st[:end_var]
implementation = st[end_var:st.rindex("END_PROGRAM")].strip("\r\n")

io = open(os.path.join(ROOT, "plc", "io_declarations.st")).read()
gvl_declaration = re.sub(r"\s+AT\s+%\S+", "", io)

proj = projects.open(PROJ)

pou = proj.find("CellControl", True)[0]
pou.textual_declaration.replace(declaration)
pou.textual_implementation.replace(implementation)
say("CellControl updated:", pou.textual_implementation.linecount, "lines of body")

gvl = proj.find("IO", True)[0]
gvl.textual_declaration.replace(gvl_declaration)
say("IO updated:", gvl.textual_declaration.linecount, "lines")

# Proof that the narrow thing stayed narrow. If the device tree changed, the
# channel configuration is at risk and the person running this needs to know
# before they download rather than after the cell stops exchanging IO.
plant = proj.find("Plant", True)
say("device tree intact:", len(plant) == 1 and len(proj.find("Modbus_TCP_Client", True)) == 1)

proj.save()
proj.close()
say("saved. Channels, mapping and task configuration untouched.")
log.close()
