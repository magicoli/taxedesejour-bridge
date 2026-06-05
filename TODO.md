# TODO — taxesejour

## Migrate Beds24 API v1 → v2
Currently using the deprecated v1 API, which requires two keys: `api_key`
(ACCOUNT ACCESS) and `prop_key` (PROPERTY ACCESS).

v2 simplifies auth to a single Bearer token — no `prop_key` needed.
Only `_fetch_raw()` and `set_booking_custom1()` in `beds24.py` need
rewriting. Check field name mapping in the `getBookings` response before
touching anything else.

Docs: https://wiki.beds24.com/index.php/Category:API_V2
API explorer: https://beds24.com/api/v2/

## Known limitation — client merge
`_same_client()` merges bookings by: same email, OR (no email on either
side AND same guest name). If one booking has an email and the other does
not, they are not merged even when the name matches.

## bokit-light integration (later)
- Tax calculation logic in `config.py` (pure functions, portable to PHP).
- The `Row` / 16-column CSV is the stable data contract for import.
