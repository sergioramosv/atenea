import threading
import time

from atenea import llm
from atenea.llm import LLM, ModelProfile, max_parallel, set_max_parallel


def _peak(profile, calls):
    """Run `calls` chats at once with a fake model and return how many ran at the same time."""
    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    def fake(self, *a, **k):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return {}

    orig = LLM._chat
    LLM._chat = fake
    try:
        ts = [threading.Thread(target=LLM(profile).chat, args=([],)) for _ in range(calls)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    finally:
        LLM._chat = orig
    return state["peak"]


def setup_function():
    llm._LIMITS.clear()
    llm._GATES.clear()


def test_default_is_two(monkeypatch):
    monkeypatch.delenv("ATENEA_MAX_PARALLEL", raising=False)
    assert _peak(ModelProfile(url="http://a/v1"), 6) == 2


def test_env_read_at_call_time(monkeypatch):
    monkeypatch.setenv("ATENEA_MAX_PARALLEL", "4")
    assert _peak(ModelProfile(url="http://b/v1"), 8) == 4


def test_set_max_parallel_global_and_per_url(monkeypatch):
    monkeypatch.delenv("ATENEA_MAX_PARALLEL", raising=False)
    set_max_parallel(3)
    set_max_parallel(5, "http://c/v1")
    assert max_parallel(ModelProfile(url="http://other/v1")) == 3
    assert _peak(ModelProfile(url="http://c/v1"), 10) == 5


def test_profile_wins():
    set_max_parallel(1)
    assert _peak(ModelProfile(url="http://d/v1", max_parallel=4), 8) == 4
