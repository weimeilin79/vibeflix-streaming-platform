import React, { useEffect, useState } from "react";
import { X, User } from "lucide-react";
import { uploadVideo } from "../lib/api";
import { initialsOf } from "./VideoCard";

// Mirrors MAX_UPLOAD_BYTES / MAX_IMAGE_BYTES in backend/main.py. Kept in sync
// by hand: the server is the enforcing side, so drift here only affects when
// the message appears, never whether the limit holds.
const MAX_UPLOAD_MB = 50;
const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;
const MAX_IMAGE_MB = 5;
const MAX_IMAGE_BYTES = MAX_IMAGE_MB * 1024 * 1024;

const formatMb = (bytes: number) => `${(bytes / (1024 * 1024)).toFixed(1)} MB`;

/** Seconds to a display runtime: M:SS, or H:MM:SS past an hour. */
const formatDuration = (seconds: number): string => {
  const total = Math.round(seconds);
  if (!Number.isFinite(total) || total <= 0) return "";
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return hours ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
};

/**
 * Reads a video file's runtime in the browser, so nobody has to type it.
 *
 * Resolves to "" if the metadata cannot be read; the transcoder reports the
 * authoritative duration on completion and overwrites whatever is stored, so
 * a failure here only affects the few minutes before that callback arrives.
 */
const readVideoDuration = (file: File): Promise<string> =>
  new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const probe = document.createElement("video");
    const done = (value: string) => {
      URL.revokeObjectURL(url);
      resolve(value);
    };
    probe.preload = "metadata";
    probe.onloadedmetadata = () => done(formatDuration(probe.duration));
    probe.onerror = () => done("");
    // A file the browser cannot demux would otherwise never settle.
    setTimeout(() => done(""), 5000);
    probe.src = url;
  });

/** Object URL for a picked file, revoked when it changes or unmounts. */
const usePreviewUrl = (file: File | null): string | null => {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!file) {
      setUrl(null);
      return;
    }
    const objectUrl = URL.createObjectURL(file);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [file]);
  return url;
};

interface UploadModalProps {
  eventCode: string;
  onClose: () => void;
  onUploadSuccess: () => void;
}

