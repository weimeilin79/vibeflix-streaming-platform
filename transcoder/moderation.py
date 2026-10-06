"""Content screening for uploaded video, run inside the transcoder job.

Deliberately placed before transcoding rather than after it. The scan needs
only the raw download, so screening first means a rejected video never costs
an encode -- and, more importantly, nothing it produced is ever written to the
public bucket. Screening after the upload would publish the file and then try
to unpublish it, which is a race against every viewer already holding the URL.

Gemini reads the object straight out of GCS, so there is no frame extraction
here: the model sees motion and audio rather than a handful of stills, which
is what catches content a midpoint thumbnail would miss.
"""

import json
import os
import time

# Screening is on wherever a project is configured; local runs have no Vertex
# endpoint and skip it. Set MODERATION_ENABLED=false to turn it off in the
# cloud (a deliberate act -- it is not the default).
MODERATION_ENABLED = os.getenv("MODERATION_ENABLED", "true").strip().lower() not in (
    "false", "0", "no",
)

# Model ids churn faster than this repo is redeployed, so this is a variable
# rather than a constant in code.
MODERATION_MODEL = os.getenv("MODERATION_MODEL", "gemini-2.5-flash")

# What happens when the scan itself cannot produce a verdict -- Vertex down,
# quota exhausted past the retry budget, a malformed response.
#
# Defaults to fail-open: this runs live in front of a workshop audience, and a
# Vertex outage silently blocking every upload in the room is a worse failure
# than a few unscreened videos in a room the organisers are watching anyway.
# Set MODERATION_FAIL_CLOSED=true for an unattended deployment.
FAIL_CLOSED = os.getenv("MODERATION_FAIL_CLOSED", "false").strip().lower() in (
    "true", "1", "yes",
)

# 429 handling. Twenty concurrent transcodes per showroom all reaching Vertex
# at once is well inside the default per-project quota, so a busy room hits
# RESOURCE_EXHAUSTED rather than a hard failure. Waiting is the correct
# response: the job is already asynchronous and nobody is watching it.
#
# Ten attempts thirty seconds apart is a five-minute ceiling, which has to stay
# comfortably inside the job's task timeout (JOB_TIMEOUT in deploy.sh, 20m).
RETRY_ATTEMPTS = int(os.getenv("MODERATION_RETRY_ATTEMPTS", "10"))
RETRY_INTERVAL_SECONDS = float(os.getenv("MODERATION_RETRY_INTERVAL", "30"))

# What screening is for: keeping harmful content off a shared screen in front
# of a room. It is NOT a topicality filter.
#
# The first version framed the task as "attendees upload short demos they built
# during the labs" and blocked anything "clearly not a workshop demo". Both
# halves misfired. These labs produce cinematic generated video -- dragons,
# noir trains, animated shorts -- and the model read exactly that polish as
# evidence of a commercial trailer rather than attendee work. Every block in
# production came back off_topic, none for anything harmful, and because
# screening runs before the encode a rejected video is never converted at all.
#
# So: the bar is harm, the benefit of the doubt goes to the submitter, and an
# organiser removes anything merely unwanted by hand.
POLICY_PROMPT = """You are screening a video submitted to a public showroom at a
Google Cloud developer workshop, where it will be shown on a shared screen to a
room of attendees.

Attendees make these videos with generative AI, so most of them look
cinematic: animation, film-like footage, dramatic scenes, title cards, music.
That polish is the expected output of the lab, not a sign the video came from
somewhere else. Judge only whether the CONTENT is harmful -- never whether it
looks professionally produced, and never whether it resembles a software demo.

Block the video only if it contains:
- sexual or sexually suggestive content, nudity
- graphic violence, gore, or depictions of self-harm
- hate speech, slurs, or harassment of a person or group
- illegal activity, or instructions for causing harm
- shocking or disturbing imagery
- a personal attack on a real, identifiable person
- advertising for an unrelated real commercial product, or a political campaign

Do NOT block for any of these:
- looking cinematic, animated, filmic, or professionally produced
- being a story, a short film, a trailer, a music video, or an advert for an
  invented product -- these are all normal lab output
- not resembling a software demo, a screen recording, or a presentation
- showing no code, no terminal, and no Google Cloud product
- rough production quality, bugs, placeholder art, or an unfinished piece
- mild profanity, cartoon peril, or mild comic violence
- a person simply appearing on camera

When it is a close call, allow it. An organiser can remove a video by hand in
seconds, but a video blocked by mistake is never converted, and its submitter
is left with no way to show their work.

Respond with allowed=true when the video is fine to show. When blocking, set
category to one short slug ("sexual", "violence", "hate", "illegal",
"shocking", "attack", "advertising") and give a one-sentence reason an
organiser can act on. Do not repeat slurs or describe graphic detail.
"""

# The policy above is the default, not the last word. Setting MODERATION_POLICY
# on the job replaces it wholesale, which is how the thresholds get tuned for a
# particular event without rebuilding the image -- a room full of security
# demos wants different limits from a room full of school outreach.
#
# The replacement must still ask for the same three fields; the response schema
# below is what actually constrains the shape.
POLICY_PROMPT = os.getenv("MODERATION_POLICY", "").strip() or POLICY_PROMPT

