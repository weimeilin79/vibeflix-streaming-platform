#!/bin/bash
set -e

# --- Configuration ----------------------------------------------------------
# The target project and every secret live in .env, which is gitignored. This
# script is committed, so it must not carry either. Copy .env.example to .env
# and fill it in before deploying.
ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.env"
if [ ! -f "${ENV_FILE}" ]; then
  echo "ERROR: ${ENV_FILE} not found."
  echo "       cp .env.example .env   then fill in PROJECT_ID and the secrets."
  exit 1
fi
# `set -a` exports everything the file defines, so it reaches gcloud too.
set -a
# shellcheck source=/dev/null
. "${ENV_FILE}"
set +a

# Fail before touching GCP rather than half-deploying against a bad config.
# Only the target project is needed: credentials live in Secret Manager, are
# generated there on first deploy, and are never written to disk.
: "${PROJECT_ID:?must be set in .env}"
REGION="${REGION:-us-central1}"

# Secret Manager names. The values are generated in GCP and read only by the
# Cloud Run runtime service account.
SECRET_DATABASE_URL="${SECRET_DATABASE_URL:-vibetube-database-url}"
SECRET_TRANSCODER_TOKEN="${SECRET_TRANSCODER_TOKEN:-vibetube-transcoder-token}"
SECRET_ADMIN_TOKEN="${SECRET_ADMIN_TOKEN:-vibetube-admin-token}"

# Resource names. Overridable from .env, but the defaults are derived from
# PROJECT_ID so that two projects never collide on a globally-unique bucket
# name and nothing has to be renamed by hand.
REPO_NAME="${REPO_NAME:-vibetube}"
DB_INSTANCE_NAME="${DB_INSTANCE_NAME:-vibetube-db-instance}"
DB_NAME="${DB_NAME:-vibetube}"
RAW_BUCKET="${RAW_BUCKET:-${PROJECT_ID}-raw-videos}"
PUBLIC_BUCKET="${PUBLIC_BUCKET:-${PROJECT_ID}-public-streams}"
JOB_NAME="${JOB_NAME:-transcoder-job}"
SERVICE_NAME="${SERVICE_NAME:-vibetube-service}"

# Admin console access. These addresses are inserted into the admin_users
# allowlist on first start; everything after that is managed in the console.
# The seed only ever adds, so removing someone there is not undone by a
# redeploy. See seed_admin_users in backend/database.py.
ADMIN_BOOTSTRAP_EMAILS="${ADMIN_BOOTSTRAP_EMAILS:-weimeilin@gmail.com,linchr@google.com}"

# Cloud SQL machine type. db-g1-small is the next tier above db-f1-micro;
# both are shared-core, so a busy event may still want a dedicated vCPU
# (db-n1-standard-1). The tier also sets the connection ceiling -- see below.
DB_TIER="${DB_TIER:-db-g1-small}"

# --- Blast radius -----------------------------------------------------------
# Uploads are anonymous, so these bound what an abusive client can cost.
#
# The database connection budget is the binding constraint: every Cloud Run
# instance holds its own pool, so keep MAX_INSTANCES x DB_POOL_MAX under the
# tier's max_connections -- 10 x 5 = 50 here. Confirm the real ceiling after
# any tier change with:  SHOW max_connections;
MAX_INSTANCES="${MAX_INSTANCES:-10}"
DB_POOL_MAX="5"
# Requests served per instance before Cloud Run adds another.
CONCURRENCY="60"
# Largest accepted upload, in bytes (50 MB). Must stay well below MEMORY:
# Cloud Run's filesystem is in-memory and the multipart body is buffered in
# full before the size check can run.
MAX_UPLOAD_BYTES="52428800"
# Set explicitly rather than relying on the 512Mi default, so the headroom
# above MAX_UPLOAD_BYTES is deliberate.
MEMORY="1Gi"
# Videos transcoding simultaneously within one showroom. Each is a billed
# Cloud Run Job, so this caps concurrent spend per event. Uploads over the cap
# are queued, not rejected.
MAX_CONCURRENT_TRANSCODES="20"
# Total guest uploads one showroom will accept. Reached, further uploads 409.
MAX_UPLOADS_PER_EVENT="300"
# Simultaneous viewers per showroom, enforced by heartbeat. At this scale the
# database tier and pool settings below are the real constraint -- see the
# capacity note in README.md before raising it further.
MAX_CONCURRENT_VIEWERS="2000"
# How long a heartbeat holds a seat. Must exceed the client's 30s interval.
PRESENCE_TTL_SECONDS="90"
# A transcode still running after this is treated as dead and its slot freed.
TRANSCODE_STALE_MINUTES="30"

