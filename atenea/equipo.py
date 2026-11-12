"""Atenea Equipo: para tareas de programación grandes, un equipo de subagentes en cadena:

    planificador (lee el código y escribe el plan) → programador (lo implementa y lo prueba) →
    revisor (revisa los cambios; si encuentra fallos, el programador los corrige, hasta 2 rondas) →
    probador (escribe/ejecuta las pruebas y comprueba el resultado) → resumen.

Cada paso es un subagente con su propio contexto (no llena el de la conversación) y aparece en los pasos de la tarea."""
from __future__ import annotations

import re

from .registry import Tool

MAX_ROUNDS = 2


def _clean(res: str) -> str:
    return re.sub(r"^Subagent result:\s*", "", res or "").strip()


def run(agent, task: str, test_command: str = "") -> str:
    agents = agent.agents or {}
    missing = [n for n in ("planificador", "programador", "revisor", "probador") if n not in agents]
    if missing:
        return f"Error: faltan los subagentes {', '.join(missing)} (atenea/agents)."
    agent._emit("notice", text="Atenea Equipo · 1/4 planificador")
    plan = _clean(agent._delegate({"agent": "planificador", "task": f"Tarea: {task}\n"
                                   + (f"Comando de pruebas sugerido: {test_command}\n" if test_command else "")
                                   + "Escribe el plan."}))
    agent._emit("notice", text="Atenea Equipo · 2/4 programador")
    work = _clean(agent._delegate({"agent": "programador", "task": f"Tarea: {task}\n\nPlan a seguir:\n{plan}"}))
    review = ""
    for rnd in range(1, MAX_ROUNDS + 1):
        agent._emit("notice", text=f"Atenea Equipo · 3/4 revisor (ronda {rnd})")
        review = _clean(agent._delegate({"agent": "revisor", "task": (
            f"Revisa los cambios hechos para esta tarea: {task}\nUsa `git diff` (y `git status`) si es un repo; si no, "
            f"lee los archivos que cambió el programador:\n{work[-2500:]}\n\nSi todo está bien responde empezando por "
            "'SIN PROBLEMAS'. Si no, lista solo los fallos reales, del más grave al menos grave.")}))
        if review.upper().startswith("SIN PROBLEMAS") or "SIN PROBLEMAS" in review.upper()[:200]:
            break
        if rnd == MAX_ROUNDS:
            break
        agent._emit("notice", text=f"Atenea Equipo · programador corrige (ronda {rnd})")
        work = _clean(agent._delegate({"agent": "programador", "task": (
            f"Tarea: {task}\n\nEl revisor encontró estos fallos en tus cambios; corrígelos:\n{review}")}))
    agent._emit("notice", text="Atenea Equipo · 4/4 probador")
    tests = _clean(agent._delegate({"agent": "probador", "task": (
        f"Comprueba que esta tarea funciona: {task}\n"
        + (f"Ejecuta: {test_command}\n" if test_command else "Busca y ejecuta las pruebas del proyecto; añade una prueba "
           "para lo nuevo si el proyecto tiene pruebas.\n")
        + "Informa qué pasa y qué falla, con la salida relevante.")}))
    return ("Atenea Equipo ha terminado. Resume al usuario el resultado en pocas líneas (qué se hizo, archivos, pruebas, "
            "lo que quede pendiente).\n\n## Plan\n" + plan[:3000] + "\n\n## Implementación\n" + work[:3000]
            + "\n\n## Revisión\n" + review[:2500] + "\n\n## Pruebas\n" + tests[:3000])


def tool(agent) -> Tool:
    return Tool("equipo", "Atenea Equipo: for big or risky coding tasks (several files, new features, refactors), run a "
                "team of subagents in sequence: planner → programmer → reviewer (fixes loop) → tester. Slower than doing "
                "it yourself, but more thorough. Give the task and, if you know it, the test command.",
                {"type": "object", "properties": {"task": {"type": "string"}, "test_command": {"type": "string"}},
                 "required": ["task"]},
                lambda task, test_command="": run(agent, task, test_command))
