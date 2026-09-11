import html
import os
import random
import re
import secrets
import shutil
import struct
import uvicorn
import uuid
import mimetypes
from contextlib import asynccontextmanager
from typing import List, Optional
from urllib.parse import unquote
from fastapi import (
    Depends, FastAPI, File, UploadFile, Form, Header, HTTPException, Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from database import (
    init_db, get_db_conn, query_placeholder, get_event, insert_video,
    new_video_id, normalize_row, scalar, utc_now_iso, DatabaseBusy,
    sweep_stale_transcodes, count_by_status, count_uploads, claim_next_pending,
    touch_presence, count_present, find_by_project, replace_video,
    find_ad, list_ads, upsert_ad, set_ads_active, placeholder_video_urls,
    load_seed_videos,
    create_event, list_events_with_counts, set_event_windows,
    list_credits, replace_credits, set_event_hashtag, set_event_social_wall,
    delete_video_by_project, delete_ad_by_project, delete_event,
    SANDBOX_EVENT_CODE,
    list_admin_users, add_admin_user, remove_admin_user, count_active_admins,
    normalize_email,
    STATUS_PENDING, STATUS_PROCESSING, STATUS_READY, STATUS_FAILED,
    STATUS_BLOCKED, SOURCE_SEED, SOURCE_UPLOAD, SOURCE_PLACEHOLDER,
    MISSING_THUMBNAIL,
    public_video, mark_video_blocked,
)
from auth import require_admin_ui, public_auth_config
from events import public_event, upload_state, ad_submission_open
from moderation import screen_submission, screen_text

# Explicitly register HLS MIME types
mimetypes.add_type("application/x-mpegURL", ".m3u8")
mimetypes.add_type("video/MP2T", ".ts")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # The container starts uvicorn directly, so the __main__ block below never
    # runs in production. Schema creation and the sandbox migration have to
    # happen here or a deployed instance queries tables that do not exist.
    init_db()
    yield

app = FastAPI(title="Vibetube API", lifespan=lifespan)

# Add CORS Middleware to support direct API hits or dev proxy bypass
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(DatabaseBusy)
async def database_busy_handler(request, exc):
    """Surfaces a saturated connection pool as a retryable 503, not a 500."""
    return JSONResponse(
        status_code=503,
        content={"detail": "The server is busy. Please try again in a moment."},
        headers={"Retry-After": "5"},
    )

# Configure upload directory
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- Abuse limits -----------------------------------------------------------
# Uploads are deliberately anonymous, so the event code is the only gate and
# anyone holding a link can post. These bound what that costs.

# Largest accepted upload. Kept well under the Cloud Run instance memory
# limit: the filesystem there is in-memory, and Starlette spools the whole
# multipart body before this endpoint runs, so the ceiling has to leave room
# for the buffered body plus the process itself.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
UPLOAD_CHUNK_BYTES = 1024 * 1024

# Avatars and custom video thumbnails. Far smaller than a video, and capped
# separately so a generous video limit does not also permit huge images.
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))

# Ceiling on videos transcoding at once in one showroom. Each one is a Cloud
# Run Job burning minutes of CPU across three renditions, triggered directly
# with no queue -- so without a cap, N uploads are N concurrent billed jobs.
# Backpressure here is crude but it is the difference between a bounded spend
# and an open one.
MAX_CONCURRENT_TRANSCODES = int(os.getenv("MAX_CONCURRENT_TRANSCODES", "20"))

# Total guest uploads a single showroom will accept, across its whole life.
# Uploads beyond this are refused outright -- unlike the concurrency cap,
# which only defers.
MAX_UPLOADS_PER_EVENT = int(os.getenv("MAX_UPLOADS_PER_EVENT", "300"))

# Viewers allowed in one showroom at a time. Presence is heartbeat-based and
# therefore approximate: a viewer who closes the tab still counts until their
# entry expires (PRESENCE_TTL_SECONDS).
MAX_CONCURRENT_VIEWERS = int(os.getenv("MAX_CONCURRENT_VIEWERS", "2000"))

# Pre-roll ads. The duration is served to the client rather than hardcoded
# there, so it can be tuned without a frontend rebuild -- but the client also
# enforces it, since a browser can ignore whatever the server says.
AD_DURATION_SECONDS = int(os.getenv("AD_DURATION_SECONDS", "10"))

# Mirrors admin.py's alphabet: no 0/O or 1/I, since codes get read off a slide.
EVENT_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_event_code(length: int = 6) -> str:
    """Non-sequential, so one code cannot be used to guess another."""
    return "".join(secrets.choice(EVENT_CODE_ALPHABET) for _ in range(length))
MAX_AD_MESSAGE_CHARS = int(os.getenv("MAX_AD_MESSAGE_CHARS", "280"))

# Mount static files to serve video uploads (kept for local development)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# The built frontend, copied in by the root Dockerfile. Absent during local
# development, where Vite serves the app and proxies /api here instead.
FRONTEND_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "static"))
FRONTEND_INDEX = os.path.join(FRONTEND_DIR, "index.html")
SERVE_FRONTEND = os.path.isfile(FRONTEND_INDEX)

_INDEX_CACHE: Optional[str] = None


def _index_template() -> str:
    """The built index.html, read once. It never changes within a container."""
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        with open(FRONTEND_INDEX, encoding="utf-8") as handle:
            _INDEX_CACHE = handle.read()
    return _INDEX_CACHE

class TranscodeCompletePayload(BaseModel):
    videoUrl: str
    thumbnailUrl: str
    # Measured by ffprobe. Empty when the probe failed, in which case the
    # value already on the row is kept.
    duration: str = ""

class TranscodeFailedPayload(BaseModel):
    error: str = "Transcoding failed."

class ModerationBlockedPayload(BaseModel):
    # A short slug the organiser can scan a list by, e.g. "sexual", "violence".
    category: str = "unspecified"
    # One sentence, written for an organiser rather than the submitter.
    reason: str = "Flagged by content screening."

class SeedVideo(BaseModel):
    title: str
    videoUrl: str
    description: str = ""
    thumbnailUrl: str = "?"
    duration: str = "0:00"
    channelName: str = "Vibetube"
    channelAvatar: str = "?"

class SeedPayload(BaseModel):
    videos: List[SeedVideo]

def require_admin(token: Optional[str]):
    """Guards the event-seeding endpoints.

    Fails closed: with no ADMIN_TOKEN configured the endpoints are unusable
    rather than open. Local development sets it in backend/.env.
    """
    secret = os.getenv("ADMIN_TOKEN")
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="Admin endpoints are disabled because ADMIN_TOKEN is not configured.",
        )
    if token != secret:
        raise HTTPException(status_code=403, detail="Forbidden")

def upload_to_gcs(bucket_name: str, file_obj, destination_blob_name: str) -> str:
    """Helper to upload a file object to a GCS bucket and return public URL."""
    from google.cloud import storage
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)
    file_obj.seek(0)
    blob.upload_from_file(file_obj)
    return f"https://storage.googleapis.com/{bucket_name}/{destination_blob_name}"

