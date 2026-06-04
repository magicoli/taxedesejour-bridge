"""Local declaration state — persisted in declarations.json.

Each entry tracks one Beds24 booking that has been declared (or intentionally
skipped) on taxesejour.fr, with the amounts at declaration time so we can
detect later changes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

STATE_FILE = Path(__file__).parent / "declarations.json"

AMOUNT_CHANGE_THRESHOLD = 0.0  # toute différence, même centimétrique, est signalée


@dataclass
class DeclarationRecord:
    book_id: str
    unit: str
    check_in: str           # ISO date
    check_out: str          # ISO date
    declared_at: str        # ISO datetime
    declared_amount_ht: float
    declared_adults: int
    declared_children: int
    status: str             # "declared" | "gift" | "blocked"
    beds24_noted: bool = False      # custom1 written to Beds24
    ts_stay_id: str = ""            # ID attribué par taxesejour.fr (ex. "14965011")

    def amount_changed(self, current_ht: float) -> bool:
        if self.status != "declared":
            return False
        return abs(current_ht - self.declared_amount_ht) > AMOUNT_CHANGE_THRESHOLD

    def change_delta(self, current_ht: float) -> float:
        return current_ht - self.declared_amount_ht


# ── Persistence ───────────────────────────────────────────────────────────────

def load() -> dict[str, DeclarationRecord]:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE) as f:
        raw = json.load(f)
    return {k: DeclarationRecord(**v) for k, v in raw.items()}


def save(records: dict[str, DeclarationRecord]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump({k: asdict(v) for k, v in records.items()}, f, indent=2, ensure_ascii=False)


# ── Mutation helpers ───────────────────────────────────────────────────────────

def mark_declared(
    records: dict[str, DeclarationRecord],
    book_id: str,
    unit: str,
    check_in: str,
    check_out: str,
    amount_ht: float,
    adults: int,
    children: int,
    ts_stay_id: str = "",
) -> DeclarationRecord:
    existing = records.get(book_id)
    rec = DeclarationRecord(
        book_id           = book_id,
        unit              = unit,
        check_in          = check_in,
        check_out         = check_out,
        declared_at       = datetime.now().isoformat(timespec="seconds"),
        declared_amount_ht= amount_ht,
        declared_adults   = adults,
        declared_children = children,
        status            = "declared",
        beds24_noted      = existing.beds24_noted if existing else False,
        ts_stay_id        = ts_stay_id or (existing.ts_stay_id if existing else ""),
    )
    records[book_id] = rec
    return rec


def mark_gift(
    records: dict[str, DeclarationRecord],
    book_id: str,
    unit: str,
    check_in: str,
    check_out: str,
) -> DeclarationRecord:
    rec = DeclarationRecord(
        book_id          = book_id,
        unit             = unit,
        check_in         = check_in,
        check_out        = check_out,
        declared_at      = datetime.now().isoformat(timespec="seconds"),
        declared_amount_ht = 0.0,
        declared_adults  = 0,
        declared_children= 0,
        status           = "gift",
        beds24_noted     = False,
    )
    records[book_id] = rec
    return rec


# ── Query helpers ─────────────────────────────────────────────────────────────

def is_tracked(records: dict[str, DeclarationRecord], book_id: str) -> bool:
    return book_id in records


def get(records: dict[str, DeclarationRecord], book_id: str) -> Optional[DeclarationRecord]:
    return records.get(book_id)


def beds24_note_value(rec: DeclarationRecord) -> str:
    """Compact string to store in Beds24 custom1 field."""
    s = (f"CANBT:{rec.declared_at[:10]}"
         f"|{rec.declared_amount_ht:.2f}€HT"
         f"|{rec.declared_adults}A/{rec.declared_children}E"
         f"|{rec.status}")
    if rec.ts_stay_id:
        s += f"|ts#{rec.ts_stay_id}"
    return s
