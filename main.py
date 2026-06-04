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

import state as st
from beds24 import Booking, BookingGroup, get_bookings, group_by_dates, set_booking_custom1
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
    p.add_argument("--no-beds24-note", action="store_true",
                   help="Ne pas écrire dans custom1 Beds24 après déclaration")
    return p.parse_args()


# ── Status per booking group ───────────────────────────────────────────────────

@dataclass
class GroupStatus:
    group: BookingGroup
    kind: str                    # "declarable" | "gift" | "blocked"
    declared: bool = False       # all bookings tracked in local state
    submitted_now: bool = False  # declared in this run
    amount_changes: list[tuple[str, float, float]] = field(default_factory=list)
    # (book_id, declared_ht, current_ht) for each changed booking
    submit_error: str = ""

    @property
    def has_changes(self) -> bool:
        return bool(self.amount_changes)

    def status_label(self) -> str:
        if self.kind == "gift":
            return "cadeau/invitation"
        if self.kind == "blocked":
            return "⚠ occupants manquants"
        if self.submit_error:
            return f"✗ erreur"
        if self.submitted_now:
            return "✓ soumis"
        if self.declared and self.has_changes:
            return "⚡ montant modifié"
        if self.declared:
            return "✓ déclaré"
        return "✗ manquant"


# ── Reconciliation with local state ───────────────────────────────────────────

def _evaluate_group(
    g: BookingGroup, records: dict[str, st.DeclarationRecord]
) -> GroupStatus:
    """Classify a group based on local state and current Beds24 data."""
    if not g.has_amount:
        return GroupStatus(group=g, kind="gift")
    if not g.has_occupants:
        return GroupStatus(group=g, kind="blocked")

    # Check if ALL bookings in the group are tracked in local state
    all_tracked = all(st.is_tracked(records, b.book_id) for b in g.bookings)
    if not all_tracked:
        return GroupStatus(group=g, kind="declarable", declared=False)

    # All tracked — check for amount changes
    changes = []
    for b in g.bookings:
        rec = st.get(records, b.book_id)
        if rec and rec.amount_changed(b.declared_amount):
            changes.append((b.book_id, rec.declared_amount_ht, b.declared_amount))

    return GroupStatus(group=g, kind="declarable", declared=True, amount_changes=changes)


# ── Per-month processing ───────────────────────────────────────────────────────

@dataclass
class MonthResult:
    year: int
    month: int
    all_bookings: list[Booking]
    statuses: list[GroupStatus]

    @property
    def platform_groups(self) -> list[BookingGroup]:
        return group_by_dates([b for b in self.all_bookings if b.is_platform])

    @property
    def declarable_statuses(self) -> list[GroupStatus]:
        return [s for s in self.statuses if s.kind == "declarable"]

    @property
    def gifts(self) -> list[GroupStatus]:
        return [s for s in self.statuses if s.kind == "gift"]

    @property
    def blocked(self) -> list[GroupStatus]:
        return [s for s in self.statuses if s.kind == "blocked"]

    @property
    def missing(self) -> list[GroupStatus]:
        return [s for s in self.declarable_statuses if not s.declared and not s.submitted_now]

    @property
    def changed(self) -> list[GroupStatus]:
        return [s for s in self.declarable_statuses if s.declared and s.has_changes]


