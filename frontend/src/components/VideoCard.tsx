import { Play, Loader2, AlertTriangle, Clock, ShieldAlert } from "lucide-react";
import { useTilt } from "../lib/useTilt";
import { formatUploadTime } from "../lib/api";

export type VideoStatus =
  | "pending"
  | "processing"
  | "ready"
  | "failed"
  /** Rejected by content screening. Never transcoded, never published. */
  | "blocked";

export interface Video {
  id: string;
  title: string;
  description: string;
  thumbnailUrl: string;
  videoUrl: string;
  duration: string;
  /** ISO-8601 UTC. The real upload time; there is no view counting. */
  createdAt?: string;
  /** Submitter's project identifier. Unique within a showroom. */
  projectId?: string | null;
  channelName: string;
  channelAvatar: string;
  eventId?: string;
  status?: VideoStatus;
}

/**
 * Up to two initials from a display name, for the avatar fallback.
 * Falls back to "?" for names that are punctuation or empty.
 */
export const initialsOf = (name: string): string => {
  const words = (name || "").trim().split(/\s+/).filter(Boolean);
  const letters = words
    .map((word) => [...word].find((ch) => /\p{L}|\p{N}/u.test(ch)) ?? "")
    .filter(Boolean);
  if (!letters.length) return "?";
  return (letters[0] + (letters[1] ?? "")).toUpperCase();
};

interface VideoCardProps {
  video: Video;
  onClick?: (video: Video) => void;
  /** Position in the grid, used to stagger the entrance animation. */
  index?: number;
}

