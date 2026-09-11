import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

DATABASE_PATH = os.path.join(os.path.dirname(__file__), "vibetube.db")
DATABASE_URL = os.getenv("DATABASE_URL")

# Every video created before events existed belongs to this event.
SANDBOX_EVENT_CODE = "sandbox"

# Playback readiness.
#   PENDING    accepted and queued; no transcoder job exists yet
#   PROCESSING a transcoder job is running for this row
#   READY      playable
#   FAILED     transcoding failed, or the job never started
#
# PENDING is what lets an upload always succeed. Without it the only way to
# bound concurrent transcodes was to reject the upload outright, which turned
# a busy showroom into a wall of errors.
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# BLOCKED is terminal and distinct from FAILED on purpose. Both are unplayable,
# but failed means "the machine could not convert this, try again" while
# blocked means "screening rejected this" -- the card says something different,
# and re-uploading the same file will reach the same verdict. A blocked row
# never had its renditions written to the public bucket: the transcoder screens
# before it encodes, so there is nothing published to retract.
STATUS_BLOCKED = "blocked"

# A job that dies without reporting back would otherwise hold its slot for
# ever. Rows processing longer than this are treated as dead: the slot is
# released and the row marked failed.
TRANSCODE_STALE_MINUTES = int(os.getenv("TRANSCODE_STALE_MINUTES", "30"))

# Stored in thumbnailUrl when a row has no poster frame yet: local uploads
# never get one, and cloud uploads only get one once the transcoder finishes.
# The frontend renders it as a placeholder tile.
#
# Never inline this into SQL as '?'. query_placeholder rewrites every ? to %s
# for Postgres with a blind string replace, so an inline sentinel becomes a
# bogus placeholder and the statement ends up with more placeholders than
# parameters. Bind it as a parameter instead.
MISSING_THUMBNAIL = "?"

# Where a row came from. Only guest uploads count against a showroom's
# upload cap; seeded content is placed by an organiser and is exempt.
SOURCE_SEED = "seed"
SOURCE_UPLOAD = "upload"

# A stand-in created when an ad arrives for a project that has no video yet.
# Points at existing seed media rather than transcoding anything, and is
# replaced in place if the team later uploads for real.
#
# Deliberately not SOURCE_UPLOAD: a placeholder is not a guest contribution and
# must not consume a slot against MAX_UPLOADS_PER_EVENT. See the cap check in
# ingest_upload, which treats a placeholder as "no upload yet" so replacing one
# is still counted.
SOURCE_PLACEHOLDER = "placeholder"

# How long a heartbeat keeps a viewer counted as present. Must comfortably
# exceed the client's heartbeat interval, or a viewer flickers out between
# beats and the room appears emptier than it is.
PRESENCE_TTL_SECONDS = int(os.getenv("PRESENCE_TTL_SECONDS", "90"))

# Addresses granted admin access on first start, comma- or space-separated.
# Only ever adds rows -- an address removed through the console stays removed
# across redeploys. Everything after the bootstrap is managed in the console.
ADMIN_BOOTSTRAP_EMAILS = os.getenv("ADMIN_BOOTSTRAP_EMAILS", "")

# Per-instance Postgres pool size. Cloud SQL caps total connections per
# instance, and Cloud Run runs many app instances against one database, so the
# real budget is DB_POOL_MAX x Cloud Run max-instances. See deploy.sh, which
# pins max-instances so that product stays under the tier's ceiling.
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "5"))

# How long a request waits for a free connection before giving up. Failing
# fast beats stacking requests behind a saturated database.
DB_POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "10"))

_pool = None
_pool_lock = threading.Lock()
# psycopg2's ThreadedConnectionPool raises the moment it is exhausted instead
# of waiting, which would turn a burst of traffic into 500s. This gates entry
# so callers queue for a connection rather than being rejected outright.
_pool_slots = threading.BoundedSemaphore(DB_POOL_MAX)


class DatabaseBusy(Exception):
    """Every pooled connection was in use and none freed within the timeout."""


def _get_pool():
    """Builds the Postgres connection pool on first use.

    Opening a fresh connection per request means a TCP handshake plus auth on
    every call, which is what let ordinary traffic exhaust the database. The
    pool is created lazily and behind a lock so concurrent first requests
    cannot each build one.
    """
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            from psycopg2.pool import ThreadedConnectionPool

            class DictConnection(psycopg2.extensions.connection):
                def cursor(self, *args, **kwargs):
                    kwargs.setdefault("cursor_factory", RealDictCursor)
                    return super().cursor(*args, **kwargs)

            # minconn == maxconn on purpose. psycopg2 closes any returned
            # connection above minconn, so a lower floor would keep
            # reconnecting under load -- the very cost pooling exists to
            # avoid. Holding the full set keeps the per-instance count fixed
            # and predictable, which is what the deploy-time budget assumes.
            _pool = ThreadedConnectionPool(
                minconn=DB_POOL_MAX,
                maxconn=DB_POOL_MAX,
                dsn=DATABASE_URL,
                connection_factory=DictConnection,
            )
    return _pool


@contextmanager
def get_db_conn():
    if DATABASE_URL:
        pool = _get_pool()
        if not _pool_slots.acquire(timeout=DB_POOL_TIMEOUT):
            raise DatabaseBusy()
        try:
            conn = pool.getconn()
        except Exception:
            # Never hold a slot for a connection that was not handed out.
            _pool_slots.release()
            raise
        try:
            yield conn
        finally:
            # Callers commit explicitly. Roll back unconditionally before
            # returning the connection: a no-op when nothing is open, and the
            # only way to clear an aborted transaction left by a failed
            # statement, which would otherwise break the next borrower.
            try:
                conn.rollback()
                pool.putconn(conn)
            except Exception:
                # Connection is unusable -- discard it so the pool replaces it.
                pool.putconn(conn, close=True)
            finally:
                _pool_slots.release()
    else:
        # SQLite is a local file used only in development; pooling it would
        # add contention rather than remove it.
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

def query_placeholder(query: str) -> str:
    """Replaces ? placeholders with %s if using PostgreSQL."""
    if DATABASE_URL:
        return query.replace("?", "%s")
    return query

