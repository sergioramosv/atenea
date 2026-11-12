"""Panel (atenea.web) tests: auth, conversations, queue, permission modes, rewind, memory, trash, file access,
artifacts and runners — against a scripted fake model (streaming + llama.cpp slot endpoints). No GPU needed.

Run:  PYTHONPATH=. .venv/bin/python -m unittest tests.test_web -v"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TMP = tempfile.mkdtemp(prefix="atenea-web-test-")
os.environ["ATENEA_CONFIG"] = os.path.join(TMP, "cfg", "config.json")   # before importing atenea


class FakeModel:
    """Scripted OpenAI-compatible server. Replies are popped in order; supports stream=True (SSE) and the llama.cpp
    /props and /slots endpoints the panel uses. A reply may carry "_delay" (seconds)."""

    def __init__(self):
        self.replies, self.requests, model = [], [], self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._json({"total_slots": 2} if self.path.startswith("/props") else [{"id": 0, "n_ctx": 65536}])

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path.startswith("/slots"):
                    return self._json({"n_saved": 1, "n_restored": 1})
                if body.get("max_tokens") == 1:                  # the panel pre-processing a chat "base"
                    return self._json({"choices": [{"message": {"role": "assistant", "content": "."},
                                                    "finish_reason": "length"}], "usage": {}})
                model.requests.append(body)
                r = dict(model.replies.pop(0)) if model.replies else {"role": "assistant", "content": "(no more replies)"}
                time.sleep(r.pop("_delay", 0))
                finish = "tool_calls" if r.get("tool_calls") else "stop"
                usage = {"prompt_tokens": 100, "completion_tokens": 10}
                if not body.get("stream"):
                    return self._json({"choices": [{"message": r, "finish_reason": finish}], "usage": usage})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delta = {"content": r.get("content") or ""}
                if r.get("tool_calls"):
                    delta["tool_calls"] = [dict(tc, index=i) for i, tc in enumerate(r["tool_calls"])]
                for ev in ({"choices": [{"delta": delta}]}, {"choices": [{"delta": {}, "finish_reason": finish}]},
                           {"choices": [], "usage": usage, "timings": {"predicted_per_second": 30.0}}):
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"


def say(text, **kw):
    return {"role": "assistant", "content": text, **kw}


def call(tool, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{time.time_ns()}", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]}


MODEL = FakeModel()
os.makedirs(os.path.dirname(os.environ["ATENEA_CONFIG"]), exist_ok=True)
json.dump({"model": {"url": MODEL.base + "/v1/chat/completions", "model": "fake", "context": 65536},
           "workspace": os.path.join(TMP, "ws"), "web": {"password": "pw-test", "host": "127.0.0.1", "port": 0},
           "telegram": {"token": "", "allowed_users": []}, "mcp_servers": {}, "skills_dirs": []},
          open(os.environ["ATENEA_CONFIG"], "w"))

from fastapi.testclient import TestClient  # noqa: E402

from atenea import web  # noqa: E402

web._llama_key = lambda: ""                      # the fake server needs no key


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(web.app)
        r = cls.c.post("/api/login", json={"password": "pw-test"})
        assert r.status_code == 200, r.text

    def setUp(self):
        MODEL.replies.clear()
        self.proj = tempfile.mkdtemp(dir=TMP)

    def run_chat(self, text, chat=None, mode="auto", project=None, timeout=20):
        r = self.c.post("/api/chat", json={"text": text, "chat": chat, "mode": mode, "project": project or self.proj})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        return d["chat"], self.wait(d["job"], timeout)

    def wait(self, jid, timeout=20):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = self.c.get(f"/api/jobs/{jid}").json()
            if j["done"]:
                return j
            time.sleep(0.1)
        self.fail("job did not finish")

    # ---- auth ----
    def test_auth(self):
        anon = TestClient(web.app)
        self.assertEqual(anon.get("/api/chats").status_code, 401)
        self.assertEqual(anon.post("/api/login", json={"password": "wrong"}).status_code, 401)
        tok = web.cli_token()
        self.assertEqual(anon.get("/api/chats", headers={"Authorization": f"Bearer {tok}"}).status_code, 200)
        self.assertEqual(anon.get("/api/chats", headers={"Authorization": "Bearer nope"}).status_code, 401)

    # ---- conversation basics ----
    def test_chat_is_saved_and_listed(self):
        MODEL.replies[:] = [say("Hola Sergio")]
        cid, j = self.run_chat("Hola")
        self.assertEqual(j["result"], "Hola Sergio")
        c = self.c.get(f"/api/chats/{cid}").json()
        self.assertEqual([e["q"] for e in c["log"]], ["Hola"])
        self.assertIn(cid, [x["id"] for x in self.c.get("/api/chats").json()["chats"]])
        self.assertEqual(j["usage"]["tps"], 30.0)                                  # real speed from timings

    def test_queue_runs_in_order(self):
        MODEL.replies[:] = [say("one", _delay=1.0), say("two")]
        r1 = self.c.post("/api/chat", json={"text": "first", "project": self.proj}).json()
        r2 = self.c.post("/api/chat", json={"text": "second", "chat": r1["chat"]}).json()
        self.assertTrue(r2.get("queued"))
        j = self.wait(r1["job"])
        self.assertTrue(j["next"])
        self.wait(j["next"])
        log = self.c.get(f"/api/chats/{r1['chat']}").json()["log"]
        self.assertEqual([(e["q"], e["a"]) for e in log], [("first", "one"), ("second", "two")])

    # ---- tools, modes, rewind ----
    def test_edit_then_rewind_restores_files(self):
        MODEL.replies[:] = [call("write_file", path="a.txt", content="new"), say("done")]
        cid, _ = self.run_chat("write it")
        self.assertEqual(open(os.path.join(self.proj, "a.txt")).read(), "new")
        r = self.c.post(f"/api/chats/{cid}/rewind", json={"index": 0, "what": "both"}).json()
        self.assertFalse(os.path.exists(os.path.join(self.proj, "a.txt")))
        self.assertEqual(r["q"], "write it")
        self.assertEqual(self.c.get(f"/api/chats/{cid}").json()["log"], [])

    def test_plan_mode_blocks_writes(self):
        MODEL.replies[:] = [call("write_file", path="b.txt", content="x"), say("plan: 1. write b.txt")]
        _, j = self.run_chat("make b", mode="plan")
        self.assertFalse(os.path.exists(os.path.join(self.proj, "b.txt")))
        self.assertEqual(j["meta"]["mode"], "plan")

    def test_ask_mode_waits_for_approval(self):
        MODEL.replies[:] = [call("bash", command="echo hi > c.txt"), say("ok")]
        r = self.c.post("/api/chat", json={"text": "run", "mode": "ask", "project": self.proj}).json()
        for _ in range(100):
            j = self.c.get(f"/api/jobs/{r['job']}").json()
            if j["approval"]:
                break
            time.sleep(0.1)
        self.assertEqual(j["approval"]["tool"], "bash")
        self.c.post(f"/api/jobs/{r['job']}/approve/yes")
        self.wait(r["job"])
        self.assertTrue(os.path.exists(os.path.join(self.proj, "c.txt")))

    # ---- memory, trash ----
    def test_memory_and_trash(self):
        self.c.put("/api/memory", json={"facts": ["Sergio likes olives"]})
        self.assertEqual(self.c.get("/api/memory").json()["facts"], ["Sergio likes olives"])
        MODEL.replies[:] = [say("ok")]
        cid, _ = self.run_chat("to delete")
        self.c.delete(f"/api/chats/{cid}")
        self.assertNotIn(cid, [x["id"] for x in self.c.get("/api/chats").json()["chats"]])
        self.assertIn(cid, [x["id"] for x in self.c.get("/api/trash").json()["items"]])
        self.c.post(f"/api/trash/{cid}/restore")
        self.assertIn(cid, [x["id"] for x in self.c.get("/api/chats").json()["chats"]])

    # ---- files and artifacts ----
    def test_file_access_rules(self):
        self.assertEqual(self.c.get("/api/file", params={"path": "/etc/passwd"}).status_code, 403)
        self.assertEqual(self.c.get("/api/file", params={"path": os.environ["ATENEA_CONFIG"]}).status_code, 403)

    def test_artifact_is_sandboxed(self):
        from atenea import artifacts as A
        m = A.save("Test", "<html><body>hi</body></html>")
        r = self.c.get(f"/a/{m['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("sandbox", r.headers.get("content-security-policy", ""))

    # ---- runner (tools executed on another computer) ----
    def test_runner_executes_tools_remotely(self):
        from atenea import tools as T
        remote_dir = tempfile.mkdtemp(dir=TMP)
        reg = self.c.post("/api/runners", json={"name": "mac", "platform": "Darwin", "cwd": remote_dir}).json()
        stop = threading.Event()
        rc = TestClient(web.app, cookies=self.c.cookies)        # its own client: it runs in another thread

        def runner():
            while not stop.is_set():
                r = rc.get(f"/api/runners/{reg['runner']}/next?wait=1").json()
                if r.get("call"):
                    c = r["call"]
                    out = "\0missing" if c["tool"] == "read_raw" else T.run(c["tool"], c["args"], remote_dir)
                    rc.post(f"/api/runners/{reg['runner']}/result", json={"id": c["id"], "output": out})
        threading.Thread(target=runner, daemon=True).start()
        self.c.patch(f"/api/chats/{reg['chat']}", json={"mode": "auto"})   # runner chats start in Accept edits
        MODEL.replies[:] = [call("bash", command="echo remote > r.txt"), say("done")]
        _, j = self.run_chat("go", chat=reg["chat"])
        stop.set()
        self.assertEqual(open(os.path.join(remote_dir, "r.txt")).read().strip(), "remote")
        self.assertIn("NOT on ZEUS", web.AGENTS[reg["chat"]].messages[0]["content"])


def tearDownModule():
    MODEL.srv.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
