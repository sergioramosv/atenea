from collections import Counter

from .models import STATUSES


def summary(tasks):
    """Número de tareas por estado, con todos los estados presentes (aunque sea 0)."""
    c = Counter(t.status for t in tasks)
    return {s: c[s] for s in c}


def overdue(tasks, today):
    """Tareas NO terminadas con fecha anterior a `today` (str ISO), ordenadas por fecha y luego por id."""
    return sorted((t for t in tasks if t.due and t.due < today), key=lambda t: (t.due, t.id))
