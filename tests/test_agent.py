"""Engine tests against a scripted fake OpenAI-compatible server (no real model needed)."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from atenea import Agent, ModelProfile, SkillSet, tool


class FakeModel:
    """Each call pops the next scripted reply. A reply is a message dict, or ("error", text) for an HTTP 500."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        model = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model.requests.append(body)
                r = model.replies.pop(0)
                if isinstance(r, tuple):
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": {"message": r[1]}}).encode())
                    return
                finish = r.pop("_finish", "stop")
                out = {"choices": [{"message": r, "finish_reason": finish}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(out).encode())

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1/chat/completions"

    def close(self):
        self.srv.shutdown()


def call(_tool, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{_tool}{len(args)}", "type": "function", "function": {"name": _tool, "arguments": json.dumps(args)}}]}


def say(text, **kw):
    return {"role": "assistant", "content": text, **kw}


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["ATENEA_API_KEY"] = "test"

    def agent(self, fake, **kw):
        return Agent(cwd=self.dir, profile=ModelProfile(url=fake.url), allow_delegate=False, **kw)

    def test_tool_then_answer(self):
        fake = FakeModel([call("write_file", path="a.txt", content="hi"), say("done")])
        self.assertEqual(self.agent(fake).run("write a.txt"), "done")
        self.assertEqual(open(os.path.join(self.dir, "a.txt")).read(), "hi")
        self.assertEqual(fake.requests[1]["messages"][-1]["role"], "tool")
        fake.close()

    def test_reasoning_is_kept_in_history(self):
        fake = FakeModel([dict(call("list_files"), reasoning_content="let me look"), say("ok")])
        self.agent(fake).run("look")
        sent = fake.requests[1]["messages"]
        self.assertTrue(any(m.get("reasoning_content") == "let me look" for m in sent))
        fake.close()

    def test_recovers_from_invalid_tool_call(self):
        fake = FakeModel([("error", "Failed to parse tool call arguments as JSON"), say("recovered")])
        self.assertEqual(self.agent(fake).run("x"), "recovered")
        self.assertIn("invalid", fake.requests[1]["messages"][-1]["content"])
        fake.close()

    def test_nudges_on_empty_reply(self):
        fake = FakeModel([say("", _finish="length"), say("finished")])
        self.assertEqual(self.agent(fake).run("x"), "finished")
        fake.close()

    def test_verify_guard_blocks_premature_finish(self):
        flag = os.path.join(self.dir, "ok")
        fake = FakeModel([say("I think I'm done"), call("bash", command="touch ok"), say("now really done")])
        ag = self.agent(fake, verify=f"test -f {flag}")
        self.assertEqual(ag.run("x"), "now really done")
        self.assertIn("Not finished yet", fake.requests[1]["messages"][-1]["content"])
        fake.close()

    def test_custom_tool_and_permissions(self):
        sent = []

        @tool(dangerous=True)
        def send_email(to: str, body: str) -> str:
            "Send an email."
            sent.append(to)
            return "sent"

        fake = FakeModel([call("send_email", to="a@b.c", body="hi"), say("blocked"),
                          call("send_email", to="x@y.z", body="hi"), say("sent ok")])
        ag = self.agent(fake, tools=[send_email], approve=lambda t, a: a["to"] == "x@y.z")
        ag.run("send to a")
        self.assertIn("NOT authorize", fake.requests[1]["messages"][-1]["content"])
        ag.run("send to x")
        self.assertEqual(sent, ["x@y.z"])
        fake.close()

    def test_tool_schema_from_signature(self):
        @tool
        def add(a: int, b: int = 1) -> int:
            "Add numbers."
            return a + b
        s = add.schema()["function"]
        self.assertEqual(s["parameters"]["properties"]["a"], {"type": "integer"})
        self.assertEqual(s["parameters"]["required"], ["a"])
        self.assertEqual(add(a=2), 3)

    def test_skills_catalog_and_loading(self):
        d = os.path.join(self.dir, "skills", "greet")
        os.makedirs(d)
        open(os.path.join(d, "SKILL.md"), "w").write("---\nname: greet\ndescription: How to greet\n---\nSay hello.")
        fake = FakeModel([call("load_skill", name="greet"), say("hello")])
        ag = self.agent(fake, skills=SkillSet(os.path.join(self.dir, "skills")))
        self.assertIn("greet: How to greet", ag.messages[0]["content"])
        ag.run("greet me")
        self.assertIn("Say hello.", fake.requests[1]["messages"][-1]["content"])
        fake.close()

    def test_session_save_and_load(self):
        fake = FakeModel([say("first"), say("second")])
        ag = self.agent(fake)
        ag.run("one")
        p = os.path.join(self.dir, "s.json")
        ag.save(p)
        ag2 = self.agent(fake).load(p)
        ag2.run("two")
        users = [m["content"] for m in fake.requests[1]["messages"] if m["role"] == "user"]
        self.assertEqual(users, ["one", "two"])
        fake.close()


if __name__ == "__main__":
    unittest.main()
