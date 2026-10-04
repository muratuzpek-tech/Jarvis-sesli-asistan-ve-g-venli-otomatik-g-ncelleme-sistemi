from dataclasses import dataclass

@dataclass
class Task:
    id: int
    title: str
    description: str
    priority: int
    status: str
    created_at: str
    completed_at: str
    tags: list[str]