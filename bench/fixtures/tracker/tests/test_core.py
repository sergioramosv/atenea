import io
import os
import tempfile
import unittest

from tracker import report
from tracker.cli import main
from tracker.query import filter_tasks
from tracker.storage import Storage


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "db.json")
        self.s = Storage(self.db)
        self.s.add("API login", priority=1, tags=["backend"], due="2026-11-01")
        self.s.add("Pantalla login", priority=3, tags=["Frontend"], due="2026-12-15")
        self.s.add("Docs", priority=2, tags=["docs"])
        self.s.add("Deploy", priority=2, tags=["backend", "ops"], due="2026-10-20")

    def test_next_id_after_delete(self):
        self.s.delete(2)
        self.assertEqual(self.s.add("Nueva").id, 5)

    def test_tag_case_insensitive(self):
        self.assertEqual([t.id for t in filter_tasks(self.s.tasks.values(), "tag:frontend")], [2])

    def test_priority_le(self):
        self.assertEqual(sorted(t.id for t in filter_tasks(self.s.tasks.values(), "priority<=2")), [1, 3, 4])

    def test_overdue_excludes_done(self):
        self.s.tasks[4].status = "done"
        self.assertEqual([t.id for t in report.overdue(self.s.tasks.values(), "2026-11-10")], [1])

    def test_summary_has_all_statuses(self):
        self.assertEqual(report.summary(self.s.tasks.values()), {"open": 4, "in_progress": 0, "done": 0})

    def test_cli_add_and_list(self):
        self.s.save()
        out = io.StringIO()
        main(["--db", self.db, "add", "Tests e2e", "--priority", "1", "--tags", "qa"], out=out)
        self.assertEqual(out.getvalue().strip(), "5")
        out = io.StringIO()
        main(["--db", self.db, "list", "tag:qa"], out=out)
        self.assertEqual(out.getvalue(), "5\topen\tP1\tTests e2e\n")


if __name__ == "__main__":
    unittest.main()
