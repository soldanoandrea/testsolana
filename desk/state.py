"""Persistent desk state (survives restarts): balance, open positions, coins already traded/dead."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .models import Position


@dataclass
class State:
    path: Path
    cash: float = 0.0
    start_balance: float = 0.0
    session_start: float = field(default_factory=time.time)
    positions: dict[str, Position] = field(default_factory=dict)
    traded: set[str] = field(default_factory=set)     # one entry per coin, ever
    dead: set[str] = field(default_factory=set)       # Filter kills + Analyst <70
    last_recap_day: str = ""

    @classmethod
    def load(cls, path: Path, start_balance: float) -> "State":
        if not path.exists():
            s = cls(path=path, cash=start_balance, start_balance=start_balance)
            s.save()
            return s
        d = json.loads(path.read_text())
        return cls(path=path, cash=d["cash"], start_balance=d["start_balance"], session_start=d["session_start"],
                   positions={m: Position(**p) for m, p in d["positions"].items()},
                   traded=set(d["traded"]), dead=set(d["dead"]), last_recap_day=d.get("last_recap_day", ""))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "cash": self.cash, "start_balance": self.start_balance, "session_start": self.session_start,
            "positions": {m: p.to_dict() for m, p in self.positions.items()},
            "traded": sorted(self.traded), "dead": sorted(self.dead), "last_recap_day": self.last_recap_day,
        }, indent=1))
        tmp.replace(self.path)