def scalar(row):
    """Extracts the first column from a row, regardless of cursor row type.

    SQLite rows are tuple-like; the Postgres RealDictCursor returns dicts.
    """
    if row is None:
        return None
    # RealDictCursor rows subclass dict; sqlite3.Row exposes keys() but not
    # values(), so it has to fall through to positional access.
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]

def utc_now_iso() -> str:
    """Current time as an ISO-8601 UTC string, the storage format for timestamps."""
    return datetime.now(timezone.utc).isoformat()

def new_video_id() -> str:
    """Generates a globally unique video id.

    Ids must be unique across every event: transcoder output paths and the
    transcode-complete callback are both keyed on the video id alone.
    """
    return f"v_{uuid.uuid4().hex[:12]}"

def load_seed_videos() -> list:
    """Reads the default seed video set, preferring self-hosted media.

    Each row carries a `seedFile` naming a clip in the deployment's own public
    bucket, and a `videoUrl` pointing at the third-party original it came from.
    When a bucket is configured the bucket copy wins; otherwise the external
    URL is used, which is what local development runs on.

    Self-hosting is deliberate. The seed set previously pointed straight at
    third-party sample hosts, and they rot: w3schools.com began returning 403
    HTML for its sample clips, which silently broke half the seeded videos in
    every showroom -- they looked fine in the grid and failed only on play.
    Serving the clips from the same bucket as real uploads removes an outage
    nobody owns from the demo path. See deploy.sh, which uploads them.
    """
    json_path = os.path.join(os.path.dirname(__file__), "mockVideos.json")
    if not os.path.exists(json_path):
        return []
    with open(json_path, "r") as f:
        videos = json.load(f)

    bucket = os.getenv("PUBLIC_STREAMS_BUCKET")
    if bucket:
        for video in videos:
            seed_file = video.get("seedFile")
            if seed_file:
                video["videoUrl"] = (
                    f"https://storage.googleapis.com/{bucket}/seed/{seed_file}"
                )
    return videos

def insert_video(cursor, video: dict, event_code: str) -> str:
    """Inserts a single video row into an event and returns its new id."""
    video_id = video.get("id") or new_video_id()
    cursor.execute(query_placeholder("""
        INSERT INTO videos (
            id, eventId, title, description, thumbnailUrl, videoUrl,
            duration, createdAt, channelName, channelAvatar,
            userId, status, processingStartedAt, source, projectId,
            uploaderIp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """), (
        video_id,
        event_code,
        video.get("title", "Untitled"),
        video.get("description", ""),
        video.get("thumbnailUrl", MISSING_THUMBNAIL),
        video.get("videoUrl", ""),
        video.get("duration", "0:00"),
        video.get("createdAt") or utc_now_iso(),
        video.get("channelName", "VibeCreator"),
        video.get("channelAvatar", "?"),
        video.get("userId"),
        # Seeded and externally hosted rows are watchable immediately; only
        # rows bound for the transcoder start out queued.
        video.get("status", STATUS_READY),
        video.get("processingStartedAt"),
        video.get("source", SOURCE_SEED),
        video.get("projectId"),
        video.get("uploaderIp"),
    ))
    return video_id


def list_events_with_counts(cursor) -> list:
    """Every showroom with its video and ad totals, for the admin view."""
    cursor.execute("SELECT * FROM events ORDER BY createdAt DESC, code")
    events = [normalize_row(row) for row in cursor.fetchall()]
    for event in events:
        cursor.execute(query_placeholder(
            "SELECT COUNT(*) FROM videos WHERE eventId = ?"
        ), (event["code"],))
        event["videoCount"] = scalar(cursor.fetchone()) or 0
        cursor.execute(query_placeholder(
            "SELECT COUNT(*) FROM ads WHERE eventId = ?"
        ), (event["code"],))
        event["adCount"] = scalar(cursor.fetchone()) or 0
    return events


def delete_video_by_project(cursor, event_code: str, project_id: str) -> int:
    """Removes a project's video. Returns rows deleted.

    Database rows only. The transcoded media stays in GCS -- deleting objects
    is a separate, irreversible operation that this should not do implicitly.
    """
    cursor.execute(query_placeholder(
        "DELETE FROM videos WHERE eventId = ? AND projectId = ?"
    ), (event_code, project_id))
    return cursor.rowcount or 0


def delete_ad_by_project(cursor, event_code: str, project_id: str) -> int:
    cursor.execute(query_placeholder(
        "DELETE FROM ads WHERE eventId = ? AND projectId = ?"
    ), (event_code, project_id))
    return cursor.rowcount or 0


def delete_event(cursor, event_code: str) -> dict:
    """Deletes a showroom and everything scoped to it.

    Ads, videos and presence rows are all keyed by event, and none of them
    mean anything without it, so they go together in one transaction -- a
    partial delete would leave rows that nothing can reach but that still
    count toward per-event totals.

    Caller commits. Returns what was removed, so the UI can say so plainly.

    Media in GCS is deliberately NOT touched: object deletion is irreversible
    and slow, and the database is the index that makes it findable. The caller
    reports the prefix instead -- see the note in the admin console and CLI.
    """
    cursor.execute(query_placeholder(
        "SELECT COUNT(*) FROM videos WHERE eventId = ?"
    ), (event_code,))
    videos = int(scalar(cursor.fetchone()) or 0)
    cursor.execute(query_placeholder(
        "SELECT COUNT(*) FROM ads WHERE eventId = ?"
    ), (event_code,))
    ads = int(scalar(cursor.fetchone()) or 0)

    cursor.execute(query_placeholder("DELETE FROM ads WHERE eventId = ?"), (event_code,))
    cursor.execute(query_placeholder("DELETE FROM videos WHERE eventId = ?"), (event_code,))
    cursor.execute(query_placeholder(
        "DELETE FROM event_presence WHERE eventId = ?"
    ), (event_code,))
    cursor.execute(query_placeholder(
        "DELETE FROM event_credits WHERE eventId = ?"
    ), (event_code,))
    cursor.execute(query_placeholder("DELETE FROM events WHERE code = ?"), (event_code,))

    return {"videos": videos, "ads": ads}


def new_credit_id() -> str:
    return f"cr_{uuid.uuid4().hex[:12]}"


def list_credits(cursor, event_code: str) -> list:
    """A room's credit links, in display order."""
    cursor.execute(query_placeholder("""
        SELECT * FROM event_credits
        WHERE eventId = ?
        ORDER BY position, createdAt, id
    """), (event_code,))
    return [normalize_row(row) for row in cursor.fetchall()]


