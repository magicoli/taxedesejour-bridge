"""Beds24 API client — fetch bookings with financial details."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import requests

from config import (
    BEDS24_API_KEY,
    BEDS24_API_URL,
    BEDS24_PROP_KEY,
    ConfigError,  # re-exported so callers can catch it
    BEDS24_ROOMS,
    PLATFORM_SOURCES,
    VALID_STATUSES,
    ht_from_total,
    taxe_sejour,
)


@dataclass
class InvoiceLine:
    description: str
    price: float
    line_type: str  # '0'=misc/fee, '1'=accom, '8'=accom variant, '200'=payment


@dataclass
class Booking:
    book_id: str
    unit: str               # gîte name (Moon, Sun, …)
    room_id: int
    check_in: date
    check_out: date
    adults: int
    children: int
    status: str             # Beds24 status code ("1"=Confirmed, "2"=New, …)
    api_source: str
    guest: str
    guest_email: str        # for client matching when no group/masterId
    master_id: str          # Beds24 group booking linkage ("" if standalone)
    # Financial fields (computed from invoice or price_field)
    price_field: float      # raw 'price' from Beds24
    acc_amount_ttc: float   # accommodation TTC (from invoice or price_field)
    taxe_in_invoice: float  # taxe de séjour amount already in the invoice (0 if none)
    invoice_lines: list[InvoiceLine] = field(default_factory=list)

    # ── Derived properties ─────────────────────────────────────────────────────

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def is_platform(self) -> bool:
        return self.api_source in PLATFORM_SOURCES

    @property
    def platform_name(self) -> str:
        return PLATFORM_SOURCES.get(self.api_source, "Direct")

    @property
    def total_received(self) -> float:
        """Total actually received from the client (accommodation TTC + invoiced taxe)."""
        return self.acc_amount_ttc + self.taxe_in_invoice

    @property
    def declared_amount(self) -> float:
        """Net (HT) to submit, derived from the total actually received."""
        return ht_from_total(self.total_received, self.adults, self.children)

    @property
    def computed_taxe(self) -> float:
        """Taxe de séjour, site method (per-night rounded)."""
        return taxe_sejour(self.declared_amount, self.nights, self.adults, self.children)

    @property
    def has_amount(self) -> bool:
        return self.acc_amount_ttc > 0

    @property
    def has_occupants(self) -> bool:
        return (self.adults + self.children) > 0

    @property
    def warnings(self) -> list[str]:
        w = []
        if not self.has_amount:
            w.append("montant nul")
        if not self.has_occupants:
            w.append("occupants manquants")
        return w


# ── Invoice parsing ────────────────────────────────────────────────────────────

def _parse_invoice(raw: list[dict]) -> tuple[float, float, float, list[InvoiceLine]]:
    """Return (acc_ttc, taxe_invoiced, payment_total, lines).

    acc_ttc       = sum of accommodation line amounts (type 0/1/8, excl. payments and taxe).
    taxe_invoiced = sum of lines whose description contains 'taxe de séjour'.
    payment_total = sum of payment lines (type 200) — what the client actually paid in total.
    """
    lines: list[InvoiceLine] = []
    acc_ttc = 0.0
    taxe_invoiced = 0.0
    payment_total = 0.0

    for raw_line in raw:
        ltype = str(raw_line.get("type", ""))
        desc  = str(raw_line.get("description") or "")
        price = float(raw_line.get("price") or 0)

        lines.append(InvoiceLine(description=desc, price=price, line_type=ltype))

        if ltype == "200":
            payment_total += price
            continue

        if "taxe de séjour" in desc.lower():
            taxe_invoiced += price
        elif ltype in ("0", "1", "8"):
            acc_ttc += price

    return acc_ttc, taxe_invoiced, payment_total, lines


# ── API call ───────────────────────────────────────────────────────────────────

def _month_offset(year: int, month: int, delta: int) -> tuple[int, int]:
    """Return (year, month) shifted by delta months."""
    m = month + delta
    return (year + (m - 1) // 12, (m - 1) % 12 + 1)


def _fetch_raw(year: int, month: int) -> list[dict]:
    # Fetch arrivals from 4 months back to end of target month.
    # Bookings are assigned by check-OUT date, so a 4-month lookback
    # covers any realistic stay length.
    y_from, m_from = _month_offset(year, month, -4)
    first = date(y_from, m_from, 1)
    last  = date(year, month, calendar.monthrange(year, month)[1])

    auth: dict = {"apiKey": BEDS24_API_KEY, "propKey": BEDS24_PROP_KEY}

    payload = {
        "authentication": auth,
        "arrivalFrom": first.isoformat(),
        "arrivalTo":   last.isoformat(),
        "includeInvoice":   True,
        "includeInfoItems": False,
        "limit": 1000,
    }

    try:
        resp = requests.post(BEDS24_API_URL + "getBookings", json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Cannot reach Beds24 API — check your internet connection.")
    except requests.exceptions.Timeout:
        raise RuntimeError("Beds24 API timed out — try again later.")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Beds24 API request failed: {e}")

    if isinstance(data, dict) and data.get("error"):
        code = data.get("errorCode", "")
        msg  = data.get("error", "")
        if code in ("2000", "2001"):
            raise ConfigError(
                f"Beds24 authentication failed (code {code}): {msg}\n"
                "Check api_key and prop_key in config.toml."
            )
        raise RuntimeError(f"Beds24 API error {code}: {msg}")

    return data if isinstance(data, list) else []


# ── Public API ─────────────────────────────────────────────────────────────────

def get_bookings(year: int, month: int) -> list[Booking]:
    """Return ALL bookings whose check-OUT falls in the given month.

    A stay is declared in the month where the prestation ends (checkout date).
    Fetches from 4 months prior to catch long cross-month stays.
    Includes platform bookings — callers decide what to do with them.

    Status handling (Beds24 admin UI codes):
      0=Cancelled  1=Confirmed  2=New  3=Request  4=Black  5=Inquiry
    - Normally only status 1 and 2 are processed.
    - Exception: Beds24 group invoices use status 3 for sub-bookings
      (placeholders). A sub-booking is included when its masterId points
      to a confirmed (status 1/2) master that carries a 'group' field.

    Amount fallback (when invoice acc_ttc ≤ 0):
    - Group master with no invoice lines: acc_ttc = 0 (amounts live on subs).
    - Standalone booking with invoice + large discount (negative acc_ttc):
      use payment_total (type-200 lines) as total received if available.
    - No invoice at all (and not a group master): fall back to price_field.
    """
    raw_list = _fetch_raw(year, month)

    # ── Pass 1: identify group master booking IDs ─────────────────────────────
    # A group master has a non-empty Beds24 'group' sub-dict AND masterId == own
    # bookId (self-referential). Sub-bookings of a confirmed master are included
    # even at status 3 (Request — the placeholder status Beds24 assigns them).
    confirmed_masters: set[str] = set()
    for row in raw_list:
        if str(row.get("status") or "") not in VALID_STATUSES:
            continue
        book_id   = str(row.get("bookId") or "")
        master_id = str(row.get("masterId") or "").strip()
        if row.get("group") and master_id == book_id:
            confirmed_masters.add(book_id)

    # ── Pass 2: build Booking objects ─────────────────────────────────────────
    bookings: list[Booking] = []

    for row in raw_list:
        status    = str(row.get("status") or "")
        book_id_s = str(row.get("bookId") or "")
        master_id = str(row.get("masterId") or "").strip()

        # Include if valid status, OR if it's a sub-booking of a confirmed group.
        is_group_sub = (status == "3" and master_id in confirmed_masters
                        and master_id != book_id_s)
        if status not in VALID_STATUSES and not is_group_sub:
            continue

        api_source = str(row.get("apiSource") or "0")

        room_id = int(row.get("roomId") or 0)
        unit = BEDS24_ROOMS.get(room_id)
        if unit is None:
            continue  # not one of our gîtes

        check_in_str  = row.get("firstNight")
        check_out_str = row.get("lastNight")
        if not check_in_str or not check_out_str:
            continue

        check_in  = date.fromisoformat(check_in_str)
        check_out = date.fromisoformat(check_out_str) + timedelta(days=1)

        adults   = int(row.get("numAdult") or 0)
        children = int(row.get("numChild") or 0)

        price_field = float(row.get("price") or 0)
        acc_ttc, taxe_inv, payment_total, inv_lines = _parse_invoice(
            row.get("invoice") or []
        )

        # A group master has a self-referential masterId and carries sub-bookings.
        # The real amounts live on sub-bookings; the master entry may be a header.
        is_group_master = (
            bool(row.get("group")) and master_id == book_id_s
        )

        # ── Amount fallback ────────────────────────────────────────────────────
        if acc_ttc <= 0:
            if not inv_lines:
                # No invoice at all.
                if is_group_master:
                    # Master placeholder with no invoice: amounts are on subs.
                    # Do NOT use price_field (would double-count sub amounts).
                    acc_ttc = 0.0
                elif price_field > 0:
                    acc_ttc = price_field
            else:
                # Invoice exists but acc_ttc is negative (large discount, manual
                # entry with partial rate line, etc.).
                if not is_group_master and payment_total > 0:
                    # For standalone bookings, the payment lines show what was
                    # actually received (accommodation + taxe combined).
                    acc_ttc  = payment_total
                    taxe_inv = 0.0
                elif price_field > 0:
                    acc_ttc = price_field

        # Name: guestFirstName/guestName hold the real name in v1 API.
        guest = (
            " ".join(p for p in (row.get("guestFirstName"), row.get("guestName")) if p).strip()
            or " ".join(p for p in (row.get("firstName"), row.get("lastName")) if p).strip()
            or "—"
        )

        bookings.append(Booking(
            book_id          = book_id_s,
            unit             = unit,
            room_id          = room_id,
            check_in         = check_in,
            check_out        = check_out,
            adults           = adults,
            children         = children,
            status           = status,
            api_source       = api_source,
            guest            = guest,
            guest_email      = str(row.get("guestEmail") or "").strip(),
            master_id        = master_id,
            price_field      = price_field,
            acc_amount_ttc   = acc_ttc,
            taxe_in_invoice  = taxe_inv,
            invoice_lines    = inv_lines,
        ))

    # Keep only bookings whose checkout falls in the requested month.
    bookings = [b for b in bookings
                if b.check_out.year == year and b.check_out.month == month]

    return sorted(bookings, key=lambda b: (b.check_in, b.unit))


@dataclass
class BookingGroup:
    """Bookings of one client whose dates overlap, treated as a single declaration.

    Dates span the full range (min check-in → max check-out). Amounts and
    occupant counts are summed; the taxe is computed on these merged totals
    (the way taxesejour.fr will, since we submit one declaration).
    """
    bookings: list[Booking]

    @property
    def check_in(self) -> date:
        return min(b.check_in for b in self.bookings)

    @property
    def check_out(self) -> date:
        return max(b.check_out for b in self.bookings)

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def units(self) -> list[str]:
        return [b.unit for b in self.bookings]

    @property
    def client_name(self) -> str:
        """Client name for control (first non-empty among the bookings)."""
        return next((b.guest for b in self.bookings if b.guest and b.guest != "—"), "—")

    @property
    def adults(self) -> int:
        return sum(b.adults for b in self.bookings)

    @property
    def children(self) -> int:
        return sum(b.children for b in self.bookings)

    @property
    def acc_amount_ttc(self) -> float:
        return sum(b.acc_amount_ttc for b in self.bookings)

    @property
    def total_received(self) -> float:
        return sum(b.total_received for b in self.bookings)

    @property
    def taxe_in_invoice(self) -> float:
        return sum(b.taxe_in_invoice for b in self.bookings)

    @property
    def declared_amount(self) -> float:
        """Net (HT) computed on the MERGED totals (one declaration).

        Matches how taxesejour.fr computes: one declaration, merged
        adults/guests ratio applied to the base HT we submit.
        """
        return ht_from_total(self.total_received, self.adults, self.children)

    @property
    def computed_taxe(self) -> float:
        """Taxe de séjour, site method (per-night rounded) on merged totals."""
        return taxe_sejour(self.declared_amount, self.nights, self.adults, self.children)

    @property
    def is_platform(self) -> bool:
        return all(b.is_platform for b in self.bookings)

    @property
    def has_amount(self) -> bool:
        return self.declared_amount > 0

    @property
    def has_occupants(self) -> bool:
        return (self.adults + self.children) > 0

    @property
    def warnings(self) -> list[str]:
        w = []
        if not self.has_amount:
            w.append("montant nul")
        if not self.has_occupants:
            w.append("occupants manquants")
        return w

    @property
    def platform_names(self) -> list[str]:
        return sorted(set(b.platform_name for b in self.bookings))


def set_booking_custom1(book_id: str, value: str) -> bool:
    """Write a short string to the booking's custom1 field in Beds24.

    Used to record the CANBT declaration info for human reference.
    Returns True on success.
    """
    payload = {
        "authentication": {"apiKey": BEDS24_API_KEY, "propKey": BEDS24_PROP_KEY},
        "bookId": book_id,
        "custom1": value,
    }
    try:
        resp = requests.post(BEDS24_API_URL + "setBooking", json=payload, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        return result.get("success") == "booking modified"
    except Exception:
        return False


def _same_client(a: Booking, b: Booking) -> bool:
    """Two bookings belong to the same client → one declaration.

    1. Same Beds24 group (master booking linkage)
    2. Same email
    3. No email on either side AND same guest name
    """
    if (a.master_id or a.book_id) == (b.master_id or b.book_id):
        return True
    ea, eb = a.guest_email.lower(), b.guest_email.lower()
    if ea and eb:
        return ea == eb
    if not ea and not eb:
        na, nb = a.guest.strip().lower(), b.guest.strip().lower()
        return bool(na) and na == nb
    return False


def _overlap(a: Booking, b: Booking) -> bool:
    """True if the two stays share at least one night (half-open intervals)."""
    return a.check_in < b.check_out and b.check_in < a.check_out


def group_bookings(bookings: list[Booking]) -> list[BookingGroup]:
    """Merge bookings of the same client whose dates overlap into one declaration.

    Two bookings are merged when they are the same client AND their date ranges
    overlap (transitively, via union-find). The merged group spans min check-in
    to max check-out, summing amounts and occupants.

    Same client at non-overlapping dates → separate declarations.
    Different clients → separate, even at identical dates.
    """
    n = len(bookings)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = bookings[i], bookings[j]
            # Platform bookings are never merged — each is its own declaration
            # handled by the platform, and they belong to distinct guests.
            if a.is_platform or b.is_platform:
                continue
            if _same_client(a, b) and _overlap(a, b):
                parent[find(i)] = find(j)

    from collections import defaultdict
    comps: dict[int, list[Booking]] = defaultdict(list)
    for i, bk in enumerate(bookings):
        comps[find(i)].append(bk)

    groups = [BookingGroup(bookings=c) for c in comps.values()]
    return sorted(groups, key=lambda g: (g.check_in, g.check_out))
