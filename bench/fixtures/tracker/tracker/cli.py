import argparse
import datetime
import sys

from . import report
from .query import filter_tasks
from .storage import Storage


def main(argv=None, out=sys.stdout, today=None):
    p = argparse.ArgumentParser(prog="tracker")
    p.add_argument("--db", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("title")
    a.add_argument("--priority", type=int, default=2)
    a.add_argument("--tags", default="")
    a.add_argument("--due")
    ls = sub.add_parser("list")
    ls.add_argument("query", nargs="*")
    d = sub.add_parser("done")
    d.add_argument("id", type=int)
    s = sub.add_parser("start")
    s.add_argument("id", type=int)
    sub.add_parser("report")
    args = p.parse_args(argv)

    st = Storage(args.db)
    if args.cmd == "add":
        t = st.add(args.title, args.priority, [x for x in args.tags.split(",") if x], args.due)
        st.save()
        print(t.id, file=out)
    elif args.cmd == "list":
        for t in filter_tasks(sorted(st.tasks.values(), key=lambda t: t.id), " ".join(args.query)):
            print(f"{t.id}\t{t.status}\tP{t.priority}\t{t.title}", file=out)
    elif args.cmd in ("done", "start"):
        st.tasks[args.id].status = "done" if args.cmd == "done" else "in_progress"
        st.save()
    elif args.cmd == "report":
        for k, v in report.summary(st.tasks.values()).items():
            print(f"{k}: {v}", file=out)
        od = report.overdue(st.tasks.values(), today or datetime.date.today().isoformat())
        print("overdue: " + ",".join(str(t.id) for t in od), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
