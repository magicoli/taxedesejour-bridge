#!/usr/bin/env python3
"""Taxe de séjour — réconciliation Beds24 / nordbasseterre.taxesejour.fr

Usage:
  ./run.sh                        # tous les mois «À déclarer», dry-run
  ./run.sh --month 2026-04        # mois spécifique, dry-run
  ./run.sh --fill                 # soumettre les manquants
  ./run.sh --recap-only           # récap seul, sans interaction taxesejour.fr
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date

from beds24 import Booking, BookingGroup, get_bookings, group_by_dates
from config import BEDS24_BOOKING_URL, TAXE_RATE, VAT_RATE
from taxesejour import DeclaredStay, TaxeSejourClient


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--month", metavar="YYYY-MM",
                   help="Mois à traiter (défaut: tous les mois «À déclarer»)")
    p.add_argument("--fill", action="store_true",
                   help="Soumettre les séjours manquants (défaut: dry-run)")
    p.add_argument("--recap-only", action="store_true",
                   help="Afficher le récap sans toucher taxesejour.fr")
    return p.parse_args()


# ── Data container ─────────────────────────────────────────────────────────────

@dataclass
class MonthResult:
    year: int
    month: int
    all_bookings: list[Booking]
    direct_groups: list[BookingGroup]   # all direct groups (incl. gifts/blocked)
    declared: list[DeclaredStay]
    already: list[BookingGroup]         # matched to a declared stay
    missing: list[BookingGroup]         # not yet declared (eligible)
    submitted: list[BookingGroup]       # successfully submitted in --fill mode
    errors: list[tuple[BookingGroup, str]]  # (group, error_message)

    @property
    def gifts(self) -> list[BookingGroup]:
        return [g for g in self.direct_groups if not g.has_amount]

    @property
    def blocked(self) -> list[BookingGroup]:
        return [g for g in self.direct_groups if g.has_amount and not g.has_occupants]

    @property
    def declarable(self) -> list[BookingGroup]:
        return [g for g in self.direct_groups if g.has_amount and g.has_occupants]

    @property
    def platform_groups(self) -> list[BookingGroup]:
        return group_by_dates([b for b in self.all_bookings if b.is_platform])

    def status_of(self, g: BookingGroup) -> str:
        gid = id(g)
        if gid in {id(x) for x in self.submitted}:
            return "✓ soumis"
        if gid in {id(x) for x in self.already}:
            return "✓ déclaré"
        if gid in {id(x) for x in self.errors}:
            return "✗ erreur"
        return "✗ manquant"


# ── Reconciliation ─────────────────────────────────────────────────────────────

def _match_groups(
    groups: list[BookingGroup], declared: list[DeclaredStay]
) -> tuple[list[BookingGroup], list[BookingGroup]]:
    """1-to-1 match (exact then overlap). Returns (already, missing)."""
    remaining = list(declared)
    already, missing = [], []
    for g in groups:
        idx = next(
            (i for i, d in enumerate(remaining)
             if d.start_date == g.check_in and d.end_date == g.check_out),
            None,
        )
        if idx is None:
            idx = next(
                (i for i, d in enumerate(remaining)
                 if max(d.start_date, g.check_in) < min(d.end_date, g.check_out)),
                None,
            )
        if idx is not None:
            remaining.pop(idx)
            already.append(g)
        else:
            missing.append(g)
    return already, missing


# ── Per-month processing ───────────────────────────────────────────────────────

def process_month(
    year: int, month: int, client: TaxeSejourClient, fill: bool
) -> MonthResult:
    all_bookings = get_bookings(year, month)
    direct_groups = group_by_dates([b for b in all_bookings if not b.is_platform])
    declared      = client.get_declared_stays(year, month)

    declarable = [g for g in direct_groups if g.has_amount and g.has_occupants]
    already, missing = _match_groups(declarable, declared)

    submitted: list[BookingGroup] = []
    errors: list[tuple[BookingGroup, str]] = []

    if fill:
        for g in missing:
            try:
                client.add_stay(
                    month     = date(year, month, 1),
                    check_in  = g.check_in,
                    check_out = g.check_out,
                    adults    = g.adults,
                    children  = g.children,
                    amount    = g.declared_amount,
                    dry_run   = False,
                )
                submitted.append(g)
            except Exception as e:
                errors.append((g, str(e)))

    return MonthResult(
        year=year, month=month,
        all_bookings=all_bookings,
        direct_groups=direct_groups,
        declared=declared,
        already=already,
        missing=missing,
        submitted=submitted,
        errors=errors,
    )


# ── Per-month status output ────────────────────────────────────────────────────

def print_month_status(r: MonthResult, dry_run: bool) -> None:
    month_label = date(r.year, r.month, 1).strftime("%B %Y")
    n_direct   = len([b for b in r.all_bookings if not b.is_platform])
    n_platform = len([b for b in r.all_bookings if b.is_platform])

    print(f"\n── {month_label} {'─' * (50 - len(month_label))}")
    print(f"  Beds24: {len(r.all_bookings)} résas  "
          f"({n_direct} directes, {n_platform} plateformes)")
    print(f"  Taxesejour: {len(r.declared)} séjour(s) déclaré(s)")

    for g in r.declarable:
        units  = "+".join(g.units)
        status = r.status_of(g)
        print(f"  {status:12s}  [{units}]  "
              f"{g.check_in}→{g.check_out}  "
              f"{g.adults}A/{g.children}C  {g.declared_amount:.0f}€ HT")

    for g in r.gifts:
        print(f"  {'(cadeau)':12s}  [{'+'.join(g.units)}]  "
              f"{g.check_in}→{g.check_out}  non déclaré")

    for g in r.blocked:
        print(f"  {'⚠ bloqué':12s}  [{'+'.join(g.units)}]  "
              f"{g.check_in}→{g.check_out}  "
              f"occupants manquants — à corriger dans Beds24")

    for g, err in r.errors:
        print(f"  {'✗ erreur':12s}  [{'+'.join(g.units)}]  ERREUR: {err}")

    if dry_run and r.missing:
        print(f"  → {len(r.missing)} séjour(s) à soumettre (--fill)")


# ── Full recap ─────────────────────────────────────────────────────────────────

def print_recap(results: list[MonthResult]) -> None:
    W = 70
    print(f"\n{'═' * W}")
    print(f"  RÉCAP COMPLET")
    print(f"{'═' * W}")

    for r in results:
        month_label = date(r.year, r.month, 1).strftime("%B %Y")
        notes: list[str] = []

        print(f"\n{'─' * W}")
        print(f"  {month_label.upper()}")
        print(f"{'─' * W}\n")

        # ── Direct bookings ────────────────────────────────────────────────────
        print("  RÉSERVATIONS DIRECTES")
        print()
        print(f"  {'Dates':19s} {'Gîte(s)':20s} {'A':>2} {'E':>2} "
              f"{'HT (€)':>9} {'Taxe (€)':>9}  Statut")
        print(f"  {'─' * (W - 2)}")

        for g in r.declarable:
            units  = "+".join(g.units)
            d_str  = f"{g.check_in.strftime('%d/%m')}→{g.check_out.strftime('%d/%m')} {g.nights}n"
            status = r.status_of(g)
            print(f"  {d_str:19s} {units:20s} {g.adults:>2} {g.children:>2} "
                  f"{g.declared_amount:>9.2f} {g.computed_taxe:>9.2f}  {status}")
            # Taxe discrepancy note
            if g.taxe_in_invoice > 0 and abs(g.computed_taxe - g.taxe_in_invoice) > 0.50:
                diff = g.computed_taxe - g.taxe_in_invoice
                note = (f"{month_label} [{units}] {g.check_in}→{g.check_out}: "
                        f"taxe facturée {g.taxe_in_invoice:.2f}€ ≠ théorique "
                        f"{g.computed_taxe:.2f}€ (écart {diff:+.2f}€, ancien calcul)")
                for b in g.bookings:
                    note += (f"\n      Beds24: "
                             f"{BEDS24_BOOKING_URL.format(book_id=b.book_id)}")
                notes.append(note)

        for g in r.gifts:
            units = "+".join(g.units)
            d_str = f"{g.check_in.strftime('%d/%m')}→{g.check_out.strftime('%d/%m')} {g.nights}n"
            print(f"  {d_str:19s} {units:20s} {g.adults:>2} {g.children:>2} "
                  f"{'0.00':>9} {'—':>9}  cadeau/invitation")
            notes.append(f"{month_label} [{units}] {g.check_in}→{g.check_out}: "
                         f"montant 0€ → cadeau/invitation, non déclaré")

        for g in r.blocked:
            units = "+".join(g.units)
            d_str = f"{g.check_in.strftime('%d/%m')}→{g.check_out.strftime('%d/%m')} {g.nights}n"
            print(f"  {d_str:19s} {units:20s} {'?':>2} {'?':>2} "
                  f"{g.declared_amount:>9.2f} {g.computed_taxe:>9.2f}  ⚠ occupants manquants")
            note = (f"{month_label} [{units}] {g.check_in}→{g.check_out}: "
                    f"occupants non renseignés dans Beds24 → non soumis")
            for b in g.bookings:
                note += f"\n      Beds24: {BEDS24_BOOKING_URL.format(book_id=b.book_id)}"
            notes.append(note)

        for g, err in r.errors:
            units = "+".join(g.units)
            d_str = f"{g.check_in.strftime('%d/%m')}→{g.check_out.strftime('%d/%m')} {g.nights}n"
            print(f"  {d_str:19s} {units:20s} {g.adults:>2} {g.children:>2} "
                  f"{g.declared_amount:>9.2f} {g.computed_taxe:>9.2f}  ✗ erreur soumission")
            notes.append(f"{month_label} [{units}] {g.check_in}→{g.check_out}: "
                         f"erreur soumission: {err}")

        # ── Platforms ──────────────────────────────────────────────────────────
        if r.platform_groups:
            print()
            print("  PLATEFORMES (taxe collectée par elles)")
            print()
            print(f"  {'Dates':19s} {'Gîte(s)':20s} {'A':>2} {'E':>2} "
                  f"{'TTC (€)':>9}  Source")
            print(f"  {'─' * (W - 2)}")
            for g in r.platform_groups:
                units = "+".join(g.units)
                d_str = f"{g.check_in.strftime('%d/%m')}→{g.check_out.strftime('%d/%m')} {g.nights}n"
                srcs  = ", ".join(g.platform_names)
                print(f"  {d_str:19s} {units:20s} {g.adults:>2} {g.children:>2} "
                      f"{g.acc_amount_ttc:>9.2f}  {srcs}")

        # ── Totals ─────────────────────────────────────────────────────────────
        counted = r.declarable + r.blocked  # gifts excluded from totals
        ht_total   = sum(g.declared_amount for g in counted)
        tva_total  = ht_total * VAT_RATE
        taxe_total = ht_total * TAXE_RATE
        taxe_inv   = sum(g.taxe_in_invoice for g in counted)

        print()
        print(f"  TOTAUX DIRECTS")
        print(f"  {'HT déclarable:':30s} {ht_total:>10.2f} €")
        print(f"  {'TVA 2,1%:':30s} {tva_total:>10.2f} €")
        print(f"  {'Taxe de séjour théorique 5%:':30s} {taxe_total:>10.2f} €")
        print(f"  {'─' * 42}")
        print(f"  {'Total (TTC + taxe):':30s} {ht_total * (1 + VAT_RATE) + taxe_total:>10.2f} €")
        if taxe_inv > 0:
            diff = taxe_total - taxe_inv
            print(f"  {'Taxe déjà facturée Beds24:':30s} {taxe_inv:>10.2f} €  "
                  f"(écart vs théorique: {diff:+.2f}€)")

        # ── Notes ──────────────────────────────────────────────────────────────
        if notes:
            print()
            print("  NOTES")
            for i, n in enumerate(notes, 1):
                print(f"  [{i}] {n}")

    print(f"\n{'═' * W}\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    fill = args.fill

    client = TaxeSejourClient()
    client.login()

    # Determine which months to process
    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
            months = [(year, month)]
        except ValueError:
            print(f"Format invalide: '{args.month}', attendu YYYY-MM")
            sys.exit(1)
    else:
        year = date.today().year
        months = client.get_pending_months(year)
        if not months:
            print("Aucun mois «À déclarer» trouvé sur taxesejour.fr.")
            return

    label = ", ".join(date(y, m, 1).strftime("%B %Y") for y, m in months)
    mode  = "--fill" if fill else "dry-run"
    print(f"{'═' * 64}")
    print(f"  Taxe de séjour — {label}  [{mode}]")
    print(f"{'═' * 64}")

    results: list[MonthResult] = []

    for y, m in months:
        r = process_month(y, m, client, fill=fill and not args.recap_only)
        results.append(r)
        print_month_status(r, dry_run=not fill)

    if not fill:
        total_missing = sum(len(r.missing) for r in results)
        if total_missing:
            print(f"\n  {total_missing} séjour(s) à soumettre. Utiliser --fill pour envoyer.")

    # Full recap always at the end
    print_recap(results)


if __name__ == "__main__":
    main()