def replace_credits(cursor, event_code: str, credits: list) -> int:
    """Replaces a room's credits with the supplied list. Caller commits.

    Whole-list replacement rather than per-row diffing: the admin form submits
    the set it wants, and reconciling additions, removals and reordering
    against stored ids is a lot of machinery for a handful of rows that only
    an organiser ever touches.

    Passing None means "leave them alone" -- distinct from passing [], which
    clears them. Without that distinction, any event edit that did not include
    credits would silently delete them.
    """
    if credits is None:
        return -1

    cursor.execute(query_placeholder(
        "DELETE FROM event_credits WHERE eventId = ?"
    ), (event_code,))

    now = utc_now_iso()
    for position, credit in enumerate(credits):
        cursor.execute(query_placeholder("""
            INSERT INTO event_credits (id, eventId, name, url, position, createdAt)
            VALUES (?, ?, ?, ?, ?, ?)
        """), (new_credit_id(), event_code, credit["name"], credit["url"],
               position, now))
    return len(credits)


def set_event_hashtag(cursor, event_code: str, hashtag):
    cursor.execute(query_placeholder(
        "UPDATE events SET shareHashtag = ? WHERE code = ?"
    ), (hashtag, event_code))


def set_event_social_wall(cursor, event_code: str, url):
    cursor.execute(query_placeholder(
        "UPDATE events SET socialWallUrl = ? WHERE code = ?"
    ), (url, event_code))


def set_event_windows(cursor, event_code: str, opens_at, closes_at, ads_closes_at):
    cursor.execute(query_placeholder("""
        UPDATE events
        SET uploadOpensAt = ?, uploadClosesAt = ?, adsClosesAt = ?
        WHERE code = ?
    """), (opens_at, closes_at, ads_closes_at, event_code))


def new_ad_id() -> str:
    return f"ad_{uuid.uuid4().hex[:12]}"


def find_ad(cursor, event_code: str, project_id: str):
    """The active ad for a project in this showroom, or None."""
    cursor.execute(query_placeholder("""
        SELECT * FROM ads
        WHERE eventId = ? AND projectId = ? AND active = 1
        LIMIT 1
    """), (event_code, project_id))
    return normalize_row(cursor.fetchone())


def list_ads(cursor, event_code: str) -> list:
    cursor.execute(query_placeholder(
        "SELECT * FROM ads WHERE eventId = ? ORDER BY createdAt DESC, id"
    ), (event_code,))
    return [normalize_row(row) for row in cursor.fetchall()]


def active_ad_projects(cursor, event_code: str) -> set:
    """Projects in this showroom with an ad that would actually play.

    One query for the whole grid rather than one per card: the video list is
    polled by every viewer while anything is transcoding, and a per-video
    lookup would multiply that by the number of cards.
    """
    cursor.execute(query_placeholder(
        "SELECT projectId FROM ads WHERE eventId = ? AND active = 1"
    ), (event_code,))
    return {scalar(row) for row in cursor.fetchall() if scalar(row)}


def upsert_ad(cursor, event_code: str, project_id: str, message: str,
              image_url: Optional[str]) -> str:
    """Creates or replaces the ad for a project.

    Re-submitting is how an advertiser corrects their copy, so this replaces
    rather than erroring. The image is preserved when the resubmission omits
    one -- same reasoning as replacing a video, where dropping the picture is
    almost never what was meant. Reactivates a previously disabled ad.
    """
    now = utc_now_iso()
    existing = find_ad_any_state(cursor, event_code, project_id)
    if existing:
        cursor.execute(query_placeholder("""
            UPDATE ads
            SET message = ?, imageUrl = COALESCE(?, imageUrl),
                active = 1, updatedAt = ?
            WHERE id = ?
        """), (message, image_url, now, existing["id"]))
        return existing["id"]

    ad_id = new_ad_id()
    cursor.execute(query_placeholder("""
        INSERT INTO ads (id, eventId, projectId, message, imageUrl, active, createdAt, updatedAt)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
    """), (ad_id, event_code, project_id, message, image_url, now, now))
    return ad_id


def find_ad_any_state(cursor, event_code: str, project_id: str):
    """Like find_ad but ignores `active`, so a disabled ad can be revived."""
    cursor.execute(query_placeholder(
        "SELECT * FROM ads WHERE eventId = ? AND projectId = ? LIMIT 1"
    ), (event_code, project_id))
    return normalize_row(cursor.fetchone())


def set_ads_active(cursor, event_code: str, active: bool) -> int:
    """Switches every ad in a showroom on or off. Returns rows affected."""
    cursor.execute(query_placeholder(
        "UPDATE ads SET active = ?, updatedAt = ? WHERE eventId = ?"
    ), (1 if active else 0, utc_now_iso(), event_code))
    return cursor.rowcount or 0


def find_by_project(cursor, event_code: str, project_id: str):
    """The existing video for a project id in this showroom, or None."""
    cursor.execute(query_placeholder(
        "SELECT * FROM videos WHERE eventId = ? AND projectId = ? LIMIT 1"
    ), (event_code, project_id))
    return normalize_row(cursor.fetchone())


def replace_video(cursor, video_id: str, video: dict):
    """Overwrites an existing row's content, keeping its id.

    Updating in place rather than deleting and re-inserting keeps every link
    that was already shared (`/e/CODE?v=<id>`) pointing at the right video, and
    means a replacement does not count against the showroom's upload cap.

    channelAvatar is preserved when the re-upload omits one -- someone
    replacing a video rarely means to drop their picture. thumbnailUrl is not:
    the old poster frame belongs to the old video, so it resets to the
    placeholder and lets the transcoder generate a fresh one.
    """
    cursor.execute(query_placeholder("""
        UPDATE videos SET
            title = ?,
            description = ?,
            videoUrl = ?,
            thumbnailUrl = ?,
            duration = ?,
            channelName = ?,
            channelAvatar = COALESCE(?, channelAvatar),
            createdAt = ?,
            status = ?,
            -- A real upload replacing a stand-in stops being one. Without
            -- this the row keeps source='placeholder' and is never counted
            -- against the showroom's upload cap.
            source = ?,
            processingStartedAt = NULL,
            -- A replacement is a different file and gets a fresh verdict.
            -- Carrying the old block over would leave a clean re-upload
            -- permanently marked by whatever the first attempt contained.
            moderationCategory = NULL,
            moderationReason = NULL,
            moderatedAt = NULL,
            uploaderIp = COALESCE(?, uploaderIp)
        WHERE id = ?
    """), (
        video.get("title", "Untitled"),
        video.get("description", ""),
        video.get("videoUrl", ""),
        video.get("thumbnailUrl", MISSING_THUMBNAIL),
        video.get("duration", "0:00"),
        video.get("channelName", "Anonymous Vibe"),
        video.get("channelAvatar"),
        video.get("createdAt") or utc_now_iso(),
        video.get("status", STATUS_READY),
        video.get("source", SOURCE_UPLOAD),
        video.get("uploaderIp"),
        video_id,
    ))

