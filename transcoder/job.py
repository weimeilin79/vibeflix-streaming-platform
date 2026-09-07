import os
import sys
import time
import requests
from google.cloud import storage
from converter import (
    transcode_to_hls, generate_master_playlist, extract_thumbnail,
    get_video_duration, select_resolutions,
)
from moderation import scan_video


def format_duration(seconds: float) -> str:
    """Seconds to a display runtime: M:SS, or H:MM:SS past an hour.

    Returns an empty string for a non-positive duration, so a failed probe
    leaves the existing value alone rather than overwriting it with 0:00.
    """
    total = int(round(seconds or 0))
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def parse_gcs_uri(gcs_uri: str):
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI: {gcs_uri}")
    parts = gcs_uri[5:].split("/", 1)
    bucket_name = parts[0]
    blob_name = parts[1] if len(parts) > 1 else ""
    return bucket_name, blob_name

def upload_folder_to_gcs(local_dir: str, bucket_name: str, gcs_prefix: str):
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    
    custom_types = {
        ".m3u8": "application/x-mpegURL",
        ".ts": "video/MP2T",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".mp4": "video/mp4"
    }
    
    for root, _, files in os.walk(local_dir):
        for file in files:
            local_file_path = os.path.join(root, file)
            rel_path = os.path.relpath(local_file_path, local_dir)
            blob_name = os.path.join(gcs_prefix, rel_path)
            
            blob = bucket.blob(blob_name)
            
            ext = os.path.splitext(file)[1].lower()
            content_type = custom_types.get(ext)
            
            if content_type:
                blob.upload_from_filename(local_file_path, content_type=content_type)
            else:
                blob.upload_from_filename(local_file_path)

def report_failure(reason: str):
    """Tells the backend this job died so the video stops showing as processing.

    Best effort: if the callback itself fails there is nothing further to do,
    and raising here would only mask the original error.
    """
    backend_url = os.getenv("BACKEND_URL")
    video_id = os.getenv("VIDEO_ID")
    secret_token = os.getenv("TRANSCODER_SECRET_TOKEN")

    print(f"Transcode failed: {reason}")
    if not backend_url or not video_id:
        return

    headers = {"X-Transcoder-Token": secret_token} if secret_token else {}
    callback_url = f"{backend_url.rstrip('/')}/api/videos/{video_id}/transcode-failed"
    try:
        requests.post(callback_url, json={"error": reason}, headers=headers, timeout=30)
        print("Reported failure to backend.")
    except Exception as e:
        print(f"Could not report failure to backend: {e}")

def fail(reason: str):
    report_failure(reason)
    sys.exit(1)

def report_blocked(category: str, reason: str):
    """Tells the backend that screening rejected this video.

    Distinct from report_failure so the row lands on `blocked` rather than
    `failed`: the card wording differs, and re-uploading the same file would
    reach the same verdict, which is not true of a transcode failure.

    Exits non-zero if the callback cannot be delivered. A blocked video whose
    verdict never arrived would otherwise sit in `processing` until the stale
    sweep quietly marks it failed -- the right end state by luck, with no
    record of why.
    """
    backend_url = os.getenv("BACKEND_URL")
    video_id = os.getenv("VIDEO_ID")
    secret_token = os.getenv("TRANSCODER_SECRET_TOKEN")

    print(f"Video blocked by screening [{category}]: {reason}")
    if not backend_url or not video_id:
        sys.exit(1)

    headers = {"X-Transcoder-Token": secret_token} if secret_token else {}
    callback_url = f"{backend_url.rstrip('/')}/api/videos/{video_id}/moderation-blocked"
    try:
        res = requests.post(
            callback_url,
            json={"category": category, "reason": reason},
            headers=headers,
            timeout=30,
        )
        res.raise_for_status()
        print("Reported screening block to backend.")
    except Exception as e:
        print(f"Could not report block to backend: {e}")
        sys.exit(1)

    # Deliberately a clean exit: the job did its work and the verdict was
    # recorded. A non-zero status here would show every rejected upload as a
    # failed Cloud Run execution in the console.
    sys.exit(0)

