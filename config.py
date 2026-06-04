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
BEDS24_PROP_KEY = _b24_canbt.get("prop_key") or _b24_mosaiques["prop_key"]
# canbt key is account-level and may not need a propKey
BEDS24_USE_PROP_KEY = not _b24_canbt.get("api_key")  # only needed for mosaiques key

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
# Taxe de séjour rate (5% of HT)
TAXE_RATE = 0.05

# apiSource codes for platforms that collect taxe de séjour on our behalf.
# These still appear in the full recap but are NOT to be declared by us.
PLATFORM_SOURCES: dict[str, str] = {
    "19": "Booking.com",
    "29": "Airbnb (iCal)",
    "46": "Airbnb (API)",
    # Add Expedia code when known
}
# iCal channel without known platform origin — treated as direct but flagged
ICAL_SOURCE = "28"