VERDICT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "allowed": {"type": "BOOLEAN"},
        "category": {"type": "STRING"},
        "reason": {"type": "STRING"},
    },
    "required": ["allowed", "category", "reason"],
}


def is_rate_limited(error: Exception) -> bool:
    """Whether an exception is a quota/rate-limit rejection worth waiting out.

    The genai client surfaces these as several different types depending on
    transport, so this matches on the status code where one is exposed and
    falls back to the message. Matching too broadly only costs a wait; matching
    too narrowly turns a transient 429 into a blocked video.
    """
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if code == 429:
        return True
    text = f"{type(error).__name__}: {error}".upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "RATE_LIMIT" in text


def call_with_retry(operation, label: str):
    """Runs `operation`, waiting out 429s up to the retry budget.

    A fixed interval rather than exponential backoff: quota refills on a fixed
    window, so backing off further just idles the job past the point the quota
    came back. Anything that is not a rate limit is raised immediately -- there
    is nothing to wait for.
    """
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - re-raised below
            if not is_rate_limited(error):
                raise
            last_error = error
            if attempt == RETRY_ATTEMPTS:
                break
            print(
                f"[moderation] {label}: rate limited (attempt {attempt}/"
                f"{RETRY_ATTEMPTS}), retrying in {RETRY_INTERVAL_SECONDS:.0f}s"
            )
            time.sleep(RETRY_INTERVAL_SECONDS)
    raise RuntimeError(
        f"{label}: still rate limited after {RETRY_ATTEMPTS} attempts "
        f"({RETRY_INTERVAL_SECONDS:.0f}s apart): {last_error}"
    )


def unavailable(reason: str) -> dict:
    """The verdict to use when no real verdict could be obtained."""
    if FAIL_CLOSED:
        return {
            "allowed": False,
            "category": "scan_unavailable",
            "reason": "This video could not be screened, so it was not published.",
            "scanned": False,
        }
    print(f"[moderation] scan unavailable, allowing by policy: {reason}")
    return {"allowed": True, "category": "", "reason": "", "scanned": False}


def scan_video(gcs_uri: str, mime_type: str = "video/mp4") -> dict:
    """Screens one video and returns a verdict.

    Returns a dict with `allowed`, `category`, `reason`, and `scanned` -- the
    last distinguishing "the model said this is fine" from "nothing looked at
    it", which the caller logs but does not act on differently.
    """
    if not MODERATION_ENABLED:
        return {"allowed": True, "category": "", "reason": "", "scanned": False}

    project = os.getenv("GCP_PROJECT")
    location = os.getenv("MODERATION_LOCATION") or os.getenv("GCP_LOCATION", "us-central1")
    if not project:
        return unavailable("GCP_PROJECT is not set")

    try:
        from google import genai
        from google.genai import types
    except ImportError as error:
        return unavailable(f"google-genai is not installed ({error})")

    try:
        client = genai.Client(vertexai=True, project=project, location=location)

        # Safety filters are switched off deliberately. They would abort the
        # request on exactly the content this call exists to identify, leaving
        # an exception to interpret instead of a verdict to record. The policy
        # above is the filter; this call needs the model to look and report.
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VERDICT_SCHEMA,
            temperature=0,
            # No tools are passed, so automatic function calling has nothing to
            # do -- but leaving it on makes the client log a paragraph of
            # advice about Chat.send_message on every single call, which in
            # Cloud Run means that paragraph in the logs of every job.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            safety_settings=[
                types.SafetySetting(category=category, threshold="BLOCK_NONE")
                for category in (
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_HARASSMENT",
                )
            ],
        )

        print(f"[moderation] scanning {gcs_uri} with {MODERATION_MODEL}")
        response = call_with_retry(
            lambda: client.models.generate_content(
                model=MODERATION_MODEL,
                contents=[
                    types.Part.from_uri(file_uri=gcs_uri, mime_type=mime_type),
                    POLICY_PROMPT,
                ],
                config=config,
            ),
            label="video scan",
        )
    except Exception as error:  # noqa: BLE001 - every failure is a non-verdict
        return unavailable(str(error))

    raw = (getattr(response, "text", "") or "").strip()
    if not raw:
        # An empty body despite BLOCK_NONE means the request was stopped
        # upstream. That is not a clean verdict, but it is not nothing either:
        # treat it as a block, since the only thing that reliably produces it
        # is content the platform itself refused to process.
        return {
            "allowed": False,
            "category": "model_refused",
            "reason": "The screening model would not return a verdict for this video.",
            "scanned": True,
        }

    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError:
        return unavailable(f"unparseable verdict: {raw[:200]}")

    allowed = bool(verdict.get("allowed"))
    return {
        "allowed": allowed,
        "category": "" if allowed else (verdict.get("category") or "unspecified"),
        "reason": "" if allowed else (verdict.get("reason") or "Flagged by content screening."),
        "scanned": True,
    }
