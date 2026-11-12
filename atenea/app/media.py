"""Images: create and edit them (FLUX.2 klein 4B on stable-diffusion.cpp, Vulkan) and SEE them (Qwen3-VL-8B on a
small llama.cpp server started on demand, since the main model is text-only).

Paths (override in config["media"]):
  sd_dir     /mnt/models/sdcpp                 (bin/sd-cli + models/)
  vision     /mnt/models/gguf/vision           (Qwen3VL-8B-Instruct-Q4_K_M.gguf + mmproj-*.gguf)
  llama_bin  the llama-server used for Ornith
Generated images go to ~/.config/atenea/images/YYYY-MM/."""
from __future__ import annotations

import base64
import glob
import io
import json
import os
import random
import subprocess
import threading
import time
import urllib.request
import uuid

from .. import config as C

_gen_lock = threading.Lock()
_vis_lock = threading.Lock()
_vision = {"proc": None, "last": 0.0}
VISION_PORT = 8081
VISION_IDLE = 600                     # stop the vision server after 10 idle minutes (frees ~6 GB)


def _cfg() -> dict:
    m = C.load().get("media") or {}
    return {"sd_dir": m.get("sd_dir", "/mnt/models/sdcpp"), "vision": m.get("vision", "/mnt/models/gguf/vision"),
            "llama_bin": m.get("llama_bin", "/mnt/models/llamacpp/b12242-vulkan/llama-b12242/llama-server")}


