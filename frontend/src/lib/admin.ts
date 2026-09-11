/**
 * Admin API client.
 *
 * Every request carries the signed-in operator's Firebase ID token. The server
 * verifies it and checks the resulting email against the `admin_users`
 * allowlist, so a token alone is not authorisation -- see auth.ts.
 */
import { VibeEvent } from "./api";
import { getIdToken } from "./auth";

export interface AdminEvent extends VibeEvent {
  videoCount: number;
  adCount: number;
}

export interface AdminVideo {
  id: string;
  title: string;
  projectId: string | null;
  channelName: string;
  status: string;
  source: string;
  createdAt: string;
  thumbnailUrl: string;
  /**
   * Admin-only fields. The public /api/events/{code}/videos response strips
   * these; only the allowlisted entries endpoint returns them.
   */
  uploaderIp?: string | null;
  moderationCategory?: string | null;
  moderationReason?: string | null;
}

export interface AdminAd {
  id: string;
  projectId: string;
  message: string;
  imageUrl: string | null;
  active: boolean;
  updatedAt: string;
}

export interface AdminEntries {
  videos: AdminVideo[];
  ads: AdminAd[];
}

const detail = async (res: Response, fallback: string) => {
  const body = await res.json().catch(() => ({}));
  // FastAPI validation errors arrive as an array of objects, not a string.
  if (Array.isArray(body.detail)) {
    return body.detail.map((d: any) => d.msg).join(", ") || fallback;
  }
  return body.detail || fallback;
};

/** Thrown on 401/403 so the console can drop back to the sign-in screen. */
export class NotAuthorizedError extends Error {}

const request = async <T>(url: string, init?: RequestInit): Promise<T> => {
  const token = await getIdToken();
  const headers: Record<string, string> = {};
  if (init?.body) headers["Content-Type"] = "application/json";
  // Omitted rather than sent empty when signed out, so the server's "sign in"
  // response is about a missing credential and not a malformed one.
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(url, { ...init, headers });
  if (res.status === 401 || res.status === 403) {
    throw new NotAuthorizedError(await detail(res, "You are not authorised."));
  }
  if (!res.ok) throw new Error(await detail(res, `Request failed (${res.status})`));
  return res.status === 204 ? (undefined as T) : res.json();
};

export interface AdminIdentity {
  email: string;
  name: string;
  picture: string;
}

export interface AdminUser {
  email: string;
  addedAt: string | null;
  addedBy: string | null;
  active: boolean;
}

/** Confirms the signed-in account is on the allowlist. Throws NotAuthorizedError if not. */
export const fetchMe = () => request<AdminIdentity>("/api/admin/me");

export const listAdminUsers = () => request<AdminUser[]>("/api/admin/users");

export const addAdminUser = (email: string) =>
  request<{ email: string; outcome: string }>("/api/admin/users", {
    method: "POST",
    body: JSON.stringify({ email }),
  });

export const removeAdminUser = (email: string) =>
  request<{ deleted: number }>(`/api/admin/users/${encodeURIComponent(email)}`, {
    method: "DELETE",
  });

export const listEvents = () => request<AdminEvent[]>("/api/admin/events");

export const listEntries = (code: string) =>
  request<AdminEntries>(`/api/admin/events/${encodeURIComponent(code)}/entries`);

export interface EventCreditInput {
  name: string;
  url: string;
}

export interface EventInput {
  name: string;
  code?: string;
  uploadOpensAt?: string | null;
  uploadClosesAt?: string | null;
  adsClosesAt?: string | null;
  seed?: boolean;
  /** Bare tag; a leading "#" is stripped server-side. */
  shareHashtag?: string | null;
  /** A missing scheme is filled in as https:// server-side. */
  socialWallUrl?: string | null;
  /**
   * Omitting this leaves stored credits untouched; sending [] clears them.
   * The form always sends an array, so an edit is always authoritative.
   */
  credits?: EventCreditInput[];
}

export const createEvent = (input: EventInput) =>
  request<AdminEvent>("/api/admin/events", {
    method: "POST",
    body: JSON.stringify(input),
  });

export const updateEvent = (code: string, input: EventInput) =>
  request<AdminEvent>(`/api/admin/events/${encodeURIComponent(code)}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  });

export interface DeletedEvent {
  deleted: string;
  videos: number;
  ads: number;
  /** Storage is not deleted with the row; shown so it can be cleaned up. */
  mediaPrefix: string;
}

export const deleteEvent = (code: string) =>
  request<DeletedEvent>(`/api/admin/events/${encodeURIComponent(code)}`, {
    method: "DELETE",
  });

export const closeEvent = (code: string) =>
  request<AdminEvent>(`/api/admin/events/${encodeURIComponent(code)}/close`, {
    method: "POST",
  });

export const deleteVideo = (code: string, projectId: string) =>
  request<{ deleted: number }>(
    `/api/admin/events/${encodeURIComponent(code)}/videos/${encodeURIComponent(projectId)}`,
    { method: "DELETE" }
  );

export const deleteAd = (code: string, projectId: string) =>
  request<{ deleted: number }>(
    `/api/admin/events/${encodeURIComponent(code)}/ads/${encodeURIComponent(projectId)}`,
    { method: "DELETE" }
  );

/**
 * <input type="datetime-local"> gives a naive local string; the API stores
 * UTC. These convert in both directions so an operator types the wall-clock
 * time they mean and sees it back unchanged.
 */
export const localInputToUtc = (value: string): string | null => {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
};

export const utcToLocalInput = (iso?: string | null): string => {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
};
