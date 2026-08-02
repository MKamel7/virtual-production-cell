"""Record the demo: tile the two windows, drive the plant, capture with OBS.

    uv run python scripts/record_demo.py

WHAT THE RECORDING IS ARGUING. The cell is producing. The plant is killed
underneath it. The controller notices, aborts, and drops every actuator. The
plant comes back and the LINK recovers on its own, while the MACHINE stays
stopped and waits to be reset by a person.

  the cell recovers the connection automatically
  and refuses to recover the machine automatically

Everything after the plant dies is the controller's own reaction. Nothing here
commands it. That is deliberate: a scripted controller would be staging the
result rather than showing it.

PREREQUISITES, none of which this script does for you:
  1. the plant console is running     scripts/demo_plant.py in its own window
  2. the cell is in Execute           plc/codesys/demo_online.py, run by CODESYS
  3. OBS is running with the P4Demo scene collection

The OBS password is READ FROM THE OBS CONFIG rather than written here. A
credential committed to this repository is exactly the mistake that had to be
undone with a history rewrite once already.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "recordings" / ".plant_control"
OBS_CONFIG = (Path.home() / "AppData/Roaming/obs-studio"
              / "plugin_config/obs-websocket/config.json")

#: The canvas, and the two panels the windows are tiled into. The windows are
#: resized to exactly these so nothing is letterboxed and no desktop shows
#: through: the recording contains the two windows and nothing else.
PANELS = (("CODESYS V3.5", 1, 0, 0, 1920, 760),
          ("VPC PLANT", 2, 0, 760, 1920, 320))

#: Long enough to read, short enough to watch. The middle beat is the longest
#: because that is where the state change happens.
BEATS = ((18, None, "Execute: plant scanning, product flowing"),
         (20, "kill", "plant killed: watch PMLState leave 6"),
         (22, "run", "plant back: the link recovers, the machine does not"),
         (4, None, "hold on the final state"))


def place_windows() -> None:
    """Tile the two windows into the panels OBS is expecting.

    DPI awareness first. Without it Windows silently rescales every coordinate
    by the display scaling and the windows land at sizes nobody asked for,
    which shows up as a window that does not fill its half of the frame.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()
    user32 = ctypes.windll.user32

    def find(substring: str) -> int | None:
        hits: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def visit(handle, _):
            if user32.IsWindowVisible(handle):
                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(handle, buf, 512)
                if substring.lower() in buf.value.lower():
                    hits.append(handle)
            return True

        user32.EnumWindows(visit, 0)
        return hits[0] if hits else None

    for substring, _, x, y, w, h in PANELS:
        handle = find(substring)
        if handle is None:
            raise SystemExit(
                f"window {substring!r} is not open. The recording would show a "
                f"blank panel, so this stops rather than producing one.")
        user32.ShowWindow(handle, 9)
        user32.SetWindowPos(handle, 0, x, y, w, h, 0x0044)
        print(f"  placed {substring}")


class Obs:
    """The few obs-websocket calls this needs, and no library to install."""

    def __init__(self) -> None:
        from websocket import create_connection

        config = json.loads(OBS_CONFIG.read_text())
        if not config.get("server_enabled"):
            raise SystemExit("obs-websocket is disabled in the OBS settings")

        self.ws = create_connection(f"ws://127.0.0.1:{config['server_port']}",
                                    timeout=20)
        hello = json.loads(self.ws.recv())
        auth = hello["d"].get("authentication")
        payload: dict[str, object] = {"rpcVersion": 1}
        if auth:
            password = config["server_password"]
            secret = base64.b64encode(
                hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
            payload["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        self.ws.send(json.dumps({"op": 1, "d": payload}))
        self.ws.recv()
        self.n = 0

    def call(self, request: str, data: dict | None = None) -> dict:
        self.n += 1
        request_id = f"r{self.n}"
        self.ws.send(json.dumps({"op": 6, "d": {
            "requestType": request, "requestId": request_id,
            "requestData": data or {}}}))
        while True:
            message = json.loads(self.ws.recv())
            if message["op"] == 7 and message["d"]["requestId"] == request_id:
                return message["d"]

    def layout(self) -> None:
        for name in ("PLC (CODESYS)", "Plant"):
            # Whole window, title bar included, so the source aspect matches
            # the panel and SCALE_OUTER crops nothing.
            self.call("SetInputSettings",
                      {"inputName": name, "inputSettings": {"client_area": False}})
        for _, item_id, x, y, w, h in PANELS:
            self.call("SetSceneItemTransform", {
                "sceneName": "Cell", "sceneItemId": item_id,
                "sceneItemTransform": {
                    "positionX": x, "positionY": y,
                    "boundsType": "OBS_BOUNDS_SCALE_OUTER",
                    "boundsWidth": w, "boundsHeight": h, "boundsAlignment": 0}})


def main() -> int:
    print("placing windows")
    place_windows()
    time.sleep(2)

    obs = Obs()
    obs.layout()
    print("layout applied")
    CONTROL.parent.mkdir(exist_ok=True)
    CONTROL.write_text("run", encoding="utf-8")
    time.sleep(2)

    if not obs.call("StartRecord")["requestStatus"]["result"]:
        raise SystemExit("OBS refused to start recording")
    print("recording")
    time.sleep(1.0)

    for seconds, command, note in BEATS:
        if command:
            CONTROL.write_text(command, encoding="utf-8")
        print(f"  {note:<55} {seconds}s", flush=True)
        time.sleep(seconds)

    result = obs.call("StopRecord")
    path = result.get("responseData", {}).get("outputPath")
    print(f"\nwrote {path}")
    print("Verify it rather than trusting it: pull frames at 10s, 32s and 58s "
          "and check PMLState reads 6, then 9, then still 9.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