def process_month(
    year: int,
    month: int,
    period_id: str,
    client: TaxeSejourClient,
    records: dict[str, st.DeclarationRecord],
    fill: bool,
    write_beds24_note: bool,
) -> MonthResult:
    all_bookings = get_bookings(year, month)
    direct_groups = group_by_dates([b for b in all_bookings if not b.is_platform])

    statuses = [_evaluate_group(g, records) for g in direct_groups]

    if fill:
        for s in statuses:
            if s.kind != "declarable" or s.declared:
                continue  # skip non-declarable and already-declared
            g = s.group
            try:
                ts_stay_id = client.add_stay(
                    month     = date(year, month, 1),
                    period_id = period_id,
                    check_in  = g.check_in,
                    check_out = g.check_out,
                    adults    = g.adults,
                    children  = g.children,
                    amount    = g.declared_amount,
                )
                s.submitted_now = True
                # Save to local state (one record per Beds24 booking in the group)
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
                        ts_stay_id = ts_stay_id,
                    )
                    if write_beds24_note:
                        rec = records[b.book_id]
                        note_val = st.beds24_note_value(rec)
                        ok = set_booking_custom1(b.book_id, note_val)
                        if ok:
                            rec.beds24_noted = True
            except Exception as e:
                s.submit_error = str(e)

        # Also save gifts to state so they don't re-appear as "missing"
        for s in statuses:
            if s.kind == "gift":
                for b in s.group.bookings:
                    if not st.is_tracked(records, b.book_id):
                        st.mark_gift(
                            records,
                            book_id   = b.book_id,
                            unit      = b.unit,
                            check_in  = b.check_in.isoformat(),
                            check_out = b.check_out.isoformat(),
                        )

        st.save(records)

    return MonthResult(year=year, month=month, all_bookings=all_bookings, statuses=statuses)


# ── Per-month compact output ───────────────────────────────────────────────────

def print_month_status(r: MonthResult, dry_run: bool) -> None:
    month_label = date(r.year, r.month, 1).strftime("%B %Y")
    n_direct   = len([b for b in r.all_bookings if not b.is_platform])
    n_platform = len([b for b in r.all_bookings if b.is_platform])

    print(f"\n── {month_label} {'─' * (50 - len(month_label))}")
    print(f"  Beds24: {len(r.all_bookings)} résas  "
          f"({n_direct} directes, {n_platform} plateformes)")

    for s in r.declarable_statuses:
        g = s.group
        units = "+".join(g.units)
        label = s.status_label()
        print(f"  {label:20s}  [{units}]  "
              f"{g.check_in}→{g.check_out}  "
              f"{g.adults}A/{g.children}C  {g.declared_amount:.0f}€ HT")
        if s.has_changes:
            for bid, old_ht, new_ht in s.amount_changes:
                print(f"    ⚡ bookId {bid}: déclaré {old_ht:.2f}€ HT → actuel {new_ht:.2f}€ HT "
                      f"(Δ {new_ht - old_ht:+.2f}€)")

    for s in r.gifts:
        print(f"  {'(cadeau)':20s}  [{'+'.join(s.group.units)}]  "
              f"{s.group.check_in}→{s.group.check_out}  non déclaré")

    for s in r.blocked:
        print(f"  {'⚠ occupants manquants':20s}  [{'+'.join(s.group.units)}]  "
              f"{s.group.check_in}→{s.group.check_out}")

    if s_errors := [s for s in r.statuses if s.submit_error]:
        for s in s_errors:
            print(f"  {'✗ erreur':20s}  [{'+'.join(s.group.units)}]  {s.submit_error}")

    pending = r.missing
    if dry_run and pending:
        print(f"  → {len(pending)} séjour(s) à soumettre (--fill)")
    if r.changed:
        print(f"  → {len(r.changed)} séjour(s) avec montant modifié (à réviser)")


# ── Full recap ─────────────────────────────────────────────────────────────────

