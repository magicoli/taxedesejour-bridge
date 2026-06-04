"""Beds24 API client — fetch direct bookings for a given month."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from config import (
    BEDS24_API_KEY,
    BEDS24_API_URL,
    BEDS24_PROP_KEY,
    BEDS24_ROOMS,
    PLATFORM_API_SOURCES,
)


@dataclass
class Booking:
    book_id: str
    unit: str           # gîte name (Moon, Sun, …)
    check_in: date      # firstNight
    check_out: date     # lastNight + 1 day
    adults: int
    children: int
    price: float        # total price (used for montant)
    source: str         # raw apiSource code
    guest: str

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days


def _room_id_to_unit(room_id: int) -> str | None:
    for name, rid in BEDS24_ROOMS.items():
        if rid == room_id:
            return name
    return None


def get_direct_bookings(year: int, month: int) -> list[Booking]:
    """Return direct bookings whose check-in falls in the given month.

    Direct = not collected by a platform (Airbnb / Booking.com / …).
    Blocks and cancellations are excluded.
    """
    first = date(year, month, 1)
    last  = date(year, month, calendar.monthrange(year, month)[1])

    payload = {
        "authentication": {
            "apiKey":  BEDS24_API_KEY,
            "propKey": BEDS24_PROP_KEY,
        },
        "arrivalFrom": first.isoformat(),
        "arrivalTo":   last.isoformat(),
        "includeInvoice":    False,
        "includeInfoItems":  False,
        "limit": 1000,
    }

    resp = requests.post(BEDS24_API_URL + "getBookings", json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Beds24 API error: {data['error']} (code {data.get('errorCode')})")

    bookings: list[Booking] = []

    for row in data if isinstance(data, list) else []:
        status = str(row.get("status", "2"))

        # Skip blocks (4=calendar block, 5=owner block) and cancellations (3)
        if status in ("3", "4", "5"):
            continue

        api_source = str(row.get("apiSource", "0"))

        # Skip platform bookings (they collect taxe de séjour themselves)
        if api_source in PLATFORM_API_SOURCES:
            continue

        room_id = int(row.get("roomId", 0))
        unit = _room_id_to_unit(room_id)
        if unit is None:
            continue  # unknown room, skip

        check_in_str  = row.get("firstNight")
        check_out_str = row.get("lastNight")
        if not check_in_str or not check_out_str:
            continue

        check_in  = date.fromisoformat(check_in_str)
        check_out = date.fromisoformat(check_out_str) + timedelta(days=1)

        bookings.append(Booking(
            book_id  = str(row.get("bookId", "")),
            unit     = unit,
            check_in = check_in,
            check_out= check_out,
            adults   = int(row.get("numAdult", 0) or 0),
            children = int(row.get("numChild", 0) or 0),
            price    = float(row.get("price", 0) or 0),
            source   = api_source,
            guest    = (f"{row.get('firstName','')} {row.get('lastName','')}").strip() or "Guest",
        ))

    return sorted(bookings, key=lambda b: (b.check_in, b.unit))
