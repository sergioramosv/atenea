#!/bin/bash
# End-to-end benchmark of Atenea on the same tasks used for Claude Code. Uses the ornith service (:8080).
# Usage: bash run_bench.sh <label> <template> <task|@file> <hidden_tests> [--no-delegate]
LABEL=$1; TPL=$2; TASK=$3; HID=$4; shift 4; EXTRA="$*"
B=/mnt/models/atenea/bench; R=$B/results; mkdir -p $R
W=$B/work/$LABEL; rm -rf $W; mkdir -p $W; cp -r $TPL/. $W/; find $W -name __pycache__ -exec rm -rf {} + 2>/dev/null
( cd $W && git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm init )
cd /mnt/models/atenea
/mnt/models/atenea/.venv/bin/python -m atenea "$TASK" --cwd $W --json $R/$LABEL.json --quiet $EXTRA ${VERIFY:+--verify "$VERIFY"} > $R/$LABEL.out 2>&1
git -C $W diff --quiet HEAD -- tests/ && UNT=1 || UNT=0
git -C $W checkout -q HEAD -- tests/; cp $HID $W/tests/
OUT=$(cd $W && timeout 120 python3 -m unittest 2>&1 | tail -4)
RAN=$(echo "$OUT" | grep -o 'Ran [0-9]*' | grep -o '[0-9]*')
FAILS=$(echo "$OUT" | grep -oE '(failures|errors)=[0-9]+' | grep -o '[0-9]*' | paste -sd+ | bc 2>/dev/null); FAILS=${FAILS:-0}
python3 - $R/$LABEL.json "$LABEL" "${RAN:-0}" "$FAILS" "$UNT" <<'EOF' | tee -a $R/scores.jsonl
import json, sys
d = json.load(open(sys.argv[1]))
print(json.dumps({"label": sys.argv[2], "secs": d["secs"], "requests": d["requests"], "tool_calls": d["tool_calls"],
                  "prompt_tokens": d["prompt_tokens"], "completion_tokens": d["completion_tokens"],
                  "tests": int(sys.argv[3]), "failed": int(sys.argv[4]), "tests_untouched": int(sys.argv[5])}))
EOF
