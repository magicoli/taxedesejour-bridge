#!/usr/bin/env python3
"""Taxe de séjour — réconciliation Beds24 / nordbasseterre.taxesejour.fr

Usage:
  ./run.sh                        # tous les mois «À déclarer», dry-run
  ./run.sh --month 2026-04        # mois spécifique, dry-run
  ./run.sh --fill                 # soumettre les séjours manquants
  ./run.sh --recap-only           # récap seul, sans interaction taxesejour.fr
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import state as st
from beds24 import Booking, BookingGroup, get_bookings, group_by_dates, set_booking_custom1
from config import BEDS24_BOOKING_URL, TAXE_RATE, VAT_RATE
from taxesejour import TaxeSejourClient

# ── Platform name normalisation ───────────────────────────────────────────────
# Any source not listed here is "Direct" — we don't expose iCal vs API
_PLATFORM_DISPLAY: dict[str, str] = {
    "19": "Booking.com",
    "29": "Airbnb",
    "46": "Airbnb",
    # Expedia: add code here when known
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


# ── Recap row ─────────────────────────────────────────────────────────────────

@dataclass
class RecapRow:
    check_in: date
    check_out: date
    nights: int
    units: str              # "Moon+Sun"
    source: str             # "Direct" / "Airbnb" / "Booking.com"
    adults: int
    children: int
    beds24_ttc: float
    beds24_taxe: float      # taxe already invoiced in Beds24
    ht_decl: float | None   # None → platform or n/a
    taxe_5pct: float | None
    ttc_recalc: float | None  # ht_decl * 1.071
    status: str             # "add" / "add ↑" / "ok" / "ok ↑" / "update ↑" / "n/a" / "—"
    book_ids: list[str] = field(default_factory=list)


# ── GroupStatus ───────────────────────────────────────────────────────────────

@dataclass
class GroupStatus:
    group: BookingGroup
    source: str             # "Direct" / platform name
    is_platform: bool
    declared: bool
    submitted_now: bool = False
    amount_changes: list[tuple[str, float, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # inline warning lines
    submit_error: str = ""

    @property
    def has_issues(self) -> bool:
        return bool(self.warnings) or bool(self.submit_error)

    def status_str(self) -> str:
        suffix = " ↑" if self.has_issues else ""
        g = self.group
        if self.is_platform:
            return "—"
        if not g.has_amount:
            return "n/a"
        if not g.has_occupants:
            return f"add{suffix}"   # can't submit yet but still "to do"
        if self.submit_error:
            return f"err ↑"
        if self.submitted_now:
            return f"add{suffix}"
        if self.declared and self.amount_changes:
            return f"update ↑"
        if self.declared:
            return "ok"
        return f"add{suffix}"

    def to_recap_row(self) -> RecapRow:
        g = self.group
        ht = g.declared_amount if (not self.is_platform and g.has_amount) else None
        return RecapRow(
            check_in    = g.check_in,
            check_out   = g.check_out,
            nights      = g.nights,
            units       = "+".join(g.units),
            source      = self.source,
            adults      = g.adults,
            children    = g.children,
            beds24_ttc  = g.acc_amount_ttc,
            beds24_taxe = g.taxe_in_invoice,
            ht_decl     = ht,
            taxe_5pct   = (ht * TAXE_RATE) if ht is not None else None,
            ttc_recalc  = (ht * (1 + VAT_RATE + TAXE_RATE)) if ht is not None else None,
            status      = self.status_str(),
            book_ids    = [b.book_id for b in g.bookings],
        )


# ── Evaluate groups ───────────────────────────────────────────────────────────

def _evaluate(g: BookingGroup, records: dict[str, st.DeclarationRecord]) -> GroupStatus:
    # Determine source from majority of bookings (or any platform wins)
    sources = [_source_label(b.api_source) for b in g.bookings]
    is_plat = any(_is_platform(b.api_source) for b in g.bookings)
    source  = next((s for s in sources if s != "Direct"), "Direct")

    all_tracked = all(st.is_tracked(records, b.book_id) for b in g.bookings)
    changes = []
    if all_tracked:
        for b in g.bookings:
            rec = st.get(records, b.book_id)
            if rec and rec.amount_changed(b.declared_amount):
                changes.append((b.book_id, rec.declared_amount_ht, b.declared_amount))

    warnings = []
    if not is_plat:
        if g.has_amount and not g.has_occupants:
            warnings.append("occupants non renseignés dans Beds24")
        for bid, old_ht, new_ht in changes:
            warnings.append(
                f"montant modifié: déclaré {old_ht:.2f}€ HT → actuel {new_ht:.2f}€ HT "
                f"(Δ {new_ht-old_ht:+.2f}€)"
            )
        if g.taxe_in_invoice > 0 and abs(g.computed_taxe - g.taxe_in_invoice) > 0:
            diff = g.computed_taxe - g.taxe_in_invoice
            warnings.append(
                f"taxe facturée Beds24 {g.taxe_in_invoice:.2f}€ ≠ théorique "
                f"{g.computed_taxe:.2f}€ (Δ {diff:+.2f}€)"
            )

    return GroupStatus(
        group          = g,
        source         = source,
        is_platform    = is_plat,
        declared       = all_tracked,
        amount_changes = changes,
        warnings       = warnings,
    )


# ── MonthResult ───────────────────────────────────────────────────────────────

@dataclass
class MonthResult:
    year: int
    month: int
    statuses: list[GroupStatus]


# ── Per-month processing ──────────────────────────────────────────────────────

def process_month(
    year: int,
    month: int,
    period_id: str,
    client: TaxeSejourClient,
    records: dict[str, st.DeclarationRecord],
    fill: bool,
    write_beds24_note: bool,
) -> tuple[MonthResult, list[Booking]]:
    all_bookings = get_bookings(year, month)
    all_groups   = group_by_dates(all_bookings)
    statuses     = [_evaluate(g, records) for g in all_groups]

    if fill:
        for s in statuses:
            g = s.group
            if s.is_platform or not g.has_amount or not g.has_occupants or s.declared:
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
                s.submitted_now = True
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
                        ok  = set_booking_custom1(b.book_id, st.beds24_note_value(rec))
                        if ok:
                            rec.beds24_noted = True
            except Exception as e:
                s.submit_error = str(e)
                s.warnings.append(str(e))

        # Save n/a (0€) groups to state so they don't resurface
        for s in statuses:
            if not s.is_platform and not s.group.has_amount:
                for b in s.group.bookings:
                    if not st.is_tracked(records, b.book_id):
                        st.mark_gift(records, b.book_id, b.unit,
                                     b.check_in.isoformat(), b.check_out.isoformat())

        st.save(records)

    return MonthResult(year=year, month=month, statuses=statuses), all_bookings


# ── Per-month compact status output (printed BEFORE the recap table) ──────────

def print_month_status(r: MonthResult, dry_run: bool) -> None:
    label = date(r.year, r.month, 1).strftime("%B %Y")
    print(f"\n── {label} {'─' * (52 - len(label))}")

    for s in r.statuses:
        g     = s.group
        units = "+".join(g.units)
        d_str = f"{g.check_in.strftime('%d/%m')}→{g.check_out.strftime('%d/%m')}"
        stat  = s.status_str()

        if s.is_platform:
            # Platforms: minimal line, no declaration amounts
            print(f"  [{stat:6s}] {units:18s} {d_str}  {s.source}")
            continue

        if g.has_amount:
            print(f"  [{stat:6s}] {units:18s} {d_str}  "
                  f"{g.adults}A/{g.children}C  {g.declared_amount:.0f}€ HT")
        else:
            print(f"  [{stat:6s}] {units:18s} {d_str}  n/a")

        # Print inline warnings + Beds24 links (these are the only notes in the output)
        for w in s.warnings:
            print(f"           ↑ {w}")
        if s.warnings or s.submit_error:
            for b in g.bookings:
                print(f"             {BEDS24_BOOKING_URL.format(book_id=b.book_id)}")

    pending = [s for s in r.statuses
               if not s.is_platform and not s.declared and not s.submitted_now
               and s.group.has_amount and s.group.has_occupants]
    if dry_run and pending:
        print(f"  → {len(pending)} à soumettre (--fill)")


# ── Unified recap table ───────────────────────────────────────────────────────

_NA = "—"

def _fmt(v: float | None, w: int = 9) -> str:
    return f"{v:>{w}.2f}" if v is not None else f"{_NA:>{w}}"

def print_recap_table(rows: list[RecapRow], csv_path: str) -> None:
    # Sort all rows by check_in
    rows = sorted(rows, key=lambda r: r.check_in)

    W = 120
    print(f"\n{'═' * W}")
    print(f"  RÉCAP")
    print(f"{'═' * W}\n")

    HDR = (f"  {'Début→Fin':12s} {'N':>3}  {'Gîte(s)':20s} {'Source':12s}"
           f" {'A':>2} {'E':>2}"
           f" {'TTC B24':>9} {'Taxe B24':>9}"
           f" {'HT décl.':>9} {'Taxe 5%':>8} {'TTC+Taxe':>9}"
           f"  Statut")
    print(HDR)
    print(f"  {'─' * (W - 2)}")

    # Accumulators for totals
    tot_b24_ttc   = 0.0
    tot_b24_taxe  = 0.0
    tot_ht        = 0.0
    tot_taxe5     = 0.0
    tot_ttcrecalc = 0.0
    tot_plat_ttc  = 0.0

    for row in rows:
        d_str = (f"{row.check_in.strftime('%d/%m')}→"
                 f"{row.check_out.strftime('%d/%m')}")
        line = (f"  {d_str:12s} {row.nights:>3}n  {row.units:20s} {row.source:12s}"
                f" {row.adults:>2} {row.children:>2}"
                f" {_fmt(row.beds24_ttc):>9} {_fmt(row.beds24_taxe if row.beds24_taxe else None):>9}"
                f" {_fmt(row.ht_decl):>9} {_fmt(row.taxe_5pct):>8} {_fmt(row.ttc_recalc):>9}"
                f"  {row.status}")
        print(line)

        if row.source != "Direct":
            tot_plat_ttc += row.beds24_ttc
        else:
            tot_b24_ttc  += row.beds24_ttc
            tot_b24_taxe += row.beds24_taxe
            if row.ht_decl is not None:
                tot_ht        += row.ht_decl
                tot_taxe5     += row.taxe_5pct  # type: ignore[operator]
                tot_ttcrecalc += row.ttc_recalc  # type: ignore[operator]

    # Column prefix width = 2 + 12 + 1 + 3 + 3 + 20 + 1 + 12 + 1 + 2 + 1 + 2 = 60
    _PFX = 60
    print(f"  {'─' * (W - 2)}")
    print(f"  {'TOTAUX DÉCLARABLES':{_PFX - 2}s}"
          f" {_fmt(tot_b24_ttc):>9} {_fmt(tot_b24_taxe if tot_b24_taxe else None):>9}"
          f" {_fmt(tot_ht):>9} {_fmt(tot_taxe5):>8} {_fmt(tot_ttcrecalc):>9}")
    if tot_plat_ttc:
        print(f"  {'PLATEFORMES (brut Beds24)':{_PFX - 2}s}"
              f" {_fmt(tot_plat_ttc):>9}")

    print(f"\n{'═' * W}\n")

    # ── CSV export ────────────────────────────────────────────────────────────
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "Début", "Fin", "Nuits", "Gîte(s)", "Source", "Adultes", "Enfants",
            "TTC Beds24", "Taxe Beds24", "HT décl.", "Taxe 5%", "TTC+Taxe", "Statut",
        ])
        for row in rows:
            writer.writerow([
                row.check_in.isoformat(),
                row.check_out.isoformat(),
                row.nights,
                row.units,
                row.source,
                row.adults,
                row.children,
                f"{row.beds24_ttc:.2f}".replace(".", ","),
                f"{row.beds24_taxe:.2f}".replace(".", ",") if row.beds24_taxe else "",
                f"{row.ht_decl:.2f}".replace(".", ",") if row.ht_decl is not None else "",
                f"{row.taxe_5pct:.2f}".replace(".", ",") if row.taxe_5pct is not None else "",
                f"{row.ttc_recalc:.2f}".replace(".", ",") if row.ttc_recalc is not None else "",
                row.status,
            ])
        # Totals
        writer.writerow([])
        writer.writerow([
            "TOTAUX DÉCLARABLES", "", "", "", "", "", "",
            f"{tot_b24_ttc:.2f}".replace(".", ","),
            f"{tot_b24_taxe:.2f}".replace(".", ",") if tot_b24_taxe else "",
            f"{tot_ht:.2f}".replace(".", ","),
            f"{tot_taxe5:.2f}".replace(".", ","),
            f"{tot_ttcrecalc:.2f}".replace(".", ","),
            "",
        ])
        if tot_plat_ttc:
            writer.writerow([
                "PLATEFORMES (brut Beds24)", "", "", "", "", "", "",
                f"{tot_plat_ttc:.2f}".replace(".", ","),
                "", "", "", "", "",
            ])
    print(f"  Récap exporté: {csv_path}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args       = parse_args()
    fill       = args.fill and not args.recap_only
    write_note = fill and not args.no_beds24_note

    records = st.load()
    client  = TaxeSejourClient()
    client.login()

    # Determine months
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

    all_rows: list[RecapRow] = []

    for y, m, pid in months:
        result, all_bookings = process_month(
            y, m, pid, client, records, fill=fill, write_beds24_note=write_note
        )
        print_month_status(result, dry_run=not fill)
        for s in result.statuses:
            all_rows.append(s.to_recap_row())

    # Summary count
    pending = sum(
        1 for row in all_rows
        if row.status.startswith("add") and row.source == "Direct" and row.ht_decl is not None
    )
    if not fill and pending:
        print(f"\n  {pending} séjour(s) à soumettre. Utiliser --fill pour envoyer.")

    # Unified recap table — always last
    print_recap_table(all_rows, args.csv)


if __name__ == "__main__":
    main()