def mark_video_blocked(cursor, video_id: str, category: str, reason: str) -> None:
    """Records a screening rejection against a video.

    videoUrl is cleared alongside the status. The row still holds the raw
    object name at this point -- the transcoder blocks before it uploads
    anything -- and leaving that in place would keep a pointer to the rejected
    file in a column the rest of the code treats as a playable URL.
    """
    cursor.execute(query_placeholder("""
        UPDATE videos SET
            status = ?,
            moderationCategory = ?,
            moderationReason = ?,
            moderatedAt = ?,
            videoUrl = '',
            processingStartedAt = NULL
        WHERE id = ?
    """), (STATUS_BLOCKED, category or "unspecified", reason or "", utc_now_iso(), video_id))


def sweep_stale_transcodes(cursor) -> int:
    """Fails rows whose transcoder died without reporting back.

    Without this a lost job holds its concurrency slot for ever, and a
    showroom that loses a few jobs stops accepting new transcodes entirely.
    Rows with no processingStartedAt predate the column, so they are swept
    too rather than being allowed to pin a slot indefinitely.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=TRANSCODE_STALE_MINUTES)
    ).isoformat()
    cursor.execute(query_placeholder("""
        UPDATE videos SET status = ?
        WHERE status = ? AND (processingStartedAt IS NULL OR processingStartedAt < ?)
    """), (STATUS_FAILED, STATUS_PROCESSING, cutoff))
    return cursor.rowcount or 0


def count_by_status(cursor, event_code: str, status: str) -> int:
    cursor.execute(query_placeholder(
        "SELECT COUNT(*) FROM videos WHERE eventId = ? AND status = ?"
    ), (event_code, status))
    return scalar(cursor.fetchone()) or 0


def placeholder_video_urls(cursor, event_code: str) -> set:
    """Seed media already standing in for a project in this showroom.

    Used to hand each project a different default where possible -- a row of
    identical stand-ins reads as a bug rather than as placeholders.
    """
    cursor.execute(query_placeholder(
        "SELECT videoUrl FROM videos WHERE eventId = ? AND source = ?"
    ), (event_code, SOURCE_PLACEHOLDER))
    # scalar(), not row["videoUrl"]. Postgres folds unquoted identifiers, so
    # this column comes back as "videourl" there and as "videoUrl" on SQLite --
    # keying by name raised KeyError in the cloud and nowhere in local tests.
    # Worse, it only fired from the second placeholder onward, because the
    # first call iterates an empty result.
    return {scalar(row) for row in cursor.fetchall()}


def count_uploads(cursor, event_code: str) -> int:
    """Guest uploads in a showroom, in any state. Seeded rows do not count."""
    cursor.execute(query_placeholder(
        "SELECT COUNT(*) FROM videos WHERE eventId = ? AND source = ?"
    ), (event_code, SOURCE_UPLOAD))
    return scalar(cursor.fetchone()) or 0


def claim_next_pending(cursor, event_code: str):
    """Moves the oldest queued row into PROCESSING and returns it.

    Returns None when the queue is empty.

    Two statements, but safe against concurrent drains: the UPDATE is a
    compare-and-swap guarded on `status = PENDING`, so if another instance
    claimed the same row between the SELECT and the UPDATE, this one matches
    zero rows and rowcount reports the loss. Only the winner gets the row
    back, and the loser simply returns None rather than double-triggering a
    transcode.
    """
    cursor.execute(query_placeholder("""
        SELECT id, videoUrl FROM videos
        WHERE eventId = ? AND status = ?
        ORDER BY createdAt, id
        LIMIT 1
    """), (event_code, STATUS_PENDING))
    row = normalize_row(cursor.fetchone())
    if not row:
        return None

    cursor.execute(query_placeholder("""
        UPDATE videos SET status = ?, processingStartedAt = ?
        WHERE id = ? AND status = ?
    """), (STATUS_PROCESSING, utc_now_iso(), row["id"], STATUS_PENDING))
    if not cursor.rowcount:
        return None
    return row


def touch_presence(cursor, event_code: str, client_id: str):
    """Records a heartbeat, then counts everyone currently present.

    Expired rows are deleted on the way through, which keeps the table from
    growing without needing a scheduled cleanup.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=PRESENCE_TTL_SECONDS)).isoformat()

    cursor.execute(query_placeholder(
        "DELETE FROM event_presence WHERE lastSeenAt < ?"
    ), (cutoff,))

    # Upsert: the same viewer reappearing must refresh rather than duplicate.
    cursor.execute(query_placeholder(
        "UPDATE event_presence SET lastSeenAt = ? WHERE eventId = ? AND clientId = ?"
    ), (now.isoformat(), event_code, client_id))
    if not cursor.rowcount:
        cursor.execute(query_placeholder(
            "INSERT INTO event_presence (eventId, clientId, lastSeenAt) VALUES (?, ?, ?)"
        ), (event_code, client_id, now.isoformat()))

    cursor.execute(query_placeholder(
        "SELECT COUNT(*) FROM event_presence WHERE eventId = ? AND lastSeenAt >= ?"
    ), (event_code, cutoff))
    return scalar(cursor.fetchone()) or 0