export const UploadModal = ({ eventCode, onClose, onUploadSuccess }: UploadModalProps) => {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [projectId, setProjectId] = useState("");
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [thumbnailFile, setThumbnailFile] = useState<File | null>(null);
  // Read from the file, never typed. Blank until the probe resolves.
  const [duration, setDuration] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Object URLs must be revoked or each re-pick leaks one for the page's life.
  const avatarPreview = usePreviewUrl(avatarFile);
  const thumbnailPreview = usePreviewUrl(thumbnailFile);

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setVideoFile(file);
    setDuration("");
    // Auto-populate title if empty
    if (!title) {
      setTitle(file.name.replace(/\.[^/.]+$/, "")); // strip extension
    }
    setDuration(await readVideoDuration(file));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title || !videoFile) return;

    // The server enforces this too; catching it here keeps someone from
    // uploading a whole video before being told the field was needed.
    if (!projectId.trim()) {
      setError("A project ID is required.");
      return;
    }

    // Courtesy check only -- the server enforces the same ceiling and is the
    // authority. Catching it here saves the user uploading a file that is
    // going to be rejected after the whole transfer.
    if (videoFile.size > MAX_UPLOAD_BYTES) {
      setError(
        `That video is ${formatMb(videoFile.size)}. The limit is ${MAX_UPLOAD_MB} MB.`
      );
      return;
    }

    const oversizedImage = [
      { file: avatarFile, label: "profile picture" },
      { file: thumbnailFile, label: "thumbnail" },
    ].find((entry) => entry.file && entry.file.size > MAX_IMAGE_BYTES);
    if (oversizedImage?.file) {
      setError(
        `That ${oversizedImage.label} is ${formatMb(oversizedImage.file.size)}. ` +
        `The limit is ${MAX_IMAGE_MB} MB.`
      );
      return;
    }

    setLoading(true);
    setError(null);

    const formData = new FormData();
    formData.append("title", title);
    formData.append("description", description);
    formData.append("duration", duration);
    formData.append("displayName", displayName.trim() || "Anonymous Vibe");
    formData.append("projectId", projectId.trim());
    formData.append("videoFile", videoFile);
    // Omitted entirely when absent, so the server sees no field rather than
    // an empty one it has to special-case.
    if (avatarFile) formData.append("avatarFile", avatarFile);
    if (thumbnailFile) formData.append("thumbnailFile", thumbnailFile);

    try {
      // The server re-checks the upload window here; it can have closed
      // between this modal opening and the file finishing its transfer.
      await uploadVideo(eventCode, formData);
      onUploadSuccess();
      onClose();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 md:p-6 bg-black/80 backdrop-blur-md transition-opacity duration-300 animate-fade-in">
      {/* Click-away overlay */}
      <div className="absolute inset-0" onClick={onClose} />

      {/* Form Container */}
      {/* bg-card rather than a fixed near-black, for the same reason as the
          player: this panel sets `text-fg` on itself, which is #18181b in
          light mode -- near-black type on a near-black panel. */}
      <div className="relative w-full max-w-lg bg-card/95 border border-hairline rounded-2xl p-6 shadow-2xl z-10 text-fg">
        <button
          onClick={onClose}
          className="absolute top-4 right-4 text-fg-muted hover:text-fg cursor-pointer p-1.5 rounded-full hover:bg-overlay transition-colors"
          type="button"
          aria-label="Close upload modal"
        >
          <X className="w-5 h-5" />
        </button>

        <h2 className="text-xl font-bold mb-1 font-display bg-gradient-to-r from-vibe-blue to-vibe-purple bg-clip-text text-transparent">
          Publish a New Stream
        </h2>
        <p className="text-xs text-fg-muted mb-4">
          Posting to showroom <span className="font-bold tracking-wider">{eventCode}</span>
        </p>

        {error && (
          <p className="text-xs text-red-500 mb-4 bg-red-500/10 p-3 rounded-lg border border-red-500/20">
            {error}
          </p>
        )}

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <label className="text-[10px] uppercase font-bold tracking-wider text-fg-muted">
              Video File * <span className="text-fg-muted/60">— max {MAX_UPLOAD_MB} MB</span>
            </label>
            <input
              required
              type="file"
              accept="video/*"
              onChange={handleFileChange}
              className="bg-input border border-hairline rounded-xl px-4 py-2.5 text-sm text-fg focus:outline-none focus:border-vibe-purple transition-colors cursor-pointer"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-[10px] uppercase font-bold tracking-wider text-fg-muted">Title *</label>
            <input
              required
              placeholder="e.g. Synthwave Chill Drive"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="bg-input border border-hairline rounded-xl px-4 py-2.5 text-sm text-fg focus:outline-none focus:border-vibe-purple transition-colors"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-[10px] uppercase font-bold tracking-wider text-fg-muted">Your Name</label>
            <input
              placeholder="Shown on the card. Leave blank to stay anonymous."
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="bg-input border border-hairline rounded-xl px-4 py-2.5 text-sm text-fg focus:outline-none focus:border-vibe-purple transition-colors"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-[10px] uppercase font-bold tracking-wider text-fg-muted">
              Project ID *
            </label>
            <input
              required
              placeholder="e.g. team-rocket-01"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              className="bg-input border border-hairline rounded-xl px-4 py-2.5 text-sm font-mono text-fg focus:outline-none focus:border-vibe-purple transition-colors"
            />
            <p className="text-[10px] text-fg-muted/70">
              Re-using an ID <span className="font-semibold text-fg-muted">replaces</span> that
              project's video, keeping any link you already shared.
            </p>
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-[10px] uppercase font-bold tracking-wider text-fg-muted">
              Profile Picture <span className="text-fg-muted/60">— optional, max {MAX_IMAGE_MB} MB</span>
            </label>
            <div className="flex items-center gap-3">
              {avatarPreview ? (
                <img
                  src={avatarPreview}
                  alt="Profile preview"
                  className="w-11 h-11 rounded-full object-cover border border-hairline flex-shrink-0"
                />
              ) : (
                // Mirrors exactly what the card will show if none is supplied.
                <div className="w-11 h-11 rounded-full border border-hairline flex items-center justify-center bg-gradient-to-br from-vibe-blue/25 to-vibe-purple/25 text-fg text-xs font-bold flex-shrink-0">
                  {displayName.trim() ? initialsOf(displayName) : <User className="w-5 h-5 text-fg-muted" />}
                </div>
              )}
              <input
                type="file"
                accept="image/*"
                onChange={(e) => setAvatarFile(e.target.files?.[0] ?? null)}
                className="flex-1 min-w-0 bg-input border border-hairline rounded-xl px-3 py-2 text-xs text-fg focus:outline-none focus:border-vibe-purple transition-colors cursor-pointer"
              />
            </div>
            {!avatarFile && (
              <p className="text-[10px] text-fg-muted/70">
                Without one, your initials are shown instead.
              </p>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-[10px] uppercase font-bold tracking-wider text-fg-muted">
              Video Thumbnail <span className="text-fg-muted/60">— optional, max {MAX_IMAGE_MB} MB</span>
            </label>
            <div className="flex items-center gap-3">
              {thumbnailPreview ? (
                <img
                  src={thumbnailPreview}
                  alt="Thumbnail preview"
                  className="w-20 h-11 rounded-lg object-cover border border-hairline flex-shrink-0"
                />
              ) : (
                <div className="w-20 h-11 rounded-lg border border-hairline bg-stage flex items-center justify-center text-[9px] text-fg-muted flex-shrink-0">
                  auto
                </div>
              )}
              <input
                type="file"
                accept="image/*"
                onChange={(e) => setThumbnailFile(e.target.files?.[0] ?? null)}
                className="flex-1 min-w-0 bg-input border border-hairline rounded-xl px-3 py-2 text-xs text-fg focus:outline-none focus:border-vibe-purple transition-colors cursor-pointer"
              />
            </div>
            {!thumbnailFile && (
              <p className="text-[10px] text-fg-muted/70">
                Without one, a frame from the middle of your video is used.
              </p>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-[10px] uppercase font-bold tracking-wider text-fg-muted">Description</label>
            <textarea
              placeholder="Tell your viewers about the vibe..."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="bg-input border border-hairline rounded-xl px-4 py-2.5 text-sm text-fg focus:outline-none focus:border-vibe-purple h-24 resize-none transition-colors"
            />
          </div>

          {/* Duration is read from the file, not asked for. Shown only as
              confirmation that it was detected. */}
          {videoFile && duration && (
            <p className="text-[10px] text-fg-muted/70 -mt-1">
              Runtime detected: <span className="font-mono text-fg-muted">{duration}</span>
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full mt-2 py-3 rounded-xl bg-gradient-to-r from-vibe-blue to-vibe-purple hover:scale-[1.01] text-white text-sm font-bold shadow-lg shadow-vibe-blue/20 transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? "Publishing Vibe..." : "Publish Vibe"}
          </button>
        </form>
      </div>
    </div>
  );
};
