"""Control of a Tapo smart plug (P100/P105/P110...) with the `tapo` library, which speaks the newer TPAP protocol.
Runs in its own venv (/mnt/models/tools/tapo-venv). Credentials come from the environment, never from arguments:
TAPO_USER, TAPO_PASS, TAPO_IP.   Usage: tapo_ctl.py status | on | off     -> prints one JSON line."""
import asyncio
import base64
import json
import os
import sys

from tapo import ApiClient


def _name(info) -> str:
    try:
        return base64.b64decode(info.nickname).decode()
    except Exception:
        return str(getattr(info, "nickname", ""))


async def main(cmd: str) -> dict:
    plug = await ApiClient(os.environ["TAPO_USER"], os.environ["TAPO_PASS"]).p100(os.environ["TAPO_IP"])
    if cmd == "on":
        await plug.on()
    elif cmd == "off":
        await plug.off()
    info = await plug.get_device_info()
    return {"on": bool(info.device_on), "name": _name(info), "model": info.model, "on_time_s": getattr(info, "on_time", None),
            "overheated": getattr(info, "overheated", None)}


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "status"))))
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {str(e)[:200]}"}))
        sys.exit(1)
