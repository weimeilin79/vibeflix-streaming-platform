"""Model Armor screening for the free text attached to a submission.

The video itself is screened in the transcoder job (transcoder/moderation.py),
where the file already lives. This module covers the other half of what a guest
can put on the page: title, description, display name, and ad copy. Those are
rendered verbatim in the grid and in the share card, so they need a filter of
their own -- a perfectly innocuous video can arrive titled with a slur.

Model Armor is the right tool for this and the wrong tool for video: it screens
text, which is exactly what this is.

The retry helper is duplicated from the transcoder rather than shared. The two
run as separate images with separate dependency sets and no common package;
a shared module would mean packaging one, which is more machinery than thirty
lines of retry justifies.
"""

import os
import time

MODERATION_ENABLED = os.getenv("MODERATION_ENABLED", "true").strip().lower() not in (
    "false", "0", "no",
)

# Model Armor evaluates against a template that defines which filters are on
# and at what confidence. deploy.sh creates it; this is its id.
TEMPLATE_ID = os.getenv("MODEL_ARMOR_TEMPLATE", "vibetube-text")

FAIL_CLOSED = os.getenv("MODERATION_FAIL_CLOSED", "false").strip().lower() in (
    "true", "1", "yes",
)

# 429 handling, deliberately a smaller budget than the transcoder's.
#
# This call sits inside the upload request, so its retry ceiling is bounded by
# the client's patience and by Cloud Run's 300s request timeout -- the job's
# 10 x 30s would blow straight through both and turn a rate limit into a hung
# upload with no response. Three attempts five seconds apart rides out a brief
# quota blip and gives up well inside the deadline.
#
# Raise MODERATION_TEXT_RETRY_ATTEMPTS/_INTERVAL only alongside the Cloud Run
# request timeout.
RETRY_ATTEMPTS = int(os.getenv("MODERATION_TEXT_RETRY_ATTEMPTS", "3"))
RETRY_INTERVAL_SECONDS = float(os.getenv("MODERATION_TEXT_RETRY_INTERVAL", "5"))


def is_rate_limited(error: Exception) -> bool:
    """Whether an exception is a quota/rate-limit rejection worth waiting out."""
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if code == 429:
        return True
    text = f"{type(error).__name__}: {error}".upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "RATE_LIMIT" in text


def call_with_retry(operation, label: str):
    """Runs `operation`, waiting out 429s up to the retry budget."""
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
        f"{label}: still rate limited after {RETRY_ATTEMPTS} attempts: {last_error}"
    )


def unavailable(reason: str) -> dict:
    """The verdict to use when no real verdict could be obtained."""
    if FAIL_CLOSED:
        return {
            "allowed": False,
            "category": "scan_unavailable",
            "reason": "Submissions cannot be screened right now. Try again shortly.",
        }
    print(f"[moderation] text scan unavailable, allowing by policy: {reason}")
    return {"allowed": True, "category": "", "reason": ""}


def _is_match(state) -> bool:
    """Whether a Model Armor match_state enum means "this tripped".

    Compared exactly, never with `in`. The enum's clean value is
    NO_MATCH_FOUND, which *contains* MATCH_FOUND as a substring -- a
    containment test reads every clean submission as a block, which is exactly
    what it did until this was tested against the real API.
    """
    if state is None:
        return False
    name = (getattr(state, "name", None) or str(state)).upper()
    return name.endswith("MATCH_FOUND") and not name.endswith("NO_MATCH_FOUND")


def _matched(result) -> tuple:
    """Pulls (matched, categories) out of a Model Armor sanitize response.

    The real response nests three levels deep:

        sanitization_result.filter_match_state          <- the verdict
        .filter_results["rai"].rai_filter_result
            .rai_filter_type_results["harassment"].match_state
        .filter_results["csam"].csam_filter_filter_result.match_state

    so the per-category labels live under a different field name for each
    filter family. The verdict is read from the top level, which is uniform;
    the labels are best effort.

    Returns None for `matched` when the top-level verdict is missing, so an
    unrecognised shape is treated as a non-verdict rather than as a pass.
    """
    result = getattr(result, "sanitization_result", None) or result
    state = getattr(result, "filter_match_state", None)
    if state is None:
        return None, ""
    if not _is_match(state):
        return False, ""

    categories = []
    try:
        filter_results = getattr(result, "filter_results", None) or {}
        for key, value in filter_results.items():
            # Each entry carries exactly one populated sub-result, named after
            # its family. Find it rather than hard-coding all five.
            for field in dir(value):
                if not field.endswith("_result"):
                    continue
                sub = getattr(value, field, None)
                if sub is None:
                    continue
                # RAI reports per-category; everything else reports one state.
                per_type = getattr(sub, "rai_filter_type_results", None)
                if per_type:
                    categories.extend(
                        str(name) for name, entry in per_type.items()
                        if _is_match(getattr(entry, "match_state", None))
                    )
                elif _is_match(getattr(sub, "match_state", None)):
                    categories.append(str(key))
    except Exception:  # noqa: BLE001 - labelling only, never the verdict
        pass

    # Deduplicated but order-stable, so a repeated family reads once.
    seen = {}
    for name in categories:
        seen[name] = None
    return True, ", ".join(seen)


def screen_text(text: str, label: str = "text") -> dict:
    """Screens one blob of guest-supplied text.

    Returns `allowed`, `category`, `reason`. Empty text is allowed without a
    round trip -- there is nothing to screen and the call costs quota.
    """
    if not MODERATION_ENABLED or not (text or "").strip():
        return {"allowed": True, "category": "", "reason": ""}

    project = os.getenv("GCP_PROJECT")
    location = os.getenv("MODERATION_LOCATION") or os.getenv("GCP_LOCATION", "us-central1")
    if not project:
        return unavailable("GCP_PROJECT is not set")

    try:
        from google.cloud import modelarmor_v1
    except ImportError as error:
        return unavailable(f"google-cloud-modelarmor is not installed ({error})")

    try:
        # Model Armor is a regional service addressed through a regional
        # endpoint; the global default host does not serve these templates.
        client = modelarmor_v1.ModelArmorClient(
            client_options={"api_endpoint": f"modelarmor.{location}.rep.googleapis.com"}
        )
        template = f"projects/{project}/locations/{location}/templates/{TEMPLATE_ID}"
        request = modelarmor_v1.SanitizeUserPromptRequest(
            name=template,
            user_prompt_data=modelarmor_v1.DataItem(text=text),
        )
        response = call_with_retry(
            lambda: client.sanitize_user_prompt(request=request),
            label=f"{label} scan",
        )
    except Exception as error:  # noqa: BLE001 - every failure is a non-verdict
        return unavailable(str(error))

    matched, categories = _matched(response)
    if matched is None:
        return unavailable("unrecognised Model Armor response shape")
    if not matched:
        return {"allowed": True, "category": "", "reason": ""}

    return {
        "allowed": False,
        "category": categories or "text_policy",
        # Deliberately does not echo the offending text back; the organiser can
        # see the submission in the admin console.
        "reason": f"The {label} was flagged by content screening.",
    }


def screen_submission(**fields) -> dict:
    """Screens several named text fields, returning the first block.

    Fields are screened separately rather than concatenated so the reason names
    the field the submitter has to fix.
    """
    for label, value in fields.items():
        verdict = screen_text(value, label=label)
        if not verdict["allowed"]:
            return verdict
    return {"allowed": True, "category": "", "reason": ""}
