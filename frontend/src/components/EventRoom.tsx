import { useCallback, useEffect, useRef, useState } from "react";
import { Film, Sun, Moon, Plus, LogOut, Lock, Clock, LayoutGrid } from "lucide-react";
import { SearchBar } from "./SearchBar";
import { VideoCard, Video } from "./VideoCard";
import { VideoPlayerModal } from "./VideoPlayerModal";
import { UploadModal } from "./UploadModal";
import { NoShowroom } from "./NoShowroom";
import { Ambience } from "./Ambience";
import { Logo } from "./Logo";
import { Footer } from "./Footer";
import { Sidebar } from "./Sidebar";
import { CreditsModal } from "./CreditsModal";
import {
  EventNotFoundError, ShowroomFullError, VibeEvent, EventSummary,
  fetchEvent, fetchEventVideos, fetchEvents, formatWindowTime, sendPresence,
} from "../lib/api";
import { RoomFull } from "./RoomFull";
import { navigate, roomPath, readVideoParam, syncVideoParam } from "../lib/router";
import { rememberCode } from "./GatePage";

// How often to re-check videos that are still queued or transcoding.
const PROCESSING_POLL_MS = 5000;

// Seat renewal. Must be comfortably shorter than the server's
// PRESENCE_TTL_SECONDS (90s) or a viewer's seat expires between beats.
const PRESENCE_HEARTBEAT_MS = 30000;

interface EventRoomProps {
  code: string;
  theme: "dark" | "light";
  onToggleTheme: () => void;
}