echo "==============================================="
echo "  Starting Vibetube Deployment Orchestrator    "
echo "==============================================="

# 1. Select the correct GCP project
echo "-> Selecting GCP project: ${PROJECT_ID}..."
if ! gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "ERROR: project '${PROJECT_ID}' does not exist or is not visible to"
  echo "       $(gcloud config get-value account 2>/dev/null)."
  echo "       Check PROJECT_ID in .env against:  gcloud projects list"
  exit 1
fi
gcloud config set project ${PROJECT_ID}

# Enabling APIs and creating a Cloud SQL instance both require billing. Without
# this check the script gets several steps in before failing on a message that
# does not name the real cause.
if [ "$(gcloud billing projects describe "${PROJECT_ID}" --format='value(billingEnabled)' 2>/dev/null)" != "True" ]; then
  echo "ERROR: billing is not enabled on '${PROJECT_ID}'."
  echo "       Cloud Run, Cloud SQL and Cloud Build all require it. Link a"
  echo "       billing account, then re-run:"
  echo "         gcloud billing accounts list"
  echo "         gcloud billing projects link ${PROJECT_ID} --billing-account=ACCOUNT_ID"
  exit 1
fi

# 2. Enable GCP Service APIs
echo "-> Enabling required GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  storage.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  identitytoolkit.googleapis.com \
  firebase.googleapis.com \
  aiplatform.googleapis.com \
  modelarmor.googleapis.com

# --- Firebase Authentication ------------------------------------------------
# The admin console signs in with Google through Firebase. This block is
# idempotent: it adds Firebase to the project and creates a web app only if
# they are missing, then reads the client configuration back rather than
# keeping a copy in .env, so there is one source of truth.
#
# The API key and auth domain read here are public values -- they identify the
# project and ship in client code. Admin access is decided by the allowlist in
# the database, checked server-side. See backend/auth.py.
echo "-> Configuring Firebase Authentication..."

fb_api() {
  # $1 method, $2 url, rest: curl args. The quota-project header is required
  # because the caller's default project may not be this one.
  local method="$1" url="$2"; shift 2
  curl -s -X "${method}" \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "x-goog-user-project: ${PROJECT_ID}" \
    -H "Content-Type: application/json" \
    "$@" "${url}"
}

json_get() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d${1} if d else '')" 2>/dev/null || true; }

if ! fb_api GET "https://firebase.googleapis.com/v1beta1/projects/${PROJECT_ID}" \
     | grep -q '"projectId"'; then
  echo "   Adding Firebase to ${PROJECT_ID}..."
  fb_api POST "https://firebase.googleapis.com/v1beta1/projects/${PROJECT_ID}:addFirebase" -d '{}' >/dev/null
  # addFirebase is a long-running operation; the web app call below fails
  # until it lands, so wait rather than racing it.
  for _ in $(seq 1 20); do
    sleep 5
    fb_api GET "https://firebase.googleapis.com/v1beta1/projects/${PROJECT_ID}" \
      | grep -q '"projectId"' && break
  done
fi

WEB_APPS="$(fb_api GET "https://firebase.googleapis.com/v1beta1/projects/${PROJECT_ID}/webApps")"
WEB_APP_NAME="$(printf '%s' "${WEB_APPS}" | json_get "['apps'][0]['name']")"

if [ -z "${WEB_APP_NAME}" ]; then
  echo "   Creating Firebase web app..."
  OP="$(fb_api POST "https://firebase.googleapis.com/v1beta1/projects/${PROJECT_ID}/webApps" \
        -d '{"displayName":"Vibetube Admin"}' | json_get "['name']")"
  for _ in $(seq 1 25); do
    sleep 5
    RESULT="$(fb_api GET "https://firebase.googleapis.com/v1beta1/${OP}")"
    WEB_APP_NAME="$(printf '%s' "${RESULT}" | json_get "['response']['name']")"
    [ -n "${WEB_APP_NAME}" ] && break
  done
fi

if [ -z "${WEB_APP_NAME}" ]; then
  echo "ERROR: Could not create or find a Firebase web app for ${PROJECT_ID}."
  echo "       The admin console cannot sign in without one."
  exit 1
