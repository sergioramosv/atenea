"""Atenea voz: dictado (Whisper large-v3-turbo, CPU int8) y voz de Atenea (Piper, español). Solo localhost:8083.

POST /stt   cuerpo = audio en cualquier formato (webm, mp4/m4a del iPhone, wav…)  -> {"text": "...", "secs": 1.2}
POST /tts   {"text": "...", "voice": "es_ES-sharvard-medium", "speaker": 1}       -> audio/wav
Whisper se carga al primer uso y se descarga tras 15 min sin uso (libera ~1,5 GB)."""
import io
import json
import os
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
VOICES = os.environ.get("ATENEA_VOICES", os.path.join(HERE, "voices"))
STT_MODEL = os.environ.get("ATENEA_STT_MODEL", "large-v3-turbo")
_stt = {"model": None, "last": 0.0}
_tts: dict = {}
_lock = threading.Lock()


def stt_model():
    from faster_whisper import WhisperModel
    with _lock:
        if _stt["model"] is None:
            _stt["model"] = WhisperModel(STT_MODEL, device="cpu", compute_type="int8", cpu_threads=12)
        _stt["last"] = time.time()
        return _stt["model"]


def reaper():
    while True:
        time.sleep(60)
        with _lock:
            if _stt["model"] is not None and time.time() - _stt["last"] > 900:
                _stt["model"] = None


def transcribe(data: bytes) -> dict:
    t0 = time.time()
    pcm = subprocess.run(["ffmpeg", "-v", "quiet", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
                         input=data, capture_output=True, timeout=120).stdout
    audio = np.frombuffer(pcm, dtype=np.float32)
    if audio.size < 1600:
        return {"text": "", "secs": 0}
    segs, info = stt_model().transcribe(audio, language=os.environ.get("ATENEA_STT_LANG", "es") or None, beam_size=1, vad_filter=True,
                                        initial_prompt="Atenea, ZEUS, Ornith, Sergio.")
    text = " ".join(s.text.strip() for s in segs).strip()
    _stt["last"] = time.time()
    return {"text": text, "language": info.language, "audio_secs": round(info.duration, 1), "secs": round(time.time() - t0, 1)}


def speak(text: str, voice: str = "es_ES-sharvard-medium", speaker: int = 1) -> bytes:
    from piper import PiperVoice, SynthesisConfig
    if voice not in _tts:
        _tts[voice] = PiperVoice.load(os.path.join(VOICES, voice + ".onnx"))
    v = _tts[voice]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        sid = speaker if v.config.num_speakers > 1 else None
        v.synthesize_wav(text, w, syn_config=SynthesisConfig(speaker_id=sid, length_scale=0.95))
    return buf.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, json.dumps({"ok": True, "stt_loaded": _stt["model"] is not None,
                                    "voices": sorted(f[:-5] for f in os.listdir(os.path.join(VOICES)) if f.endswith(".onnx"))}).encode())

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            if self.path.startswith("/stt"):
                return self._send(200, json.dumps(transcribe(data)).encode())
            if self.path.startswith("/tts"):
                j = json.loads(data or b"{}")
                return self._send(200, speak(j.get("text", "")[:3000], j.get("voice") or "es_ES-sharvard-medium",
                                             int(j.get("speaker", 1))), "audio/wav")
            self._send(404, b"{}")
        except Exception as e:
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())


if __name__ == "__main__":
    threading.Thread(target=reaper, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8083), H).serve_forever()
