"""Atenea voz: dictado (Whisper large-v3-turbo, CPU int8) y voz de Atenea (Piper, español). Solo localhost:8083.

POST /stt   cuerpo = audio en cualquier formato (webm, mp4/m4a del iPhone, wav…)  -> {"text": "...", "secs": 1.2}
POST /stt?engine=parakeet  -> lo mismo con NVIDIA Parakeet TDT v3 (sherpa-onnx, ~0,1 s): para Atenea en vivo
POST /tts   {"text": "...", "voice": "supertonic:F1" | "kokoro:ef_dora" | "es_ES-sharvard-medium", "speaker": 1} -> audio/wav
            Supertonic 3 and Kokoro run on CPU (ONNX) about 7x faster than real time; Piper is the old voice.
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
STT_FAST = os.environ.get("ATENEA_STT_FAST", "small")         # for live partial transcripts while you speak
_stt = {"model": None, "last": 0.0, "fast": None, "fast_last": 0.0}
_tts: dict = {}
PARAKEET = os.environ.get("ATENEA_PARAKEET", os.path.join(os.path.dirname(VOICES), "models", "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"))
_pk: dict = {"rec": None}
SMART_TURN = os.environ.get("ATENEA_SMART_TURN", os.path.join(os.path.dirname(VOICES), "models", "smart-turn", "smart-turn-v3.0.onnx"))
_st: dict = {}


def turn_complete(audio) -> float | None:
    """Pipecat Smart Turn v3: probability that the speaker has finished (vs. pausing mid-sentence). ~40 ms on CPU."""
    if not os.path.isfile(SMART_TURN):
        return None
    with _lock:
        if "s" not in _st:
            import onnxruntime as ort
            from transformers import WhisperFeatureExtractor
            so = ort.SessionOptions()
            so.inter_op_num_threads, so.intra_op_num_threads = 1, 2
            _st["s"], _st["fe"] = ort.InferenceSession(SMART_TURN, so), WhisperFeatureExtractor(chunk_length=8)
    a = np.asarray(audio, dtype=np.float32)[-128000:]
    a = np.pad(a, (128000 - len(a), 0))
    f = _st["fe"](a, sampling_rate=16000, return_tensors="np", padding="max_length", max_length=128000,
                  truncation=True, do_normalize=True).input_features.astype(np.float32)
    return float(_st["s"].run(None, {"input_features": f})[0][0][0])


def parakeet():
    import sherpa_onnx
    with _lock:
        if _pk["rec"] is None:
            m = PARAKEET + "/"
            _pk["rec"] = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=m + "encoder.int8.onnx", decoder=m + "decoder.int8.onnx", joiner=m + "joiner.int8.onnx",
                tokens=m + "tokens.txt", num_threads=8, model_type="nemo_transducer")
        return _pk["rec"]


def transcribe_parakeet(data: bytes, turn: bool = False) -> dict:
    t0 = time.time()
    pcm = subprocess.run(["ffmpeg", "-v", "quiet", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
                         input=data, capture_output=True, timeout=60).stdout
    audio = np.frombuffer(pcm, dtype=np.float32)
    if audio.size < 1600:
        return {"text": "", "secs": 0}
    rec = parakeet()
    st = rec.create_stream()
    st.accept_waveform(16000, audio)
    rec.decode_stream(st)
    out = {"text": st.result.text.strip(), "engine": "parakeet", "audio_secs": round(audio.size / 16000, 1)}
    if turn:
        out["complete"] = turn_complete(np.concatenate([audio, np.zeros(3200, np.float32)]))
    out["secs"] = round(time.time() - t0, 2)
    return out
_lock = threading.Lock()


def stt_model(fast: bool = False):
    from faster_whisper import WhisperModel
    key, name = ("fast", STT_FAST) if fast else ("model", STT_MODEL)
    with _lock:
        if _stt[key] is None:
            _stt[key] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=8 if fast else 12)
        _stt["fast_last" if fast else "last"] = time.time()
        return _stt[key]


def reaper():
    while True:
        time.sleep(60)
        with _lock:
            if _stt["model"] is not None and time.time() - _stt["last"] > 900:
                _stt["model"] = None
            if _stt["fast"] is not None and time.time() - _stt["fast_last"] > 900:
                _stt["fast"] = None


def transcribe(data: bytes, fast: bool = False) -> dict:
    t0 = time.time()
    pcm = subprocess.run(["ffmpeg", "-v", "quiet", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
                         input=data, capture_output=True, timeout=120).stdout
    audio = np.frombuffer(pcm, dtype=np.float32)
    if audio.size < 1600:
        return {"text": "", "secs": 0}
    segs, info = stt_model(fast).transcribe(audio, language=os.environ.get("ATENEA_STT_LANG", "es") or None, beam_size=1, vad_filter=True,
                                        initial_prompt="Atenea, ZEUS, Ornith, Sergio.")
    text = " ".join(s.text.strip() for s in segs).strip()
    _stt["last"] = time.time()
    return {"text": text, "language": info.language, "audio_secs": round(info.duration, 1), "secs": round(time.time() - t0, 1)}


MODELS = os.path.join(os.path.dirname(VOICES), "models")
DEFAULT_VOICE = os.environ.get("ATENEA_VOICE", "kokoro:ef_dora")      # Castilian pronunciation (espeak "es")
_eng: dict = {}


def _wav(audio, sr: int) -> bytes:
    a = np.asarray(audio, dtype=np.float32).squeeze()
    peak = float(np.abs(a).max()) if a.size else 0.0
    if peak > 1e-4:
        a = a * (0.7 / peak)                       # same loudness for every voice, without clipping
    a = np.clip(a, -1, 1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes((a * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _supertonic():
    with _lock:
        if "st" not in _eng:
            from supertonic import TTS
            _eng["st"] = TTS(model="supertonic-3", model_dir=os.path.join(MODELS, "supertonic"), auto_download=False,
                             intra_op_num_threads=8)
            _eng["st_styles"] = {}
        return _eng["st"]


def _kokoro():
    with _lock:
        if "ko" not in _eng:
            from kokoro_onnx import Kokoro
            _eng["ko"] = Kokoro(os.path.join(MODELS, "kokoro", "kokoro-v1.0.onnx"), os.path.join(MODELS, "kokoro", "voices-v1.0.bin"))
        return _eng["ko"]


def voices() -> list[str]:
    out = []
    if os.path.isdir(os.path.join(MODELS, "supertonic", "voice_styles")):
        out += sorted("supertonic:" + f[:-5] for f in os.listdir(os.path.join(MODELS, "supertonic", "voice_styles")) if f.endswith(".json"))
    if os.path.isfile(os.path.join(MODELS, "kokoro", "voices-v1.0.bin")):
        out += ["kokoro:ef_dora", "kokoro:em_alex", "kokoro:em_santa"]
    return out + sorted(f[:-5] for f in os.listdir(VOICES) if f.endswith(".onnx"))


def speak(text: str, voice: str = "", speaker: int = 1, speed: float = 1.0) -> bytes:
    voice = voice or DEFAULT_VOICE
    if voice.startswith("supertonic:"):
        tts = _supertonic()
        name = voice.split(":", 1)[1]
        st = _eng["st_styles"].get(name) or _eng["st_styles"].setdefault(name, tts.get_voice_style(name))
        wav, _ = tts.synthesize(text, st, lang="es", total_steps=int(os.environ.get("ATENEA_SUPERTONIC_STEPS", "6")), speed=1.05 * speed, silence_duration=0.15)
        return _wav(wav, tts.sample_rate)
    if voice.startswith("kokoro:"):
        a, sr = _kokoro().create(text, voice=voice.split(":", 1)[1], speed=speed, lang="es")
        return _wav(a, sr)
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
        self._send(200, json.dumps({"ok": True, "stt_loaded": _stt["model"] is not None, "default": DEFAULT_VOICE,
                                    "voices": voices()}).encode())

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            if self.path.startswith("/stt") and "engine=parakeet" in self.path and os.path.isdir(PARAKEET):
                return self._send(200, json.dumps(transcribe_parakeet(data, "turn=1" in self.path)).encode())
            if self.path.startswith("/stt"):
                return self._send(200, json.dumps(transcribe(data, "fast=1" in self.path)).encode())
            if self.path.startswith("/tts"):
                j = json.loads(data or b"{}")
                return self._send(200, speak(j.get("text", "")[:3000], j.get("voice") or "",
                                             int(j.get("speaker", 1)), float(j.get("speed") or 1.0)), "audio/wav")
            self._send(404, b"{}")
        except Exception as e:
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())


if __name__ == "__main__":
    threading.Thread(target=reaper, daemon=True).start()
    if os.path.isdir(PARAKEET):
        threading.Thread(target=parakeet, daemon=True).start()
    threading.Thread(target=lambda: speak("Hola.", DEFAULT_VOICE), daemon=True).start()
    threading.Thread(target=lambda: turn_complete(np.zeros(16000, np.float32)), daemon=True).start()   # warm the default voice      # load at start: live voice needs it instantly
    ThreadingHTTPServer(("127.0.0.1", 8083), H).serve_forever()