def trigger_transcoder_job(video_id: str, unique_filename: str, event_code: str):
    """Triggers the Cloud Run Transcoder Job with environment overrides."""
    from google.cloud import run_v2

    job_name = os.getenv("TRANSCODER_JOB_NAME")
    project = os.getenv("GCP_PROJECT")
    location = os.getenv("GCP_LOCATION", "us-central1")
    raw_bucket = os.getenv("RAW_VIDEOS_BUCKET")
    public_bucket = os.getenv("PUBLIC_STREAMS_BUCKET")
    backend_url = os.getenv("BACKEND_URL")
    secret_token = os.getenv("TRANSCODER_SECRET_TOKEN")

    if not all([job_name, project, raw_bucket, public_bucket]):
        print("GCP variables not fully configured for transcoder job triggering. Skipping job trigger.")
        return False

    try:
        job_path = f"projects/{project}/locations/{location}/jobs/{job_name}"
        input_uri = f"gs://{raw_bucket}/{unique_filename}"
        # Namespaced by event so a finished event's media can be purged wholesale.
        output_dir_uri = f"gs://{public_bucket}/{event_code}/{video_id}/"

        client = run_v2.JobsClient()

        overrides = {
            "container_overrides": [
                {
                    "env": [
                        {"name": "INPUT_GCS_URI", "value": input_uri},
                        {"name": "OUTPUT_GCS_DIR", "value": output_dir_uri},
                        {"name": "VIDEO_ID", "value": video_id},
                        {"name": "BACKEND_URL", "value": backend_url},
                        {"name": "TRANSCODER_SECRET_TOKEN", "value": secret_token},
                        # Screening settings travel with the job rather than
                        # being baked into its revision, so turning screening
                        # off or changing models does not need a job redeploy.
                        {"name": "MODERATION_ENABLED",
                         "value": os.getenv("MODERATION_ENABLED", "true")},
                        {"name": "MODERATION_MODEL",
                         "value": os.getenv("MODERATION_MODEL", "gemini-2.5-flash")},
                        {"name": "MODERATION_FAIL_CLOSED",
                         "value": os.getenv("MODERATION_FAIL_CLOSED", "false")},
                        # The job is deployed with no environment of its own --
                        # everything it knows arrives in these overrides. Vertex
                        # needs a project and a region to address, and without
                        # them the scan reports "GCP_PROJECT is not set" and
                        # fail-open publishes the video unscreened, which looks
                        # exactly like a clean pass unless you read the logs.
                        {"name": "GCP_PROJECT", "value": project},
                        {"name": "GCP_LOCATION", "value": location},
                    ]
                }
            ]
        }

        request = run_v2.RunJobRequest(
            name=job_path,
            overrides=overrides
        )

        print(f"Triggering Cloud Run Job {job_path} with overrides...")
        operation = client.run_job(request=request)
        print(f"Cloud Run Job run triggered successfully: {operation.metadata}")
        return True
    except Exception as e:
        print(f"Error triggering Cloud Run Job: {e}")
        return False

def client_ip(request: Request) -> Optional[str]:
    """The submitter's address, for the admin console's record.

    Behind Cloud Run the socket peer is always the front end, so the real
    client is the first entry in X-Forwarded-For -- Google's load balancer
    appends to that header, and everything after the first hop is
    infrastructure. The header is client-settable in principle, so this is a
    lead for an organiser to follow, not an identity to rely on.

    Worth knowing before reading anything into it: conference wifi is NAT'd and
    mobile carriers use CGNAT, so a whole room can share one address.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    return getattr(request.client, "host", None)


def create_placeholder_video(cursor, code: str, project_id: str) -> str:
    """Adopts a seed clip as a stand-in for a project that has no video yet.

    Points at seed media that is already in the public bucket and already
    playable, so nothing is encoded, uploaded or queued -- a placeholder costs
    one row and no transcoder job.

    Prefers a clip not already standing in for another project in this room.
    Identical stand-ins across a grid read as a bug rather than as
    placeholders; falling back to a rotation once every clip is taken keeps
    that preference from becoming a hard failure in a busy showroom.
    """
    seeds = load_seed_videos()
    if not seeds:
        raise HTTPException(
            status_code=409,
            detail=(
                f"No video found for project '{project_id}', and no seed "
                "content is configured to stand in for it."
            ),
        )

    used = placeholder_video_urls(cursor, code)
    unused = [s for s in seeds if s.get("videoUrl") not in used]
    # Deterministic once every clip is in use, so the rotation is even rather
    # than randomly clumping on one clip.
    chosen = random.choice(unused) if unused else seeds[len(used) % len(seeds)]

    video_id = new_video_id()
    insert_video(cursor, {
        "id": video_id,
        # Project id first so an organiser scanning the grid can see at a
        # glance which submission a card belongs to, then the clip actually
        # standing in, so the card matches what plays.
        "title": f"{project_id} : {chosen.get('title', 'Sample clip')}",
        "description": (
            "This project submitted an ad before its video. A sample clip is "
            "standing in; it is replaced automatically when the team uploads."
        ),
        "thumbnailUrl": chosen.get("thumbnailUrl", MISSING_THUMBNAIL),
        "videoUrl": chosen.get("videoUrl", ""),
        "duration": chosen.get("duration", "0:00"),
        "createdAt": utc_now_iso(),
        "channelName": "Ad submission",
        "channelAvatar": chosen.get("channelAvatar", "?"),
        "status": STATUS_READY,
        "source": SOURCE_PLACEHOLDER,
        "projectId": project_id,
    }, code)
    return video_id


class UploadTooLarge(Exception):
    """Raised once a request body passes MAX_UPLOAD_BYTES."""


def looks_like_video(head: bytes) -> bool:
    """Sniffs a container signature from the first bytes of an upload.

    The browser's accept="video/*" is a file-picker filter, not a guarantee,
    and the declared content type is caller-supplied. Without this, arbitrary
    bytes can be stored and served with a .mp4 name: locally that reaches the
    player as an undecodable black box, and in the cloud it burns a transcoder
    job before failing. This is a cheap gate, not a full validation -- the
    transcoder remains the real authority on whether a file is usable.
    """
    if len(head) < 12:
        return False
    # ISO base media (mp4/m4v/mov): a 4-byte size then 'ftyp'.
    if head[4:8] == b"ftyp":
        return True
    # Matroska / WebM EBML header.
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return True
    # AVI: 'RIFF' .... 'AVI '
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return True
    # Ogg, FLV, and MPEG program/transport streams.
    if head[:4] == b"OggS" or head[:3] == b"FLV":
        return True
    if head[:3] == b"\x00\x00\x01" or head[0:1] == b"\x47":
        return True
    return False


def looks_like_image(head: bytes) -> bool:
    """Sniffs a still-image signature. Same reasoning as looks_like_video."""
    if len(head) < 12:
        return False
    if head[:3] == b"\xff\xd8\xff":                       # JPEG
        return True
    if head[:8] == b"\x89PNG\r\n\x1a\n":                  # PNG
        return True
    if head[:6] in (b"GIF87a", b"GIF89a"):                # GIF
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":     # WebP
        return True
    return False


def store_optional_image(image_file: Optional[UploadFile], label: str) -> Optional[str]:
    """Validates and stores an optional image, returning its URL or None.

    Kept separate from the video path: images have their own (much smaller)
    ceiling, and an unusable avatar should never fail an otherwise good video
    upload silently -- it raises so the uploader is told which file was wrong.
    """
    if image_file is None or not image_file.filename:
        return None

    file_obj = image_file.file
    file_obj.seek(0)
    head = file_obj.read(12)
    file_obj.seek(0, os.SEEK_END)
    size = file_obj.tell()
    file_obj.seek(0)

    if size == 0:
        return None
    if size > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"That {label} is too large. The limit is "
                   f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB.",
        )
    if not looks_like_image(head):
        raise HTTPException(
            status_code=400,
            detail=f"That {label} is not an image. Use a JPEG, PNG, GIF or WebP.",
        )

    extension = os.path.splitext(image_file.filename)[1].lower() or ".jpg"
    unique_filename = f"{uuid.uuid4().hex}{extension}"
    public_bucket = os.getenv("PUBLIC_STREAMS_BUCKET")

    if public_bucket:
        # Straight to the public bucket: these are display assets, not
        # transcoder input, so they need no processing.
        try:
            return upload_to_gcs(public_bucket, file_obj, f"images/{unique_filename}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to store {label}: {e}")

    path = os.path.join(UPLOAD_DIR, unique_filename)
    with open(path, "wb") as buffer:
        shutil.copyfileobj(file_obj, buffer)
    return f"/uploads/{unique_filename}"


def enforce_upload_size(video_file: UploadFile):
    """Rejects oversized uploads, streaming rather than trusting the header.

    Uploads are anonymous, so an unbounded body is a direct route to filling a
    bucket and running up storage costs. Content-Length is only a hint -- it is
    absent on chunked requests and trivially wrong -- so the body is measured
    as it is read. Starlette has already spilled anything large to a temp file
    on disk by this point, which caps memory but not disk, so this still has to
    run before the file is copied anywhere durable.
    """
    file_obj = video_file.file
    total = 0
    head = b""
    file_obj.seek(0)
    while True:
        chunk = file_obj.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        if not head:
            head = chunk[:12]
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise UploadTooLarge()
    if total == 0:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if not looks_like_video(head):
        raise HTTPException(
            status_code=400,
            detail="That file does not look like a video. Please upload a video file.",
        )
    file_obj.seek(0)
    return total


def store_raw_upload(video_file: UploadFile) -> tuple:
    """Persists an uploaded file to GCS (or local disk) and returns (url, filename)."""
    file_extension = os.path.splitext(video_file.filename or "")[1]
    unique_filename = f"{uuid.uuid4().hex}{file_extension}"
    raw_bucket = os.getenv("RAW_VIDEOS_BUCKET")

    if raw_bucket:
        try:
            print(f"Uploading raw file {unique_filename} to GCS bucket {raw_bucket}...")
            video_url = upload_to_gcs(raw_bucket, video_file.file, unique_filename)
            print(f"File uploaded successfully to {video_url}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to upload video to GCS: {str(e)}")
    else:
        file_path = os.path.join(UPLOAD_DIR, unique_filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(video_file.file, buffer)
        video_url = f"/uploads/{unique_filename}"

    return video_url, unique_filename

def set_video_status(video_id: str, status: str):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            query_placeholder("UPDATE videos SET status = ? WHERE id = ?"),
            (status, video_id),
        )
        conn.commit()

def drain_queue(code: str) -> int:
    """Starts transcoder jobs for queued videos, up to the concurrency cap.

    Called after an upload and after every transcoder callback, so a freed
    slot is refilled immediately without anything polling. Safe to call at any
    time: if the queue is empty or the cap is reached it does nothing.

    Each job is triggered outside the database transaction that claimed it.
    Holding a connection open across a Cloud Run API call would tie up a pool
    slot for the duration of a network round trip, and the pool is small.
    """
    started = 0
    while True:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            # Reclaim slots held by jobs that died silently before counting.
            swept = sweep_stale_transcodes(cursor)
            if swept:
                print(f"Released {swept} stale transcode slot(s) in {code}")

            if count_by_status(cursor, code, STATUS_PROCESSING) >= MAX_CONCURRENT_TRANSCODES:
                conn.commit()
                return started

            claimed = claim_next_pending(cursor, code)
            conn.commit()

        if not claimed:
            return started

        video_id = claimed["id"]
        # videoUrl holds the raw object name until the transcoder rewrites it.
        raw_name = os.path.basename(claimed.get("videoUrl") or "")
        if trigger_transcoder_job(video_id, raw_name, code):
            started += 1
        else:
            # The job never started, so nothing will ever call back for it.
            set_video_status(video_id, STATUS_FAILED)


def drain_event_of(video_id: str):
    """Drains the queue of whichever showroom a video belongs to.

    The transcoder callbacks identify a video, not an event, so the event has
    to be looked up before the queue can be advanced.
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            query_placeholder("SELECT eventId FROM videos WHERE id = ?"), (video_id,)
        )
        event_code = scalar(cursor.fetchone())
    if event_code:
        drain_queue(event_code)


