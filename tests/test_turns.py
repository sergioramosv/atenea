"""Turn budget: free todo turns, the turns-left warning and the final answer at the limit."""
import tempfile
import unittest

from atenea import Agent, ModelProfile
from test_agent import FakeModel, call, say


class TurnsTest(unittest.TestCase):
    def agent(self, fake, **kw):
        return Agent(cwd=tempfile.mkdtemp(), profile=ModelProfile(url=fake.url), allow_delegate=False, builtin=True, **kw)

    def test_todo_turns_are_free(self):
        todo = call("todo_write", todos=[{"content": "check x", "status": "in_progress"}])
        fake = FakeModel([todo, todo, call("list_files", path="."), say("done")])
        self.assertEqual(self.agent(fake, max_turns=1).run("go"), "done")
        fake.close()

    def test_warns_when_turns_run_out_and_asks_for_a_final_answer(self):
        ls = call("list_files", path=".")
        fake = FakeModel([ls, ls, say("final answer")])
        ag = self.agent(fake, max_turns=2, turns_warning=1)
        self.assertEqual(ag.run("go"), "final answer")
        sent = fake.requests[-1]
        self.assertEqual(sent.get("tool_choice"), "none")
        self.assertTrue(any("tool turn(s) left" in (m.get("content") or "") for m in sent["messages"] if m["role"] == "tool"))
        fake.close()

    def test_final_answer_that_is_still_a_tool_call_is_not_returned(self):
        ls = call("list_files", path=".")
        fake = FakeModel([ls, say("<tool_call><function=read_file></function></tool_call>")])
        self.assertEqual(self.agent(fake, max_turns=1).run("go"), "Turn limit reached without finishing.")
        fake.close()


if __name__ == "__main__":
    unittest.main()