def _out_path(prefix: str) -> str:
    d = os.path.join(os.path.dirname(C.PATH), "images", time.strftime("%Y-%m"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{prefix}-{time.strftime('%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.png")


def _sd(args: list[str], out: str, timeout: int = 900) -> str:
    c = _cfg()
    sd = c["sd_dir"]
    if not os.path.exists(os.path.join(sd, "bin", "sd-cli")):
        return "Error: image generation is not installed (stable-diffusion.cpp not found)."
    cmd = [os.path.join(sd, "bin", "sd-cli"),
           "--diffusion-model", os.path.join(sd, "models", "flux-2-klein-4b-Q4_0.gguf"),
           "--vae", os.path.join(sd, "models", "flux2-vae.safetensors"),
           "--llm", os.path.join(sd, "models", "Qwen3-4B-Q4_K_M.gguf"),
           "--cfg-scale", "1.0", "--steps", "4", "--sampling-method", "euler", "-o", out, *args]
    t0 = time.time()
    with _gen_lock:                                    # one image at a time (shares the GPU with the LLM)
        r = subprocess.run(cmd, cwd=sd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LD_LIBRARY_PATH": os.path.join(sd, "bin")})
    if r.returncode != 0 or not os.path.exists(out):
        return f"Error: generation failed (exit {r.returncode}): {(r.stderr or r.stdout)[-800:]}"
    return f"{time.time() - t0:.0f}"


def _size(w, h) -> tuple[int, int]:
    def snap(v, d):
        try:
            v = int(v or d)
        except (TypeError, ValueError):
            v = d
        return max(256, min(1536, v // 64 * 64))
    return snap(w, 1024), snap(h, 1024)


def generate_image(prompt: str, width: int = 1024, height: int = 1024, seed: int = -1) -> str:
    w, h = _size(width, height)
    out = _out_path("img")
    s = int(seed) if seed is not None and int(seed) >= 0 else random.randint(0, 2**31 - 1)
    t = _sd(["-p", prompt, "-W", str(w), "-H", str(h), "-s", str(s)], out)
    if t.startswith("Error"):
        return t
    return (f"Image created in {t} s ({w}×{h}, seed {s}). Show it to the user with this markdown line:\n"
            f"![{prompt[:80].replace(']', '')}]({out})")


def edit_image(path: str, instruction: str, width: int = 0, height: int = 0) -> str:
    if not os.path.isfile(path):
        return f"Error: image not found: {path}"
    if not width or not height:
        try:
            from PIL import Image
            with Image.open(path) as im:
                width, height = im.size
            k = min(1.0, 1024 / max(width, height))
            width, height = width * k, height * k
        except Exception:
            width, height = 1024, 1024
    w, h = _size(width, height)
    out = _out_path("edit")
    t = _sd(["-r", path, "-p", instruction, "-W", str(w), "-H", str(h), "-s", str(random.randint(0, 2**31 - 1))], out)
    if t.startswith("Error"):
        return t
    return f"Edited image created in {t} s. Show it with:\n![{instruction[:80].replace(']', '')}]({out})"


# ---------------- vision ----------------
def _vision_files() -> tuple[str, str] | None:
    d = _cfg()["vision"]
    models = [f for f in glob.glob(os.path.join(d, "*.gguf")) if "mmproj" not in os.path.basename(f)]
    proj = glob.glob(os.path.join(d, "mmproj*.gguf"))
    if not models or not proj:
        return None
    return models[0], proj[0]


def _vision_up() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{VISION_PORT}/health", timeout=2).read()
        return True
    except Exception:
        return False


def _ensure_vision() -> str | None:
    if _vision_up():
        return None
    files = _vision_files()
    if not files:
        return "Error: the vision model is not installed yet."
    llama = _cfg()["llama_bin"]
    log = open(os.path.join(os.path.dirname(C.PATH), "vision.log"), "ab")
    _vision["proc"] = subprocess.Popen(
        [llama, "-m", files[0], "--mmproj", files[1], "-ngl", "99", "-c", "16384", "-np", "1", "--host", "127.0.0.1",
         "--port", str(VISION_PORT), "--jinja", "--no-webui"], stdout=log, stderr=log,
        env={**os.environ, "LD_LIBRARY_PATH": os.path.dirname(llama)}, start_new_session=True)
    for _ in range(180):
        if _vision["proc"].poll() is not None:
            return "Error: the vision server could not start (see ~/.config/atenea/vision.log)."
        if _vision_up():
            threading.Thread(target=_vision_reaper, daemon=True).start()
            return None
        time.sleep(1)
    return "Error: the vision server took too long to start."


def _vision_reaper():
    while True:
        time.sleep(30)
        p = _vision["proc"]
        if p is None or p.poll() is not None:
            return
        if time.time() - _vision["last"] > VISION_IDLE and not _vis_lock.locked():
            p.terminate()
            _vision["proc"] = None
            return


def _prep(data: bytes) -> tuple[str, str]:
    """Downscale big photos (the model works at ~1-2 MP) and return (mime, base64)."""
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        im.thumbnail((1600, 1600))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return "image/jpeg", base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return "image/png", base64.b64encode(data).decode()


def look_at_image(data: bytes, question: str) -> str:
    with _vis_lock:
        err = _ensure_vision()
        if err:
            return err
        _vision["last"] = time.time()
        mime, b64 = _prep(data)
        body = {"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": question or "Describe this image in detail, including any text in it."}]}],
            "max_tokens": 1500, "temperature": 0.2}
        req = urllib.request.Request(f"http://127.0.0.1:{VISION_PORT}/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            d = json.load(urllib.request.urlopen(req, timeout=600))
        except Exception as e:
            return f"Error: vision model failed: {e}"
        _vision["last"] = time.time()
        return "What the image shows (from the vision model):\n" + (d["choices"][0]["message"].get("content") or "").strip()


INSTRUCTIONS = """- IMAGES: you cannot see images yourself, but you have tools: look_at_image(path, question) asks a vision model
  (describes photos, reads text/receipts/screenshots, answers questions about them); generate_image(prompt) creates an
  image (~40 s, FLUX.2 klein; write detailed English prompts: subject, setting, style, light, camera; put any text to
  draw in quotes); edit_image(path, instruction) changes an existing image (~60 s). When the user attaches an image,
  look at it before answering. Show results with the markdown line the tool returns."""