def ingest_upload(code: str, video_file: UploadFile, title: str, description: str,
                  duration: str, display_name: str, source: str = SOURCE_UPLOAD,
                  project_id: Optional[str] = None,
                  avatar_file: Optional[UploadFile] = None,
                  thumbnail_file: Optional[UploadFile] = None,
                  uploader_ip: Optional[str] = None) -> str:
    """Stores an upload, records it, and queues it for transcoding.

    The upload itself always succeeds once it passes validation: the row is
    written PENDING and `drain_queue` starts a job only if the showroom is
    below its concurrency cap. Anything over the cap waits its turn instead of
    being rejected, which is the difference between a busy room and a broken
    one.

    Without GCS configured there is no transcoder, so the raw file is the
    final artifact and the row is immediately READY -- otherwise every locally
    uploaded video would sit queued for ever.
    """
    will_transcode = bool(os.getenv("RAW_VIDEOS_BUCKET"))

    # Validation first: everything here runs before the body is stored, so a
    # rejected upload costs no bucket write.
    try:
        enforce_upload_size(video_file)
    except UploadTooLarge:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"That video is too large. The limit is {limit_mb} MB.",
        )

    # Text screening runs here, before anything is stored: the title and
    # description are rendered in the grid and in the share card, so they need
    # a verdict even though the video itself is screened later in the
    # transcoder. Rejecting now costs no bucket write and gives the submitter
    # an error they can act on, rather than a card that silently vanishes.
    #
    # Organiser seeding is exempt -- it is placed deliberately by someone
    # holding the admin token, and screening it only spends quota.
    if source == SOURCE_UPLOAD:
        verdict = screen_submission(
            title=title, description=description, name=display_name,
        )
        if not verdict["allowed"]:
            raise HTTPException(status_code=422, detail=verdict["reason"])

    project_id = (project_id or "").strip() or None

    # Required for guest uploads: the project id is what ties a video to its
    # ad and what the admin console deletes by, so a video without one is
    # unmanageable afterwards. Organiser seeding stays exempt -- seeded
    # content is placed deliberately and has no project behind it.
    if source == SOURCE_UPLOAD and not project_id:
        raise HTTPException(
            status_code=400,
            detail="A project ID is required to publish a video.",
        )

    # A project id that already exists means "replace that submission", not
    # "reject this one" -- teams iterate, and re-submitting is the normal case.
    existing = None
    if project_id:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            existing = find_by_project(cursor, code, project_id)
            # A placeholder is always READY and has no job behind it, so this
            # guard cannot apply to one.
            if existing and existing.get("status") in (STATUS_PENDING, STATUS_PROCESSING):
                # Replacing now would leave the in-flight job to call back
                # against a row it no longer describes, overwriting the new
                # video's URLs with the old one's output.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"The previous upload for project '{project_id}' is still "
                        "processing. Try again once it finishes."
                    ),
                )

    # Organiser seeding is exempt: the cap exists to bound guest contributions,
    # and someone with the admin token can already do as they like. A
    # replacement is exempt too -- it consumes no additional slot.
    #
    # A placeholder is the exception to that exception. It was created by an ad
    # submission, not an upload, so count_uploads never counted it; treating
    # the replacement as exempt would let every ad submission mint one upload
    # that sits outside the cap entirely.
    replacing_placeholder = bool(
        existing and existing.get("source") == SOURCE_PLACEHOLDER
    )
    if source == SOURCE_UPLOAD and (existing is None or replacing_placeholder):
        with get_db_conn() as conn:
            cursor = conn.cursor()
            if count_uploads(cursor, code) >= MAX_UPLOADS_PER_EVENT:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"This showroom has reached its limit of "
                        f"{MAX_UPLOADS_PER_EVENT} uploads."
                    ),
                )

    # Images first: they are small and cheap to reject, so a bad avatar fails
    # before the video is committed to storage.
    avatar_url = store_optional_image(avatar_file, "profile picture")
    thumbnail_url = store_optional_image(thumbnail_file, "video thumbnail")

    video_url, unique_filename = store_raw_upload(video_file)

    fields = {
        "title": title,
        "description": description,
        # "?" is the placeholder the transcoder replaces. A thumbnail the
        # uploader supplied is kept as-is -- see transcode_complete.
        "thumbnailUrl": thumbnail_url or "?",
        "videoUrl": video_url,
        "duration": duration,
        "createdAt": utc_now_iso(),
        "channelName": display_name.strip() or "Anonymous Vibe",
        "status": STATUS_PENDING if will_transcode else STATUS_READY,
        "uploaderIp": uploader_ip,
    }

    with get_db_conn() as conn:
        cursor = conn.cursor()
        if existing:
            video_id = existing["id"]
            # None, not "?", so COALESCE keeps the previous picture when this
            # re-upload did not include one.
            replace_video(cursor, video_id, {
                **fields, "channelAvatar": avatar_url, "source": source,
            })
            print(f"Replaced video {video_id} for project '{project_id}' in {code}")
        else:
            video_id = new_video_id()
            insert_video(cursor, {
                **fields,
                "id": video_id,
                # "?" tells the card to fall back to the uploader's initials.
                "channelAvatar": avatar_url or "?",
                "userId": None,
                "source": source,
                "projectId": project_id,
            }, code)
        conn.commit()

    if will_transcode:
        drain_queue(code)

    return video_id

