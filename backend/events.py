"""Event window logic.

The upload window is enforced here and nowhere else. The frontend is told
whether uploads are open so it can hide the button, but that is presentation
only -- the client clock is neither trustworthy nor reliably correct, so
POST /api/videos re-checks against the server clock on every request.
"""

from datetime import datetime, timezone

def parse_iso(value):
    """Parses a stored ISO-8601 timestamp into an aware UTC datetime.

    Returns None for empty values. Naive timestamps are assumed to be UTC,
    which matches what the admin CLI writes.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def upload_state(event: dict, now: datetime = None) -> dict:
    """Resolves an event's upload window into a state the UI can render.

    Either bound may be absent, meaning unbounded on that side. An event with
    neither bound accepts uploads indefinitely.
    """
    now = now or datetime.now(timezone.utc)
    opens_at = parse_iso(event.get("uploadOpensAt"))
    closes_at = parse_iso(event.get("uploadClosesAt"))

    if opens_at and now < opens_at:
        return {
            "uploadOpen": False,
            "uploadState": "pending",
            "reason": "Uploads for this showroom have not opened yet.",
        }
    if closes_at and now >= closes_at:
        return {
            "uploadOpen": False,
            "uploadState": "closed",
            "reason": "The upload window for this showroom has closed.",
        }
    return {
        "uploadOpen": True,
        "uploadState": "open",
        "reason": None,
    }

def ad_submission_open(event: dict, now: datetime = None) -> bool:
    """Whether ads can still be submitted or replaced for this showroom.

    This gates *changes only*. An ad that has already been uploaded keeps
    playing indefinitely -- the deadline freezes the set of ads rather than
    retiring them, so an event's videos do not silently lose their pre-roll
    the moment the window shuts. To stop ads playing, deactivate them
    explicitly: `admin.py set-ads --disable`.

    Absent means submissions stay open, matching an unset upload deadline.
    """
    closes_at = parse_iso(event.get("adsClosesAt"))
    if not closes_at:
        return True
    return (now or datetime.now(timezone.utc)) < closes_at


def public_event(event: dict, now: datetime = None, credits: list = None) -> dict:
    """Shapes an event row for the API, with the window resolved server-side.

    `credits` is passed in rather than read here because this module has no
    database access by design -- it is the window logic and nothing else.
    Callers that have a cursor supply the list; those that do not get an empty
    one, which reads as "no credits" and simply hides the nav item.
    """
    state = upload_state(event, now)
    return {
        "code": event["code"],
        "name": event["name"],
        "uploadOpensAt": event.get("uploadOpensAt"),
        "uploadClosesAt": event.get("uploadClosesAt"),
        "adsClosesAt": event.get("adsClosesAt"),
        "adSubmissionOpen": ad_submission_open(event, now),
        # Empty string rather than null: the frontend appends this to share
        # text, and "undefined" reaching a post is worse than nothing.
        "shareHashtag": (event.get("shareHashtag") or "").strip(),
        # Empty string hides the top-bar button. Stored with a scheme, so the
        # client can use it as an href without touching it.
        "socialWallUrl": (event.get("socialWallUrl") or "").strip(),
        "credits": [
            {"name": c["name"], "url": c["url"]} for c in (credits or [])
        ],
        **state,
    }
