from dataclasses import dataclass, field


@dataclass
class Reply:
    state: dict | None
    text: str
    remember: bool = False
    files: list[dict] = field(default_factory=list)
