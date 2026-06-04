#!/usr/bin/env python3
"""Taxe de séjour — réconciliation Beds24 / nordbasseterre.taxesejour.fr

Usage:
  ./run.sh                        # tous les mois «À déclarer», dry-run
  ./run.sh --month 2026-04        # mois spécifique, dry-run
  ./run.sh --fill                 # soumettre les séjours manquants
  ./run.sh --recap-only           # récap seul, sans interaction taxesejour.fr
  ./run.sh --csv recap.csv        # fichier CSV (défaut: recap.csv)
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import state as st
from beds24 import Booking, BookingGroup, get_bookings, group_by_dates, set_booking_custom1
from config import BEDS24_BOOKING_URL, TAXE_RATE, VAT_RATE
from taxesejour import TaxeSejourClient

# ── Platform normalisation ────────────────────────────────────────────────────
_PLATFORM_DISPLAY: dict[str, str] = {
    "19": "Booking.com",
    "29": "Airbnb",
    "46": "Airbnb",
    # Add Expedia code when known
}

def _source_label(api_source: str) -> str:
    return _PLATFORM_DISPLAY.get(api_source, "Direct")

def _is_platform(api_source: str) -> bool:
    return api_source in _PLATFORM_DISPLAY


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--month", metavar="YYYY-MM")
    p.add_argument("--fill", action="store_true",
                   help="Soumettre les séjours manquants")
    p.add_argument("--recap-only", action="store_true",
                   help="Récap seul, sans interaction taxesejour.fr")
    p.add_argument("--no-beds24-note", action="store_true",
                   help="Ne pas écrire dans custom1 Beds24")
    p.add_argument("--csv", metavar="FILE", default="recap.csv",
                   help="Fichier CSV de sortie (défaut: recap.csv)")
    return p.parse_args()


# ── Canonical row structure ───────────────────────────────────────────────────
# 15 columns, same order in terminal table, CSV, and Beds24 note.

@dataclass
class Row:
    check_in: date
    check_out: date
    nights: int
    units: str              # "Moon+Sun"
    adults: int
    children: int
    ids_b24: str            # "81349082" or "81349082,71612097"
    origine: str            # "Direct" / "Airbnb" / "Booking.com"
    ttc_b24: float          # accommodation TTC from Beds24 invoice/price field
    taxe_b24: float         # taxe already invoiced in Beds24 (0 if none)
    id_ts: str              # taxesejour.fr stay id(s) from local state
    base_ht: Optional[float]    # None for platforms and n/a
    taxe_sejour: Optional[float]  # base_ht * 5%
    total: Optional[float]      # base_ht * 1.071 (= HT + TVA 2.1% + taxe 5%)
    statut: str             # "add" / "add ↑" / "ok" / "ok ↑" / "update ↑" / "n/a" / "—"
    warnings: list[str] = field(default_factory=list)
    book_ids_for_links: list[str] = field(default_factory=list)

    @property
    def delta(self) -> Optional[float]:
        """Total recalculé - (TTC B24 + Taxe B24). Positive = taxe sous-collectée."""
        if self.total is None:
            return None
        return self.total - (self.ttc_b24 + self.taxe_b24)


# ── GroupStatus → Row ─────────────────────────────────────────────────────────

def _build_row(
    g: BookingGroup, records: dict[str, st.DeclarationRecord]
) -> Row:
    """Compute all fields for a booking group."""
    is_plat = any(_is_platform(b.api_source) for b in g.bookings)
    source  = next((_source_label(b.api_source)
                    for b in g.bookings if _is_platform(b.api_source)), "Direct")

    # IDs
    ids_b24 = ",".join(b.book_id for b in g.bookings)
    book_ids_list = [b.book_id for b in g.bookings]

    # Local state: declared? amount changed? ts IDs?
    all_tracked = all(st.is_tracked(records, b.book_id) for b in g.bookings)
    ts_ids = sorted(set(
        r.ts_stay_id for b in g.bookings
        if (r := st.get(records, b.book_id)) and r.ts_stay_id
    ))
    id_ts = ",".join(ts_ids) if ts_ids else ""

    # Amount changes
    amount_changes: list[tuple[str, float, float]] = []
    if all_tracked:
        for b in g.bookings:
            rec = st.get(records, b.book_id)
            if rec and rec.amount_changed(b.declared_amount):
                amount_changes.append((b.book_id, rec.declared_amount_ht, b.declared_amount))

    # Warnings
    warnings: list[str] = []
    if not is_plat:
        if g.has_amount and not g.has_occupants:
            warn = f"occupants manquants — corriger dans Beds24"
            for bid in book_ids_list:
                warn += f" | {BEDS24_BOOKING_URL.format(book_id=bid)}"
            warnings.append(warn)
        for bid, old_ht, new_ht in amount_changes:
            diff = new_ht - old_ht
            warn = (f"montant modifié depuis déclaration: {old_ht:.2f}→{new_ht:.2f}€ HT "
                    f"(Δ {diff:+.2f}€) | {BEDS24_BOOKING_URL.format(book_id=bid)}")
            warnings.append(warn)
        if g.taxe_in_invoice > 0 and abs(g.computed_taxe - g.taxe_in_invoice) > 0:
            diff = g.computed_taxe - g.taxe_in_invoice
            warn = (f"taxe Beds24 {g.taxe_in_invoice:.2f}€ ≠ théorique "
                    f"{g.computed_taxe:.2f}€ (Δ {diff:+.2f}€)")
            for bid in book_ids_list:
                warn += f" | {BEDS24_BOOKING_URL.format(book_id=bid)}"
            warnings.append(warn)

    # Declaration amounts (None for platforms and 0€ bookings)
    base_ht = taxe_sej = total = None
    if not is_plat and g.has_amount:
        base_ht  = g.declared_amount
        taxe_sej = base_ht * TAXE_RATE
        total    = base_ht * (1 + VAT_RATE + TAXE_RATE)

    # Status
    has_issues = bool(warnings)
    suffix = " ↑" if has_issues else ""
    if is_plat:
        statut = "—"
    elif not g.has_amount:
        statut = "n/a"
    elif all_tracked and amount_changes:
        statut = f"update ↑"
    elif all_tracked:
        statut = f"ok{suffix}"
    else:
        statut = f"add{suffix}"

    return Row(
        check_in           = g.check_in,
        check_out          = g.check_out,
        nights             = g.nights,
        units              = "+".join(g.units),
        adults             = g.adults,
        children           = g.children,
        ids_b24            = ids_b24,
        origine            = source,
        ttc_b24            = g.acc_amount_ttc,
        taxe_b24           = g.taxe_in_invoice,
        id_ts              = id_ts,
        base_ht            = base_ht,
        taxe_sejour        = taxe_sej,
        total              = total,
        statut             = statut,
        warnings           = warnings,
        book_ids_for_links = book_ids_list,
    )


# ── Per-month processing ──────────────────────────────────────────────────────

def process_month(
    year: int,
    month: int,
    period_id: str,
    client: TaxeSejourClient,
    records: dict[str, st.DeclarationRecord],
    fill: bool,
    write_beds24_note: bool,
) -> list[Row]:
    all_bookings = get_bookings(year, month)
    all_groups   = group_by_dates(all_bookings)
    rows         = [_build_row(g, records) for g in all_groups]

    if fill:
        for row, g in zip(rows, all_groups):
            if (row.statut not in ("add", "add ↑") or row.base_ht is None
                    or not g.has_occupants):
                continue
            try:
                ts_id = client.add_stay(
                    month     = date(year, month, 1),
                    period_id = period_id,
                    check_in  = g.check_in,
                    check_out = g.check_out,
                    adults    = g.adults,
                    children  = g.children,
                    amount    = g.declared_amount,
                )
                # Update status in this run
                row.statut = "ok"
                row.id_ts  = ts_id
                # Save to state
                for b in g.bookings:
                    st.mark_declared(
                        records,
                        book_id    = b.book_id,
                        unit       = b.unit,
                        check_in   = b.check_in.isoformat(),
                        check_out  = b.check_out.isoformat(),
                        amount_ht  = b.declared_amount,
                        adults     = b.adults,
                        children   = b.children,
                        ts_stay_id = ts_id,
                    )
                    if write_beds24_note:
                        rec = records[b.book_id]
                        ok  = set_booking_custom1(b.book_id, st.beds24_note_value(rec, row))
                        if ok:
                            rec.beds24_noted = True
            except Exception as e:
                row.statut = "err ↑"
                row.warnings.append(f"erreur soumission: {e}")

        # Save n/a groups to state (0€ stay = not declarable)
        for row, g in zip(rows, all_groups):
            if not row.origine == "Direct" or row.ttc_b24 > 0:
                continue
            for b in g.bookings:
                if not st.is_tracked(records, b.book_id):
                    st.mark_gift(records, b.book_id, b.unit,
                                 b.check_in.isoformat(), b.check_out.isoformat())

        st.save(records)
        # Rebuild rows to reflect updated state
        rows = [_build_row(g, records) for g in all_groups]

    return rows


# ── Per-run output (ONLY warnings/errors, one line each) ──────────────────────

def print_run_warnings(month_label: str, rows: list[Row]) -> None:
    issues = [r for r in rows if r.warnings]
    if not issues:
        return
    print(f"\n── {month_label}")
    for row in issues:
        prefix = f"  {row.check_in.strftime('%d/%m')}→{row.check_out.strftime('%d/%m')} [{row.units}]"
        for w in row.warnings:
            print(f"  ⚠ {prefix}  {w}")


# ── Recap table ───────────────────────────────────────────────────────────────

_D = "—"

def _v(x: Optional[float], w: int = 9) -> str:
    return f"{x:>{w}.2f}" if x is not None else f"{_D:>{w}}"

def _s(x: str, w: int) -> str:
    """Truncate string to width."""
    return x[:w] if len(x) > w else x

def print_recap(rows: list[Row], csv_path: str) -> None:
    rows = sorted(rows, key=lambda r: (r.check_in, r.check_out))

    # ── Terminal table ────────────────────────────────────────────────────────
    # Column widths
    CW = {
        "date":    8,   # dd/mm/aa
        "nuits":   5,
        "units":   18,
        "pers":    2,   # A and E each
        "ids_b24": 14,  # truncated if needed
        "origine": 12,
        "money":   9,
        "id_ts":   10,
        "statut":  9,
    }

    def _date(d: date) -> str:
        return d.strftime("%d/%m/%y")

    HDR = (
        f"  {'Début':8} {'Fin':8} {'N':>5}  "
        f"{'Gîte(s)':18} {'A':>2} {'E':>2}  "
        f"{'ID Beds24':14} {'Origine':12}  "
        f"{'TTC B24':>9} {'Taxe B24':>9}  "
        f"{'ID TS':10}  "
        f"{'Base HT':>9} {'Taxe Séj.':>9} {'Total':>9}  "
        f"Statut"
    )
    W = len(HDR) + 2
    print(f"\n{'═' * W}")
    print(f"  RÉCAP")
    print(f"{'═' * W}\n")
    print(HDR)
    print(f"  {'─' * (W - 2)}")

    # Accumulators
    acc = dict(ttc=0.0, taxe_b=0.0, ht=0.0, taxe_s=0.0, total=0.0, plat=0.0)

    for row in rows:
        ids_short = _s(row.ids_b24, 14)
        id_ts_s   = _s(row.id_ts, 10)
        line = (
            f"  {_date(row.check_in):8} {_date(row.check_out):8} {row.nights:>5}  "
            f"{row.units:18} {row.adults:>2} {row.children:>2}  "
            f"{ids_short:14} {row.origine:12}  "
            f"{_v(row.ttc_b24):>9} {_v(row.taxe_b24 or None):>9}  "
            f"{id_ts_s:10}  "
            f"{_v(row.base_ht):>9} {_v(row.taxe_sejour):>9} {_v(row.total):>9}  "
            f"{row.statut}"
        )
        print(line)

        if row.origine == "Direct":
            acc["ttc"]  += row.ttc_b24
            acc["taxe_b"] += row.taxe_b24
            if row.base_ht is not None:
                acc["ht"]    += row.base_ht
                acc["taxe_s"] += row.taxe_sejour  # type: ignore[operator]
                acc["total"]  += row.total         # type: ignore[operator]
        else:
            acc["plat"] += row.ttc_b24

    # Totals row — prefix chars = 2+8+1+8+1+5+2+18+1+2+1+2+2+14+1+12 = 80
    # then "  " before TTC → 82 total chars before money columns
    PFX = 80
    print(f"  {'─' * (W - 2)}")
    print(
        f"  {'TOTAUX DIRECTS':{PFX}}"
        f"  {_v(acc['ttc']):>9} {_v(acc['taxe_b'] or None):>9}  "
        f"{'':10}  "
        f"{_v(acc['ht']):>9} {_v(acc['taxe_s']):>9} {_v(acc['total']):>9}"
    )
    if acc["plat"]:
        print(f"  {'PLATEFORMES':{PFX}}  {_v(acc['plat']):>9}")

    print(f"\n{'═' * W}\n")

    # ── CSV ───────────────────────────────────────────────────────────────────
    HEADERS = [
        "Début", "Fin", "Nuits", "Gîte(s)", "Adultes", "Enfants",
        "ID Beds24", "Origine",
        "TTC B24", "Taxe B24",
        "ID Taxesejour",
        "Base HT", "Taxe Séjour", "Total",
        "Statut",
    ]

    def _csv_money(x: Optional[float]) -> str:
        return f"{x:.2f}".replace(".", ",") if x is not None else ""

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(HEADERS)
        for row in rows:
            w.writerow([
                row.check_in.strftime("%d/%m/%Y"),
                row.check_out.strftime("%d/%m/%Y"),
                row.nights,
                row.units,
                row.adults,
                row.children,
                row.ids_b24,
                row.origine,
                _csv_money(row.ttc_b24),
                _csv_money(row.taxe_b24 or None),
                row.id_ts,
                _csv_money(row.base_ht),
                _csv_money(row.taxe_sejour),
                _csv_money(row.total),
                row.statut,
            ])
        w.writerow([])
        w.writerow(
            ["TOTAUX DIRECTS"] + [""] * 7
            + [_csv_money(acc["ttc"]), _csv_money(acc["taxe_b"] or None), ""]
            + [_csv_money(acc["ht"]), _csv_money(acc["taxe_s"]), _csv_money(acc["total"]), ""]
        )
        if acc["plat"]:
            w.writerow(
                ["PLATEFORMES"] + [""] * 7
                + [_csv_money(acc["plat"])] + [""] * 6
            )

    print(f"  Récap exporté: {csv_path}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args       = parse_args()
    fill       = args.fill and not args.recap_only
    write_note = fill and not args.no_beds24_note

    records = st.load()
    client  = TaxeSejourClient()
    client.login()

    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
        except ValueError:
            print(f"Format invalide: '{args.month}', attendu YYYY-MM")
            sys.exit(1)
        all_pending = client.get_pending_months(year)
        match = next(((y, m, pid) for y, m, pid in all_pending
                      if y == year and m == month), None)
        months = [match] if match else [(year, month, "")]
    else:
        year   = date.today().year
        months = client.get_pending_months(year)
        if not months:
            print("Aucun mois «À déclarer» trouvé sur taxesejour.fr.")
            return

    label = ", ".join(date(y, m, 1).strftime("%B %Y") for y, m, _ in months)
    mode  = "--fill" if fill else "dry-run"
    print(f"{'═' * 64}")
    print(f"  Taxe de séjour — {label}  [{mode}]")
    print(f"{'═' * 64}")

    all_rows: list[Row] = []
    for y, m, pid in months:
        month_rows = process_month(y, m, pid, client, records,
                                   fill=fill, write_beds24_note=write_note)
        month_label = date(y, m, 1).strftime("%B %Y")
        print_run_warnings(month_label, month_rows)
        all_rows.extend(month_rows)

    print_recap(all_rows, args.csv)


if __name__ == "__main__":
    main()