def load_event_or_404(cursor, code: str) -> dict:
    event = get_event(cursor, code)
    if not event:
        raise HTTPException(status_code=404, detail="No showroom found for that event code.")
    return event

@app.get("/api/events")
def list_public_events():
    """Every showroom, for the in-room switcher.

    NOTE: this makes all event codes public. Codes are generated
    non-sequentially so one cannot be used to guess another, but listing them
    removes that protection entirely -- anyone in any room can now see and
    enter every other room. Only the code and name are exposed; no counts, no
    windows, no content.
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM events ORDER BY createdAt DESC, code")
        rows = [normalize_row(row) for row in cursor.fetchall()]

    # Dates are included so the gate can order events by how near they are to
    # today. All three are returned rather than one derived value, so the
    # client can decide what "the event's date" means without another call.
    return JSONResponse(content=[
        {
            "code": r["code"],
            "name": r["name"],
            "uploadOpensAt": r.get("uploadOpensAt"),
            "uploadClosesAt": r.get("uploadClosesAt"),
            "createdAt": r.get("createdAt"),
        }
        for r in rows
    ])


@app.get("/api/events/{code}")
def read_event(code: str):
    """Resolves an event code. A 404 here is what renders the 'no showroom' screen."""
    with get_db_conn() as conn:
        cursor = conn.cursor()
        event = load_event_or_404(cursor, code)
        # Credits come back on the room fetch the client already makes, so the
        # Credits nav item can decide whether to exist on first paint rather
        # than appearing a moment later.
        return JSONResponse(
            content=public_event(event, credits=list_credits(cursor, code))
        )

class PresencePayload(BaseModel):
    clientId: str


@app.post("/api/events/{code}/presence")
def event_presence(code: str, payload: PresencePayload):
    """Heartbeat that both claims and reports a seat in a showroom.

    Capacity is enforced here rather than at page load so that a viewer who
    stops beating -- closed tab, asleep laptop -- releases their seat on its
    own. The count is therefore approximate by design: someone who leaves
    still occupies a seat until PRESENCE_TTL_SECONDS elapses.

    An already-present viewer is never evicted, so a full room does not start
    throwing people out; it simply stops admitting new ones.
    """
    client_id = payload.clientId.strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="clientId is required.")

    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)

        others = count_present(cursor, code, exclude_client=client_id)
        if others >= MAX_CONCURRENT_VIEWERS:
            conn.commit()
            raise HTTPException(
                status_code=503,
                detail=(
                    f"This showroom is full ({MAX_CONCURRENT_VIEWERS} viewers). "
                    "Try again in a moment."
                ),
            )

        present = touch_presence(cursor, code, client_id)
        conn.commit()

    return {
        "present": present,
        "capacity": MAX_CONCURRENT_VIEWERS,
    }


# --- Admin API --------------------------------------------------------------
# Every route here is guarded by `require_admin_ui` (see auth.py), declared in
# the decorator rather than called in the handler so that a new admin route
# cannot ship unguarded by forgetting a line.
#
# The guard is two checks: Google must have signed the caller's identity, and
# that identity must be on the `admin_users` allowlist. The allowlist is read
# per request, so revoking access takes effect immediately rather than when
# the caller's ID token expires.

class CreditInput(BaseModel):
    name: str
    url: str


class AdminEventPayload(BaseModel):
    name: str
    code: Optional[str] = None
    # ISO-8601 UTC. The browser converts from the operator's local time.
    uploadOpensAt: Optional[str] = None
    uploadClosesAt: Optional[str] = None
    adsClosesAt: Optional[str] = None
    seed: bool = True
    shareHashtag: Optional[str] = None
    socialWallUrl: Optional[str] = None
    # None means "leave the existing credits alone"; [] means "clear them".
    # An edit that omits the field must not wipe what is already there.
    credits: Optional[List[CreditInput]] = None


MAX_CREDITS_PER_EVENT = 20
MAX_CREDIT_NAME_CHARS = 60


# Tokens a room may attach to shared posts. Kept small: a wall of tags reads
# as spam, and LinkedIn demotes posts that look like one.
MAX_SHARE_TAGS = 10
MAX_TAG_BODY_CHARS = 60


def clean_hashtag(raw: Optional[str]) -> Optional[str]:
    """Normalises the tags an organiser types into a ready-to-post string.

    Accepts hashtags and @mentions, separated by spaces or commas, in any
    mixture: "#VibeSummit26, GoogleCloud @googlecloud" stores
    "#VibeSummit26 #GoogleCloud @googlecloud".

    Each token keeps its sigil, defaulting to "#" when none is given -- a bare
    word is far more often a missing "#" than an intended mention, and turning
    it into an @ would tag a stranger. The sigil is kept rather than stripped
    because the two are not interchangeable downstream: this string is now
    pasted into post text verbatim.

    Everything outside [0-9A-Za-z_] is dropped from a token's body, since it
    does not survive as a tag or a handle on any platform.

    Despite the name, this column holds one or more tokens. The name is kept
    to avoid renaming a live column for cosmetic reasons.
    """
    text = (raw or "").strip()
    if not text:
        return None

    tokens = []
    seen = set()
    for piece in re.split(r"[\s,]+", text):
        if not piece:
            continue
        sigil = "@" if piece.startswith("@") else "#"
        body = re.sub(r"[^0-9A-Za-z_]", "", piece)[:MAX_TAG_BODY_CHARS]
        if not body:
            continue
        # "#gemini" and "@gemini" are different things, so the sigil is part
        # of the identity; case is not.
        key = f"{sigil}{body.lower()}"
        if key in seen:
            continue
        seen.add(key)
        tokens.append(f"{sigil}{body}")
        if len(tokens) >= MAX_SHARE_TAGS:
            break

    return " ".join(tokens) or None


def clean_wall_url(raw: Optional[str]) -> Optional[str]:
    """Normalises the social-wall link an organiser types.

    A scheme is added when one is missing, because "my.walls.io/abc" is what
    people paste and a bare host in an href is read as a relative path -- the
    button would navigate inside the showroom instead of out to the wall.
    https, not http: every wall host worth linking serves TLS, and silently
    downgrading someone's link is worse than failing.

    Anything with a scheme that is not http(s) is rejected, same as credits.
    """
    text = (raw or "").strip()
    if not text:
        return None

    # Any scheme at all, not just one followed by "//" -- `javascript:alert(1)`
    # has a scheme but no slashes, and testing for "://" let it through to be
    # rewritten as `https://javascript:alert(1)`. Detect the colon form, then
    # accept only http(s).
    if re.match(r"^[a-z][a-z0-9+.-]*:", text, re.I):
        if not re.match(r"^https?://", text, re.I):
            raise HTTPException(
                status_code=400,
                detail="The social wall link must be an http:// or https:// address.",
            )
    else:
        text = f"https://{text}"
    return text[:500]


def clean_credits(credits: Optional[List[CreditInput]]) -> Optional[list]:
    """Validates operator-supplied credit links.

    Rows with neither a name nor a URL are dropped rather than rejected --
    the admin form always submits its trailing empty row, and erroring on it
    would make adding a credit feel broken.
    """
    if credits is None:
        return None

    cleaned = []
    for credit in credits:
        name = (credit.name or "").strip()
        url = (credit.url or "").strip()
        if not name and not url:
            continue
        if not name or not url:
            raise HTTPException(
                status_code=400,
                detail="A credit needs both a name and a link.",
            )
        # Only http(s). A javascript: or data: URL here would be rendered as a
        # link in every room, and "the admin typed it" is not a reason to hand
        # that to every viewer.
        if not re.match(r"^https?://", url, re.I):
            raise HTTPException(
                status_code=400,
                detail=f"Credit '{name}' must link to an http:// or https:// address.",
            )
        cleaned.append({"name": name[:MAX_CREDIT_NAME_CHARS], "url": url})

    if len(cleaned) > MAX_CREDITS_PER_EVENT:
        raise HTTPException(
            status_code=400,
            detail=f"A showroom can have at most {MAX_CREDITS_PER_EVENT} credits.",
        )
    return cleaned


class AdminUserPayload(BaseModel):
    email: str


@app.get("/api/auth/config")
def auth_config():
    """Public Firebase client configuration for the sign-in page.

    Served at runtime rather than compiled into the frontend bundle, so the
    same image can be pointed at another Firebase project by changing an
    environment variable. These values identify the project; they do not grant
    access to anything -- see the note in auth.py.
    """
    return JSONResponse(content=public_auth_config())


@app.get("/api/admin/me")
def admin_me(admin: dict = Depends(require_admin_ui)):
    """Confirms the caller is a signed-in admin, and says who they are.

    The console calls this after sign-in to decide whether to show itself, so
    an address that is not on the allowlist gets a clear message instead of
    every panel failing separately.
    """
    return JSONResponse(content=admin)


@app.get("/api/admin/users", dependencies=[Depends(require_admin_ui)])
def admin_list_users():
    with get_db_conn() as conn:
        cursor = conn.cursor()
        users = list_admin_users(cursor)
    return JSONResponse(content=[
        {
            "email": u["email"],
            "addedAt": u.get("addedAt"),
            "addedBy": u.get("addedBy"),
            "active": bool(u.get("active")),
        }
        for u in users
    ])


@app.post("/api/admin/users")
def admin_add_user(payload: AdminUserPayload, admin: dict = Depends(require_admin_ui)):
    """Grants admin access to a Google address."""
    email = normalize_email(payload.email)
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email address is required.")

    with get_db_conn() as conn:
        cursor = conn.cursor()
        outcome = add_admin_user(cursor, email, admin["email"])
        conn.commit()

    return JSONResponse(content={"email": email, "outcome": outcome}, status_code=201)


@app.delete("/api/admin/users/{email}")
def admin_remove_user(email: str, admin: dict = Depends(require_admin_ui)):
    """Revokes admin access.

    Two guards against locking everyone out of the console: an admin cannot
    remove themselves, and the last remaining admin cannot be removed at all.
    Recovering from an empty allowlist would mean a redeploy or a hand-written
    SQL statement against Cloud SQL.
    """
    address = normalize_email(unquote(email))
    if address == admin["email"]:
        raise HTTPException(
            status_code=400,
            detail="You cannot remove your own admin access.",
        )

    with get_db_conn() as conn:
        cursor = conn.cursor()
        if count_active_admins(cursor) <= 1:
            raise HTTPException(
                status_code=400,
                detail="Cannot remove the last admin.",
            )
        removed = remove_admin_user(cursor, address)
        conn.commit()

    if not removed:
        raise HTTPException(status_code=404, detail=f"{address} is not an admin.")
    return {"deleted": removed, "email": address}


@app.get("/api/admin/events", dependencies=[Depends(require_admin_ui)])
def admin_list_events():
    with get_db_conn() as conn:
        cursor = conn.cursor()
        events = list_events_with_counts(cursor)
        # Credits are read per event rather than joined: this list is a
        # handful of rooms on an admin-only page, and the join would have to
        # aggregate to avoid multiplying the counts already computed above.
        credits = {e["code"]: list_credits(cursor, e["code"]) for e in events}
    return JSONResponse(content=[
        {**public_event(event, credits=credits.get(event["code"])),
         "videoCount": event["videoCount"], "adCount": event["adCount"]}
        for event in events
    ])


@app.post("/api/admin/events", dependencies=[Depends(require_admin_ui)])
def admin_create_event(payload: AdminEventPayload):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Event name is required.")

    code = (payload.code or "").strip() or generate_event_code()
    opens, closes = payload.uploadOpensAt, payload.uploadClosesAt
    if opens and closes and closes <= opens:
        raise HTTPException(status_code=400, detail="End time must be after start time.")

    with get_db_conn() as conn:
        cursor = conn.cursor()
        if get_event(cursor, code):
            raise HTTPException(status_code=409, detail=f"Event '{code}' already exists.")
        seeded = create_event(cursor, code, name, opens, closes, with_seed=payload.seed)
        if payload.adsClosesAt:
            set_event_windows(cursor, code, opens, closes, payload.adsClosesAt)
        set_event_hashtag(cursor, code, clean_hashtag(payload.shareHashtag))
        set_event_social_wall(cursor, code, clean_wall_url(payload.socialWallUrl))
        replace_credits(cursor, code, clean_credits(payload.credits))
        conn.commit()
        event = get_event(cursor, code)
        credits = list_credits(cursor, code)

    return JSONResponse(
        content={**public_event(event, credits=credits), "seeded": seeded},
        status_code=201,
    )


@app.patch("/api/admin/events/{code}", dependencies=[Depends(require_admin_ui)])
def admin_update_event(code: str, payload: AdminEventPayload):
    """Replaces the event's window. Absent fields clear that bound."""
    opens, closes = payload.uploadOpensAt, payload.uploadClosesAt
    if opens and closes and closes <= opens:
        raise HTTPException(status_code=400, detail="End time must be after start time.")

    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)
        set_event_windows(cursor, code, opens, closes, payload.adsClosesAt)
        if payload.name.strip():
            cursor.execute(query_placeholder(
                "UPDATE events SET name = ? WHERE code = ?"
            ), (payload.name.strip(), code))
        set_event_hashtag(cursor, code, clean_hashtag(payload.shareHashtag))
        set_event_social_wall(cursor, code, clean_wall_url(payload.socialWallUrl))
        # None here means the caller did not send the field, so the stored
        # credits stay put -- see replace_credits.
        replace_credits(cursor, code, clean_credits(payload.credits))
        conn.commit()
        event = get_event(cursor, code)
        credits = list_credits(cursor, code)

    return JSONResponse(content=public_event(event, credits=credits))


