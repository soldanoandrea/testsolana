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
    day_key: str = ""                                 # local day the loss limit is measured against
    day_start_equity: float = 0.0                     # equity at that day's first tick

    @classmethod
    def load(cls, path: Path, start_balance: float, log=None) -> "State":
        if not path.exists():
            s = cls(path=path, cash=start_balance, start_balance=start_balance)
            s.save()
            return s
        d = json.loads(path.read_text())
        return cls(path=path, cash=d["cash"], start_balance=d["start_balance"], session_start=d["session_start"],
                   positions=cls._load_positions(d["positions"], log),
                   traded=set(d["traded"]), dead=set(d["dead"]), last_recap_day=d.get("last_recap_day", ""),
                   day_key=d.get("day_key", ""), day_start_equity=d.get("day_start_equity", 0.0))

    @staticmethod
    def _load_positions(raw: dict, log=None) -> dict[str, Position]:
        """A stop read back from disk is not trusted: the file could have been edited or half-written.

        Re-check the one invariant that bounds the loss — the stop is never below the hard stop off
        the real entry — and raise it back if it is. Lowering a stop is a violation wherever it comes
        from, so it is logged as one and the live gate will refuse to start on it.
        """
        out: dict[str, Position] = {}
        for mint, p in raw.items():
            pos = Position(**p)
            floor = pos.stop_floor()
            if pos.entry_price > 0 and pos.stop_price < floor - 1e-12:
                stored = pos.stop_price
                pos.raise_stop(floor)
                if log is not None:
                    log.violation("stop_below_floor_on_load", mint=mint, stored=stored, floor=floor,
                                  entry=pos.entry_price, detail="stop raised back to the hard floor")
            out[mint] = pos
        return out

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "cash": self.cash, "start_balance": self.start_balance, "session_start": self.session_start,
            "positions": {m: p.to_dict() for m, p in self.positions.items()},
            "traded": sorted(self.traded), "dead": sorted(self.dead), "last_recap_day": self.last_recap_day,
            "day_key": self.day_key, "day_start_equity": self.day_start_equity,
        }, indent=1))
        tmp.replace(self.path)