def count_present(cursor, event_code: str, exclude_client: str = None) -> int:
    """Viewers seen within the TTL, optionally ignoring one client."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=PRESENCE_TTL_SECONDS)
    ).isoformat()
    if exclude_client:
        cursor.execute(query_placeholder("""
            SELECT COUNT(*) FROM event_presence
            WHERE eventId = ? AND lastSeenAt >= ? AND clientId <> ?
        """), (event_code, cutoff, exclude_client))
    else:
        cursor.execute(query_placeholder(
            "SELECT COUNT(*) FROM event_presence WHERE eventId = ? AND lastSeenAt >= ?"
        ), (event_code, cutoff))
    return scalar(cursor.fetchone()) or 0


# Pre-roll ads attached to four of the eight seed videos, so a fresh showroom
# demonstrates the ad flow without anyone having to submit one.
#
# Ads are matched to videos by projectId, and seed videos have none, so each
# entry below also supplies the projectId its video is given. The other four
# seed videos keep a null projectId -- a showroom where some videos have a
# pre-roll and some do not is the realistic case, and worth demonstrating.
#
# Keyed by title rather than by position: seed rows get fresh ids in every
# event, so a title is the only stable handle, and it keeps working if the
# order in mockVideos.json changes.
#
# Two carry an image and two are text-only, because the two treatments animate
# differently and both are worth seeing. The images are existing self-hosted
# thumbnails, deliberately not the ad's own video, so an ad reads as an ad
# rather than as the video played back twice.
SEED_ADS = (
    {
        "title": "Midnight Neon Drive // Synthwave Chill Radio",
        "projectId": "seed-synthhorizon",
        "message": "Nightdrive Audio — analog warmth, digital precision. "
                   "Hand-built synths for people who still care about the low end.",
        "imageUrl": None,
    },
    {
        "title": "Exploring Tokyo's Hidden Cyberpunk Alleys at 3 AM",
        "projectId": "seed-tokyodrifter",
        "message": "Kaido Optics — lenses fast enough for 3 AM.",
        "imageUrl": "/images/thumbnails/v6.jpg",
    },
    {
        "title": "Why I Switched from VS Code to Vim (And Regret It)",
        "projectId": "seed-escapecolonq",
        "message": "Still fighting your editor? Keymap Coffee. "
                   "Dark roast, no modal switching required.",
        "imageUrl": None,
    },
    {
        "title": "Design Systems: The Secret to Speed or a Creative Prison?",
        "projectId": "seed-pixelperfect",
        "message": "Tokens, not opinions. Ship a design system your engineers "
                   "will actually use.",
        "imageUrl": "/images/thumbnails/v3.jpg",
    },
)


def attach_seed_ads(cursor, event_code: str) -> int:
    """Gives a showroom's seed videos their demo pre-roll ads.

    One path serves both fresh seeding and showrooms that were seeded before
    these ads existed, so there is only one behaviour to trust.

    Idempotent by construction: the projectId is only claimed when the video
    has none, and `upsert_ad` replaces rather than inserting a duplicate. That
    matters because the unique index on (eventId, projectId) turns a second
    naive pass into an IntegrityError.

    Returns the number of videos *newly* linked to an ad, not the number of
    ads written -- a re-run refreshes the copy on ads that already exist, and
    reporting those as "attached" would make a no-op look like work.
    """
    attached = 0
    for ad in SEED_ADS:
        cursor.execute(query_placeholder(
            "SELECT id, projectId FROM videos WHERE eventId = ? AND title = ?"
        ), (event_code, ad["title"]))
        video = normalize_row(cursor.fetchone())
        # The seed set can change; skip rather than leave an ad with no video
        # to play before, which the read path treats as a missing ad anyway.
        if not video:
            continue

        project_id = video.get("projectId")
        if not project_id:
            # Never take a projectId a guest upload already holds -- that
            # would collide on the unique index and fail the whole seed.
            cursor.execute(query_placeholder(
                "SELECT 1 FROM videos WHERE eventId = ? AND projectId = ?"
            ), (event_code, ad["projectId"]))
            if cursor.fetchone():
                continue
            cursor.execute(query_placeholder(
                "UPDATE videos SET projectId = ? WHERE id = ?"
            ), (ad["projectId"], video["id"]))
            project_id = ad["projectId"]
            attached += 1

        upsert_ad(cursor, event_code, project_id, ad["message"], ad["imageUrl"])
    return attached


def repair_seed_media(cursor) -> int:
    """Repoints already-seeded rows at the current seed media URLs.

    Showrooms seeded earlier still hold whatever URL was current then, and
    some of those hosts now return 403 -- videos that look fine in the grid
    and fail only when someone presses play. Matching on title is safe because
    only rows marked as seed content are touched, so a guest upload that
    happens to share a title is left alone.
    """
    updated = 0
    for seed in load_seed_videos():
        title, url = seed.get("title"), seed.get("videoUrl")
        if not title or not url:
            continue
        cursor.execute(query_placeholder("""
            UPDATE videos SET videoUrl = ?
            WHERE source = ? AND title = ? AND videoUrl <> ?
        """), (url, SOURCE_SEED, title, url))
        updated += cursor.rowcount
    return updated


def seed_event(cursor, event_code: str) -> int:
    """Gives an event its own fresh copy of the default seed videos and ads.

    Copies get new ids so the same seed set can exist in every event at once.
    The rows point at externally hosted media, so this costs no storage.
    """
    seeds = load_seed_videos()
    for seed in seeds:
        copy = dict(seed)
        copy.pop("id", None)
        insert_video(cursor, copy, event_code)

    # After the videos exist, so an ad is never written without the video it
    # plays before.
    attach_seed_ads(cursor, event_code)
    return len(seeds)

def create_event(cursor, code: str, name: str, opens_at, closes_at, with_seed: bool = True) -> int:
    """Creates an event row and optionally seeds it. Returns the seeded count."""
    cursor.execute(query_placeholder("""
        INSERT INTO events (code, name, uploadOpensAt, uploadClosesAt, createdAt)
        VALUES (?, ?, ?, ?, ?)
    """), (code, name, opens_at, closes_at, utc_now_iso()))
    return seed_event(cursor, code) if with_seed else 0

# Every camelCase column across both tables, keyed by its lowercase form.
# Postgres folds unquoted identifiers, so `SELECT *` there yields `createdat`
# and `uploadopensat` where SQLite yields `createdAt` and `uploadOpensAt`.
# Rows are normalised back to these spellings on the way out so that callers --
# the window logic and the entire frontend contract -- see one shape whichever
# engine is behind them.
_CANONICAL_FIELDS = (
    "id", "eventId", "title", "description", "thumbnailUrl", "videoUrl",
    "duration", "createdAt", "channelName", "channelAvatar", "userId", "status",
    "processingStartedAt", "source", "projectId",
    "moderationCategory", "moderationReason", "moderatedAt", "uploaderIp",
    "code", "name", "uploadOpensAt", "uploadClosesAt", "adsClosesAt",
    "shareHashtag", "socialWallUrl", "position", "url",
    "clientId", "lastSeenAt",
    "message", "imageUrl", "active", "updatedAt",
    "email", "addedAt", "addedBy",
)
_CANONICAL_BY_LOWER = {field.lower(): field for field in _CANONICAL_FIELDS}

# Columns that must never reach an unauthenticated caller.
#
# The public video list is a `SELECT *` fed straight through normalize_row, so
# a new column is published the moment it is added. uploaderIp in particular is
# personal data and the whole point of recording it is that only an organiser
# sees it; moderationReason is withheld because it describes what was in a
# video the viewer is deliberately not being shown.
PRIVATE_VIDEO_FIELDS = ("uploaderIp", "moderationCategory", "moderationReason")


def public_video(row: dict) -> dict:
    """One video as an anonymous viewer may see it.

    Drops the admin-only columns, and blanks the media URLs on a blocked row:
    the HLS output was never uploaded, but a locally-stored raw file would
    otherwise still be fetchable by anyone reading the JSON.
    """
    video = {key: value for key, value in row.items() if key not in PRIVATE_VIDEO_FIELDS}
    if video.get("status") == STATUS_BLOCKED:
        video["videoUrl"] = ""
        # Both images go too. Neither is screened -- only sniffed for a valid
        # signature -- so a submission rejected for its video must not keep
        # serving a poster frame or an avatar chosen by the same uploader.
        video["thumbnailUrl"] = MISSING_THUMBNAIL
        video["channelAvatar"] = MISSING_THUMBNAIL
    return video

def normalize_email(email) -> str:
    """Canonical form for an allowlist lookup.

    Every read and write of admin_users.email goes through this, so a row can
    never be stored in a spelling that a later lookup fails to match.
    """
    return (email or "").strip().lower()

def is_admin_email(cursor, email: str) -> bool:
    """Whether this address is currently allowed to administer the site."""
    address = normalize_email(email)
    if not address:
        return False
    cursor.execute(query_placeholder(
        "SELECT active FROM admin_users WHERE email = ?"
    ), (address,))
    row = cursor.fetchone()
    # A row switched inactive is kept for the audit trail but must not pass.
    return row is not None and bool(scalar(row))

def list_admin_users(cursor) -> list:
    """The live allowlist. Revoked rows are retained but not listed.

    Only active rows are returned, so removing someone in the console makes
    them disappear from it, while the row itself stays behind to block a
    redeploy from re-granting access -- see seed_admin_users.
    """
    cursor.execute("SELECT * FROM admin_users WHERE active = 1 ORDER BY email")
    return [normalize_row(row) for row in cursor.fetchall()]

def count_active_admins(cursor) -> int:
    cursor.execute("SELECT COUNT(*) FROM admin_users WHERE active = 1")
    return int(scalar(cursor.fetchone()) or 0)

def add_admin_user(cursor, email: str, added_by: str) -> str:
    """Grants admin access. Re-adding a revoked address reactivates it."""
    address = normalize_email(email)
    cursor.execute(query_placeholder(
        "SELECT active FROM admin_users WHERE email = ?"
    ), (address,))
    existing = cursor.fetchone()

    if existing is None:
        cursor.execute(query_placeholder("""
            INSERT INTO admin_users (email, addedAt, addedBy, active)
            VALUES (?, ?, ?, 1)
        """), (address, utc_now_iso(), normalize_email(added_by) or added_by))
        return "added"

    if bool(scalar(existing)):
        return "unchanged"

    cursor.execute(query_placeholder("""
        UPDATE admin_users SET active = 1, addedAt = ?, addedBy = ? WHERE email = ?
    """), (utc_now_iso(), normalize_email(added_by) or added_by, address))
    return "restored"

def remove_admin_user(cursor, email: str) -> int:
    """Revokes admin access. Returns the number of rows actually revoked.

    Deactivates rather than deletes. Two reasons, both load-bearing:

      * A deleted row is indistinguishable from one that never existed, so the
        bootstrap seed would re-add the address on the next deploy and
        silently restore access somebody had deliberately revoked.
      * It keeps who-was-an-admin answerable after the fact.

    `is_admin_email` and `list_admin_users` both filter on `active`, so a
    revoked row is inert everywhere: the caller is refused on their next
    request and the address vanishes from the console's list.

    The `active = 1` guard makes this report 0 for an address that was already
    revoked, which the endpoint turns into a 404 rather than a false success.
    """
    address = normalize_email(email)
    cursor.execute(query_placeholder(
        "UPDATE admin_users SET active = 0 WHERE email = ? AND active = 1"
    ), (address,))
    return cursor.rowcount

def seed_admin_users(cursor, emails) -> int:
    """Inserts bootstrap admins that are not already present.

    Only ever adds. A deploy must not resurrect an address that an operator
    deliberately removed, so an existing row -- active or not -- is left alone.
    """
    added = 0
    for raw in emails:
        address = normalize_email(raw)
        if not address:
            continue
        cursor.execute(query_placeholder(
            "SELECT 1 FROM admin_users WHERE email = ?"
        ), (address,))
        if cursor.fetchone() is not None:
            continue
        cursor.execute(query_placeholder("""
            INSERT INTO admin_users (email, addedAt, addedBy, active)
            VALUES (?, ?, ?, 1)
        """), (address, utc_now_iso(), "bootstrap"))
        added += 1
    return added

def normalize_row(row):
    """Restores canonical camelCase keys on a result row."""
    if row is None:
        return None
    return {
        _CANONICAL_BY_LOWER.get(str(key).lower(), key): value
        for key, value in dict(row).items()
    }

def get_event(cursor, code: str):
    """Looks up a single event by code. Returns a dict or None."""
    cursor.execute(query_placeholder("SELECT * FROM events WHERE code = ?"), (code,))
    return normalize_row(cursor.fetchone())

def _column_names(cursor, table: str) -> set:
    """Existing column names for a table, lowercased.

    Postgres folds unquoted identifiers to lower case, so a column declared as
    `userId` is stored as `userid`, while SQLite preserves the original
    spelling. Comparing case-sensitively therefore reports every camelCase
    column as missing on Postgres, and the follow-up ALTER fails with
    DuplicateColumn. Normalising both sides keeps the check portable.
    """
    if DATABASE_URL:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        return {str(scalar(row)).lower() for row in cursor.fetchall()}
    cursor.execute(f"PRAGMA table_info({table})")
    return {str(row[1]).lower() for row in cursor.fetchall()}

def _add_column_if_missing(cursor, table: str, column: str, ddl_type: str):
    if column.lower() not in _column_names(cursor, table):
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

VIDEOS_DDL = """
    CREATE TABLE IF NOT EXISTS videos (
        id TEXT PRIMARY KEY,
        eventId TEXT,
        title TEXT NOT NULL,
        description TEXT,
        thumbnailUrl TEXT,
        videoUrl TEXT,
        duration TEXT,
        -- No view counting: there is no endpoint that records a play, so any
        -- number here would be decoration. createdAt is the real upload time.
        createdAt TEXT,
        channelName TEXT,
        channelAvatar TEXT,
        userId TEXT,
        status TEXT DEFAULT 'ready',
        -- When the row was handed to a transcoder. Used to detect jobs that
        -- died without reporting back; null unless status is 'processing'.
        processingStartedAt TEXT,
        -- Submitter's project identifier. Unique within a showroom, absent on
        -- seeded rows.
        projectId TEXT,
        -- Content screening. Set when status is 'blocked'; null otherwise.
        moderationCategory TEXT,
        moderationReason TEXT,
        moderatedAt TEXT,
        -- The submitter's IP, from X-Forwarded-For. Recorded so an organiser
        -- can identify whoever put something in the room that has to be dealt
        -- with. Never leaves the admin console -- see PRIVATE_VIDEO_FIELDS.
        uploaderIp TEXT
    )
