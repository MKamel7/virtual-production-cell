"""Bring the cell up to Execute and leave the IDE online, for the demo recording.

Run WITH the UI, because the whole point is that a camera can see it:

    "E:\\CODESYS\\CODESYS\\Common\\CODESYS.exe" ^
        --profile="CODESYS V3.5 SP22 Patch 3" ^
        --runscript="plc\\codesys\\demo_online.py"

ScriptEngine, so IronPython 2.7 inside the IDE process. No f-strings.

WHAT THIS DOES, AND WHERE IT DELIBERATELY STOPS. It opens the project, logs in,
starts the task, and issues the commands that take the cell from its power on
state to Execute. Then it exits, leaving the IDE logged in with the declaration
view updating live.

It does NOT drive the rest of the demo. Killing the plant and bringing it back
happens on the plant side, and the controller reacts on its own. That reaction is
the entire argument the recording makes, so scripting the controller through it
would be staging the result rather than showing it.

THE ORDER MATTERS AND IS NOT ARBITRARY:

  safety reset first   SAFETY_OK is an input. The controller cannot grant itself
                       torque, it can only ask, and until torque comes back every
                       actuator is held low no matter what state the machine is
                       in. Reaching Execute first would produce a cell that is
                       nominally producing and physically doing nothing.

  then Clear, Reset,   PackML's own sequence out of Aborted, which is the power
  then Start           on state. Not a workaround: a controller that powers up
                       Idle is one command away from running a machine nobody
                       has reset.
"""
import time

PROJECT = r"E:\Projects\virtual-production-cell\plc\codesys\cell.project"
LOG = r"E:\Projects\virtual-production-cell\plc\codesys\demo.log"

log = open(LOG, "w")


def say(text):
    log.write(str(text) + "\n")
    log.flush()


def read(app_online, expression):
    """A value, or None while the application is not readable yet."""
    try:
        return app_online.read_value(expression)
    except Exception:
        return None


def state_of(app_online):
    """PMLState as a number.

    The online API returns CODESYS typed literals, not bare numbers: PMLState
    comes back as "INT#6" and a counter as "WORD#65". Calling int() on that
    raises, and the first version of this swallowed the error and returned None,
    so every wait below timed out reporting "stuck at None" while the cell was
    in fact stepping through the states exactly as asked. The commands had
    worked; only the reading of them was broken.
    """
    raw = read(app_online, "CellControl.PMLState")
    if raw is None:
        return None
    text = str(raw)
    if "#" in text:
        text = text.split("#")[-1]
    try:
        return int(text)
    except ValueError:
        say("could not parse PMLState from " + repr(raw))
        return None


def pulse(app_online, expression):
    """Set a one-shot command. The program clears it at the end of the scan."""
    app_online.set_prepared_value(expression, "TRUE")
    app_online.write_prepared_values()


def wait_until(predicate, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


try:
    say("opening " + PROJECT)
    proj = projects.open(PROJECT)
    app = proj.find("Application", True)[0]

    say("logging in")
    app_online = online.create_online_application(app)
    # The device has user management enabled, so this raises a credential
    # prompt the first time. Once the credentials are stored for this Windows
    # user the call goes through unattended, which is the only reason an
    # unattended run is possible at all.
    app_online.login(OnlineChangeOption.Never, True)
    say("logged in: " + str(app_online.is_logged_in))

    say("starting the task")
    if str(app_online.application_state) != "run":
        app_online.start()
    time.sleep(1.5)
    say("power on state: " + str(state_of(app_online)))
except Exception, error:
    import traceback
    say("FAILED before the command sequence: " + str(error))
    say(traceback.format_exc())
    log.close()
    raise

# The link watchdog holds the controller in abort until it can see the plant
# scanning. If this never clears, the plant is not running and nothing below
# will work, so it is worth failing loudly here rather than three steps later.
say("waiting for the plant link")
linked = wait_until(lambda: read(app_online, "CellControl.LinkFault") == "FALSE", 30.0)
say("  link fault cleared: " + str(linked))

say("asking the safety channel for torque")
pulse(app_online, "CellControl.CmdSafetyReset")
torque = wait_until(lambda: read(app_online, "IO.SAFETY_OK") == "TRUE", 15.0)
say("  SAFETY_OK: " + str(torque))

for expression, target, label in (
        ("CellControl.CmdClear", 2, "Clear -> Stopped"),
        ("CellControl.CmdReset", 4, "Reset -> Idle"),
        ("CellControl.CmdStart", 6, "Start -> Execute")):
    say(label)
    pulse(app_online, expression)
    if wait_until(lambda: state_of(app_online) == target):
        say("  reached " + str(target))
    else:
        say("  DID NOT reach " + str(target) + ", stuck at " + str(state_of(app_online)))

say("final state: " + str(state_of(app_online)))
say("produced: " + str(read(app_online, "IO.PRODUCED")))
say("leaving the IDE online, script done")
log.close()
