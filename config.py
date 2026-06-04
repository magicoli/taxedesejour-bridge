"""Load credentials and site constants from pa.toml."""

import sys
from pathlib import Path

PA_TOML = Path.home() / ".claude" / "pa.toml"

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _load() -> dict:
    with open(PA_TOML, "rb") as f:
        return tomllib.load(f)


_cfg = _load()

# ── taxesejour.fr ─────────────────────────────────────────────────────────────
TAXESEJOUR = _cfg["nordbasseterre"]["taxesejour"]["fr"]
TS_URL         = "https://nordbasseterre.taxesejour.fr"
TS_USERNAME    = TAXESEJOUR["username"]
TS_PASSWORD    = TAXESEJOUR["password"]
TS_HOST_ID     = 1695931
TS_LODGING_ID  = 2216111
TS_REGISTRE_ID = 1248441  # unique registre for "Gîtes Mosaïques"

# ── Beds24 ────────────────────────────────────────────────────────────────────
# Prefer [beds24.canbt] key (needs IP whitelisting in Beds24 settings).
# Falls back to [mosaiques.beds24] which has no IP restriction.
BEDS24_API_URL = "https://api.beds24.com/json/"

_b24_canbt    = _cfg.get("beds24", {}).get("canbt", {})
_b24_mosaiques = _cfg["mosaiques"]["beds24"]

BEDS24_API_KEY  = _b24_canbt.get("api_key") or _b24_mosaiques["api_key"]
# propKey identifies the property; always use mosaiques prop_key regardless of which API key
BEDS24_PROP_KEY = _b24_mosaiques["prop_key"]

# Beds24 booking URL (for direct links in reports)
BEDS24_BOOKING_URL = "https://beds24.com/control3.php?pagetype=bookings&bookid={book_id}"

# Room IDs → gîte name
BEDS24_ROOMS: dict[int, str] = {
    552313: "Moon",
    552316: "Sun",
    552315: "Zandoli",
    552314: "Violeta",
    552312: "Zetoil",
}

# VAT rate in Guadeloupe (2.1%) — amounts in Beds24 are TTC
VAT_RATE = 0.021
# Taxe de séjour rate (5% of HT, adults only)
TAXE_RATE = 0.05


# ── Tax formula (single source of truth) ──────────────────────────────────────
# total_ttc = ht * (1 + VAT_RATE + TAXE_RATE * adults/guests)
# Only adults are subject to the taxe de séjour; children are exempt.

def ts_ratio(adults: int, children: int) -> float:
    """Fraction of guests subject to the taxe de séjour (adults / total)."""
    guests = adults + children
    return adults / guests if guests > 0 else 0.0


def ht_from_total(total_ttc: float, adults: int, children: int) -> float:
    """Recover base HT from the total actually received.

    Inverts: total_ttc = ht * (1 + VAT_RATE + TAXE_RATE * adults/guests)
    """
    return total_ttc / (1 + VAT_RATE + TAXE_RATE * ts_ratio(adults, children))


def taxe_sejour(ht: float, adults: int, children: int) -> float:
    """Taxe de séjour due: ht * (adults/guests) * TAXE_RATE."""
    return ht * ts_ratio(adults, children) * TAXE_RATE


def total_ttc(ht: float, adults: int, children: int) -> float:
    """Total = HT + TVA + taxe de séjour."""
    return ht * (1 + VAT_RATE) + taxe_sejour(ht, adults, children)

# apiSource codes for platforms that collect taxe de séjour on our behalf.
# Shown in the recap for reference but NOT declared by us.
# 28 (generic / our-website iCal import) is treated as Direct.
PLATFORM_SOURCES: dict[str, str] = {
    "19": "Booking.com",
    "29": "Airbnb",   # Airbnb iCal feed
    "46": "Airbnb",   # Airbnb API
    # Add Expedia code when known
}

# Beds24 booking status (from the admin UI <select>):
#   0 Cancelled | 1 Confirmed | 2 New | 3 Request | 4 Black | 5 Inquiry
# Only Confirmed and New are real bookings to process.
VALID_STATUSES: set[str] = {"1", "2"}
