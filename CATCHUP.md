# Vibetube Catch-Up & Hand-Off Guide

Current state of the project, what is deployed, and what is unfinished.

> `README.md` is the reference for how everything works and how to run it.
> This file is the shorter question: where are we, and what is still open.
> `ARCHITECTURE.md` predates the event system and is kept only for history.

---

## 1. What this is

Vibetube is event-based video streaming for Google Cloud workshops. A viewer
enters an **event code** and lands in a showroom at `/e/CODE`. Each showroom is
isolated: its own copy of the seed videos, its own uploads, its own upload
window. Attendees upload the videos they built during the labs; nobody signs
in except organisers.

Live at **vibetube.dev** — GCP project `vibetube-streaming-platform`,
`us-central1`.

Seven showrooms in production as of this writing: `aicampTOR`, `GoogleDMV`,
`aicampNYC`, `UBC`, `GoogleSVL`, `GoogleNYC`, `sandbox`.

---

## 2. How it is put together

One Cloud Run **service** (FastAPI serving both the API and the built React
app) plus one Cloud Run **job** (FFmpeg transcoder). Cloud SQL PostgreSQL in
the cloud, SQLite locally, chosen by `DATABASE_URL`.

```
upload → PENDING → PROCESSING → [screen] → transcode → public bucket → READY
                                   │
                                   └──────→ BLOCKED  (nothing published)
```

**Screening runs before the encode, and that ordering is load-bearing.** A
rejected video costs no CPU and nothing it produced ever reaches the public
bucket. It also means a blocked video was never transcoded, so there is
nothing to simply un-hide — see the open item below.

Two screens, two services:

| Surface | Service | Where |
|---|---|---|
| The video | Gemini on Vertex | `transcoder/moderation.py`, inside the job |
| Title, description, name, ad copy | Model Armor | `backend/moderation.py`, in-request |

---

## 3. Things that will bite you

**Postgres folds unquoted identifiers.** `videoUrl` comes back as `videourl`
in the cloud and `videoUrl` on SQLite. Read rows through `normalize_row` or
`scalar`, never `row["videoUrl"]`. This shipped a 500 to production once, and
local tests could not reproduce it.

**Building an image is not deploying it.** `deploy.sh` builds both images
first, then updates Cloud Run. A deploy that dies in between leaves new images
in the registry while the job keeps running the old digest, because Cloud Run
pins `:latest` to a digest at deploy time. When verifying a deploy, check that
it reached `-> Deploying Vibetube Cloud Run Service`, not that the build
succeeded.

**Event codes are case-insensitive to match, case-sensitive in storage.** The
stored spelling is authoritative: handlers key videos, ads and presence off
`event["code"]`, never off the path parameter, or `/e/googlenyc` resolves the
event and then finds none of its content.

**`dev.sh` leaves orphans if it is killed hard.** They hold ports 8000 and
5173 and the next run fails on "address already in use". Check with
`lsof -nP -iTCP:8000`.

---

## 4. Open items

**A blocked video cannot be released.** Screening sometimes rejects legitimate
attendee work. The policy was rewritten to stop blocking videos for looking
cinematic, which was the cause of every false positive so far, but there is no
way to release one that was already blocked. It is not a status flip: the
video was never transcoded, and `mark_video_blocked` clears `videoUrl`, which
held the only pointer to the raw file. Raw files themselves survive in the
raw-videos bucket, and the filenames are recoverable from Cloud Run job logs
until those age out. Four attendee videos are in this state across three
events.

**Seed videos cannot carry ads.** Ads attach by `projectId` and seeded rows
have none, so no seed card can ever show the pre-roll or the AD badge. In a
room that is mostly seeds, that caps ad coverage severely. Restoring project
ids to the seed entries would fix it, and would ride along on the seed
realignment that already runs in `init_db`.

**`policycheck-1791290607` is a test video in `sandbox`** from verifying the
screening policy. Safe to delete from the admin console.

**Mentions only link on X.** A `@handle` in share text is plain text on
LinkedIn, which creates a mention only through its own autocomplete.

**No way to hop between rooms from inside one.** The banner used to list every
other showroom, which put every code in front of anyone who reached any room.
It now shows only the current room. Getting elsewhere means going back to the
gate page.

---

## 5. Where things live

| | |
|---|---|
| `backend/main.py` | API, upload ingest, ad submission, admin endpoints |
| `backend/database.py` | schema, migrations, every query |
| `backend/moderation.py` | Model Armor text screening |
| `transcoder/job.py` | the transcode pipeline, screening gate |
| `transcoder/moderation.py` | Gemini video screening and the policy |
| `frontend/src/components/` | the whole UI |
| `deploy.sh` | one script, idempotent, reads `.env` |
| `dev.sh` | both halves locally — 5173 frontend, 8000 backend |

The seed clips are **not** in this repo: they live in the public bucket under
`seed/`, and `deploy.sh` uploads them from `SEED_MEDIA_DIR` only when one is
missing. `backend/mockVideos.json` is the manifest.
