"""MeloTTS (Spanish, Castilian phonemes) as a tiny local HTTP server on 127.0.0.1:8084.
Runs in its own Python 3.11 venv (/mnt/models/tools/melo-venv) because MeloTTS pins old libraries; the Atenea voice
service starts it on demand and stops it after 10 minutes without use.   POST /tts {"text", "speed"} -> audio/wav"""
import io
import json
import os
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import torch

torch.set_num_threads(int(os.environ.get("MELO_THREADS", "8")))
from melo.api import TTS  # noqa: E402

MODEL = TTS(language="ES", device="cpu")
SID = list(MODEL.hps.data.spk2id.values())[0]
LOCK = threading.Lock()
LAST = [time.time()]


def synth(text: str, speed: float = 1.0) -> bytes:
    with LOCK:
        audio = MODEL.tts_to_file(text, SID, None, speed=speed, quiet=True)
    a = np.asarray(audio, dtype=np.float32)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MODEL.hps.data.sampling_rate)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"ok": True, "idle": round(time.time() - LAST[0])}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        LAST[0] = time.time()
        j = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            body, ct, code = synth(j.get("text", "")[:2000], float(j.get("speed") or 1.0)), "audio/wav", 200
        except Exception as e:
            body, ct, code = json.dumps({"error": str(e)}).encode(), "application/json", 500
        self.send_response(code); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)


if __name__ == "__main__":
    synth("Hola.")
    ThreadingHTTPServer(("127.0.0.1", 8084), H).serve_forever()
