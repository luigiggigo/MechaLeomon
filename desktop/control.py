"""Small, dependency-free control helpers shared by the GUI and tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ControlFiles:
    root: Path

    @property
    def pause_file(self) -> Path:
        return self.root / "pause.request"

    @property
    def stop_file(self) -> Path:
        return self.root / "stop.request"

    @property
    def status_file(self) -> Path:
        return self.root / "status.jsonl"

    @property
    def pause_ack_file(self) -> Path:
        return self.root / "pause.request.ack"

    def reset(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (self.pause_file, self.stop_file, self.status_file, self.pause_ack_file):
            path.unlink(missing_ok=True)

    def pause(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.pause_file.touch()

    def resume(self) -> None:
        self.pause_file.unlink(missing_ok=True)
        self.pause_ack_file.unlink(missing_ok=True)

    def stop(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.stop_file.touch()
        self.pause_file.unlink(missing_ok=True)
        self.pause_ack_file.unlink(missing_ok=True)


def parse_status_line(line: str) -> dict | None:
    """Parse one status-file line and reject malformed/non-object JSON."""
    try:
        value = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get("state"), str) else None
