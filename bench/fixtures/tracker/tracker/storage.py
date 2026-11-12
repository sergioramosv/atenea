import json
import os
from dataclasses import asdict

from .models import Task

VERSION = 1


class Storage:
    """Persistencia en un fichero JSON con formato {"version": N, "tasks": [...]}."""

    def __init__(self, path):
        self.path = path
        self.tasks = {}
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            self.tasks = {t["id"]: Task(**t) for t in data["tasks"]}

    def save(self):
        with open(self.path, "w") as f:
            json.dump({"version": VERSION, "tasks": [asdict(t) for t in self.tasks.values()]}, f, indent=1)

    def next_id(self):
        """Siguiente id libre: uno más que el mayor id existente (1 si no hay tareas)."""
        return len(self.tasks) + 1

    def add(self, title, priority=2, tags=None, due=None):
        t = Task(self.next_id(), title, priority=priority, tags=list(tags or []), due=due)
        self.tasks[t.id] = t
        return t

    def delete(self, task_id):
        del self.tasks[task_id]
