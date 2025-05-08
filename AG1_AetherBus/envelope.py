from dataclasses import dataclass, field, fields
from typing import Any
import uuid
from datetime import datetime
import time

@dataclass
class Envelope:
    role: str
    content: Any = None
    session_code: str | None = None
    agent_name: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    billing_hint: str | None = None
    trace: list[str] = field(default_factory=list)
    user_id: str | None = None
    task_id: str | None = None
    target: str | None = None
    reply_to: str | None = None
    envelope_type: str | None = "message"
    tools_used: list[str] = field(default_factory=list)
    auth_signature: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    headers: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    envelope_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str | None = None

    def to_dict(self):
        return self.__dict__

    @classmethod
    def from_dict(cls, data: dict):
        allowed = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in allowed}
        return cls(**clean)

    def add_hop(self, who: str):
        self.trace.append(f"{who}:{int(time.time())}")
