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

STATE_FILE = Path(__file__).parent / "data" / "declarations.json"

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


def beds24_note_value(rec: DeclarationRecord, row: "Any | None" = None) -> str:
    """Labeled field list stored in Beds24 custom1 field.

    Same 15 columns as the recap table, same order, vertical format.
    `row` is the Row dataclass from main.py (passed to avoid circular import).
    """
    from config import TAXE_RATE, VAT_RATE
    taxe  = rec.declared_amount_ht * TAXE_RATE
    total = rec.declared_amount_ht * (1 + VAT_RATE + TAXE_RATE)

    lines = [
        f"Début: {rec.check_in}",
        f"Fin: {rec.check_out}",
    ]
    if row is not None:
        lines.append(f"Nuits: {row.nights}")
        lines.append(f"Gîte(s): {row.units}")
    lines += [
        f"Adultes: {rec.declared_adults} | Enfants: {rec.declared_children}",
        f"ID Beds24: {rec.book_id}",
    ]
    if row is not None:
        lines.append(f"Origine: {row.origine}")
        lines.append(f"TTC B24: {row.ttc_b24:.2f}€"
                     + (f" | Taxe B24: {row.taxe_b24:.2f}€" if row.taxe_b24 else ""))
    if rec.ts_stay_id:
        lines.append(f"ID Taxesejour: {rec.ts_stay_id}")
    lines += [
        f"Base HT: {rec.declared_amount_ht:.2f}€",
        f"Taxe séjour: {taxe:.2f}€",
        f"Total: {total:.2f}€",
        f"Statut: déclaré le {rec.declared_at[:10]}",
    ]
    return " | ".join(lines)