fi

WEB_CONFIG="$(fb_api GET "https://firebase.googleapis.com/v1beta1/${WEB_APP_NAME}/config")"
FIREBASE_API_KEY="$(printf '%s' "${WEB_CONFIG}" | json_get "['apiKey']")"
FIREBASE_AUTH_DOMAIN="$(printf '%s' "${WEB_CONFIG}" | json_get "['authDomain']")"

if [ -z "${FIREBASE_API_KEY}" ] || [ -z "${FIREBASE_AUTH_DOMAIN}" ]; then
  echo "ERROR: Could not read the Firebase web configuration."
  exit 1
fi
echo "   Firebase web app ready (authDomain: ${FIREBASE_AUTH_DOMAIN})"

# Identity Platform must be initialised before any sign-in works. Already-
# initialised projects return an error here, which is fine to ignore.
fb_api POST \
  "https://identitytoolkit.googleapis.com/v2/projects/${PROJECT_ID}/identityPlatform:initializeAuth" \
  -d '{}' >/dev/null 2>&1 || true

# Whether an operator still has to switch the Google provider on by hand.
# Creating the OAuth client it needs has no supported API, so this is checked
# and reported rather than automated -- see the closing message.
#
# The provider is identified by its resource name ending in "/google.com" and
# by `enabled`. This response carries no `idpId` field -- that only appears on
# the defaultSupportedIdps *catalogue* endpoint -- so grepping for one always
# reported "not enabled" and nagged about a step already done.
GOOGLE_IDP_ENABLED="$(fb_api GET \
  "https://identitytoolkit.googleapis.com/admin/v2/projects/${PROJECT_ID}/defaultSupportedIdpConfigs" \
  | python3 -c "
import sys, json
try:
    configs = json.load(sys.stdin).get('defaultSupportedIdpConfigs', [])
except Exception:
    configs = []
print(int(any(
    c.get('name', '').endswith('/google.com') and c.get('enabled') and c.get('clientId')
    for c in configs
)))
" 2>/dev/null || echo 0)"

# --- Secret Manager ---------------------------------------------------------
# Credentials are generated inside GCP on first deploy and never leave it. They
# are not in .env, not in this script, and never written to disk -- so there is
# nothing to leak from a laptop or a commit.
#
# The Cloud Run runtime service account is granted read access per secret,
# rather than a project-wide role.
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
RUNTIME_SA="${RUNTIME_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"

secret_exists() {
  gcloud secrets describe "$1" >/dev/null 2>&1
}

# Creates the secret with a freshly generated value if it does not exist yet.
# Existing secrets are left completely alone: re-running this script must never
# rotate a credential out from under a running service.
ensure_generated_secret() {
  local name="$1"
  if secret_exists "${name}"; then
    echo "   Secret ${name} already exists (leaving it unchanged)."
    return
  fi
  echo "   Creating secret ${name} with a generated value..."
  gcloud secrets create "${name}" --replication-policy=automatic >/dev/null
  openssl rand -hex 32 | tr -d '\n' \
    | gcloud secrets versions add "${name}" --data-file=- >/dev/null
}

grant_secret_access() {
  gcloud secrets add-iam-policy-binding "$1" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
}

echo "-> Ensuring Secret Manager entries..."
ensure_generated_secret "${SECRET_TRANSCODER_TOKEN}"
ensure_generated_secret "${SECRET_ADMIN_TOKEN}"

# 3. Create Cloud Storage Buckets
echo "-> Checking GCS Buckets..."
if ! gcloud storage buckets describe gs://${RAW_BUCKET} >/dev/null 2>&1; then
  echo "   Creating raw videos bucket gs://${RAW_BUCKET}..."
  gcloud storage buckets create gs://${RAW_BUCKET} --location=${REGION}
fi

