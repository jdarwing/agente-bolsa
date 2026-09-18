"""
Bitácora de auditoría — transversal a todas las capas.
Registra cada DECISIÓN (no solo cada operación): propuestas aprobadas y rechazadas, frenos,
reactivaciones, errores. Formato JSON Lines, solo-append: nunca se edita ni se borra una línea.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, layer: str, event: str, **data: Any) -> dict:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "layer": layer, "event": event}
        rec.update({k: _plain(v) for k, v in data.items()})
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


def _plain(v: Any) -> Any:
    if is_dataclass(v) and not isinstance(v, type):
        return {k: _plain(x) for k, x in asdict(v).items()}
    if isinstance(v, (set, frozenset, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    return v
