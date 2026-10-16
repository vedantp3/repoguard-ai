from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.security import redact_object


class EventLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: str, **data: Any) -> None:
        record = redact_object({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        })
        line = json.dumps(record, ensure_ascii=True, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")