if ! gcloud storage buckets describe gs://${PUBLIC_BUCKET} >/dev/null 2>&1; then
  echo "   Creating public streaming bucket gs://${PUBLIC_BUCKET}..."
  gcloud storage buckets create gs://${PUBLIC_BUCKET} --location=${REGION}
  
  # Allow public read access to streaming files.
  #
  # Buckets are created with public access prevention enforced, which blocks
  # the allUsers binding below. `gcloud storage buckets update --clear-pap`
  # reports success but leaves the setting at "enforced"; the JSON API does
  # apply it, so that is used here. Re-check with:
  #   gcloud storage buckets describe gs://BUCKET \
  #     --format='value(public_access_prevention)'   # want: inherited
  echo "   Clearing public access prevention..."
  curl -s -X PATCH \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "x-goog-user-project: ${PROJECT_ID}" \
    -H "Content-Type: application/json" \
    "https://storage.googleapis.com/storage/v1/b/${PUBLIC_BUCKET}?fields=iamConfiguration" \
    -d '{"iamConfiguration":{"publicAccessPrevention":"inherited"}}' >/dev/null

  PAP_STATE="$(gcloud storage buckets describe gs://${PUBLIC_BUCKET} \
    --format='value(public_access_prevention)' 2>/dev/null || true)"
  if [ "${PAP_STATE}" = "enforced" ]; then
    echo "ERROR: public access prevention is still enforced on gs://${PUBLIC_BUCKET}."
    echo "       This is usually an organization policy on"
    echo "       constraints/storage.publicAccessPrevention, which only an org"
    echo "       admin can exempt. Viewers stream directly from this bucket, so"
    echo "       playback will not work until it is cleared."
    exit 1
  fi

  echo "   Applying public reader IAM policies..."
  gcloud storage buckets add-iam-policy-binding gs://${PUBLIC_BUCKET} \
    --member=allUsers \
    --role=roles/storage.objectViewer
    
  # Create CORS config for streaming delivery
  echo "   Applying CORS rules to allow browser media player downloads..."
  cat <<EOF > /tmp/cors.json
[
  {
    "origin": ["*"],
    "method": ["GET", "HEAD", "OPTIONS"],
    "responseHeader": ["Content-Type", "Access-Control-Allow-Origin"],
    "maxAgeSeconds": 3600
  }
]
EOF
  gcloud storage buckets update gs://${PUBLIC_BUCKET} --cors-file=/tmp/cors.json
  rm -f /tmp/cors.json
fi

# --- Seed media -------------------------------------------------------------
# The seed videos are served from this bucket rather than from third-party
# sample hosts, which rot: w3schools.com started returning 403 HTML for its
# sample clips and silently broke half the seeded videos in every showroom.
# They looked fine in the grid and failed only on play.
#
# The clips themselves are NOT in this repo -- 25 MB of binary in a repo that
# gets cloned at every workshop is not worth it. They live in the bucket, and
# are uploaded once from SEED_MEDIA_DIR (set it in .env only when adding or
# replacing clips; an ordinary deploy needs it no more than it needs the
# videos). Anything already present is skipped, so redeploys cost nothing.
#
# Whatever is here must match the seedFile values in backend/mockVideos.json.
SEED_MEDIA_FILES="video01.mp4 video02.mp4 video03.mp4 video04.mp4 video05.mp4 video06.mp4"

echo "-> Checking seed media in gs://${PUBLIC_BUCKET}/seed/..."
for NAME in ${SEED_MEDIA_FILES}; do
  if gcloud storage objects describe "gs://${PUBLIC_BUCKET}/seed/${NAME}" >/dev/null 2>&1; then
    continue
  fi
  if [ -z "${SEED_MEDIA_DIR:-}" ] || [ ! -f "${SEED_MEDIA_DIR}/${NAME}" ]; then
    echo "   WARNING: ${NAME} is missing from the bucket and SEED_MEDIA_DIR"
    echo "            does not provide it. Seeded showrooms will show a card"
    echo "            whose video does not play until it is uploaded:"
    echo "              gcloud storage cp ${NAME} gs://${PUBLIC_BUCKET}/seed/"
    continue
  fi
  echo "   Uploading ${NAME} from ${SEED_MEDIA_DIR}..."
  gcloud storage cp "${SEED_MEDIA_DIR}/${NAME}" "gs://${PUBLIC_BUCKET}/seed/${NAME}" \
    --content-type=video/mp4 --cache-control="public, max-age=86400" >/dev/null
  echo "   Stored ${NAME}."
done