"""

# Presence heartbeats, used to cap concurrent viewers per showroom. Rows are
# transient: anything older than the TTL is swept on the next heartbeat.
ADS_DDL = """
    CREATE TABLE IF NOT EXISTS ads (
        id TEXT PRIMARY KEY,
        eventId TEXT NOT NULL,
        -- Matches videos.projectId: an ad plays before that project's video.
        projectId TEXT NOT NULL,
        message TEXT NOT NULL,
        -- Null for a text-only ad, which gets the animated treatment instead.
        imageUrl TEXT,
        active INTEGER DEFAULT 1,
        createdAt TEXT,
        updatedAt TEXT
    )
"""

# Sponsor / attribution links shown behind the Credits item in a room's nav.
#
# A table rather than a JSON column on events so ordering is explicit and a
# single credit can be removed without rewriting the set -- the same reasoning
# as ads, and the same lifecycle: meaningless without their event, so they are
# deleted with it.
EVENT_CREDITS_DDL = """
    CREATE TABLE IF NOT EXISTS event_credits (
        id TEXT PRIMARY KEY,
        eventId TEXT NOT NULL,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        -- Display order, 0-based. Set from the list's order in the admin form
        -- rather than inferred from createdAt, so reordering does not require
        -- deleting and re-adding.
        position INTEGER DEFAULT 0,
        createdAt TEXT
    )
