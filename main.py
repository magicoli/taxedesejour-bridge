#!/usr/bin/env python3
"""Taxe de séjour -- reconciliation Beds24 / nordbasseterre.taxesejour.fr

Default mode submits and updates declarations automatically.

Usage:
  ./run.sh                        # all pending months -- submit/update
  ./run.sh --month 2026-05        # specific month -- submit/update
  ./run.sh --dry-run              # report only, no submissions
  ./run.sh --dry-run --month 2026-05
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import state as st
from beds24 import Booking, BookingGroup, get_bookings, group_bookings, set_booking_custom1
from config import BEDS24_BOOKING_URL, VALID_STATUSES
from taxesejour import TaxeSejourClient


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--month", metavar="YYYY-MM")
    p.add_argument("--dry-run", action="store_true",
                   help="Report only -- do not submit or update declarations")
    p.add_argument("--no-beds24-note", action="store_true",
                   help="Do not write to Beds24 custom1 field after submission")
    return p.parse_args()


# ── Canonical row structure ───────────────────────────────────────────────────
# 15 columns, same order in terminal table, CSV, and Beds24 note.

@dataclass
class Row:
    check_in: date
    check_out: date
    nights: int
    units: str              # "Moon+Sun"
    client: str             # guest name (for control, not submitted to taxesejour)
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
    statut: str             # "add" / "add !" / "ok" / "ok !" / "update !" / "n/a" / "—"
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
    g: BookingGroup,
    records: dict[str, st.DeclarationRecord],
    site_detail: dict[tuple, tuple],
) -> Row:
    """Compute all fields for a booking group.

    `site_detail` = {(check_in, check_out): (stay_id, taxe)} already declared on
    taxesejour.fr (incl. manual entries) — used to capture the site ID, avoid
    duplicates, and compare the site's taxe against ours.
    """
    is_plat = g.is_platform
    source  = ", ".join(g.platform_names) if is_plat else ""

    # Match against what's already declared on the site (exact dates).
    site_match = site_detail.get((g.check_in, g.check_out))
    on_site    = site_match is not None
    site_id    = site_match[0] if on_site else ""
    site_taxe  = site_match[1] if on_site else None

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

    # errors:        Beds24 data problems that must be fixed before any fill can run.
    # notifications: informational issues (known mismatches, amounts changed);
    #                shown before the table, do not block submission.
    errors:        list[str] = []
    notifications: list[str] = []
    if not is_plat:
        # Group sub-bookings with invalid status are a Beds24 encoding error.
        # We still include them for correct totals but force status "error".
        invalid_subs = [b for b in g.bookings if b.status not in VALID_STATUSES]
        if invalid_subs:
            confirmed_total = sum(
                b.total_received for b in g.bookings if b.status in VALID_STATUSES
            )
            full_total = g.total_received
            warn = (
                f"{len(invalid_subs)} linked booking(s) with invalid Beds24 status "
                f"(confirmed={confirmed_total:.2f}, group={full_total:.2f}, "
                f"diff={full_total - confirmed_total:.2f}) -- fix status in Beds24"
            )
            for b in invalid_subs:
                warn += f" -- {BEDS24_BOOKING_URL.format(book_id=b.book_id)}"
            errors.append(warn)
        if g.has_amount and not g.has_occupants:
            warn = "missing occupants -- fix in Beds24"
            for bid in book_ids_list:
                warn += f" -- {BEDS24_BOOKING_URL.format(book_id=bid)}"
            errors.append(warn)
        # Amount changed since last declaration: needs updating (informational).
        for bid, old_ht, new_ht in amount_changes:
            diff = new_ht - old_ht
            notifications.append(
                f"amount changed since declaration: {old_ht:.2f} -> {new_ht:.2f} net "
                f"(diff {diff:.2f}) -- {BEDS24_BOOKING_URL.format(book_id=bid)}"
            )
        # computed != registered: the taxesejour.fr declaration has a different taxe.
        # Normal workflow -- will be resolved by --fill when update is implemented.
        if on_site and g.has_occupants and abs((site_taxe or 0) - g.computed_taxe) > 0.005:
            diff_ts = (site_taxe or 0) - g.computed_taxe
            notifications.append(
                f"computed {g.computed_taxe:.2f} != registered {site_taxe:.2f} "
                f"(diff {diff_ts:.2f}) -- declaration ts#{site_id} needs update"
            )
        # estimated != computed: Beds24 provisional taxe differs from our formula.
        # Known issue; does not affect the declared total, just the invoice label.
        if g.taxe_in_invoice > 0 and abs(g.computed_taxe - g.taxe_in_invoice) > 0:
            diff_b24 = g.computed_taxe - g.taxe_in_invoice
            warn = (
                f"estimated {g.taxe_in_invoice:.2f} != computed {g.computed_taxe:.2f} "
                f"(diff {diff_b24:.2f})"
            )
            for b in g.bookings:
                if b.taxe_in_invoice > 0:
                    warn += f" -- {BEDS24_BOOKING_URL.format(book_id=b.book_id)}"
            notifications.append(warn)
    warnings = errors + notifications

    # taxesejour ID: prefer the live site match, fall back to local state
    id_ts = site_id or id_ts
    site_taxe_mismatch = (
        on_site and g.has_occupants and abs((site_taxe or 0) - g.computed_taxe) > 0.005
    )

    # Declaration amounts (None for platforms and 0-amount bookings).
    # total == g.total_received by construction (ht*(1+VAT) + ts == received).
    base_ht = taxe_sej = total = None
    if not is_plat and g.has_amount:
        base_ht  = g.declared_amount
        taxe_sej = g.computed_taxe
        total    = g.total_received

    # Status values: error > update > ok > add > n/a > -
    if is_plat:
        statut = "-"
    elif not g.has_amount:
        statut = "n/a"
    elif errors:
        statut = "error"           # Beds24 data error -- must fix first
    elif on_site and site_taxe_mismatch:
        statut = "update"          # on site but taxe differs
    elif on_site:
        statut = "ok"
    elif all_tracked and amount_changes:
        statut = "update"          # amount changed since declaration
    elif all_tracked:
        statut = "ok"
    else:
        statut = "add"

    return Row(
        check_in           = g.check_in,
        check_out          = g.check_out,
        nights             = g.nights,
        units              = "+".join(g.units),
        client             = g.client_name,
        adults             = g.adults,
        children           = g.children,
        ids_b24            = ids_b24,
        origine            = source,
        ttc_b24            = g.total_received,      # Brut = total reçu du client
        taxe_b24           = g.taxe_in_invoice,     # dont Est. Tax provisoire encaissée
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
    all_groups   = group_bookings(all_bookings)
    site_detail  = client.get_declared_stays_detail(year, month, period_id)
    rows         = [_build_row(g, records, site_detail) for g in all_groups]

    if fill:
        for row, g in zip(rows, all_groups):
            # "error": Beds24 data problem — skip, must fix first.
            # "failed": previous submission error — skip, must investigate before retrying.
            # "add": new declaration → add_stay.
            # "update": declaration exists but taxe differs → update_stay in place.
            if row.statut not in ("add", "update") or row.base_ht is None:
                continue
            try:
                if row.statut == "update" and row.id_ts:
                    ts_id = client.update_stay(
                        stay_id   = row.id_ts,
                        month     = date(year, month, 1),
                        check_in  = g.check_in,
                        check_out = g.check_out,
                        adults    = g.adults,
                        children  = g.children,
                        amount    = g.declared_amount,
                    )
                    row.statut = "updated"
                else:
                    ts_id = client.add_stay(
                        month     = date(year, month, 1),
                        period_id = period_id,
                        check_in  = g.check_in,
                        check_out = g.check_out,
                        adults    = g.adults,
                        children  = g.children,
                        amount    = g.declared_amount,
                    )
                    row.statut = "added"
                row.id_ts  = ts_id
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
                        if set_booking_custom1(b.book_id, st.beds24_note_value(rec, row)):
                            rec.beds24_noted = True
            except Exception as e:
                row.statut = "failed"
                row.warnings.append(f"submission failed: {e}")

        st.save(records)

    return rows


# ── Per-run output (ONLY warnings/errors, one line each) ──────────────────────

def print_run_warnings(month_label: str, rows: list[Row]) -> None:
    issues = [r for r in rows if r.warnings]
    if not issues:
        return
    print(f"\n-- {month_label}")
    for row in issues:
        prefix = f"{row.check_in.strftime('%d/%m/%y')}->{row.check_out.strftime('%d/%m/%y')} [{row.units}]"
        for w in row.warnings:
            print(f"{prefix}  {w}")


# ── Recap table ───────────────────────────────────────────────────────────────

_D = ""

def _v(x: Optional[float], w: int = 9) -> str:
    return f"{x:>{w}.2f}" if x is not None else f"{_D:>{w}}"

def _s(x: str, w: int) -> str:
    """Truncate string to width."""
    return x[:w] if len(x) > w else x

def _csv_path(year: int, month: int) -> str:
    from pathlib import Path
    Path("data").mkdir(exist_ok=True)
    return f"data/taxe-de-sejour-{year}-{month:02d}.csv"


def _totals(rows: list[Row]) -> dict:
    acc = dict(ttc=0.0, taxe_b=0.0, ht=0.0, taxe_s=0.0, total=0.0)
    for r in rows:
        acc["ttc"]    += r.ttc_b24
        acc["taxe_b"] += r.taxe_b24
        if r.base_ht is not None:
            acc["ht"]    += r.base_ht
            acc["taxe_s"] += r.taxe_sejour   # type: ignore[operator]
            acc["total"]  += r.total          # type: ignore[operator]
    return acc


def _render_terminal(rows: list[Row], acc: dict) -> None:
    HDR = (
        f"{'Checkin':8} {'Checkout':8} {'Ngt':>5}  "
        f"{'Accommodation':18} {'Client':18} {'Ad':>2} {'Ch':>2}  "
        f"{'ChanMgrID':14} {'Origin':12}  "
        f"{'Gross':>9} {'EstTax':>9}  "
        f"{'RegisterID':10}  "
        f"{'Net':>9} {'TouristTax':>9} {'TTC':>9}  "
        f"Status"
    )
    SEP = "-" * len(HDR)
    print(SEP)
    print(HDR)
    print(SEP)
    for r in rows:
        print(
            f"{r.check_in.strftime('%d/%m/%y'):8} {r.check_out.strftime('%d/%m/%y'):8}"
            f" {r.nights:>5}  "
            f"{_s(r.units, 18):18} {_s(r.client, 18):18} {r.adults:>2} {r.children:>2}  "
            f"{_s(r.ids_b24, 14):14} {r.origine:12}  "
            f"{_v(r.ttc_b24):>9} {_v(r.taxe_b24 or None):>9}  "
            f"{_s(r.id_ts, 10):10}  "
            f"{_v(r.base_ht):>9} {_v(r.taxe_sejour):>9} {_v(r.total):>9}  "
            f"{r.statut}"
        )
    # prefix = 8+1+8+1+5+2+18+1+18+1+2+1+2+2+14+1+12+2 = 99 chars before money cols
    print(SEP)
    print(
        f"{'TOTAUX':99}"
        f"{_v(acc['ttc']):>9} {_v(acc['taxe_b'] or None):>9}  "
        f"{'':10}  "
        f"{_v(acc['ht']):>9} {_v(acc['taxe_s']):>9} {_v(acc['total']):>9}"
    )


def _write_csv(rows: list[Row], acc: dict, csv_path: str) -> None:
    def _m(x: Optional[float]) -> str:
        return f"{x:.2f}".replace(".", ",") if x is not None else ""

    HEADERS = [
        "Checkin", "Checkout", "Nights", "Accommodation", "Client", "Adults", "Children",
        "ChanMgrID", "Origin", "Gross", "Estimated Tax", "RegisterID",
        "Excl.Tax", "Tourist Tax", "Total", "Status",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(HEADERS)
        for r in rows:
            w.writerow([
                r.check_in.strftime("%d/%m/%Y"), r.check_out.strftime("%d/%m/%Y"),
                r.nights, r.units, r.client, r.adults, r.children,
                r.ids_b24, r.origine,
                _m(r.ttc_b24), _m(r.taxe_b24 or None), r.id_ts,
                _m(r.base_ht), _m(r.taxe_sejour), _m(r.total),
                r.statut,
            ])
        w.writerow([])
        w.writerow(
            ["TOTAUX"] + [""] * 8
            + [_m(acc["ttc"]), _m(acc["taxe_b"] or None), ""]
            + [_m(acc["ht"]), _m(acc["taxe_s"]), _m(acc["total"]), ""]
        )


def _action_line(rows: list[Row]) -> str:
    """Return the recommended next action based on the statuses in the recap.

    Status hierarchy (in priority order):
      error   -- Beds24 data problems that must be fixed before --fill
      add     -- not yet declared; --fill will submit them (-> added)
      update  -- declared but taxe differs; --fill will update them (-> updated)
      added   -- just submitted by --fill; verify on site and close the month
      updated -- just updated by --fill; same
      ok      -- declared correctly; close the month when all are ok
      n/a/-   -- no declaration needed (free stays / platform bookings)
    """
    from collections import Counter
    counts = Counter(r.statut for r in rows)

    if counts["error"] > 0:
        n = counts["error"]
        return f"{n} error(s) -- fix in Beds24 before running --fill"

    if counts["failed"] > 0:
        n = counts["failed"]
        return f"{n} submission(s) failed -- investigate errors above before retrying --fill"

    to_add    = counts["add"]
    to_update = counts["update"]
    if to_add + to_update > 0:
        parts = []
        if to_add:    parts.append(f"{to_add} to add")
        if to_update: parts.append(f"{to_update} to update")
        return ", ".join(parts) + " -- run --fill"

    just_done = counts["added"] + counts["updated"]
    if just_done > 0:
        return "declarations submitted -- verify on taxesejour.fr and submit the month"

    if counts["ok"] > 0:
        return "all declarations up to date -- submit the month on taxesejour.fr"

    # Only n/a and platform entries -- nothing declarable
    return "no declarations for this month -- submit an empty declaration on taxesejour.fr"


def print_recap(rows: list[Row], year: int, month: int) -> None:
    rows = sorted(rows, key=lambda r: (r.check_in, r.check_out))
    acc  = _totals(rows)
    _render_terminal(rows, acc)
    print(f"\n-> {_action_line(rows)}")
    _write_csv(rows, acc, _csv_path(year, month))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args       = parse_args()
    fill       = not args.dry_run
    write_note = fill and not args.no_beds24_note

    records = st.load()
    client  = TaxeSejourClient()
    client.login()

    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
        except ValueError:
            print(f"Invalid format: '{args.month}', expected YYYY-MM")
            sys.exit(1)
        # Search all actionable months (including non-"À déclarer" statuses)
        # to find the period_id. Falls back to explicit lookup if not found.
        all_pending = client.get_pending_months(year)
        match = next(((y, m, pid) for y, m, pid in all_pending
                      if y == year and m == month), None)
        if match is None:
            # Month not found in pending list — look up period_id directly
            periods = client._find_periods(year)
            month_str = f"{year}-{month:02d}-01"
            pid = next(
                (pid for pid, months in periods.items() if month_str in months),
                "",
            )
            match = (year, month, pid)
        months = [match]
    else:
        year   = date.today().year
        months = client.get_pending_months(year)
        if not months:
            print("No pending months found on taxesejour.fr.")
            return

    for y, m, pid in months:
        month_rows = process_month(y, m, pid, client, records,
                                   fill=fill, write_beds24_note=write_note)
        month_label = date(y, m, 1).strftime("%B %Y")
        print_run_warnings(month_label, month_rows)
        print_recap(month_rows, y, m)


if __name__ == "__main__":
    main()