# 4. Provision Cloud SQL PostgreSQL Instance
#
# The database password is generated here, used immediately, and stored only as
# part of the DATABASE_URL secret. It is held in a shell variable for the few
# lines that need it and never echoed, written to a file, or passed to Cloud
# Run directly -- the service reads the whole DSN from Secret Manager.
echo "-> Checking Cloud SQL Database Instance..."
if ! gcloud sql instances describe ${DB_INSTANCE_NAME} >/dev/null 2>&1; then
  echo "   WARNING: Cloud SQL instance provisioning can take 5 to 10 minutes."
  echo "   Creating database instance ${DB_INSTANCE_NAME} (PostgreSQL 15, ${DB_TIER})..."
  gcloud sql instances create ${DB_INSTANCE_NAME} \
    --database-version=POSTGRES_15 \
    --tier=${DB_TIER} \
    --region=${REGION} \
    --quiet

  GENERATED_DB_PASSWORD="$(openssl rand -base64 32 | tr -d '\n/+=' | cut -c1-32)"

  echo "   Setting database master password..."
  gcloud sql users set-password postgres \
    --instance=${DB_INSTANCE_NAME} \
    --password="${GENERATED_DB_PASSWORD}" >/dev/null

  echo "   Creating database '${DB_NAME}'..."
  gcloud sql databases create ${DB_NAME} --instance=${DB_INSTANCE_NAME} >/dev/null

  echo "   Storing DATABASE_URL in Secret Manager..."
  DSN="postgresql://postgres:${GENERATED_DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${PROJECT_ID}:${REGION}:${DB_INSTANCE_NAME}"
  if ! secret_exists "${SECRET_DATABASE_URL}"; then
    gcloud secrets create "${SECRET_DATABASE_URL}" --replication-policy=automatic >/dev/null
  fi
  printf '%s' "${DSN}" | gcloud secrets versions add "${SECRET_DATABASE_URL}" --data-file=- >/dev/null
  unset GENERATED_DB_PASSWORD DSN
fi

# An instance without a matching secret cannot be recovered automatically: the
# password is not retrievable from Cloud SQL. Say so plainly rather than
# deploying a service that cannot reach its database.
if ! secret_exists "${SECRET_DATABASE_URL}"; then
  echo ""
  echo "ERROR: Cloud SQL instance '${DB_INSTANCE_NAME}' exists but secret"
  echo "       '${SECRET_DATABASE_URL}' does not, so the connection string is unknown."
  echo "       Either store the existing DSN:"
  echo "         printf '%s' 'postgresql://postgres:PASSWORD@/${DB_NAME}?host=/cloudsql/${PROJECT_ID}:${REGION}:${DB_INSTANCE_NAME}' \\"
  echo "           | gcloud secrets create ${SECRET_DATABASE_URL} --data-file=-"
  echo "       or reset the password and store the new one the same way:"
  echo "         gcloud sql users set-password postgres --instance=${DB_INSTANCE_NAME} --prompt-for-password"
  exit 1
fi

# 5. Create Artifact Registry
echo "-> Checking Docker Artifact Registry..."
if ! gcloud artifacts repositories describe ${REPO_NAME} --location=${REGION} >/dev/null 2>&1; then
  echo "   Creating Artifact Registry Repository..."
  gcloud artifacts repositories create ${REPO_NAME} \
    --repository-format=docker \
    --location=${REGION}
fi

# 6. Build and Publish Docker Containers
# Two images only: the app (API + built frontend in one container) and the
# transcoder. The app image builds from the repo root so its Dockerfile can
# reach both frontend/ and backend/.
echo "-> Building and pushing container images..."
gcloud builds submit --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/transcoder:latest ./transcoder
gcloud builds submit --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/app:latest .

# --- Content screening ------------------------------------------------------
# Two services, two surfaces: Vertex (Gemini) screens the video inside the
# transcoder job, Model Armor screens the text the submitter typed. Both run as
# RUNTIME_SA, which the Cloud Run service and the job share.
MODEL_ARMOR_TEMPLATE="${MODEL_ARMOR_TEMPLATE:-vibetube-text}"

echo "-> Granting the runtime service account access to screening services..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/aiplatform.user" \
  --condition=None >/dev/null
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/modelarmor.user" \
  --condition=None >/dev/null

# Gemini reads the raw upload out of GCS itself, and it does that as the Vertex
# AI service agent rather than as RUNTIME_SA -- so the job having bucket access
# is not enough. Granted explicitly rather than relying on the service agent's
# default project roles, which on a freshly enabled project take a couple of
# minutes to propagate: until they do, every scan fails PERMISSION_DENIED and
# (fail-open being the default) the first uploads of a new deployment go
# through unscreened.
AIPLATFORM_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"
echo "   Granting the Vertex AI service agent read access to gs://${RAW_BUCKET}..."
gcloud storage buckets add-iam-policy-binding "gs://${RAW_BUCKET}" \
  --member="serviceAccount:${AIPLATFORM_SA}" \
  --role="roles/storage.objectViewer" >/dev/null

