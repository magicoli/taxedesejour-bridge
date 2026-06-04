"""Beds24 API client — fetch bookings with financial details."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import requests

import tomllib
from pathlib import Path

from config import (
    BEDS24_API_KEY,
    BEDS24_API_URL,
    BEDS24_PROP_KEY,
    BEDS24_ROOMS,
    ICAL_SOURCE,
    PLATFORM_SOURCES,
    TAXE_RATE,
    VAT_RATE,
)

def _get_fallback_auth() -> dict | None:
    """Return mosaiques auth dict if available (used when canbt key has IP restriction)."""
    try:
        with open(Path.home() / ".claude" / "pa.toml", "rb") as f:
            cfg = tomllib.load(f)
        b = cfg["mosaiques"]["beds24"]
        return {"apiKey": b["api_key"], "propKey": b["prop_key"]}
    except Exception:
        return None


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
    api_source: str
    guest: str
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
        if self.api_source in PLATFORM_SOURCES:
            return PLATFORM_SOURCES[self.api_source]
        if self.api_source == ICAL_SOURCE:
            return "iCal (direct?)"
        return "Direct"

    @property
    def acc_amount_ht(self) -> float:
        """Accommodation price HT (excl. VAT 2.1%)."""
        return self.acc_amount_ttc / (1 + VAT_RATE)

    @property
    def declared_amount(self) -> float:
        """Amount to declare on taxesejour.fr: accommodation HT.

        The platform computes: declared * 5% = taxe de séjour.
        We simply convert TTC → HT (÷ 1.021). When taxe was already invoiced
        separately (taxe_in_invoice > 0), acc_amount_ttc already excludes it.
        """
        return self.acc_amount_ttc / (1 + VAT_RATE)

    @property
    def computed_taxe(self) -> float:
        """Taxe de séjour at 5% on declared_amount."""
        return self.declared_amount * TAXE_RATE

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
        if self.api_source == ICAL_SOURCE:
            w.append("iCal — vérifier si direct")
        return w


# ── Invoice parsing ────────────────────────────────────────────────────────────

def _parse_invoice(raw: list[dict]) -> tuple[float, float, list[InvoiceLine]]:
    """Return (acc_ttc, taxe_invoiced, lines).

    acc_ttc = sum of accommodation line amounts (type 0/1/8, excl. payments and taxe).
    taxe_invoiced = sum of lines whose description contains 'taxe de séjour'.
    """
    lines: list[InvoiceLine] = []
    acc_ttc = 0.0
    taxe_invoiced = 0.0

    for raw_line in raw:
        ltype = str(raw_line.get("type", ""))
        desc  = str(raw_line.get("description") or "")
        price = float(raw_line.get("price") or 0)

        lines.append(InvoiceLine(description=desc, price=price, line_type=ltype))

        if ltype == "200":   # payment — ignore for amounts
            continue

        if "taxe de séjour" in desc.lower():
            taxe_invoiced += price
        elif ltype in ("0", "1", "8"):
            acc_ttc += price

    return acc_ttc, taxe_invoiced, lines


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

    resp = requests.post(BEDS24_API_URL + "getBookings", json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # IP restriction on canbt key → silent fallback to mosaiques key
    if isinstance(data, dict) and data.get("errorCode") == "1022":
        fallback = _get_fallback_auth()
        if fallback:
            payload["authentication"] = fallback
            resp = requests.post(BEDS24_API_URL + "getBookings", json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        else:
            raise RuntimeError(
                "Clé Beds24 canbt bloquée (IP restriction). "
                "Désactiver la restriction IP dans les paramètres Beds24, "
                f"ou ajouter [mosaiques.beds24] dans pa.toml comme fallback."
            )

    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Beds24 API error: {data['error']} (code {data.get('errorCode')})")

    return data if isinstance(data, list) else []


# ── Public API ─────────────────────────────────────────────────────────────────

def get_bookings(year: int, month: int) -> list[Booking]:
    """Return ALL bookings whose check-OUT falls in the given month.

    A stay is declared in the month where the prestation ends (checkout date).
    Fetches from 4 months prior to catch long cross-month stays.
    Includes platform bookings — callers decide what to do with them.
    Blocks (status 4/5) and cancellations (status 3) are excluded.
    """
    raw_list = _fetch_raw(year, month)
    bookings: list[Booking] = []

    for row in raw_list:
        status = str(row.get("status", "2"))
        if status in ("3", "4", "5"):
            continue

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

        price_field = float(row.get("price") or 0)
        acc_ttc, taxe_inv, inv_lines = _parse_invoice(row.get("invoice") or [])

        # Fall back to price_field when invoice total is absent or unreliable
        # (e.g. large discount makes net negative, or no invoice lines)
        if acc_ttc <= 0 and price_field > 0:
            acc_ttc = price_field

        guest = (
            f"{row.get('firstName', '') or ''} {row.get('lastName', '') or ''}".strip()
            or "—"
        )

        bookings.append(Booking(
            book_id          = str(row.get("bookId", "")),
            unit             = unit,
            room_id          = room_id,
            check_in         = check_in,
            check_out        = check_out,
            adults           = int(row.get("numAdult") or 0),
            children         = int(row.get("numChild") or 0),
            api_source       = str(row.get("apiSource") or "0"),
            guest            = guest,
            price_field      = price_field,
            acc_amount_ttc   = acc_ttc,
            taxe_in_invoice  = taxe_inv,
            invoice_lines    = inv_lines,
        ))

    # Keep only bookings whose checkout falls in the requested month
    bookings = [b for b in bookings
                if b.check_out.year == year and b.check_out.month == month]

    return sorted(bookings, key=lambda b: (b.check_in, b.unit))


@dataclass
class BookingGroup:
    """One or more Beds24 bookings with identical dates, treated as one declaration."""
    check_in: date
    check_out: date
    bookings: list[Booking]

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def units(self) -> list[str]:
        return [b.unit for b in self.bookings]

    @property
    def adults(self) -> int:
        return sum(b.adults for b in self.bookings)

    @property
    def children(self) -> int:
        return sum(b.children for b in self.bookings)

    @property
    def declared_amount(self) -> float:
        return sum(b.declared_amount for b in self.bookings)

    @property
    def acc_amount_ttc(self) -> float:
        return sum(b.acc_amount_ttc for b in self.bookings)

    @property
    def taxe_in_invoice(self) -> float:
        return sum(b.taxe_in_invoice for b in self.bookings)

    @property
    def computed_taxe(self) -> float:
        return sum(b.computed_taxe for b in self.bookings)

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
        sources = set(b.api_source for b in self.bookings)
        if ICAL_SOURCE in sources:
            w.append("iCal — vérifier si direct")
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


def group_by_dates(bookings: list[Booking]) -> list[BookingGroup]:
    """Group bookings with the same (check_in, check_out) into one declaration group."""
    from collections import defaultdict
    groups: dict[tuple, list[Booking]] = defaultdict(list)
    for b in bookings:
        groups[(b.check_in, b.check_out)].append(b)
    return [
        BookingGroup(check_in=k[0], check_out=k[1], bookings=v)
        for k, v in sorted(groups.items())
    ]
