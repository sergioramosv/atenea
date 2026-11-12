from dataclasses import dataclass, field

STATUSES = ("open", "in_progress", "done")


@dataclass
class Task:
    id: int
    title: str
    status: str = "open"          # open | in_progress | done
    priority: int = 2             # 1 = más urgente ... 4 = menos urgente
    tags: list = field(default_factory=list)
    due: str | None = None        # fecha ISO AAAA-MM-DD o None