# Model Armor evaluates against a named template rather than inline settings,
# so one has to exist before the first call. Created only when missing --
# re-running must not overwrite thresholds an organiser has since tuned in the
# console.
#
# Non-fatal on purpose. The gcloud surface for Model Armor is newer than the
# rest of this script, and a flag that has since been renamed should not take
# the whole deploy down: screening degrades to fail-open (or fail-closed, if
# MODERATION_FAIL_CLOSED is set) and the message below says what to fix.
if gcloud model-armor templates describe "${MODEL_ARMOR_TEMPLATE}" \
     --location="${REGION}" >/dev/null 2>&1; then
  echo "   Model Armor template ${MODEL_ARMOR_TEMPLATE} already exists."
else
  echo "   Creating Model Armor template ${MODEL_ARMOR_TEMPLATE}..."
  if ! gcloud model-armor templates create "${MODEL_ARMOR_TEMPLATE}" \
        --location="${REGION}" \
        --rai-settings-filters='[{"filterType":"HATE_SPEECH","confidenceLevel":"MEDIUM_AND_ABOVE"},{"filterType":"HARASSMENT","confidenceLevel":"MEDIUM_AND_ABOVE"},{"filterType":"SEXUALLY_EXPLICIT","confidenceLevel":"MEDIUM_AND_ABOVE"},{"filterType":"DANGEROUS","confidenceLevel":"MEDIUM_AND_ABOVE"}]' \
        >/dev/null 2>&1; then
    echo "   WARNING: could not create the Model Armor template automatically."
    echo "            Text screening will be skipped until it exists. Create it"
    echo "            in the console (Security > Model Armor) or with:"
    echo "              gcloud model-armor templates create ${MODEL_ARMOR_TEMPLATE} --location=${REGION}"
  fi
fi

# 7. Deploy Cloud Run Transcoder Job
echo "-> Deploying/Updating Cloud Run Job: ${JOB_NAME}..."
# Run jobs replace if exists, else create
# Resources are set explicitly rather than left at Cloud Run's 512Mi/1-CPU
# default, which is not enough to transcode and silently loses renditions:
# ffmpeg is SIGKILLed mid-encode (exit -9) and the job still reports success,
# so a video quietly ships with 480p and 720p but no 1080p.
#
# Memory is the binding constraint because Cloud Run's filesystem is in-memory
# -- the downloaded original AND all three renditions sit in /tmp, counting
# against this limit alongside ffmpeg's own working set. Budget roughly:
#   MAX_UPLOAD_BYTES (input) + ~3x that (renditions) + ~0.5Gi for ffmpeg
# 2Gi leaves comfortable headroom over a 50 MB upload.
JOB_MEMORY="${JOB_MEMORY:-2Gi}"
# Transcoding is CPU-bound: nearly all wall time is libx264, and memory is a
# cliff (too little and ffmpeg is SIGKILLed) rather than a throughput limit --
# more memory buys nothing once it fits. x264 parallelises well, and Cloud Run
# bills per CPU-second, so 4 CPUs for half the time costs about the same as 2.
JOB_CPU="${JOB_CPU:-4}"
# Below TRANSCODE_STALE_MINUTES, so a wedged job is killed by Cloud Run before
# the stale sweep has to reclaim its slot.
#
# The content scan runs inside this budget and rides out 429s for up to five
# minutes (MODERATION_RETRY_ATTEMPTS x MODERATION_RETRY_INTERVAL), so a room
# hitting Vertex quota spends that before encoding starts. 20m still leaves
# room for a full ladder; raise both this and TRANSCODE_STALE_MINUTES together
# if the retry budget is raised.
JOB_TIMEOUT="${JOB_TIMEOUT:-20m}"

if gcloud run jobs describe ${JOB_NAME} --region=${REGION} >/dev/null 2>&1; then
  gcloud run jobs update ${JOB_NAME} \
    --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/transcoder:latest \
    --region=${REGION} \
    --memory=${JOB_MEMORY} \
    --cpu=${JOB_CPU} \
    --task-timeout=${JOB_TIMEOUT}