@app.post("/api/admin/events/{code}/close", dependencies=[Depends(require_admin_ui)])
def admin_close_event(code: str):
    """Shuts an event: no further video uploads and no further ad changes.

    Both deadlines are set to now rather than to a flag, so the existing
    server-side window logic keeps being the single source of truth.
    """
    now = utc_now_iso()
    with get_db_conn() as conn:
        cursor = conn.cursor()
        event = load_event_or_404(cursor, code)
        set_event_windows(cursor, code, event.get("uploadOpensAt"), now, now)
        conn.commit()
        event = get_event(cursor, code)
        credits = list_credits(cursor, code)
    return JSONResponse(content=public_event(event, credits=credits))


@app.get("/api/admin/events/{code}/entries", dependencies=[Depends(require_admin_ui)])
def admin_list_entries(code: str):
    """Every video and ad in a showroom, keyed by project for the admin table."""
    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)
        cursor.execute(query_placeholder(
            "SELECT * FROM videos WHERE eventId = ? ORDER BY createdAt DESC, id"
        ), (code,))
        videos = [normalize_row(row) for row in cursor.fetchall()]
        ads = list_ads(cursor, code)

    return JSONResponse(content={
        "videos": [
            {
                "id": v["id"], "title": v["title"], "projectId": v.get("projectId"),
                "channelName": v.get("channelName"), "status": v.get("status"),
                "source": v.get("source"), "createdAt": v.get("createdAt"),
                "thumbnailUrl": v.get("thumbnailUrl"),
                # The only place these three are served. This endpoint is
                # behind require_admin_ui; the public list drops them in
                # public_video. Keep them out of any other response.
                "uploaderIp": v.get("uploaderIp"),
                "moderationCategory": v.get("moderationCategory"),
                "moderationReason": v.get("moderationReason"),
            }
            for v in videos
        ],
        "ads": [
            {
                "id": a["id"], "projectId": a.get("projectId"),
                "message": a.get("message"), "imageUrl": a.get("imageUrl"),
                "active": bool(a.get("active")), "updatedAt": a.get("updatedAt"),
            }
            for a in ads
        ],
    })


