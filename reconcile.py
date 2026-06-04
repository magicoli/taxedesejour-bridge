#!/usr/bin/env python3
"""Reconcile Beds24 direct bookings with nordbasseterre.taxesejour.fr declarations.

Usage:
  python3 reconcile.py                   # previous month, dry-run
  python3 reconcile.py --month 2026-04   # specific month, dry-run
  python3 reconcile.py --fill            # actually submit missing stays
"""

from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date

from beds24 import Booking, get_direct_bookings
from taxesejour import DeclaredStay, TaxeSejourClient


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--month", metavar="YYYY-MM",
                   help="Month to process (default: current month)")
    p.add_argument("--fill", action="store_true",
                   help="Submit missing stays to taxesejour.fr (default: dry-run)")
    return p.parse_args()


def current_month() -> tuple[int, int]:
    today = date.today()
    return today.year, today.month


def _match_bookings(
    bookings: list[Booking], declared: list[DeclaredStay]
) -> tuple[list[Booking], list[Booking]]:
    """1-to-1 match between Beds24 bookings and declared stays.

    Matching strategy (in order of preference):
    1. Exact date match (check_in == start, check_out == end)
    2. Overlap — a declared stay covers the booking's date range

    Each declared stay is consumed at most once, to avoid a single
    declaration "absorbing" multiple bookings of the same group week.
    Returns (already_declared, missing).
    """
    remaining = list(declared)  # declared stays not yet matched
    already_declared: list[Booking] = []
    missing: list[Booking] = []

    for b in bookings:
        # Prefer exact match
        match_idx = next(
            (i for i, d in enumerate(remaining)
             if d.start_date == b.check_in and d.end_date == b.check_out),
            None,
        )
        if match_idx is None:
            # Fall back to overlap match
            match_idx = next(
                (i for i, d in enumerate(remaining)
                 if max(d.start_date, b.check_in) < min(d.end_date, b.check_out)),
                None,
            )
        if match_idx is not None:
            remaining.pop(match_idx)
            already_declared.append(b)
        else:
            missing.append(b)

    return already_declared, missing


def main():
    args = parse_args()

    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
        except ValueError:
            print(f"Error: invalid --month format '{args.month}', expected YYYY-MM")
            sys.exit(1)
    else:
        year, month = current_month()

    month_label = date(year, month, 1).strftime("%B %Y")
    print(f"\n{'='*60}")
    print(f"  Taxe de séjour reconciliation — {month_label}")
    print(f"{'='*60}\n")

    # ── 1. Fetch Beds24 direct bookings ────────────────────────────────────────
    print("Fetching Beds24 direct bookings…")
    try:
        b24_bookings = get_direct_bookings(year, month)
    except Exception as e:
        print(f"ERROR fetching Beds24: {e}")
        sys.exit(1)

    if not b24_bookings:
        print("  No direct bookings found in Beds24 for this month.\n")
    else:
        print(f"  Found {len(b24_bookings)} direct booking(s):\n")
        for b in b24_bookings:
            print(f"  [{b.unit:7s}] {b.check_in} → {b.check_out} "
                  f"({b.nights}n, {b.adults}A/{b.children}C) "
                  f"{b.price:.0f}€  — {b.guest}")

    # ── 2. Fetch already-declared stays ───────────────────────────────────────
    print("\nFetching declared stays from taxesejour.fr…")
    client = TaxeSejourClient()
    try:
        declared = client.get_declared_stays(year, month)
    except Exception as e:
        print(f"ERROR fetching taxesejour.fr: {e}")
        sys.exit(1)

    if not declared:
        print("  No stays declared yet for this month.")
    else:
        print(f"  Found {len(declared)} declared stay(s):\n")
        for d in declared:
            print(f"  {d.start_date} → {d.end_date} ({d.nights}n)  {d.label}")

    # ── 3. Reconcile ──────────────────────────────────────────────────────────
    print("\n--- Reconciliation ---\n")

    already_declared, missing = _match_bookings(b24_bookings, declared)
    declared_set = {id(b) for b in already_declared}

    for b in b24_bookings:
        if id(b) in declared_set:
            print(f"  ✓ [{b.unit:7s}] {b.check_in} → {b.check_out}  already declared")
        else:
            print(f"  ✗ [{b.unit:7s}] {b.check_in} → {b.check_out}  MISSING  ({b.guest})")

    print(f"\n  Summary: {len(already_declared)} already declared, {len(missing)} missing")

    if not missing:
        print("\n  Nothing to do.\n")
        return

    # ── 4. Fill missing (or dry-run) ──────────────────────────────────────────
    dry_run = not args.fill
    if dry_run:
        print("\n  [dry-run mode — use --fill to actually submit]\n")

    month_date = date(year, month, 1)
    errors = 0

    for b in missing:
        print(f"\n  Adding [{b.unit}] {b.check_in} → {b.check_out} "
              f"({b.adults}A/{b.children}C, {b.price:.0f}€)…")
        try:
            client.add_stay(
                month     = month_date,
                check_in  = b.check_in,
                check_out = b.check_out,
                adults    = b.adults,
                children  = b.children,
                amount    = b.price,
                dry_run   = dry_run,
            )
            if not dry_run:
                print("    → OK")
        except Exception as e:
            print(f"    → ERROR: {e}")
            errors += 1

    print()
    if errors:
        print(f"  {errors} error(s) occurred. Review the output above.")
        sys.exit(1)
    elif not dry_run:
        print(f"  Done. {len(missing)} stay(s) submitted to taxesejour.fr.")
        print("  Go to the site to review before submitting the monthly declaration.")
    print()


if __name__ == "__main__":
    main()