else
  gcloud run jobs create ${JOB_NAME} \
    --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/transcoder:latest \
    --region=${REGION} \
    --memory=${JOB_MEMORY} \
    --cpu=${JOB_CPU} \
    --task-timeout=${JOB_TIMEOUT}
fi

# 8. Deploy the app service. Deployed once to learn its own URL, then updated
# with it, since the transcoder needs a callback address to report back to.
#
# Access is granted immediately before the deploy so the first revision can
# read its secrets; IAM changes can take a moment to propagate, and a revision
# that starts without them crash-loops on a missing DATABASE_URL.
echo "-> Granting the runtime service account access to the secrets..."
for SECRET in "${SECRET_DATABASE_URL}" "${SECRET_TRANSCODER_TOKEN}" "${SECRET_ADMIN_TOKEN}"; do
  grant_secret_access "${SECRET}"
done

echo "-> Deploying Vibetube Cloud Run Service..."
gcloud run deploy ${SERVICE_NAME} \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/app:latest \
  --region=${REGION} \
  --allow-unauthenticated \
  --max-instances=${MAX_INSTANCES} \
  --concurrency=${CONCURRENCY} \
  --memory=${MEMORY} \
  --add-cloudsql-instances=${PROJECT_ID}:${REGION}:${DB_INSTANCE_NAME} \
  --service-account=${RUNTIME_SA} \
  --set-secrets="DATABASE_URL=${SECRET_DATABASE_URL}:latest,TRANSCODER_SECRET_TOKEN=${SECRET_TRANSCODER_TOKEN}:latest,ADMIN_TOKEN=${SECRET_ADMIN_TOKEN}:latest" \
  --set-env-vars="^|^RAW_VIDEOS_BUCKET=${RAW_BUCKET}|PUBLIC_STREAMS_BUCKET=${PUBLIC_BUCKET}|TRANSCODER_JOB_NAME=${JOB_NAME}|GCP_PROJECT=${PROJECT_ID}|GCP_LOCATION=${REGION}|DB_POOL_MAX=${DB_POOL_MAX}|MAX_UPLOAD_BYTES=${MAX_UPLOAD_BYTES}|MAX_CONCURRENT_TRANSCODES=${MAX_CONCURRENT_TRANSCODES}|MAX_UPLOADS_PER_EVENT=${MAX_UPLOADS_PER_EVENT}|MAX_CONCURRENT_VIEWERS=${MAX_CONCURRENT_VIEWERS}|PRESENCE_TTL_SECONDS=${PRESENCE_TTL_SECONDS}|TRANSCODE_STALE_MINUTES=${TRANSCODE_STALE_MINUTES}|FIREBASE_PROJECT_ID=${PROJECT_ID}|FIREBASE_API_KEY=${FIREBASE_API_KEY}|FIREBASE_AUTH_DOMAIN=${FIREBASE_AUTH_DOMAIN}|ADMIN_BOOTSTRAP_EMAILS=${ADMIN_BOOTSTRAP_EMAILS}|MODERATION_ENABLED=${MODERATION_ENABLED:-true}|MODERATION_MODEL=${MODERATION_MODEL:-gemini-2.5-flash}|MODERATION_FAIL_CLOSED=${MODERATION_FAIL_CLOSED:-false}|MODEL_ARMOR_TEMPLATE=${MODEL_ARMOR_TEMPLATE}"

# Get the service URL, which is now both the site and the API origin.
SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} --region=${REGION} --format="value(status.url)")
echo "   Service URL resolved: ${SERVICE_URL}"

# Hand the service its own URL so it can tell the transcoder where to call back.
echo "   Updating environment variables with service URL..."
gcloud run services update ${SERVICE_NAME} \
  --region=${REGION} \
  --update-env-vars="BACKEND_URL=${SERVICE_URL}"

# Firebase refuses a sign-in popup from any origin not on this list, so every
# Cloud Run host has to be added or the admin console fails with
# auth/unauthorized-domain.
#
# Cloud Run serves the service on more than one hostname -- the project-number
# form and the older hashed form -- and an operator may reach /admin by either,
# so all of them are authorised rather than just status.url. They come from the
# urls annotation; parsing them out of one URL string with sed is what broke
# this before (BSD sed has no \? in a basic regex, so the host came out as
# "https:").
#
# The API replaces authorizedDomains wholesale, so existing entries are read
# first and merged here.
echo "-> Authorising Cloud Run hosts for Firebase sign-in..."
SERVICE_URLS_JSON="$(gcloud run services describe ${SERVICE_NAME} --region=${REGION} \
  --format="value(metadata.annotations['run.googleapis.com/urls'])" 2>/dev/null || true)"

