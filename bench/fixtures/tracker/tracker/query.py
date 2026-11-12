"""Filtro de tareas con un mini-lenguaje.

Términos separados por espacios, que se combinan con AND:
  status:<estado>
  tag:<etiqueta>          (sin distinguir mayúsculas/minúsculas)
  priority<op><n>         (comparación numérica)
  due<op><AAAA-MM-DD>     (las tareas sin fecha nunca cumplen un término due)
con <op> en  <, <=, >, >=, =
"""
import operator
import re

OPS = {"<=": operator.lt, ">=": operator.ge, "<": operator.lt, ">": operator.gt, "=": operator.eq}
TERM = re.compile(r"^(priority|due)(<=|>=|<|>|=)(.+)$")


def _match(task, term):
    if term.startswith("status:"):
        return task.status == term[7:]
    if term.startswith("tag:"):
        return term[4:] in task.tags
    m = TERM.match(term)
    if not m:
        raise ValueError(f"término no válido: {term}")
    field, op, value = m.groups()
    if field == "priority":
        return OPS[op](task.priority, int(value))
    if task.due is None:
        return False
    return OPS[op](task.due, value)


def filter_tasks(tasks, expr):
    terms = expr.split()
    return [t for t in tasks if all(_match(t, term) for term in terms)]