@app.delete("/api/admin/events/{code}")
def admin_delete_event(code: str, admin: dict = Depends(require_admin_ui)):
    """Deletes a showroom along with its videos, ads and presence rows.

    The sandbox is refused: `init_db` recreates and reseeds it on the next
    cold start, so "deleting" it would appear to work and then silently undo
    itself. Saying no is clearer than a delete that does not stay done.
    """
    if code == SANDBOX_EVENT_CODE:
        raise HTTPException(
            status_code=400,
            detail="The sandbox showroom cannot be deleted; it is recreated on restart.",
        )

    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)
        removed = delete_event(cursor, code)
        conn.commit()

    print(f"Admin {admin['email']} deleted event {code} "
          f"({removed['videos']} videos, {removed['ads']} ads)")

    return {
        "deleted": code,
        **removed,
        # Storage outlives the row on purpose -- see delete_event.
        "mediaPrefix": f"gs://{os.getenv('PUBLIC_STREAMS_BUCKET', '<public-bucket>')}/{code}/",
    }


@app.delete("/api/admin/events/{code}/videos/{project_id}", dependencies=[Depends(require_admin_ui)])
def admin_delete_video(code: str, project_id: str):
    """Deletes a project's video, and its ad along with it.

    The cascade is deliberate: ads are matched to videos by projectId, so an
    ad left behind after its video has gone can never play again. Leaving it
    would only produce invisible rows that still count in the admin totals.

    Both statements share one transaction, so a failure cannot leave the video
    deleted and the ad stranded.
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)
        removed = delete_video_by_project(cursor, code, project_id)
        if not removed:
            raise HTTPException(
                status_code=404,
                detail=f"No video for project '{project_id}' in this showroom.",
            )
        ads_removed = delete_ad_by_project(cursor, code, project_id)
        conn.commit()

    return {"deleted": removed, "adsDeleted": ads_removed, "projectId": project_id}


@app.delete("/api/admin/events/{code}/ads/{project_id}", dependencies=[Depends(require_admin_ui)])
def admin_delete_ad(code: str, project_id: str):
    """Deletes only the ad. The video is untouched and keeps playing."""
    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)
        removed = delete_ad_by_project(cursor, code, project_id)
        conn.commit()
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"No ad for project '{project_id}' in this showroom.",
        )
    return {"deleted": removed, "projectId": project_id}


@app.post("/api/events/{code}/ads")
async def create_ad(
    code: str,
    projectId: str = Form(...),
    message: str = Form(...),
    imageFile: Optional[UploadFile] = File(None),
):
    """Submits the pre-roll ad for a project.

    Open like video upload, but the project must already have a video in this
    showroom. An ad is forced in front of viewers rather than sitting in a
    grid, so tying submission to an existing participant is the least this
    endpoint should require.

    Re-submitting the same projectId replaces the ad; omitting the image keeps
    whichever one is already stored.
    """
    project_id = projectId.strip()
    text = message.strip()

    if not project_id:
        raise HTTPException(status_code=400, detail="projectId is required.")
    if not text:
        raise HTTPException(status_code=400, detail="message is required.")
    if len(text) > MAX_AD_MESSAGE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"That message is too long. The limit is {MAX_AD_MESSAGE_CHARS} characters.",
        )

    # An ad is the one thing on the site a viewer cannot skip past, so its copy
    # is screened before it is stored. Cheap: one short string.
    verdict = screen_text(text, label="ad message")
    if not verdict["allowed"]:
        raise HTTPException(status_code=422, detail=verdict["reason"])

    with get_db_conn() as conn:
        cursor = conn.cursor()
        event = load_event_or_404(cursor, code)

        # The deadline gates changes only; ads already uploaded keep playing.
        if not ad_submission_open(event):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Ad submissions for this showroom are closed. Existing ads "
                    "keep playing but can no longer be changed."
                ),
            )

        # An ad for a project with no video used to be a 409. It now adopts
        # one of the seed clips as a stand-in, so the ad has something to play
        # in front of and the team can upload later without resubmitting it.
        if not find_by_project(cursor, code, project_id):
            placeholder_id = create_placeholder_video(cursor, code, project_id)
            conn.commit()
            print(f"Created placeholder {placeholder_id} for project "
                  f"'{project_id}' in {code}")

    image_url = store_optional_image(imageFile, "ad image")

    with get_db_conn() as conn:
        cursor = conn.cursor()
        ad_id = upsert_ad(cursor, code, project_id, text, image_url)
        conn.commit()

    return {"id": ad_id, "projectId": project_id, "status": "success"}


@app.get("/api/events/{code}/ads/{project_id}")
def get_ad_for_project(code: str, project_id: str):
    """The ad to play before a project's video, or null.

    Fetched when a video opens rather than bundled into the video list: that
    list is polled every few seconds by every viewer, and ad copy has no
    business riding along with it.

    Returns 200 with a null ad rather than 404 when there is nothing to show,
    so the player has one success path and never treats "no ad" as an error.
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)
        # No window check here on purpose: an uploaded ad plays indefinitely.
        # `active` (set by `admin.py set-ads --disable`) is the only thing that
        # stops an ad showing.
        ad = find_ad(cursor, code, project_id)

    if not ad:
        return JSONResponse(content={"ad": None})

    return JSONResponse(content={
        "ad": {
            "id": ad["id"],
            "projectId": ad["projectId"],
            "message": ad["message"],
            "imageUrl": ad.get("imageUrl"),
            "durationSeconds": AD_DURATION_SECONDS,
        }
    })


@app.get("/api/events/{code}/ads")
def get_event_ads(code: str, token: str = Header(None, alias="X-Admin-Token")):
    """Lists every ad in a showroom, including disabled ones. Admin only."""
    require_admin(token)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)
        return JSONResponse(content=list_ads(cursor, code))


@app.get("/api/events/{code}/videos")
def get_event_videos(code: str):
    """Lists the videos belonging to one event. Newest first."""
    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)
        cursor.execute(query_placeholder(
            "SELECT * FROM videos WHERE eventId = ? ORDER BY createdAt DESC, id"
        ), (code,))
        # public_video, not the raw row: this endpoint is unauthenticated, and
        # a plain SELECT * would publish uploaderIp and the moderation notes
        # along with everything else.
        videos = [public_video(normalize_row(row)) for row in cursor.fetchall()]
        return JSONResponse(content=videos)

