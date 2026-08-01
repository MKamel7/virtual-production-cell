"""Build the CODESYS project from the repository, rather than clicking it once.

Run from the repository root:

    "E:\\CODESYS\\CODESYS\\Common\\CODESYS.exe" ^
        --profile="CODESYS V3.5 SP22 Patch 3" --noUI ^
        --runscript="plc\\codesys\\build_project.py"

This is a CODESYS ScriptEngine script, so it runs under IronPython 2.7 inside
the IDE process and not under the project's own Python. Keep it 2.7 compatible.

WHY THIS EXISTS. The same reason `io_declarations.st` is generated: a hand built
project is a second copy of the address map and the task configuration, kept in
a binary file nobody can diff. Here the project is a build artifact and this
script is the source. Delete `cell.project` and run this to get it back.

WHAT IT DOES NOT DO, stated rather than left to be discovered: the Modbus slave's
IP address and its channel list. Those live in the Modbus device's own editor and
are not reachable through `device_parameters`, which comes back empty for both
the Ethernet adapter and the slave. Four fields and three channels in the GUI,
listed in `docs/WINDOWS_SETUP.md`. Everything else here is reproducible.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTDIR = os.path.join(ROOT, "plc", "codesys")
PROJ = os.path.join(OUTDIR, "cell.project")

log = open(os.path.join(OUTDIR, "build.log"), "w")

def say(*bits):
    line = " ".join(str(b) for b in bits)
    log.write(line + "\n")
    log.flush()

def attempt(label, fn):
    try:
        result = fn()
        say("OK    " + label)
        return result
    except Exception as e:
        say("FAIL  " + label + " -> " + repr(e)[:300])
        return None

# ---- source material, read from the files that already own it --------------
st = open(os.path.join(ROOT, "plc", "cell_control.st")).read()
end_var = st.index("END_VAR") + len("END_VAR")
declaration = st[:end_var]
implementation = st[end_var:st.rindex("END_PROGRAM")].strip("\r\n")

io = open(os.path.join(ROOT, "plc", "io_declarations.st")).read()
# The direct addresses come out. They are the MODBUS addresses, which is what
# makes the generated file readable next to process_image.py, but CODESYS
# assigns its own %I/%Q offsets from the slave's position in the device tree.
# Keeping them would create a second address map that disagrees with the first,
# so the channels are mapped to these variables by name instead.
gvl_declaration = re.sub(r"\s+AT\s+%\S+", "", io)

if os.path.exists(PROJ):
    # Refusing rather than overwriting, because the one thing this script cannot
    # rebuild is the only thing a person put in by hand: the Modbus slave's
    # address and its channel list. Silently recreating the project would throw
    # that away and leave a cell that builds, downloads and exchanges no IO,
    # which looks like a network fault and is not one.
    say("REFUSED: %s already exists." % PROJ)
    say("It carries the Modbus channel configuration, which this script cannot")
    say("rebuild. Delete it deliberately if you want a fresh project.")
    log.close()
    raise SystemExit(1)

# ---- device tree -----------------------------------------------------------
# Device ids come from the repository on this machine, printed by
# device_repository.get_all_devices(). They are pinned rather than searched by
# name so an upgrade that renames a device fails loudly instead of silently
# picking a different one.
proj = projects.create(PROJ)
attempt("PLC: CODESYS Control Win V3 x64",
        lambda: proj.add("Device", DeviceID(4096, "0000 0004", "3.5.22.30")))
plc = proj.find("Device", True)[0]

attempt("Ethernet adapter",
        lambda: plc.add("Ethernet", DeviceID(110, "0000 0002", "4.2.0.0")))
eth = proj.find("Ethernet", True)[0]

attempt("Modbus TCP Client",
        lambda: eth.add("Modbus_TCP_Client", DeviceID(88, "0000 0003", "4.6.0.0")))
client = proj.find("Modbus_TCP_Client", True)[0]

attempt("Modbus TCP Server, the plant",
        lambda: client.add("Plant", DeviceID(89, "0000 0005", "4.6.0.0")))
plant = proj.find("Plant", True)[0]


def set_parameter(device, parameter_id, value, label):
    """Set one device parameter, found by id rather than by name.

    Ids are stable across CODESYS language settings; visible names are not, and a
    script that matched on 'IPAddress' would silently configure nothing on a
    German installation. Missing ids raise rather than pass quietly, because a
    slave left on its default 192.168.0.1 produces a cell that builds cleanly and
    never exchanges a byte.
    """
    for connector in device.connectors:
        try:
            parameters = connector.host_parameters
        except Exception:
            continue
        for parameter in parameters:
            if str(parameter.id) == parameter_id:
                parameter.value = value
                say("OK    %s -> %s" % (label, value))
                return True
    say("FAIL  %s: no parameter with id %s" % (label, parameter_id))
    return False


# The plant runs on this machine, so the slave is loopback. Port 502 is already
# the default and is set explicitly anyway: a default that happens to be right is
# not the same as a decision, and it would move silently if the default changed.
set_parameter(plant, "9102", "[127, 0, 0, 1]", "slave IP address")
set_parameter(plant, "9103", "502", "slave port")
set_parameter(plant, "9100", "1", "slave unit id")

# ---- application objects ---------------------------------------------------
app = proj.find("Application", True)[0]

gvl = attempt("GVL from io_declarations.st", lambda: app.create_gvl("IO"))
if gvl is not None:
    attempt("write GVL", lambda: gvl.textual_declaration.replace(gvl_declaration))

pou = attempt("program CellControl", lambda: app.create_program("CellControl"))
if pou is not None:
    attempt("write declaration", lambda: pou.textual_declaration.replace(declaration))
    attempt("write implementation",
            lambda: pou.textual_implementation.replace(implementation))

# ---- task configuration ----------------------------------------------------
task_config = attempt("task configuration", lambda: app.create_task_configuration())
if task_config is None:
    found = proj.find("Task Configuration", True)
    task_config = found[0] if found else None

if task_config is not None:
    task = attempt("MainTask", lambda: task_config.create_task("MainTask"))
    if task is not None:
        attempt("cyclic", lambda: setattr(task, "kind_of_task", KindOfTask.Cyclic))
        # 20 ms, inside the plant's 50 ms scan. A task slower than the plant
        # scans misses the one scan QC reject window: bad product ships and
        # every counter still reads healthy. docs/WINDOWS_SETUP.md has the
        # measurement.
        attempt("interval 20 ms", lambda: setattr(task, "interval", "20"))
        attempt("priority 1", lambda: setattr(task, "priority", "1"))
        attempt("call CellControl", lambda: task.pous.add("CellControl"))

attempt("save", lambda: proj.save())
say("built:", PROJ, "exists:", os.path.exists(PROJ))
attempt("close", lambda: proj.close())
log.close()
