# Benchmarks

End-to-end tasks used to compare Athena with Claude Code (both driving Ornith-1.5-35B-A3B on ZEUS).
The fixtures and task prompts are kept exactly as they were run (in Spanish) so results stay comparable.

| Fixture | Task prompt | Hidden tests |
|---|---|---|
| `fixtures/inventory` | `task1.txt` | `fixtures/hidden/inventory_hidden.py` |
| `fixtures/tracker` | `task2.txt` | `fixtures/hidden/tracker_hidden.py` |
| `fixtures/utils` | `task3.txt` | `fixtures/hidden/utils_hidden.py` |

```bash
VERIFY="python3 -m unittest" bash bench/run_bench.sh tracker-1 bench/fixtures/tracker @bench/task2.txt \
    bench/fixtures/hidden/tracker_hidden.py --no-delegate
```
Results are appended to `bench/results/scores.jsonl`.
