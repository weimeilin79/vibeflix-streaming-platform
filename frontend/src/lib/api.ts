import { Video } from "../components/VideoCard";

export interface VibeEvent {
  code: string;
  name: string;
  uploadOpensAt: string | null;
  uploadClosesAt: string | null;
  /** Deadline for submitting or replacing ads. Existing ads keep playing. */
  adsClosesAt: string | null;
  adSubmissionOpen: boolean;
  /** Resolved by the server -- never computed from the browser clock. */
  uploadOpen: boolean;
  uploadState: "open" | "pending" | "closed";
  reason: string | null;
  /**
   * Bare tag with no leading "#", normalised server-side. Empty when the room
   * has none, which is the default and means share text is unchanged.
   */
  shareHashtag: string;
  /**
   * External social-wall link for this room. Empty for most rooms, in which
   * case the top-bar button is not rendered at all.
   */
  socialWallUrl: string;
  /**
   * Sponsor / attribution links. Empty for most rooms, in which case the
   * Credits nav item is not rendered at all.
   */
  credits: EventCredit[];
}

export interface EventCredit {
  name: string;
  url: string;
}

/** Thrown when an event code does not resolve, so the caller can show No Showroom. */
export class EventNotFoundError extends Error {}

/** Thrown when a showroom is at its concurrent-viewer capacity. */
export class ShowroomFullError extends Error {}

const CLIENT_ID_KEY = "vibetube:clientId";

/**
 * A stable per-browser id, so one viewer refreshing or opening the player does
 * not consume extra seats. Falls back to a per-session value when storage is
 * unavailable (private browsing), which merely counts that viewer again on a
 * hard reload rather than breaking entry.
 */
let memoryClientId: string | null = null;

export const getClientId = (): string => {
  try {
    const existing = localStorage.getItem(CLIENT_ID_KEY);
    if (existing) return existing;
    const created = crypto.randomUUID();
    localStorage.setItem(CLIENT_ID_KEY, created);
    return created;
  } catch {
    if (!memoryClientId) memoryClientId = crypto.randomUUID();
    return memoryClientId;
  }
};

export interface EventSummary {
  code: string;
  name: string;
  uploadOpensAt?: string | null;
  uploadClosesAt?: string | null;
  createdAt?: string | null;
}

/**
 * The date an event "happens on": when uploads open, else when they close,
 * else when it was created.
 */
export const eventDate = (event: EventSummary): Date | null => {
  const raw = event.uploadOpensAt || event.uploadClosesAt || event.createdAt;
  if (!raw) return null;
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? null : date;
};

/**
 * Nearest to today first, past and future treated alike -- an event yesterday
 * is as relevant as one tomorrow. Same-day events tie and keep their relative
 * order, which is what makes the sort stable. Undated events sort last.
 */
export const byDateProximity = (a: EventSummary, b: EventSummary): number => {
  const now = Date.now();
  const distance = (event: EventSummary) => {
    const date = eventDate(event);
    return date ? Math.abs(date.getTime() - now) : Number.POSITIVE_INFINITY;
  };
  return distance(a) - distance(b);
};

/**
 * All showrooms, for the in-room switcher. Resolves to an empty list on
 * failure so the chip row simply shows only the current event rather than
 * breaking the page.
 */
export const fetchEvents = async (): Promise<EventSummary[]> => {
  try {
    const res = await fetch("/api/events");
    if (!res.ok) return [];
    return await res.json();
  } catch {
    return [];
  }
};

export interface Ad {
  id: string;
  projectId: string;
  message: string;
  imageUrl: string | null;
  durationSeconds: number;
}

/**
 * The pre-roll ad for a project, or null when there is nothing to show.
 *
 * Fetched when a video opens rather than carried on the video list, which is
 * polled by every viewer. Resolves to null on any failure: an ad system must
 * never be able to stop a video from playing.
 */
export const fetchAd = async (code: string, projectId?: string | null): Promise<Ad | null> => {
  if (!projectId) return null;
  try {
    const res = await fetch(
      `/api/events/${encodeURIComponent(code)}/ads/${encodeURIComponent(projectId)}`
    );
    if (!res.ok) return null;
    const body = await res.json();
    return body.ad ?? null;
  } catch {
    return null;
  }
};

export interface PresenceState {
  present: number;
  capacity: number;
}

/** Claims or renews a seat in a showroom. Throws ShowroomFullError at capacity. */
export const sendPresence = async (code: string): Promise<PresenceState> => {
  const res = await fetch(`/api/events/${encodeURIComponent(code)}/presence`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clientId: getClientId() }),
  });
  if (res.status === 404) {
    throw new EventNotFoundError(await detailOf(res, "No showroom found."));
  }
  if (res.status === 503) {
    throw new ShowroomFullError(await detailOf(res, "This showroom is full."));
  }
  if (!res.ok) throw new Error(await detailOf(res, "Could not join the showroom."));
  return res.json();
};

const detailOf = async (res: Response, fallback: string) => {
  const body = await res.json().catch(() => ({}));
  return body.detail || fallback;
};

export const fetchEvent = async (code: string): Promise<VibeEvent> => {
  const res = await fetch(`/api/events/${encodeURIComponent(code)}`);
  if (res.status === 404) {
    throw new EventNotFoundError(await detailOf(res, "No showroom found."));
  }
  if (!res.ok) {
    throw new Error(await detailOf(res, "Failed to load this showroom."));
  }
  return res.json();
};

export const fetchEventVideos = async (code: string): Promise<Video[]> => {
  const res = await fetch(`/api/events/${encodeURIComponent(code)}/videos`);
  if (res.status === 404) {
    throw new EventNotFoundError(await detailOf(res, "No showroom found."));
  }
  if (!res.ok) {
    throw new Error(await detailOf(res, "Failed to load videos."));
  }
  return res.json();
};

export const uploadVideo = async (code: string, formData: FormData): Promise<void> => {
  const res = await fetch(`/api/events/${encodeURIComponent(code)}/videos`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) {
    // A 403 here is the upload window having closed since the page loaded.
    throw new Error(await detailOf(res, "Failed to upload video."));
  }
};

/** Formats a stored UTC timestamp in the viewer's local timezone. */
/**
 * Absolute upload time, rendered in the viewer's own timezone.
 *
 * Timestamps are stored as ISO-8601 with an explicit +00:00 offset, so Date
 * parses them as UTC rather than as local wall-clock time. The year only
 * appears when it is not the current one, which keeps the common case short.
 */
export const formatUploadTime = (iso?: string | null): string => {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const thisYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    ...(thisYear ? {} : { year: "numeric" }),
    hour: "numeric",
    minute: "2-digit",
  });
};

export const formatWindowTime = (iso: string | null): string => {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
};
