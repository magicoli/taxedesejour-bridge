#!/usr/bin/env python3
"""Reconcile Beds24 direct bookings with nordbasseterre.taxesejour.fr declarations.

Usage:
  python3 reconcile.py                        # current month, dry-run
  python3 reconcile.py --month 2026-04        # specific month, dry-run
  python3 reconcile.py --month 2026-04 --fill # submit missing stays
  python3 reconcile.py --month 2026-04 --recap-only  # full recap, no form interaction
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from beds24 import Booking, BookingGroup, get_bookings, group_by_dates
from config import ICAL_SOURCE, PLATFORM_SOURCES, TAXE_RATE, VAT_RATE
from taxesejour import DeclaredStay, TaxeSejourClient


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--month", metavar="YYYY-MM",
                   help="Month to process (default: current month)")
    p.add_argument("--fill", action="store_true",
                   help="Submit missing stays to taxesejour.fr (default: dry-run)")
    p.add_argument("--recap-only", action="store_true",
                   help="Show full recap only, skip form interaction")
    return p.parse_args()


def current_month() -> tuple[int, int]:
    today = date.today()
    return today.year, today.month


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_amount(ht: float, ttc: float, taxe_inv: float) -> str:
    taxe_theorique = ht * TAXE_RATE
    s = f"{ht:8.2f}€ HT  (TTC: {ttc:.2f}€, taxe théorique: {taxe_theorique:.2f}€"
    if taxe_inv > 0:
        s += f", facturée: {taxe_inv:.2f}€"
        diff = taxe_theorique - taxe_inv
        if abs(diff) > 0.01:
            s += f" ← écart {diff:+.2f}€"
    s += ")"
    return s


def _warn(warnings: list[str]) -> str:
    return "  ⚠ " + ", ".join(warnings) if warnings else ""


# ── Reconciliation matching ───────────────────────────────────────────────────

def _match_groups(
    groups: list[BookingGroup], declared: list[DeclaredStay]
) -> tuple[list[BookingGroup], list[BookingGroup]]:
    """1-to-1 match between booking groups and declared stays.

    Returns (already_declared_groups, missing_groups).
    Each declared stay is consumed at most once.
    """
    remaining = list(declared)
    already: list[BookingGroup] = []
    missing: list[BookingGroup] = []

    for g in groups:
        # Try exact match first, then overlap
        match_idx = next(
            (i for i, d in enumerate(remaining)
             if d.start_date == g.check_in and d.end_date == g.check_out),
            None,
        )
        if match_idx is None:
            match_idx = next(
                (i for i, d in enumerate(remaining)
                 if max(d.start_date, g.check_in) < min(d.end_date, g.check_out)),
                None,
            )
        if match_idx is not None:
            remaining.pop(match_idx)
            already.append(g)
        else:
            missing.append(g)

    return already, missing


# ── Output sections ───────────────────────────────────────────────────────────

def print_recap(all_bookings: list[Booking], year: int, month: int) -> None:
    """Print full accounting recap — all sources, grouped by dates."""
    month_label = date(year, month, 1).strftime("%B %Y")
    print(f"\n{'─'*64}")
    print(f"  RECAP COMPLET — {month_label}")
    print(f"{'─'*64}\n")

    all_groups = group_by_dates(all_bookings)

    direct_groups   = [g for g in all_groups if not g.is_platform]
    platform_groups = [g for g in all_groups if g.is_platform]

    # ── Direct / iCal ─────────────────────────────────────────────────────────
    if direct_groups:
        print("  À DÉCLARER (direct / iCal):\n")
        for g in direct_groups:
            units_str = "+".join(g.units)
            print(f"  [{units_str:18s}] {g.check_in} → {g.check_out}  {g.nights}n  "
                  f"{g.adults}A/{g.children}C")
            print(f"    {_fmt_amount(g.declared_amount, g.acc_amount_ttc, g.taxe_in_invoice)}"
                  f"{_warn(g.warnings)}")
            for b in g.bookings:
                src_label = b.platform_name
                print(f"      ↳ {b.unit} (src={b.api_source}/{src_label}, bookId={b.book_id})"
                      f"  {b.adults}A/{b.children}C  {b.acc_amount_ttc:.2f}€ TTC")
    else:
        print("  Aucune réservation directe ce mois.\n")

    # ── Platforms ─────────────────────────────────────────────────────────────
    if platform_groups:
        print(f"\n  PLATEFORMES (taxe collectée par elles):\n")
        for g in platform_groups:
            units_str = "+".join(g.units)
            platforms = ", ".join(g.platform_names)
            print(f"  [{units_str:18s}] {g.check_in} → {g.check_out}  {g.nights}n  "
                  f"{g.adults}A/{g.children}C  {g.acc_amount_ttc:.2f}€ TTC  — {platforms}")

    # ── Summary totals ────────────────────────────────────────────────────────
    dir_ttc  = sum(g.acc_amount_ttc for g in direct_groups)
    dir_ht   = sum(g.declared_amount for g in direct_groups)
    dir_taxe = sum(g.computed_taxe for g in direct_groups)
    plat_ttc = sum(g.acc_amount_ttc for g in platform_groups)

    taxe_inv_total = sum(g.taxe_in_invoice for g in direct_groups)
    print(f"\n  ── Totaux ──────────────────────────────────────────────────────")
    print(f"  Direct:     {len(direct_groups):2} groupes  "
          f"TTC: {dir_ttc:9.2f}€  HT: {dir_ht:9.2f}€  Taxe théorique: {dir_taxe:.2f}€")
    if taxe_inv_total > 0:
        diff = dir_taxe - taxe_inv_total
        print(f"  Taxe déjà facturée Beds24: {taxe_inv_total:.2f}€  "
              f"(écart vs théorique: {diff:+.2f}€)")
    print(f"  Plateformes:{len(platform_groups):2} groupes  "
          f"TTC: {plat_ttc:9.2f}€  (taxe collectée par plateformes)")
    print()


def print_reconciliation(
    direct_groups: list[BookingGroup],
    declared: list[DeclaredStay],
) -> tuple[list[BookingGroup], list[BookingGroup]]:
    """Print diff and return (already_declared, missing)."""
    already, missing = _match_groups(direct_groups, declared)
    already_ids = {id(g) for g in already}

    print(f"  {'─'*58}")
    print(f"  RÉCONCILIATION\n")

    if not direct_groups:
        print("  Aucune réservation directe à déclarer.\n")
        return already, missing

    for g in direct_groups:
        units = "+".join(g.units)
        status = "✓" if id(g) in already_ids else "✗"
        flag = " MANQUANT" if id(g) not in already_ids else ""
        warn = _warn(g.warnings) if id(g) not in already_ids else ""
        print(f"  {status} [{units:18s}] {g.check_in} → {g.check_out}  "
              f"{g.adults}A/{g.children}C  {g.declared_amount:.2f}€ HT"
              f"{flag}{warn}")

    print(f"\n  ✓ {len(already)} déjà déclarés,  ✗ {len(missing)} manquants\n")
    return already, missing


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
        except ValueError:
            print(f"Erreur: format --month invalide '{args.month}', attendu YYYY-MM")
            sys.exit(1)
    else:
        year, month = current_month()

    month_label = date(year, month, 1).strftime("%B %Y")
    print(f"\n{'═'*64}")
    print(f"  Taxe de séjour — {month_label}")
    print(f"{'═'*64}")

    # ── 1. Fetch Beds24 ───────────────────────────────────────────────────────
    print("\nRécupération Beds24…")
    try:
        all_bookings = get_bookings(year, month)
    except Exception as e:
        print(f"ERREUR Beds24: {e}")
        sys.exit(1)

    print(f"  {len(all_bookings)} réservation(s) au total")

    direct_bookings   = [b for b in all_bookings if not b.is_platform]
    platform_bookings = [b for b in all_bookings if b.is_platform]
    direct_groups = group_by_dates(direct_bookings)

    print(f"  {len(direct_bookings)} directe(s) → {len(direct_groups)} groupe(s)")
    print(f"  {len(platform_bookings)} plateforme(s) (Airbnb/Booking — pas à déclarer)")

    # Always print the full recap
    print_recap(all_bookings, year, month)

    if args.recap_only:
        return

    # ── 2. Fetch taxesejour.fr ────────────────────────────────────────────────
    print("Récupération taxesejour.fr…")
    client = TaxeSejourClient()
    try:
        declared = client.get_declared_stays(year, month)
    except Exception as e:
        print(f"ERREUR taxesejour.fr: {e}")
        sys.exit(1)

    if declared:
        print(f"  {len(declared)} séjour(s) déclaré(s):")
        for d in declared:
            print(f"    {d.start_date} → {d.end_date} ({d.nights}n)")
    else:
        print("  Aucun séjour déclaré ce mois.")

    print()

    # ── 3. Reconcile ─────────────────────────────────────────────────────────
    _, missing = print_reconciliation(direct_groups, declared)

    if not missing:
        print("  Rien à faire.\n")
        return

    # ── 4. Fill ───────────────────────────────────────────────────────────────
    dry_run = not args.fill
    if dry_run:
        print("  Mode dry-run — utiliser --fill pour soumettre réellement.\n")
    else:
        print("  Soumission des séjours manquants…\n")

    month_date = date(year, month, 1)
    errors = 0

    for g in missing:
        units = "+".join(g.units)
        print(f"  → [{units}] {g.check_in} → {g.check_out} "
              f"({g.adults}A/{g.children}C, {g.declared_amount:.2f}€ HT)")

        if g.warnings:
            for w in g.warnings:
                print(f"     ⚠ {w}")
            if not g.has_amount or not g.has_occupants:
                print("     SKIP — données insuffisantes pour soumettre")
                errors += 1
                continue

        if dry_run:
            print("     [dry-run]")
            continue

        try:
            client.add_stay(
                month     = month_date,
                check_in  = g.check_in,
                check_out = g.check_out,
                adults    = g.adults,
                children  = g.children,
                amount    = g.declared_amount,
                dry_run   = False,
            )
            print("     ✓ soumis")
        except Exception as e:
            print(f"     ✗ ERREUR: {e}")
            errors += 1

    print()
    if errors and not dry_run:
        print(f"  {errors} erreur(s). Voir ci-dessus.")
        sys.exit(1)
    elif not dry_run:
        print(f"  {len(missing) - errors} séjour(s) soumis.")
        print("  Vérifiez sur le site avant de clôturer la déclaration mensuelle.")
    print()


if __name__ == "__main__":
    main()