def print_recap(results: list[MonthResult]) -> None:
    W = 72
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

        for s in r.declarable_statuses:
            g = s.group
            units = "+".join(g.units)
            d_str = f"{g.check_in.strftime('%d/%m')}→{g.check_out.strftime('%d/%m')} {g.nights}n"
            print(f"  {d_str:19s} {units:20s} {g.adults:>2} {g.children:>2} "
                  f"{g.declared_amount:>9.2f} {g.computed_taxe:>9.2f}  {s.status_label()}")

            # Amount changed note
            if s.has_changes:
                for bid, old_ht, new_ht in s.amount_changes:
                    delta = new_ht - old_ht
                    note = (f"{month_label} [{units}]: montant modifié depuis déclaration "
                            f"— déclaré {old_ht:.2f}€ HT, actuel {new_ht:.2f}€ HT (Δ {delta:+.2f}€)\n"
                            f"      Beds24: {BEDS24_BOOKING_URL.format(book_id=bid)}")
                    notes.append(note)

            # Taxe discrepancy note
            if g.taxe_in_invoice > 0 and abs(g.computed_taxe - g.taxe_in_invoice) > 0.50:
                diff = g.computed_taxe - g.taxe_in_invoice
                note = (f"{month_label} [{units}] {g.check_in}→{g.check_out}: "
                        f"taxe facturée Beds24 {g.taxe_in_invoice:.2f}€ ≠ théorique "
                        f"{g.computed_taxe:.2f}€ (écart {diff:+.2f}€, ancien calcul)")
                for b in g.bookings:
                    note += f"\n      Beds24: {BEDS24_BOOKING_URL.format(book_id=b.book_id)}"
                notes.append(note)

        for s in r.gifts:
            g = s.group
            units = "+".join(g.units)
            d_str = f"{g.check_in.strftime('%d/%m')}→{g.check_out.strftime('%d/%m')} {g.nights}n"
            print(f"  {d_str:19s} {units:20s} {g.adults:>2} {g.children:>2} "
                  f"{'0.00':>9} {'—':>9}  cadeau/invitation")
            notes.append(f"{month_label} [{units}] {g.check_in}→{g.check_out}: "
                         f"montant 0€ → cadeau/invitation, non déclaré")

        for s in r.blocked:
            g = s.group
            units = "+".join(g.units)
            d_str = f"{g.check_in.strftime('%d/%m')}→{g.check_out.strftime('%d/%m')} {g.nights}n"
            print(f"  {d_str:19s} {units:20s} {'?':>2} {'?':>2} "
                  f"{g.declared_amount:>9.2f} {g.computed_taxe:>9.2f}  ⚠ occupants manquants")
            note = (f"{month_label} [{units}] {g.check_in}→{g.check_out}: "
                    f"occupants non renseignés dans Beds24 → non soumis")
            for b in g.bookings:
                note += f"\n      Beds24: {BEDS24_BOOKING_URL.format(book_id=b.book_id)}"
            notes.append(note)

        for s in [s for s in r.statuses if s.submit_error]:
            notes.append(f"{month_label} [{'+'.join(s.group.units)}]: "
                         f"erreur soumission — {s.submit_error}")

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
        counted = [s.group for s in r.declarable_statuses + r.blocked]
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
    args   = parse_args()
    fill   = args.fill and not args.recap_only
    write_note = fill and not args.no_beds24_note

    # Load local state once
    records = st.load()

    client = TaxeSejourClient()
    client.login()

    # Determine months
    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
        except ValueError:
            print(f"Format invalide: '{args.month}', attendu YYYY-MM")
            sys.exit(1)
        # Look up period_id for this specific month
        all_pending = client.get_pending_months(year)
        match = next(((y, m, pid) for y, m, pid in all_pending if y == year and m == month), None)
        if match:
            months = [match]
        else:
            # Month not in pending list — allow anyway with empty period_id
            months = [(year, month, "")]
    else:
        year = date.today().year
        months = client.get_pending_months(year)
        if not months:
            print("Aucun mois «À déclarer» trouvé sur taxesejour.fr.")
            return

    label = ", ".join(date(y, m, 1).strftime("%B %Y") for y, m, _ in months)
    mode  = "--fill" if fill else "dry-run"
    print(f"{'═' * 64}")
    print(f"  Taxe de séjour — {label}  [{mode}]")
    print(f"{'═' * 64}")

    results: list[MonthResult] = []
    for y, m, pid in months:
        r = process_month(y, m, pid, client, records, fill=fill, write_beds24_note=write_note)
        results.append(r)
        print_month_status(r, dry_run=not fill)

    # Summary line
    total_missing = sum(len(r.missing) for r in results)
    total_changed = sum(len(r.changed) for r in results)
    if not fill and total_missing:
        print(f"\n  {total_missing} séjour(s) à soumettre. Utiliser --fill pour envoyer.")
    if total_changed:
        print(f"  {total_changed} séjour(s) déclaré(s) avec montant modifié — voir récap.")

    # Full recap always at the end
    print_recap(results)


if __name__ == "__main__":
    main()