CURRENT_DOMAINS_JSON="$(fb_api GET \
  "https://identitytoolkit.googleapis.com/admin/v2/projects/${PROJECT_ID}/config" \
  | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin).get('authorizedDomains',[])))" 2>/dev/null || echo '[]')"

DOMAIN_RESULT="$(python3 - "${CURRENT_DOMAINS_JSON}" "${SERVICE_URLS_JSON}" "${SERVICE_URL}" <<'PYEOF'
import json, sys
from urllib.parse import urlparse

existing = json.loads(sys.argv[1] or "[]")
try:
    urls = json.loads(sys.argv[2] or "[]")
except json.JSONDecodeError:
    urls = []
urls.append(sys.argv[3])

added = []
for url in urls:
    host = urlparse(url).netloc or urlparse("https://" + url).netloc
    if host and host not in existing:
        existing.append(host)
        added.append(host)

print(json.dumps({"payload": {"authorizedDomains": existing}, "added": added}))
PYEOF
)"

DOMAINS_ADDED="$(printf '%s' "${DOMAIN_RESULT}" | python3 -c "import sys,json;print(' '.join(json.load(sys.stdin)['added']))")"
if [ -n "${DOMAINS_ADDED}" ]; then
  printf '%s' "${DOMAIN_RESULT}" \
    | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin)['payload']))" \
    | fb_api PATCH \
        "https://identitytoolkit.googleapis.com/admin/v2/projects/${PROJECT_ID}/config?updateMask=authorizedDomains" \
        --data-binary @- >/dev/null
  echo "   Added: ${DOMAINS_ADDED}"
else
  echo "   Already authorised."
fi

echo "==============================================="
echo "  Deployment completed successfully!"
echo "  Vibetube: ${SERVICE_URL}"
echo "  Admin:    ${SERVICE_URL}/admin"
echo ""

if [ "${GOOGLE_IDP_ENABLED:-0}" = "0" ]; then
  echo "  ONE MANUAL STEP REMAINS -- the admin console cannot sign in yet."
  echo "  Google sign-in needs an OAuth client, and creating one has no API."
  echo "  Enable it once, here (takes about 10 seconds):"
  echo ""
  echo "    https://console.firebase.google.com/project/${PROJECT_ID}/authentication/providers"
  echo "    -> Google -> Enable -> pick a support email -> Save"
  echo ""
  echo "  Admins allowed to sign in after that:"
  echo "    ${ADMIN_BOOTSTRAP_EMAILS}"
  echo "  Manage the rest in the console's Admins panel."
  echo ""
fi

echo "  Create an event before sharing a link. The DSN comes from Secret"
echo "  Manager, so no password is printed here or stored on disk:"
echo ""
echo "    cloud-sql-proxy ${PROJECT_ID}:${REGION}:${DB_INSTANCE_NAME} &"
echo "    cd backend"
echo "    export DATABASE_URL=\"\$(gcloud secrets versions access latest \\"
echo "        --secret=${SECRET_DATABASE_URL} --project=${PROJECT_ID} \\"
echo "      | sed 's#@/#@127.0.0.1:5432/#; s#?host=.*##')\""
echo "    python admin.py create-event --name 'My Event' --code DEMO"
echo "    -> share ${SERVICE_URL}/e/DEMO"
echo ""
echo "  Admin token, when you need it for the seeding endpoints:"
echo "    gcloud secrets versions access latest --secret=${SECRET_ADMIN_TOKEN} --project=${PROJECT_ID}"
echo "==============================================="

# Services left behind by earlier layouts and by the Vibeflix -> Vibetube
# rename. They keep running and serving stale code until removed, but deleting
# a service is not something this script should do on its own -- report it and
# let the operator decide.
for OLD_SERVICE in frontend-service backend-service vibeflix-service; do
  if gcloud run services describe ${OLD_SERVICE} --region=${REGION} >/dev/null 2>&1; then
    echo ""
    echo "NOTE: '${OLD_SERVICE}' is superseded by '${SERVICE_NAME}' and is no"
    echo "      longer used. Its URL will stop working once you remove it:"
    echo "        gcloud run services delete ${OLD_SERVICE} --region=${REGION}"
  fi
done