@app.post("/api/events/{code}/videos")
async def create_video(
    request: Request,
    code: str,
    title: str = Form(...),
    description: str = Form(""),
    duration: str = Form("3:00"),
    displayName: str = Form("Anonymous Vibe"),
    projectId: str = Form(""),
    videoFile: UploadFile = File(...),
    avatarFile: Optional[UploadFile] = File(None),
    thumbnailFile: Optional[UploadFile] = File(None),
):
    """Anonymous upload into an event, allowed only inside the upload window.

    avatarFile and thumbnailFile are optional. Without an avatar the card falls
    back to the uploader's initials; without a thumbnail the transcoder's
    generated frame is used.
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        event = load_event_or_404(cursor, code)

    state = upload_state(event)
    if not state["uploadOpen"]:
        raise HTTPException(status_code=403, detail=state["reason"])

    video_id = ingest_upload(
        code, videoFile, title, description, duration, displayName,
        project_id=projectId, avatar_file=avatarFile, thumbnail_file=thumbnailFile,
        uploader_ip=client_ip(request),
    )
    return {"id": video_id, "status": "success"}

@app.post("/api/events/{code}/seed")
def seed_event_metadata(
    code: str,
    payload: SeedPayload,
    token: str = Header(None, alias="X-Admin-Token"),
):
    """Adds pre-seeded videos that are already hosted elsewhere. Admin only."""
    require_admin(token)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)
        created = [insert_video(cursor, video.model_dump(), code) for video in payload.videos]
        conn.commit()
    return {"created": created, "count": len(created)}

@app.post("/api/events/{code}/seed-upload")
async def seed_event_upload(
    code: str,
    title: str = Form(...),
    description: str = Form(""),
    duration: str = Form("3:00"),
    displayName: str = Form("Vibetube"),
    videoFile: UploadFile = File(...),
    token: str = Header(None, alias="X-Admin-Token"),
):
    """Admin file upload that runs the full transcode pipeline.

    Deliberately exempt from the upload window: organizers seed rooms before
    they open and top them up after they close.
    """
    require_admin(token)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        load_event_or_404(cursor, code)

    video_id = ingest_upload(code, videoFile, title, description, duration, displayName,
                             source=SOURCE_SEED)
    return {"id": video_id, "status": "success"}

@app.post("/api/videos/{video_id}/transcode-complete")
def transcode_complete(
    video_id: str,
    payload: TranscodeCompletePayload,
    token: str = Header(None, alias="X-Transcoder-Token")
):
    """Transcoder callback.

    Intentionally not window-checked: a video uploaded just before the window
    closes finishes transcoding after it, and rejecting that callback would
    strand the video with a placeholder thumbnail forever.
    """
    secret_token = os.getenv("TRANSCODER_SECRET_TOKEN")
    if secret_token and token != secret_token:
        raise HTTPException(status_code=403, detail="Forbidden")

    with get_db_conn() as conn:
        cursor = conn.cursor()
        # The generated thumbnail only replaces the "?" placeholder. An
        # uploader who supplied their own poster frame keeps it -- otherwise
        # the transcoder would silently discard their choice minutes later.
        #
        # That placeholder is bound as a parameter rather than written inline
        # as '?'. `query_placeholder` rewrites every ? to %s for Postgres with
        # a blind string replace, so an inline '?' became the literal '%s' and
        # left the statement with one more placeholder than it had parameters.
        # psycopg2 then raised "IndexError: tuple index out of range", the
        # callback 500'd, and the video sat in `processing` for ever. SQLite
        # leaves ? alone, so this only ever failed in the cloud.
        cursor.execute(
            query_placeholder("""
                UPDATE videos
                SET videoUrl = ?,
                    thumbnailUrl = CASE WHEN thumbnailUrl IS NULL OR thumbnailUrl = ?
                                        THEN ? ELSE thumbnailUrl END,
                    duration = CASE WHEN ? <> '' THEN ? ELSE duration END,
                    status = ?
                WHERE id = ?
            """),
            (payload.videoUrl, MISSING_THUMBNAIL, payload.thumbnailUrl,
             payload.duration, payload.duration, STATUS_READY, video_id)
        )
        conn.commit()
    print(f"Video {video_id} transcoding completed. URLs updated to {payload.videoUrl}")
    # A slot just freed: start the next queued video immediately rather than
    # waiting for someone to upload again.
    drain_event_of(video_id)
    return {"status": "success"}

@app.post("/api/videos/{video_id}/moderation-blocked")
def moderation_blocked(
    video_id: str,
    payload: ModerationBlockedPayload,
    token: str = Header(None, alias="X-Transcoder-Token")
):
    """Screening callback: the transcoder rejected this video's content.

    Reached before any encoding happens, so there is nothing in the public
    bucket to clean up -- the row simply becomes unplayable and the card says
    the submission is under review. The reason and category are stored for the
    admin console and deliberately not returned to viewers.

    Not window-checked, for the same reason the other two callbacks are not: a
    verdict that lands after the window closes still has to be recorded.
    """
    secret_token = os.getenv("TRANSCODER_SECRET_TOKEN")
    if secret_token and token != secret_token:
        raise HTTPException(status_code=403, detail="Forbidden")

    with get_db_conn() as conn:
        cursor = conn.cursor()
        mark_video_blocked(cursor, video_id, payload.category, payload.reason)
        conn.commit()
    print(f"Video {video_id} blocked by screening: {payload.category}")
    # The slot this job held is free again, exactly as on success or failure.
    drain_event_of(video_id)
    return {"status": "recorded"}

@app.post("/api/videos/{video_id}/transcode-failed")
def transcode_failed(
    video_id: str,
    payload: TranscodeFailedPayload,
    token: str = Header(None, alias="X-Transcoder-Token")
):
    """Transcoder failure callback.

    Without this a job that dies leaves the video processing forever, since
    the only other write to that row is the success callback.
    """
    secret_token = os.getenv("TRANSCODER_SECRET_TOKEN")
    if secret_token and token != secret_token:
        raise HTTPException(status_code=403, detail="Forbidden")

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            query_placeholder("UPDATE videos SET status = ? WHERE id = ?"),
            (STATUS_FAILED, video_id)
        )
        conn.commit()
    print(f"Video {video_id} transcoding failed: {payload.error}")
    # A failure frees its slot too; the queue must not stall on bad videos.
    drain_event_of(video_id)
    return {"status": "recorded"}

# Registered last on purpose: FastAPI matches routes in definition order, so
# every /api route above wins before this catch-all sees the request.
if SERVE_FRONTEND:
    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str, request: Request):
        """Serves the built SPA, falling back to index.html for client routes.

        Deep links like /e/SUMMIT have no file on disk -- the router resolves
        them in the browser -- so anything that is not a real asset returns
        index.html rather than a 404.
        """
        # Unmatched API paths must stay JSON 404s, not the HTML shell.
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        candidate = os.path.realpath(os.path.join(FRONTEND_DIR, full_path))
        # Containment check: without it, ../ in the URL would escape the build.
        within_build = candidate == FRONTEND_DIR or candidate.startswith(FRONTEND_DIR + os.sep)
        if within_build and os.path.isfile(candidate):
            # Hashed filenames change whenever their content does, so they can
            # be cached hard. index.html must NOT be -- see below.
            headers = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if full_path.startswith("assets/")
                else None
            )
            return FileResponse(candidate, headers=headers)

        # A missing build asset must 404 rather than fall through to the shell.
        #
        # Returning index.html here meant a stale, cached index.html asking for
        # a bundle that a later deploy had replaced got 200 + HTML instead of a
        # 404. The browser then parsed HTML as JavaScript ("Unexpected token
        # '<'"), the app never booted, and it looked like the API was broken.
        # Only extensionless paths are client routes; anything with a file
        # extension was meant to be a real file.
        if os.path.splitext(full_path)[1]:
            raise HTTPException(status_code=404, detail="Not Found")

        # Social crawlers do not run JavaScript, so a client-rendered page has
        # nothing for them to read. The tags have to be in the HTML as served.
        #
        # no-cache means "revalidate before reuse", not "never store": without
        # it browsers kept serving an old shell after a deploy, pinning users
        # to bundles that no longer exist.
        return HTMLResponse(
            render_index_with_meta(full_path, request),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

ANONYMOUS_NAMES = {"", "anonymous vibe", "anonymous"}

# Kept byte-identical to buildShareText in frontend/src/components/
# ShareButtons.tsx, including the pick() hash, so the X post and the LinkedIn
# card say the same thing about the same video.
CREDITED_BLURBS = (
    '🍿 Grab the popcorn — {name} made "{title}" with Google Cloud and Gemini.',
    '🎬 Lights, camera, {name}! "{title}", built with Google Cloud and Gemini.',
    '🚀 {name} shipped it: "{title}", cooked up with Google Cloud and Gemini.',
    '✨ Straight from {name}\'s brain to your screen — "{title}", made with Google Cloud and Gemini.',
    '🤖 {name} + Google Cloud + Gemini = "{title}". Roll the tape.',
    '🎧 {name} hit record and out came "{title}", powered by Google Cloud and Gemini.',
)

ANONYMOUS_BLURBS = (
    '🍿 Grab the popcorn — "{title}", made with Google Cloud and Gemini.',
    '🎬 Lights, camera, "{title}" — built with Google Cloud and Gemini.',
    '🚀 Freshly shipped: "{title}", cooked up with Google Cloud and Gemini.',
    '✨ Someone made "{title}" with Google Cloud and Gemini, and it is worth a look.',
    '🤖 Google Cloud + Gemini = "{title}". Roll the tape.',
    '🎧 Somebody hit record and out came "{title}", powered by Google Cloud and Gemini.',
)


def pick_variant(seed: str, count: int) -> int:
    """Stable index from a seed. Must match pick() in ShareButtons.tsx.

    Deterministic rather than random so a video's share text never changes
    between the card someone sees and the post they publish, while different
    videos still get different lines.

    Hashes UTF-16 code units, not codepoints: JavaScript's charCodeAt yields
    surrogate halves for anything above the BMP, so iterating Python's ord()
    would disagree with the frontend on any seed containing an emoji.
    """
    encoded = (seed or "").encode("utf-16-le")
    units = struct.unpack(f"<{len(encoded) // 2}H", encoded)
    h = 0
    for unit in units:
        h = (h * 31 + unit) & 0xFFFFFFFF
    return h % count


def share_blurb(author: Optional[str], title: str, event_name: str,
                description: Optional[str] = None, limit: int = 300,
                seed: str = "", hashtag: Optional[str] = None) -> str:
    """The line that appears on a shared card.

    Deliberately playful and in the uploader's voice: this is what someone's
    followers actually read, and a flat "X created Y" reads like a changelog.
    Anonymous uploads use a creator-less phrasing so a card never claims to be
    from "Anonymous Vibe".
    """
    name = (author or "").strip()
    anonymous = name.lower() in ANONYMOUS_NAMES
    variants = ANONYMOUS_BLURBS if anonymous else CREDITED_BLURBS
    template = variants[pick_variant(seed or title, len(variants))]
    blurb = template.format(name=name, title=title)

    extra = (description or "").strip()
    if extra:
        blurb = f"{blurb} {extra}"
    elif event_name:
        blurb = f"{blurb} Now showing in {event_name}."
    # The room's tags, if it has any. Appended before truncation so a long
    # description loses its tail rather than the tags -- those are the part the
    # organiser chose deliberately.
    #
    # Used verbatim: the stored string already carries each token's sigil, so
    # re-adding "#" here would produce "##VibeSummit" and would mangle any
    # @mention into "#@handle".
    tags = (hashtag or "").strip()
    if tags:
        room = limit - len(tags) - 1
        if len(blurb) > room:
            blurb = blurb[:room].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
        return f"{blurb} {tags}"

    # Crawlers truncate long descriptions anyway; doing it here keeps the cut
    # at a word boundary rather than mid-sentence.
    if len(blurb) > limit:
        blurb = blurb[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return blurb


def _absolute(url: Optional[str], request: Request) -> Optional[str]:
    """Makes a stored URL absolute. Crawlers reject relative og:image."""
    if not url or url == "?":
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{str(request.base_url).rstrip('/')}/{url.lstrip('/')}"


def _meta_for(path: str, request: Request) -> dict:
    """Resolves the page being served into share-card metadata.

    Three cases: a single video, a showroom, and everything else. Any database
    failure falls through to the site-level defaults -- a missing preview is a
    far better outcome than a 500 on the page itself.
    """
    site = "Vibetube"
    meta = {
        "title": site,
        "description": "Enter an event code to step into the showroom.",
        "image": _absolute("/logo.svg", request),
        "url": str(request.url),
        "type": "website",
    }

    match = re.match(r"^e/([^/]+)/?$", path)
    if not match:
        return meta

    code = unquote(match.group(1))
    video_id = request.query_params.get("v")

    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            event = get_event(cursor, code)
            if not event:
                return meta

            if video_id:
                cursor.execute(
                    query_placeholder(
                        "SELECT * FROM videos WHERE id = ? AND eventId = ?"
                    ),
                    (video_id, code),
                )
                video = normalize_row(cursor.fetchone())
                # A blocked submission falls through to the showroom's own
                # card. Its title passed text screening, but building a share
                # preview for something the room is deliberately not being
                # shown hands out a link that looks like a video and is not.
                if video and video.get("status") == STATUS_BLOCKED:
                    video = None
                if video:
                    meta.update({
                        "title": f"{video['title']} — {site}",
                        "description": share_blurb(
                            video.get("channelName"),
                            video["title"],
                            event["name"],
                            video.get("description"),
                            seed=video["id"],
                            hashtag=event.get("shareHashtag"),
                        ),
                        "image": _absolute(video.get("thumbnailUrl"), request)
                                 or meta["image"],
                        "type": "video.other",
                    })
                    return meta

            room_line = f"Watch what people are sharing in {event['name']}."
            tags = (event.get("shareHashtag") or "").strip()
            meta.update({
                "title": f"{event['name']} — {site}",
                "description": f"{room_line} {tags}" if tags else room_line,
            })
    except Exception as e:
        # Never let preview metadata break page delivery.
        print(f"Could not build share metadata for '{path}': {e}")

    return meta


def render_index_with_meta(path: str, request: Request) -> str:
    """Injects Open Graph and Twitter card tags into the built index.html.

    Every value is HTML-escaped: titles and descriptions are supplied by
    whoever uploaded the video, so injecting them raw would let an uploader
    close the meta tag and write arbitrary markup into the page.
    """
    meta = _meta_for(path, request)
    esc = lambda value: html.escape(str(value or ""), quote=True)

    tags = [
        f'<meta property="og:site_name" content="Vibetube" />',
        f'<meta property="og:type" content="{esc(meta["type"])}" />',
        f'<meta property="og:title" content="{esc(meta["title"])}" />',
        f'<meta property="og:description" content="{esc(meta["description"])}" />',
        f'<meta property="og:url" content="{esc(meta["url"])}" />',
        f'<meta name="description" content="{esc(meta["description"])}" />',
        # LinkedIn reads og:*; X needs its own card type to render an image.
        f'<meta name="twitter:card" content="summary_large_image" />',
        f'<meta name="twitter:title" content="{esc(meta["title"])}" />',
        f'<meta name="twitter:description" content="{esc(meta["description"])}" />',
    ]
    if meta["image"]:
        tags.append(f'<meta property="og:image" content="{esc(meta["image"])}" />')
        tags.append(f'<meta name="twitter:image" content="{esc(meta["image"])}" />')

    block = "\n    ".join(tags)
    template = _index_template()
    if "</head>" in template:
        return template.replace("</head>", f"    {block}\n  </head>", 1)
    return block + template


if __name__ == "__main__":
    init_db()
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
