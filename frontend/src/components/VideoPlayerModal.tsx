import { X, AlertTriangle, Play } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Video, initialsOf } from "./VideoCard";
import { ShareButtons } from "./ShareButtons";
import { Ad, fetchAd, formatUploadTime } from "../lib/api";
import { videoShareUrl } from "../lib/router";
import { AdOverlay } from "./AdOverlay";

interface VideoPlayerModalProps {
  video: Video;
  /** Showroom the video belongs to, needed to build its shareable link. */
  eventCode: string;
  /** The room's share hashtag, if the organiser set one. */
  shareHashtag?: string;
  onClose: () => void;
}

export const VideoPlayerModal = ({
  video,
  eventCode,
  shareHashtag,
  onClose
}: VideoPlayerModalProps) => {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [playbackFailed, setPlaybackFailed] = useState(false);

  // "resolving" until we know whether an ad exists, so playback never starts
  // underneath one. Any failure resolves to "playing" -- an ad must never be
  // able to prevent the video from running.
  const [adPhase, setAdPhase] = useState<"resolving" | "showing" | "playing">("resolving");
  const [ad, setAd] = useState<Ad | null>(null);

  useEffect(() => {
    let cancelled = false;
    setAd(null);
    setAdPhase("resolving");

    fetchAd(eventCode, video.projectId).then((found) => {
      if (cancelled) return;
      if (found) {
        setAd(found);
        setAdPhase("showing");
      } else {
        setAdPhase("playing");
      }
    });

    return () => {
      cancelled = true;
    };
  }, [eventCode, video.id, video.projectId]);

  const onAdFinished = useCallback(() => setAdPhase("playing"), []);

  // Browsers block unmuted autoplay without a user gesture, and a 10s ad can
  // outlive the activation from the click that opened the video. When that
  // happens the frame just sits there looking broken, so surface a real play
  // control instead of only logging the rejection.
  const [autoplayBlocked, setAutoplayBlocked] = useState(false);

  const attemptPlay = useCallback((element: HTMLVideoElement) => {
    element
      .play()
      .then(() => setAutoplayBlocked(false))
      .catch(() => setAutoplayBlocked(true));
  }, []);

  const startPlaybackManually = useCallback(() => {
    const element = videoRef.current;
    if (!element) return;
    // This runs inside a click handler, so the gesture requirement is met.
    element.play().then(() => setAutoplayBlocked(false)).catch(() => {});
  }, []);

  // Keyed on the source rather than the whole video object: the object
  // identity changes on every poll-driven refresh, which would otherwise
  // re-run load() and restart playback for no reason. Gated on adPhase so the
  // video does not start (and burn bandwidth) behind the ad.
  useEffect(() => {
    if (adPhase !== "playing") return;
    const videoElement = videoRef.current;
    if (!videoElement) return;

    setPlaybackFailed(false);
    let hlsInstance: any = null;

    // An undecodable file otherwise leaves a silent black rectangle with no
    // indication anything went wrong.
    const onError = () => setPlaybackFailed(true);
    const onPlaying = () => setAutoplayBlocked(false);
    videoElement.addEventListener("error", onError);
    videoElement.addEventListener("playing", onPlaying);

    if (video.videoUrl.endsWith(".m3u8")) {
      const Hls = (window as any).Hls;
      if (Hls && Hls.isSupported()) {
        hlsInstance = new Hls();
        hlsInstance.loadSource(video.videoUrl);
        hlsInstance.attachMedia(videoElement);
        // hls.js swallows media errors internally, so the element's own error
        // event never fires for a broken stream.
        hlsInstance.on(Hls.Events.ERROR, (_e: any, data: any) => {
          if (data?.fatal) setPlaybackFailed(true);
        });
        hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
          attemptPlay(videoElement);
        });
      } else if (videoElement.canPlayType("application/vnd.apple.mpegurl")) {
        // Native support (e.g. Safari)
        videoElement.src = video.videoUrl;
        videoElement.load();
        attemptPlay(videoElement);
      }
    } else {
      // Standard video format (e.g., MP4)
      videoElement.src = video.videoUrl;
      videoElement.load();
      attemptPlay(videoElement);
    }

    return () => {
      videoElement.removeEventListener("error", onError);
      videoElement.removeEventListener("playing", onPlaying);
      if (hlsInstance) {
        hlsInstance.destroy();
      }
    };
  }, [video.videoUrl, adPhase, attemptPlay]);

  // Lock body scroll when modal is open
  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "unset";
    };
  }, []);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 md:p-6 bg-black/80 backdrop-blur-md transition-opacity duration-300 animate-fade-in">
      {/* Click-away overlay */}
      <div className="absolute inset-0" onClick={onClose} />

      {/* Modal Container */}
      {/* bg-card, not a fixed near-black. The panel used to be #0d0d12 in both
          themes while everything written on it used `text-fg`, which in light
          mode is #18181b -- near-black type on a near-black panel. The video
          frame below stays black regardless, which is what actually matters
          for watching; the surrounding chrome follows the theme. */}
      <div className="relative w-full max-w-4xl bg-card/95 border border-hairline rounded-2xl overflow-hidden shadow-2xl z-10 flex flex-col max-h-[90vh]">
        {/* Close Button */}
        <button
          onClick={onClose}
          className="absolute top-4 right-4 z-20 p-2 rounded-full bg-stage/60 hover:bg-stage/85 text-fg-muted hover:text-fg border border-hairline cursor-pointer transition-colors duration-150"
        >
          <X className="w-5 h-5" />
        </button>

        {/* Video Player & Details Scrollable Area */}
        <div className="flex-1 flex flex-col min-h-0 overflow-y-auto scrollbar-thin">
          {/* Aspect-video Wrapper */}
          <div className="relative aspect-video w-full bg-black">
            <video
              ref={videoRef}
              controls
              autoPlay
              className="absolute inset-0 w-full h-full object-contain"
            />

            {/* Covers the frame while the pre-roll runs, so the player's own
                controls cannot be used to start the video early. */}
            {adPhase === "showing" && ad && (
              <AdOverlay ad={ad} onFinished={onAdFinished} />
            )}

            {/* Autoplay refused: a 10s ad can outlast the click that opened
                the video, leaving a frozen first frame. Give people something
                obvious to press. */}
            {adPhase === "playing" && autoplayBlocked && !playbackFailed && (
              <button
                onClick={startPlaybackManually}
                aria-label="Play video"
                className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-4 bg-black/55 backdrop-blur-[2px] cursor-pointer group"
              >
                <span className="relative flex items-center justify-center">
                  <span className="absolute inset-0 rounded-full bg-vibe-blue/40 blur-2xl animate-ping" />
                  <span className="relative flex items-center justify-center w-20 h-20 md:w-24 md:h-24 rounded-full bg-vibe-blue text-white shadow-2xl shadow-vibe-blue/40 transition-transform duration-200 group-hover:scale-110">
                    <Play className="w-9 h-9 md:w-11 md:h-11 fill-current ml-1" />
                  </span>
                </span>
                <span className="text-sm font-bold text-white/90">Tap to play</span>
              </button>
            )}

            {playbackFailed && adPhase === "playing" && (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/85 px-6 text-center">
                <AlertTriangle className="w-9 h-9 text-amber-400" />
                <p className="text-sm font-bold text-white">
                  This video can't be played
                </p>
                <p className="text-xs text-white/60 max-w-sm">
                  The file is missing or is not in a format your browser can
                  decode. Uploading it again usually fixes it.
                </p>
              </div>
            )}
          </div>

          {/* Video Metadata */}
          <div className="p-5 md:p-6 flex-1 flex flex-col">
            <h2 className="text-xl md:text-2xl font-bold text-fg mb-3">
              {video.title}
            </h2>

            <span className="text-xs text-fg-muted font-medium">
              Uploaded {formatUploadTime(video.createdAt)}
            </span>

            {/* Its own row: sharing competed with the timestamp for attention
                when both sat on one line. */}
            <div className="mt-4 mb-6 p-3 rounded-2xl bg-overlay border border-hairline">
              <ShareButtons
                url={videoShareUrl(eventCode, video.id)}
                title={video.title}
                authorName={video.channelName}
                seed={video.id}
                hashtag={shareHashtag}
                // A placeholder is an ad with no video of its own; a real
                // upload that also has an ad gets three captions from each
                // lab. `ad` is resolved before this renders, so the kind is
                // settled by the time the share panel is on screen.
                kind={
                  video.source === "placeholder"
                    ? "ad"
                    : ad
                      ? "both"
                      : "video"
                }
              />
            </div>

            <hr className="border-hairline mb-6" />

            {/* Channel Info */}
            <div className="flex items-start gap-4">
              {video.channelAvatar && video.channelAvatar !== "?" ? (
                <img
                  src={video.channelAvatar}
                  alt={video.channelName}
                  className="w-12 h-12 rounded-full object-cover border border-hairline"
                />
              ) : (
                <div
                  className="w-12 h-12 rounded-full border border-hairline flex items-center justify-center bg-gradient-to-br from-vibe-blue/25 to-vibe-purple/25 text-fg text-sm font-bold shrink-0 select-none"
                  title={video.channelName}
                >
                  {initialsOf(video.channelName)}
                </div>
              )}
              <div className="min-w-0 flex-1">
                <h4 className="font-bold text-fg text-sm mb-1">
                  {video.channelName}
                </h4>
                {video.projectId && (
                  <p className="text-xs text-fg-muted font-medium font-mono">
                    Project {video.projectId}
                  </p>
                )}
                
                {/* Description */}
                <p className="text-sm text-fg/90 mt-4 leading-relaxed whitespace-pre-line bg-overlay p-4 rounded-xl border border-hairline">
                  {video.description}
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