def main():
    input_uri = os.getenv("INPUT_GCS_URI")
    output_dir_uri = os.getenv("OUTPUT_GCS_DIR")
    video_id = os.getenv("VIDEO_ID")
    backend_url = os.getenv("BACKEND_URL")
    secret_token = os.getenv("TRANSCODER_SECRET_TOKEN")

    if not input_uri or not output_dir_uri or not video_id:
        print("Missing required environment variables (INPUT_GCS_URI, OUTPUT_GCS_DIR, VIDEO_ID)")
        sys.exit(1)

    job_started = time.monotonic()

    def phase(label: str, since: float) -> float:
        """Logs how long a phase took and returns a fresh mark."""
        now = time.monotonic()
        print(f"[timing] {label}: {now - since:.1f}s (elapsed {now - job_started:.1f}s)")
        return now

    print(f"Starting transcode job for Video {video_id}")
    print(f"Input URI: {input_uri}")
    print(f"Output URI: {output_dir_uri}")
    
    # 1. Download raw file from GCS
    try:
        raw_bucket, raw_blob = parse_gcs_uri(input_uri)
        storage_client = storage.Client()
        bucket = storage_client.bucket(raw_bucket)
        blob = bucket.blob(raw_blob)
        
        local_input = "/tmp/input.mp4"
        mark = time.monotonic()
        blob.download_to_filename(local_input)
        print("Raw video downloaded successfully.")
        mark = phase("download", mark)
    except Exception as e:
        fail(f"Error downloading video: {e}")
        
    # 2. Screen the content before spending anything on it.
    #
    # Ordered ahead of transcoding on purpose. The scan reads the object from
    # GCS, so it needs nothing the encode produces, and running it first means
    # a rejected video costs no encode and -- the part that matters -- never
    # reaches the public bucket. Screening after the upload would publish the
    # file first and then race every viewer holding the URL.
    scan_started = time.monotonic()
    verdict = scan_video(input_uri)
    phase("content scan", scan_started)
    if not verdict["allowed"]:
        report_blocked(verdict["category"], verdict["reason"])
    if not verdict.get("scanned"):
        print("[moderation] this video was not screened; see the log above for why")

    # 3. Run transcoding locally in container
    local_output_dir = "/tmp/transcoded"
    os.makedirs(local_output_dir, exist_ok=True)
    
    # Only rungs at or below the source height. Upscaling adds no detail and
    # the largest rendition dominates encode time, so a 720p master skips the
    # 1080p rung and finishes in a fraction of the time.
    resolutions = select_resolutions(local_input)
    encode_started = time.monotonic()
    print(f"Encoding ladder for this source: {', '.join(resolutions)}")
    success_resolutions = []
    
    for res in resolutions:
        try:
            print(f"Processing resolution {res}...")
            transcode_to_hls(local_input, local_output_dir, res)
            success_resolutions.append(res)
        except Exception as e:
            print(f"Failed processing resolution {res}: {e}")
            
    if not success_resolutions:
        fail("All resolutions failed.")
        
    # Generate master playlist
    try:
        generate_master_playlist(local_output_dir, success_resolutions)
        print("Master playlist generated.")
    except Exception as e:
        fail(f"Failed to generate master playlist: {e}")
        
    # Extract thumbnail (using mid-point calculation built inside converter.py)
    try:
        print("Extracting thumbnail...")
        extract_thumbnail(local_input, local_output_dir)
        print("Thumbnail extracted.")
        phase("encode + thumbnail", encode_started)
    except Exception as e:
        fail(f"Failed to extract thumbnail: {e}")
        
    # 4. Upload outputs back to GCS
    try:
        out_bucket, out_prefix = parse_gcs_uri(output_dir_uri)
        upload_started = time.monotonic()
        upload_folder_to_gcs(local_output_dir, out_bucket, out_prefix)
        print("Transcoded files uploaded to GCS successfully.")
        phase("upload", upload_started)
    except Exception as e:
        fail(f"Failed to upload output to GCS: {e}")
        
    # 5. Notify backend
    if backend_url:
        callback_url = f"{backend_url.rstrip('/')}/api/videos/{video_id}/transcode-complete"
        # Standard GCS URL syntax
        video_url = f"https://storage.googleapis.com/{out_bucket}/{out_prefix.rstrip('/')}/master.m3u8"
        thumbnail_url = f"https://storage.googleapis.com/{out_bucket}/{out_prefix.rstrip('/')}/thumbnail.jpg"
        
        headers = {}
        if secret_token:
            headers["X-Transcoder-Token"] = secret_token
            
        payload = {
            "videoUrl": video_url,
            "thumbnailUrl": thumbnail_url,
            # ffprobe already read this to place the thumbnail; reporting it
            # means nobody has to type a runtime by hand, and the value shown
            # is the file's own rather than whatever a form was told.
            "duration": format_duration(get_video_duration(local_input)),
        }
        
        try:
            print(f"Sending callback to backend at {callback_url}")
            res = requests.post(callback_url, json=payload, headers=headers)
            res.raise_for_status()
            print("Backend callback successful.")
        except Exception as e:
            print(f"Failed to notify backend: {e}")
            sys.exit(1)
            
    print("Transcoding job completed successfully!")

if __name__ == "__main__":
    main()