export const EventRoom = ({ code, theme, onToggleTheme }: EventRoomProps) => {
  const [event, setEvent] = useState<VibeEvent | null>(null);
  const [videos, setVideos] = useState<Video[]>([]);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [roomFull, setRoomFull] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [selectedVideo, setSelectedVideo] = useState<Video | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [creditsOpen, setCreditsOpen] = useState(false);

  // A shared link carries ?v=<id>. Consumed once, after the first load
  // resolves, so arriving on a link opens that video rather than the grid.
  const pendingShareId = useRef<string | null>(readVideoParam());

  // Keep the address bar in step with the open video so the URL is always
  // copyable, without pushing history entries for modal state.
  const openVideo = useCallback((video: Video | null) => {
    setSelectedVideo(video);
    syncVideoParam(video ? video.id : null);
  }, []);

  // Avoids flipping the grid into its loading state on background polls.
  const refresh = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const [loadedEvent, loadedVideos] = await Promise.all([
        fetchEvent(code),
        fetchEventVideos(code),
      ]);
      setEvent(loadedEvent);
      setVideos(loadedVideos);
      setNotFound(false);
      setError(null);
      rememberCode(code);
    } catch (err: any) {
      if (err instanceof EventNotFoundError) {
        setNotFound(true);
      } else {
        setError(err.message);
      }
    } finally {
      if (!silent) setLoading(false);
    }
  }, [code]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Resolve a shared ?v= link once the videos exist. Cleared either way, so a
  // link to a deleted or still-processing video simply lands on the grid
  // instead of retrying on every poll.
  useEffect(() => {
    const shareId = pendingShareId.current;
    if (!shareId || !videos.length) return;
    pendingShareId.current = null;
    const match = videos.find((video) => video.id === shareId);
    if (match && (match.status ?? "ready") === "ready") {
      setSelectedVideo(match);
    } else {
      syncVideoParam(null);
    }
  }, [videos]);

  // Claim a seat on arrival, then renew it. Losing the seat mid-visit does not
  // eject anyone: the server never evicts an already-present viewer, so only
  // the initial claim can be refused.
  // Other showrooms, for the chip switcher. Fetched once per room; the list
  // changes far too rarely to justify polling it.
  const [otherEvents, setOtherEvents] = useState<EventSummary[]>([]);
  useEffect(() => {
    let cancelled = false;
    fetchEvents().then((all) => {
      if (!cancelled) setOtherEvents(all.filter((e) => e.code !== code));
    });
    return () => {
      cancelled = true;
    };
  }, [code]);

  const [presenceAttempt, setPresenceAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;

    const beat = async () => {
      try {
        await sendPresence(code);
        if (!cancelled) setRoomFull(false);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ShowroomFullError) setRoomFull(true);
        else if (err instanceof EventNotFoundError) setNotFound(true);
        // Any other failure is transient; the next beat retries.
      }
    };

    beat();
    const timer = setInterval(beat, PRESENCE_HEARTBEAT_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [code, presenceAttempt]);

  const hasProcessing = videos.some(
    (video) => video.status === "processing" || video.status === "pending"
  );

  // Poll only while something is transcoding, so an idle room is quiet.
  // Also refreshes the event, which is what closes the upload window live.
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  useEffect(() => {
    if (!hasProcessing) return;
    const timer = setInterval(() => refreshRef.current(true), PROCESSING_POLL_MS);
    return () => clearInterval(timer);
  }, [hasProcessing]);

  if (notFound) return <NoShowroom code={code} />;
  if (roomFull) {
    return <RoomFull code={code} onRetry={() => setPresenceAttempt((n) => n + 1)} />;
  }

  const filteredVideos = videos.filter((video) => {
    const query = searchQuery.toLowerCase().trim();
    if (!query) return true;
    return (
      video.title.toLowerCase().includes(query) ||
      video.description.toLowerCase().includes(query) ||
      video.channelName.toLowerCase().includes(query)
    );
  });

  const windowNotice = () => {
    if (!event || event.uploadOpen) return null;
    if (event.uploadState === "pending" && event.uploadOpensAt) {
      return {
        icon: <Clock className="w-3.5 h-3.5" />,
        text: `Uploads open ${formatWindowTime(event.uploadOpensAt)}`,
      };
    }
    return {
      icon: <Lock className="w-3.5 h-3.5" />,
      text: "Uploads closed",
    };
  };
  const notice = windowNotice();

  return (
    <div className="min-h-screen stage-vignette relative bg-stage text-fg flex flex-col transition-colors duration-300">
      <Ambience />

      {/* Top bar: logo left, search centred, actions right -- spanning the
          full width above the rail, as YouTube does. */}
      <header className="fixed top-0 inset-x-0 z-40 h-16 flex items-center gap-3 px-3 md:px-5 bg-stage/85 backdrop-blur-xl border-b border-hairline">
        <div className="flex items-center gap-2 flex-shrink-0">
          <Sidebar
            code={code}
            credits={event?.credits}
            onOpenCredits={() => setCreditsOpen(true)}
          />
          <button
            onClick={() => navigate("/")}
            aria-label="Vibetube home"
            className="cursor-pointer"
          >
            {/* The lockup is roughly 2:1, so width drives height -- anything
                above ~120px overflows the 64px bar and gets clipped. */}
            <Logo theme={theme} className="w-[96px] md:w-[112px]" shine={false} />
          </button>
        </div>

        <div className="flex-1 flex justify-center min-w-0 px-2">
          <div className="w-full max-w-xl">
            <SearchBar
              value={searchQuery}
              onChange={setSearchQuery}
              onClear={() => setSearchQuery("")}
              compact
            />
          </div>
        </div>

        <div className="flex items-center gap-1.5 flex-shrink-0">
          {/* Only when the organiser has set one. Opens in a new tab rather
              than navigating away: someone sent to the wall mid-showroom
              loses their place in the grid and their presence heartbeat. */}
          {event?.socialWallUrl && (
            <a
              href={event.socialWallUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 px-3 md:px-4 py-2 rounded-full bg-card hover:bg-card-hover border border-hairline text-fg-muted hover:text-fg transition-all duration-200 cursor-pointer"
              aria-label="Open the social wall in a new tab"
              title="Social wall"
            >
              <LayoutGrid className="w-5 h-5" />
              <span className="hidden md:inline text-sm font-bold">Social wall</span>
            </a>
          )}

          {event?.uploadOpen && (
            <button
              onClick={() => setUploadOpen(true)}
              className="flex items-center gap-2 px-3 md:px-4 py-2 rounded-full bg-card hover:bg-card-hover border border-hairline text-fg-muted hover:text-fg transition-all duration-200 cursor-pointer"
              aria-label="Publish new stream"
            >
              <Plus className="w-5 h-5" />
              <span className="hidden md:inline text-sm font-bold">Upload</span>
            </button>
          )}

          <button
            onClick={onToggleTheme}
            className="p-2.5 rounded-full hover:bg-overlay text-fg-muted hover:text-fg transition-all duration-200 cursor-pointer"
            aria-label="Toggle theme"
          >
            {theme === "dark" ? (
              <Sun className="w-5 h-5 text-amber-400" />
            ) : (
              <Moon className="w-5 h-5 text-indigo-500" />
            )}
          </button>

          <button
            onClick={() => navigate("/")}
            className="p-2.5 rounded-full hover:bg-overlay text-fg-muted hover:text-red-400 transition-all duration-200 cursor-pointer"
            aria-label="Leave showroom"
            title="Leave showroom"
          >
            <LogOut className="w-5 h-5" />
          </button>
        </div>
      </header>

      {/* pt-16 clears the fixed bar, lg:pl-[72px] the fixed rail. Padding
          rather than flex siblings keeps the footer full-width. */}
      <div className="relative z-10 flex-1 flex flex-col pt-16 lg:pl-[72px]">
        <div className="flex-1 flex flex-col px-4 md:px-6 py-5 w-full">
          {/* Context strip, in the position YouTube gives its filter chips.
              The current showroom is the solid chip; the rest switch rooms.
              Scrolls horizontally rather than wrapping once there are many. */}
          <div className="flex items-center gap-2 mb-6 rise overflow-x-auto pb-1 -mx-1 px-1">
            <span className="shrink-0 px-3 py-1.5 rounded-full bg-fg text-stage text-xs font-bold whitespace-nowrap">
              {event?.name || "Loading showroom…"}
            </span>
            <span className="shrink-0 px-3 py-1.5 rounded-full bg-overlay border border-hairline text-[10px] font-bold uppercase tracking-[0.2em] text-fg-muted">
              {code}
            </span>
            {notice && (
              <span className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-overlay border border-hairline text-[10px] font-bold uppercase tracking-wider text-fg-muted">
                {notice.icon}
                {notice.text}
              </span>
            )}

            {otherEvents.length > 0 && (
              <span className="shrink-0 w-px h-5 bg-hairline mx-1" aria-hidden="true" />
            )}
            {otherEvents.map((other) => (
              <button
                key={other.code}
                onClick={() => navigate(roomPath(other.code))}
                title={`Go to ${other.name}`}
                className="shrink-0 px-3 py-1.5 rounded-full bg-overlay border border-hairline text-xs font-medium text-fg-muted hover:text-fg hover:border-vibe-purple/40 transition-all duration-150 cursor-pointer whitespace-nowrap"
              >
                {other.name}
              </button>
            ))}
          </div>

        <main className="flex-1">
          {loading ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <div className="relative mb-5">
                <span className="absolute inset-0 rounded-full bg-vibe-blue/25 blur-2xl animate-ping" />
                <span className="absolute -inset-4 rounded-full border border-vibe-purple/25 animate-[spin_4s_linear_infinite]" />
                <Film className="relative w-12 h-12 text-vibe-blue flicker" />
              </div>
              <p className="text-sm text-fg-muted font-medium tracking-wide">
                Tuning into the vibe frequency...
              </p>
            </div>
          ) : error ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <div className="w-16 h-16 rounded-full bg-red-500/10 border border-red-500/20 flex items-center justify-center text-red-500 mb-4">
                <Film className="w-8 h-8" />
              </div>
              <h3 className="text-lg font-bold text-fg mb-1">Failed to connect to Vibetube</h3>
              <p className="text-sm text-red-400 max-w-xs">{error}</p>
            </div>
          ) : filteredVideos.length > 0 ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
              {filteredVideos.map((video, index) => (
                <VideoCard
                  key={video.id}
                  video={video}
                  index={index}
                  onClick={openVideo}
                />
              ))}
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <div className="w-16 h-16 rounded-full bg-overlay border border-hairline flex items-center justify-center text-fg-muted mb-4">
                <Film className="w-8 h-8" />
              </div>
              <h3 className="text-lg font-bold text-fg mb-1">
                {searchQuery ? "No streams matching that vibe" : "This showroom is empty"}
              </h3>
              <p className="text-sm text-fg-muted max-w-xs">
                {searchQuery
                  ? "Try searching for something else."
                  : "Be the first to publish a stream here."}
              </p>
            </div>
          )}
        </main>
        </div>

        <Footer />
      </div>

      {selectedVideo && (
        <VideoPlayerModal
          video={selectedVideo}
          eventCode={code}
          shareHashtag={event?.shareHashtag}
          onClose={() => openVideo(null)}
        />
      )}

      {uploadOpen && (
        <UploadModal
          eventCode={code}
          onClose={() => setUploadOpen(false)}
          onUploadSuccess={() => refresh(true)}
        />
      )}

      {creditsOpen && event?.credits?.length ? (
        <CreditsModal
          credits={event.credits}
          eventName={event.name}
          onClose={() => setCreditsOpen(false)}
        />
      ) : null}
    </div>
  );
};