"""

# The admin allowlist. Signing in with Google proves who someone is; this
# table is what decides whether that person may administer anything. Kept as a
# table rather than an env var so access can be granted and revoked without a
# redeploy, and so a revocation takes effect on the caller's next request.
ADMIN_USERS_DDL = """
    CREATE TABLE IF NOT EXISTS admin_users (
        -- Always stored lower-cased; Google addresses are case-insensitive and
        -- a mixed-case row would silently never match.
        email TEXT PRIMARY KEY,
        addedAt TEXT,
        -- Who granted the access. 'bootstrap' for the deploy-time seed.
        addedBy TEXT,
        active INTEGER DEFAULT 1
    )
"""

PRESENCE_DDL = """
    CREATE TABLE IF NOT EXISTS event_presence (
        eventId TEXT NOT NULL,
        clientId TEXT NOT NULL,
        lastSeenAt TEXT NOT NULL,
        PRIMARY KEY (eventId, clientId)
    )
"""

EVENTS_DDL = """
    CREATE TABLE IF NOT EXISTS events (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        uploadOpensAt TEXT,
        uploadClosesAt TEXT,
        createdAt TEXT
    )
"""

def init_db():
    """Creates tables, migrates pre-event rows into the sandbox event, and seeds it."""
    with get_db_conn() as conn:
        cursor = conn.cursor()

        cursor.execute(EVENTS_DDL)
        cursor.execute(VIDEOS_DDL)
        cursor.execute(PRESENCE_DDL)
        cursor.execute(ADS_DDL)
        cursor.execute(ADMIN_USERS_DDL)
        cursor.execute(EVENT_CREDITS_DDL)
        conn.commit()

        # Bootstrap admins, so a fresh deployment is administrable by someone.
        # Adds only -- see seed_admin_users.
        bootstrap = [
            part for part in ADMIN_BOOTSTRAP_EMAILS.replace(",", " ").split()
        ]
        if bootstrap:
            granted = seed_admin_users(cursor, bootstrap)
            conn.commit()
            if granted:
                print(f"Granted admin access to {granted} bootstrap address(es)")

        # Lets an organiser switch ads off once the event is over, without a
        # redeploy. Same shape and resolver as the upload window.
        _add_column_if_missing(cursor, "events", "adsClosesAt", "TEXT")
        # Per-room share hashtag. NULL on existing rooms, which read as "no
        # hashtag" -- so nothing about their share text changes.
        _add_column_if_missing(cursor, "events", "shareHashtag", "TEXT")
        # Optional link to an external social wall for the event. NULL hides
        # the button entirely, which is the state of every existing room.
        _add_column_if_missing(cursor, "events", "socialWallUrl", "TEXT")
        conn.commit()

        # Columns added after the original single-tenant schema shipped.
        _add_column_if_missing(cursor, "videos", "userId", "TEXT")
        _add_column_if_missing(cursor, "videos", "eventId", "TEXT")
        _add_column_if_missing(cursor, "videos", "createdAt", "TEXT")
        _add_column_if_missing(cursor, "videos", "status", "TEXT")
        _add_column_if_missing(cursor, "videos", "processingStartedAt", "TEXT")
        _add_column_if_missing(cursor, "videos", "projectId", "TEXT")
        # Distinguishes guest uploads from seeded content, so the per-showroom
        # upload cap counts only what guests actually contributed.
        _add_column_if_missing(cursor, "videos", "source", "TEXT")
        # Content screening, added with the moderation pipeline. Existing rows
        # keep NULLs: they predate screening and are not retroactively blocked.
        _add_column_if_missing(cursor, "videos", "moderationCategory", "TEXT")
        _add_column_if_missing(cursor, "videos", "moderationReason", "TEXT")
        _add_column_if_missing(cursor, "videos", "moderatedAt", "TEXT")
        _add_column_if_missing(cursor, "videos", "uploaderIp", "TEXT")
        conn.commit()

        # Rows predating the column are seed content by definition.
        cursor.execute(query_placeholder(
            "UPDATE videos SET source = ? WHERE source IS NULL"
        ), (SOURCE_SEED,))
        conn.commit()

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_videos_eventId ON videos (eventId)")
        # The queue drains by (event, status) and orders by age on every
        # upload and every callback, so it is worth an index of its own.
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_event_status "
            "ON videos (eventId, status)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_presence_event_seen "
            "ON event_presence (eventId, lastSeenAt)"
        )
        # Enforced in the database as well as in the handler: two uploads
        # racing with the same project id would otherwise both pass the
        # application check. Partial, so the many seeded rows with no project
        # id do not collide with each other.
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_event_project "
            "ON videos (eventId, projectId) WHERE projectId IS NOT NULL"
        )
        # One ad per project per showroom; re-submitting replaces it. Enforced
        # in the database as well as the handler so two concurrent submissions
        # cannot both insert.
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ads_event_project "
            "ON ads (eventId, projectId)"
        )
        conn.commit()

        # The sandbox event always exists; it is where pre-event videos live.
        if not get_event(cursor, SANDBOX_EVENT_CODE):
            cursor.execute(query_placeholder("""
                INSERT INTO events (code, name, uploadOpensAt, uploadClosesAt, createdAt)
                VALUES (?, ?, ?, ?, ?)
            """), (SANDBOX_EVENT_CODE, "Sandbox", None, None, utc_now_iso()))
            conn.commit()

        # Backfill: any video predating the events table belongs to the sandbox.
        cursor.execute(query_placeholder(
            "UPDATE videos SET eventId = ? WHERE eventId IS NULL"
        ), (SANDBOX_EVENT_CODE,))
        conn.commit()

        # Give rows written before createdAt existed a sortable timestamp.
        cursor.execute(query_placeholder(
            "UPDATE videos SET createdAt = ? WHERE createdAt IS NULL"
        ), (utc_now_iso(),))
        conn.commit()

        # Rows predating the status column are already watchable.
        cursor.execute(query_placeholder(
            "UPDATE videos SET status = ? WHERE status IS NULL"
        ), (STATUS_READY,))
        conn.commit()

        # Retire the original sample clips.
        #
        # The seed set was replaced wholesale; rows already written into
        # existing showrooms do not migrate themselves, and seeded rows carry
        # no projectId, so the admin console cannot delete them either. This
        # swaps them once.
        #
        # Scoped tightly on purpose: only rows whose source is 'seed' AND whose
        # videoUrl names one of the four retired clips. A guest upload, a
        # placeholder, or a row seeded from the current set is never matched,
        # so re-running this is a no-op.
        # Defined by what the current set IS, not by a list of what it used to
        # be: a seed row whose videoUrl names none of the current clips is
        # stale, whatever it points at. Listing the old filenames instead was
        # the first attempt and quietly missed half of them -- the retired
        # clips were served from ".../sintel/trailer.mp4" and
        # ".../bunny/trailer.mp4", which share a basename and match no
        # hyphenated name.
        current_files = [s.get("seedFile") for s in load_seed_videos() if s.get("seedFile")]
        stale_events = []
        if current_files:
            keep = " AND ".join("videoUrl NOT LIKE ?" for _ in current_files)
            params = (SOURCE_SEED, *[f"%{name}%" for name in current_files])
            cursor.execute(query_placeholder(
                f"SELECT DISTINCT eventId FROM videos WHERE source = ? AND ({keep})"
            ), params)
            stale_events = [scalar(row) for row in cursor.fetchall()]

        if stale_events:
            cursor.execute(query_placeholder(
                f"DELETE FROM videos WHERE source = ? AND ({keep})"
            ), params)
            conn.commit()
            print(f"Removed retired seed videos from {len(stale_events)} showroom(s)")

            for event_code in stale_events:
                if not event_code:
                    continue
                # Re-seed only a room left with no seed content at all. A room
                # an organiser had already topped up with the new clips keeps
                # what it has rather than gaining a second copy.
                cursor.execute(query_placeholder(
                    "SELECT COUNT(*) FROM videos WHERE eventId = ? AND source = ?"
                ), (event_code, SOURCE_SEED))
                if scalar(cursor.fetchone()):
                    continue
                count = seed_event(cursor, event_code)
                conn.commit()
                print(f"Re-seeded {event_code} with {count} current seed videos")

        # Seed the sandbox only when it is empty, so restarts never duplicate rows.
        cursor.execute(query_placeholder(
            "SELECT COUNT(*) FROM videos WHERE eventId = ?"
        ), (SANDBOX_EVENT_CODE,))
        if scalar(cursor.fetchone()) == 0:
            count = seed_event(cursor, SANDBOX_EVENT_CODE)
            conn.commit()
            print(f"Seeded sandbox event with {count} videos from mockVideos.json")

        # Bring showrooms seeded by an earlier version up to date: repair dead
        # media URLs, and give their seed videos the demo pre-roll ads. Both
        # are idempotent, so this is a no-op once applied.
        repaired = repair_seed_media(cursor)
        conn.commit()
        if repaired:
            print(f"Repointed {repaired} seeded video(s) at current media URLs")

        cursor.execute("SELECT code FROM events")
        codes = [scalar(row) for row in cursor.fetchall()]
        attached = sum(attach_seed_ads(cursor, code) for code in codes)
        conn.commit()
        if attached:
            print(f"Attached {attached} seed ad(s) across {len(codes)} showroom(s)")

        print("Database initialized")
