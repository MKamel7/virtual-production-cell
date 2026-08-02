"""The plant, in a console that narrates itself, for the demo recording.

The recording has no audio, so the plant window has to say what is happening on
its own. This wrapper prints the state changes in plain words and nothing else;
it does not touch the protocol, the scan loop or the controller.

WHAT IT DOES NOT DO, because it would undermine the point: it never talks to the
PLC and it never restarts the plant on its own. It starts and stops a child
process on instruction and reports what it did. Everything the controller does in
response, dropping the actuators, refusing to resume, is the controller's own
behaviour reacting to a plant that vanished, which is the only claim this
recording makes.

Driven by a one-line control file so the orchestrator can act on it without
needing to send keystrokes into a console window.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "recordings" / ".plant_control"

RULE = "=" * 68


def banner(text: str) -> None:
    print(f"\n{RULE}\n  {text}\n{RULE}\n", flush=True)


def spawn() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "vpc.server"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    CONTROL.parent.mkdir(exist_ok=True)
    CONTROL.write_text("run", encoding="utf-8")

    print("PLANT  bottling cell simulation")
    print("       Modbus TCP on 127.0.0.1:502, scanning every 50 ms")
    banner("PLANT RUNNING")

    plant: subprocess.Popen[bytes] | None = spawn()
    last = "run"

    while True:
        try:
            command = CONTROL.read_text(encoding="utf-8").strip()
        except OSError:
            command = last

        if command != last:
            if command == "kill" and plant is not None:
                plant.terminate()
                plant.wait(timeout=10)
                plant = None
                banner("PLANT STOPPED  <<-- the controller has just lost its plant")
            elif command == "run" and plant is None:
                plant = spawn()
                banner("PLANT RESTARTED  <<-- the link will come back on its own")
            elif command == "quit":
                if plant is not None:
                    plant.terminate()
                return 0
            last = command

        if plant is None:
            print("  no plant. Nothing is scanning, and nothing is being made.",
                  flush=True)
        else:
            print("  scanning...", flush=True)
        time.sleep(2.0)


if __name__ == "__main__":
    sys.exit(main())