export const VideoCard = ({ video, onClick, index = 0 }: VideoCardProps) => {
  // Rows without a status predate the column and are watchable.
  const status = video.status ?? "ready";
  const isQueued = status === "pending";
  const isProcessing = status === "processing";
  const isFailed = status === "failed";
  const isBlocked = status === "blocked";
  const isPlayable = !isQueued && !isProcessing && !isFailed && !isBlocked;

  const tilt = useTilt<HTMLDivElement>();

  return (
    <div
      ref={tilt.ref}
      onPointerMove={isPlayable ? tilt.onPointerMove : undefined}
      onPointerLeave={isPlayable ? tilt.onPointerLeave : undefined}
      onClick={() => isPlayable && onClick?.(video)}
      aria-disabled={!isPlayable}
      // Cap the stagger so a large showroom does not leave the last cards
      // waiting seconds to appear.
      style={{ animationDelay: `${Math.min(index, 11) * 55}ms` }}
      className={`group rise relative flex flex-col bg-card rounded-2xl overflow-hidden border border-hairline shadow-lg ${
        isPlayable ? "tilt hover:bg-card-hover cursor-pointer" : "cursor-default"
      }`}
    >
      {isPlayable && <div className="tilt__rim" />}
      {isPlayable && <div className="tilt__sheen" />}

      {/* Thumbnail section */}
      <div className="relative aspect-video w-full overflow-hidden bg-black/40 flex items-center justify-center">
        {video.thumbnailUrl && video.thumbnailUrl !== "?" ? (
          <img
            src={video.thumbnailUrl}
            alt={video.title}
            className={`w-full h-full object-cover transition-transform duration-500 ${
              isPlayable ? "group-hover:scale-105" : "opacity-40"
            }`}
            loading="lazy"
          />
        ) : isPlayable ? (
          // Playable but no poster frame yet. A bare "?" here read as a broken
          // card; say what is actually happening instead. Thumbnails are cut
          // by the transcoder, so this is what a card looks like until that
          // job reports back (and stays this way locally, where nothing
          // transcodes).
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 thumb-shimmer">
            <Loader2 className="w-6 h-6 text-fg-muted animate-spin" />
            <span className="text-[11px] font-bold uppercase tracking-wider text-fg-muted">
              Preparing preview
            </span>
            <span className="text-[10px] text-fg-muted/70 font-medium px-6 text-center">
              The video is watchable now.
            </span>
          </div>
        ) : null}

        {isQueued && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2.5 bg-black/50 backdrop-blur-[2px]">
            <Clock className="w-7 h-7 text-vibe-purple" />
            <span className="text-xs font-bold uppercase tracking-wider text-white/90">
              Queued
            </span>
            <span className="text-[10px] text-white/60 font-medium px-6 text-center">
              Waiting for a free slot. This starts automatically.
            </span>
          </div>
        )}

        {isProcessing && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2.5 bg-black/50 backdrop-blur-[2px]">
            <Loader2 className="w-7 h-7 text-vibe-purple animate-spin" />
            <span className="text-xs font-bold uppercase tracking-wider text-white/90">
              Processing
            </span>
            <span className="text-[10px] text-white/60 font-medium px-6 text-center">
              Building HD streams. This appears automatically when ready.
            </span>
          </div>
        )}

        {isFailed && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/60 backdrop-blur-[2px]">
            <AlertTriangle className="w-7 h-7 text-amber-400" />
            <span className="text-xs font-bold uppercase tracking-wider text-white/90">
              Processing failed
            </span>
            <span className="text-[10px] text-white/60 font-medium px-6 text-center">
              This video could not be converted. Try uploading it again.
            </span>
          </div>
        )}

        {/* Screening rejected this submission. The card stays in the grid --
            a submission that silently disappears reads as a bug to whoever
            uploaded it, and the room can see something was held back. What it
            deliberately does not say is what was in it, or who sent it: the
            category, the reason, and the uploader's address are in the admin
            console and nowhere a viewer can reach. */}
        {isBlocked && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/70 backdrop-blur-[3px]">
            <ShieldAlert className="w-7 h-7 text-rose-400" />
            <span className="text-xs font-bold uppercase tracking-wider text-white/90">
              Under review
            </span>
            <span className="text-[10px] text-white/60 font-medium px-6 text-center">
              This submission was held back by content screening.
            </span>
          </div>
        )}

        {/* Hover overlay with Play button */}
        {isPlayable && (
          <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 flex items-center justify-center transition-opacity duration-300">
            <div className="p-3.5 bg-vibe-blue rounded-full text-white shadow-lg shadow-vibe-blue/30 transform scale-75 group-hover:scale-100 transition-transform duration-300">
              <Play className="w-6 h-6 fill-current" />
            </div>
          </div>
        )}

        {/* Video Duration Badge */}
        {isPlayable && (
          <span className="absolute bottom-3 right-3 px-2 py-0.5 bg-black/75 backdrop-blur-sm text-xs font-semibold text-white tracking-wide rounded-md border border-hairline">
            {video.duration}
          </span>
        )}
      </div>

      {/* Info Section */}
      <div className="flex gap-3 p-4 flex-1">
        {/* Channel Avatar */}
        <div className="flex-shrink-0">
          {video.channelAvatar && video.channelAvatar !== "?" ? (
            <img
              src={video.channelAvatar}
              alt={video.channelName}
              className="w-10 h-10 rounded-full object-cover border border-hairline"
            />
          ) : (
            // No uploaded picture: initials, so every card still reads as
            // someone's rather than a row of identical placeholder icons.
            <div
              className="w-10 h-10 rounded-full border border-hairline flex items-center justify-center bg-gradient-to-br from-vibe-blue/25 to-vibe-purple/25 text-fg text-xs font-bold tracking-wide select-none"
              title={video.channelName}
            >
              {initialsOf(video.channelName)}
            </div>
          )}
        </div>

        {/* Title / Channel / Stats */}
        <div className="flex flex-col min-w-0 flex-1">
          <h3 className={`text-sm font-semibold leading-snug text-fg line-clamp-2 transition-colors duration-200 ${isPlayable ? "group-hover:text-vibe-blue" : ""}`} title={video.title}>
            {video.title}
          </h3>

          <p className="text-xs text-fg-muted mt-1.5 font-medium truncate">
            {video.channelName}
          </p>

          <div className="flex items-center flex-wrap text-xs text-fg-muted mt-1 font-medium gap-x-1.5 gap-y-1">
            <span>{formatUploadTime(video.createdAt)}</span>
            {video.projectId && (
              <>
                <span className="w-1 h-1 bg-fg-muted/40 rounded-full" />
                <span
                  className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-overlay border border-hairline truncate max-w-[10rem]"
                  title={video.projectId}
                >
                  {video.projectId}
                </span>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
