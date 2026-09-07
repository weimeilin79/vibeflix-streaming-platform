import { useCallback, useEffect, useState } from "react";
import {
  Plus, Trash2, Lock, RefreshCw, ChevronRight, Megaphone, Film,
  Sun, Moon, LogOut, ShieldAlert,
} from "lucide-react";
import { Logo } from "./Logo";
import {
  AdminEvent, AdminEntries, EventInput, AdminIdentity, NotAuthorizedError,
  listEvents, listEntries, createEvent, updateEvent, closeEvent, deleteEvent,
  deleteVideo, deleteAd, localInputToUtc, utcToLocalInput, fetchMe,
} from "../lib/admin";
import {
  SignedInUser, onAdminAuthChanged, signInWithGoogle, signOutAdmin,
  authConfigured,
} from "../lib/auth";
import { formatUploadTime } from "../lib/api";
import { AdminSignIn } from "./AdminSignIn";
import { AdminUsers } from "./AdminUsers";

/**
 * Unlinked admin console at /admin.
 *
 * Gated by Google sign-in plus the admin_users allowlist -- see auth.ts and
 * backend/auth.py. Nothing below the gate renders until the server has
 * confirmed the caller, so no admin markup reaches an unauthorised visitor.
 *
 * Still deliberately unlinked: nothing in the app points here. There
 * is intentionally no navigation to this page anywhere in the app.
 */
// Mirrors SANDBOX_EVENT_CODE in backend/database.py. The server refuses to
// delete it because init_db recreates it; the button is disabled to match, so
// the rule is visible before the click rather than after it.
const SANDBOX_CODE = "sandbox";

const EMPTY_FORM: EventInput & { code: string } = {
  name: "",
  code: "",
  uploadOpensAt: "",
  uploadClosesAt: "",
  adsClosesAt: "",
  seed: true,
};

interface AdminPageProps {
  theme: "dark" | "light";
  onToggleTheme: () => void;
}

export const AdminPage = ({ theme, onToggleTheme }: AdminPageProps) => {
  const [events, setEvents] = useState<AdminEvent[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [entries, setEntries] = useState<AdminEntries | null>(null);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Which destructive control is asking "are you sure?", keyed so only one
  // asks at a time. Every confirmation here is inline rather than a native
  // confirm(): Chrome lets a user tick "prevent this page from creating
  // additional dialogs" after a few prompts, and from then on confirm()
  // returns false without showing anything -- so deletes silently did nothing
  // and looked like a broken button.
  const [confirming, setConfirming] = useState<string | null>(null);

  // Runs `action` once the same control has been clicked twice.
  const confirmThen = (key: string, action: () => void) => {
    if (confirming === key) {
      setConfirming(null);
      action();
    } else {
      setConfirming(key);
    }
  };

  // --- Access ---------------------------------------------------------------
  // Two steps, and both have to pass before any admin data is fetched: Firebase
  // says who is signed in, then the server says whether that address is on the
  // allowlist. Fetching first and reacting to a 401 would flash the console at
  // someone who is not allowed to see it.
  const [gate, setGate] = useState<
    "loading" | "signedOut" | "denied" | "unconfigured" | "ready"
  >("loading");
  const [identity, setIdentity] = useState<AdminIdentity | null>(null);
  const [signedIn, setSignedIn] = useState<SignedInUser | null>(null);
  const [authMessage, setAuthMessage] = useState<string | null>(null);

  useEffect(() => {
    return onAdminAuthChanged(async (user) => {
      setSignedIn(user);
      if (!user) {
        setIdentity(null);
        // Distinguish "nobody signed in" from "this server cannot do sign-in".
        setGate((await authConfigured()) ? "signedOut" : "unconfigured");
        return;
      }
      try {
        setIdentity(await fetchMe());
        setAuthMessage(null);
        setGate("ready");
      } catch (err: any) {
        setIdentity(null);
        setAuthMessage(err.message);
        setGate(err instanceof NotAuthorizedError ? "denied" : "unconfigured");
      }
    });
  }, []);

  const handleSignIn = async () => {
    setBusy(true);
    setAuthMessage(null);
    try {
      await signInWithGoogle();
    } catch (err: any) {
      setAuthMessage(err.message);
    } finally {
      setBusy(false);
    }
  };

  const handleSignOut = async () => {
    await signOutAdmin();
    setAuthMessage(null);
  };

  const refreshEvents = useCallback(async () => {
    try {
      setEvents(await listEvents());
      setError(null);
    } catch (err: any) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    if (gate === "ready") refreshEvents();
  }, [gate, refreshEvents]);

  const openEvent = useCallback(async (code: string) => {
    setSelected(code);
    setEntries(null);
    try {
      setEntries(await listEntries(code));
    } catch (err: any) {
      setError(err.message);
    }
  }, []);

  // Every mutation funnels through here so one place owns the busy flag,
  // error surface, and the refresh that keeps counts honest afterwards.
  const run = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
      setNotice(success);
      await refreshEvents();
      if (selected) await openEvent(selected);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const submitForm = (e: React.FormEvent) => {
    e.preventDefault();
    const payload: EventInput = {
      name: form.name,
      code: form.code.trim() || undefined,
      uploadOpensAt: localInputToUtc(form.uploadOpensAt || ""),
      uploadClosesAt: localInputToUtc(form.uploadClosesAt || ""),
      adsClosesAt: localInputToUtc(form.adsClosesAt || ""),
      seed: form.seed,
    };
    if (editing) {
      run(() => updateEvent(editing, payload), `Updated ${editing}`).then(() => {
        setEditing(null);
        setForm({ ...EMPTY_FORM });
      });
    } else {
      run(() => createEvent(payload), "Event created").then(() =>
        setForm({ ...EMPTY_FORM })
      );
    }
  };

  const startEdit = (event: AdminEvent) => {
    setEditing(event.code);
    setForm({
      name: event.name,
      code: event.code,
      uploadOpensAt: utcToLocalInput(event.uploadOpensAt),
      uploadClosesAt: utcToLocalInput(event.uploadClosesAt),
      adsClosesAt: utcToLocalInput(event.adsClosesAt),
      seed: false,
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const field =
    "w-full bg-input border border-hairline rounded-xl px-3 py-2 text-sm text-fg " +
    "focus:outline-none focus:border-vibe-purple transition-colors";
  const label =
    "text-[10px] uppercase font-bold tracking-wider text-fg-muted mb-1 block";

  // Nothing below this point renders until the allowlist has confirmed the
  // caller, so no admin markup or data can appear for an unauthorised visitor.
  if (gate !== "ready" || !identity) {
    return (
      <AdminSignIn
        state={gate === "ready" ? "loading" : gate}
        theme={theme}
        deniedEmail={gate === "denied" ? signedIn?.email : null}
        message={authMessage}
        busy={busy}
        onSignIn={handleSignIn}
        onSignOut={handleSignOut}
      />
    );
  }

  return (
    <div className="min-h-screen bg-stage text-fg">
      <header className="border-b border-hairline px-5 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Logo theme={theme} className="w-[120px]" shine={false} />
          <span className="h-8 w-px bg-hairline" />
          <div>
            <h1 className="font-display text-xl font-black tracking-tight">
              Admin
            </h1>
            <p className="text-xs text-fg-muted">Events, videos and ads</p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {/* Who is acting. Deletions here are irreversible, so the account
              responsible stays visible rather than living in a menu. */}
          <div className="hidden sm:flex items-center gap-2 pr-1">
            {identity.picture ? (
              <img
                src={identity.picture}
                alt=""
                referrerPolicy="no-referrer"
                className="w-7 h-7 rounded-full border border-hairline"
              />
            ) : (
              <span className="w-7 h-7 rounded-full bg-overlay border border-hairline flex items-center justify-center text-[10px] font-bold text-fg-muted">
                {identity.email.slice(0, 2).toUpperCase()}
              </span>
            )}
            <span className="text-xs text-fg-muted max-w-[180px] truncate">
              {identity.email}
            </span>
          </div>
          <button
            onClick={handleSignOut}
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-card border border-hairline text-xs font-bold text-fg-muted hover:text-fg transition-colors cursor-pointer"
          >
            <LogOut className="w-4 h-4" />
            <span className="hidden sm:inline">Sign out</span>
          </button>
          <button
            onClick={onToggleTheme}
            aria-label="Toggle theme"
            className="p-2 rounded-xl bg-card border border-hairline text-fg-muted hover:text-fg transition-colors cursor-pointer"
          >
            {theme === "dark" ? (
              <Sun className="w-4 h-4 text-amber-400" />
            ) : (
              <Moon className="w-4 h-4 text-indigo-500" />
            )}
          </button>
          <button
            onClick={refreshEvents}
            className="flex items-center gap-2 px-3 py-2 rounded-xl bg-card border border-hairline text-xs font-bold text-fg-muted hover:text-fg transition-colors cursor-pointer"
          >
            <RefreshCw className="w-4 h-4" />
            Refresh
          </button>
        </div>
      </header>

      {(error || notice) && (
        <div
          className={`mx-5 mt-3 p-3 rounded-xl text-xs font-medium text-fg border ${
            error
              ? "bg-red-500/10 border-red-500/40"
              : "bg-emerald-500/10 border-emerald-500/40"
          }`}
        >
          {error || notice}
        </div>
      )}

      <div className="p-5 grid gap-5 lg:grid-cols-[380px_1fr] items-start">
        <div className="flex flex-col gap-5">
        {/* Create / edit */}
        <section className="rounded-2xl bg-card border border-hairline p-4 h-fit">
          <h2 className="text-sm font-bold mb-3">
            {editing ? `Edit ${editing}` : "New event"}
          </h2>
          <form onSubmit={submitForm} className="flex flex-col gap-3">
            <div>
              <label className={label}>Event name *</label>
              <input
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="Vibe Summit 2026"
                className={field}
              />
            </div>

            <div>
              <label className={label}>
                Event code {editing ? "(cannot change)" : "— blank to generate"}
              </label>
              <input
                value={form.code}
                disabled={!!editing}
                onChange={(e) => setForm({ ...form, code: e.target.value })}
                placeholder="SUMMIT"
                className={`${field} font-mono disabled:opacity-50`}
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={label}>Uploads start</label>
                <input
                  type="datetime-local"
                  value={form.uploadOpensAt || ""}
                  onChange={(e) => setForm({ ...form, uploadOpensAt: e.target.value })}
                  className={field}
                />
              </div>
              <div>
                <label className={label}>Uploads end</label>
                <input
                  type="datetime-local"
                  value={form.uploadClosesAt || ""}
                  onChange={(e) => setForm({ ...form, uploadClosesAt: e.target.value })}
                  className={field}
                />
              </div>
            </div>

            <div>
              <label className={label}>Ad submissions close</label>
              <input
                type="datetime-local"
                value={form.adsClosesAt || ""}
                onChange={(e) => setForm({ ...form, adsClosesAt: e.target.value })}
                className={field}
              />
              <p className="text-[10px] text-fg-muted/70 mt-1">
                Existing ads keep playing after this; it only stops changes.
              </p>
            </div>

            <p className="text-[10px] text-fg-muted/70">
              Times are entered in your local timezone and stored as UTC. Leave
              blank for no limit.
            </p>

            {!editing && (
              <label className="flex items-center gap-2 text-xs text-fg-muted">
                <input
                  type="checkbox"
                  checked={form.seed}
                  onChange={(e) => setForm({ ...form, seed: e.target.checked })}
                />
                Seed with the 8 sample videos
              </label>
            )}

            <div className="flex gap-2">
              <button
                type="submit"
                disabled={busy}
                className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl bg-gradient-to-r from-vibe-blue to-vibe-purple text-white text-sm font-bold disabled:opacity-50 cursor-pointer"
              >
                <Plus className="w-4 h-4" />
                {editing ? "Save changes" : "Create event"}
              </button>
              {editing && (
                <button
                  type="button"
                  onClick={() => {
                    setEditing(null);
                    setForm({ ...EMPTY_FORM });
                  }}
                  className="px-4 py-2.5 rounded-xl bg-overlay border border-hairline text-sm font-bold text-fg-muted hover:text-fg cursor-pointer"
                >
                  Cancel
                </button>
              )}
            </div>
          </form>
        </section>

        <AdminUsers
          currentEmail={identity.email}
          onError={setError}
          onNotice={setNotice}
        />
        </div>

        {/* Events + entries */}
        <section className="flex flex-col gap-5">
          <div className="rounded-2xl bg-card border border-hairline overflow-hidden">
            <h2 className="text-sm font-bold px-4 py-3 border-b border-hairline">
              Events ({events.length})
            </h2>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-[10px] uppercase tracking-wider text-fg-muted">
                  <tr className="border-b border-hairline">
                    <th className="text-left font-bold px-4 py-2">Code</th>
                    <th className="text-left font-bold px-4 py-2">Name</th>
                    <th className="text-left font-bold px-4 py-2">Uploads</th>
                    <th className="text-right font-bold px-4 py-2">Videos</th>
                    <th className="text-right font-bold px-4 py-2">Ads</th>
                    <th className="px-4 py-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((event) => (
                    <tr
                      key={event.code}
                      className={`border-b border-hairline last:border-0 hover:bg-overlay ${
                        selected === event.code ? "bg-overlay" : ""
                      }`}
                    >
                      <td className="px-4 py-2.5 font-mono text-xs font-bold">{event.code}</td>
                      <td className="px-4 py-2.5">{event.name}</td>
                      <td className="px-4 py-2.5">
                        <span
                          className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full ${
                            event.uploadOpen
                              ? "bg-emerald-500/20 text-emerald-600"
                              : "bg-fg-muted/15 text-fg-muted"
                          }`}
                        >
                          {event.uploadState}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 text-right tabular-nums">{event.videoCount}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums">{event.adCount}</td>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            onClick={() => startEdit(event)}
                            className="px-2.5 py-1.5 rounded-lg bg-overlay border border-hairline text-[11px] font-bold text-fg-muted hover:text-fg cursor-pointer"
                          >
                            Edit
                          </button>
                          <button
                            disabled={busy || !event.uploadOpen}
                            onClick={() =>
                              confirmThen(`close:${event.code}`, () =>
                                run(() => closeEvent(event.code), `${event.code} closed`)
                              )
                            }
                            title="Stop video uploads and ad changes"
                            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-overlay border border-hairline text-[11px] font-bold text-fg-muted hover:text-amber-600 disabled:opacity-40 cursor-pointer"
                          >
                            <Lock className="w-3 h-3" />
                            {confirming === `close:${event.code}` ? "Confirm?" : "Close"}
                          </button>
                          <button
                            onClick={() => openEvent(event.code)}
                            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-overlay border border-hairline text-[11px] font-bold text-fg-muted hover:text-fg cursor-pointer"
                          >
                            Entries
                            <ChevronRight className="w-3 h-3" />
                          </button>

                          {/* Two-step, and the second step names what goes with
                              it -- the counts are the whole point of pausing. */}
                          {confirming === `event:${event.code}` ? (
                            <>
                              <button
                                disabled={busy}
                                onClick={() =>
                                  run(
                                    () => deleteEvent(event.code),
                                    `Deleted ${event.code} and its ${event.videoCount} video(s)`
                                  ).then(() => {
                                    setConfirming(null);
                                    if (selected === event.code) {
                                      setSelected(null);
                                      setEntries(null);
                                    }
                                  })
                                }
                                className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-red-600 border border-red-600 text-[11px] font-bold text-white hover:bg-red-700 disabled:opacity-40 cursor-pointer"
                              >
                                <Trash2 className="w-3 h-3" />
                                Delete {event.videoCount}v / {event.adCount}a?
                              </button>
                              <button
                                onClick={() => setConfirming(null)}
                                className="px-2.5 py-1.5 rounded-lg bg-overlay border border-hairline text-[11px] font-bold text-fg-muted hover:text-fg cursor-pointer"
                              >
                                Cancel
                              </button>
                            </>
                          ) : (
                            <button
                              disabled={busy || event.code === SANDBOX_CODE}
                              onClick={() => setConfirming(`event:${event.code}`)}
                              title={
                                event.code === SANDBOX_CODE
                                  ? "The sandbox is recreated on restart, so it cannot be deleted"
                                  : `Delete ${event.code} and everything in it`
                              }
                              className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-overlay border border-hairline text-[11px] font-bold text-fg-muted hover:text-red-600 hover:border-red-600/40 disabled:opacity-30 disabled:hover:text-fg-muted disabled:hover:border-hairline cursor-pointer disabled:cursor-not-allowed"
                            >
                              <Trash2 className="w-3 h-3" />
                              Delete
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                  {!events.length && (
                    <tr>
                      <td colSpan={6} className="px-4 py-6 text-center text-xs text-fg-muted">
                        No events yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {selected && (
            <div className="rounded-2xl bg-card border border-hairline overflow-hidden">
              <h2 className="text-sm font-bold px-4 py-3 border-b border-hairline flex items-center gap-2">
                Entries in <span className="font-mono">{selected}</span>
              </h2>

              {!entries ? (
                <p className="px-4 py-6 text-xs text-fg-muted">Loading…</p>
              ) : (
                <div className="divide-y divide-hairline">
                  <div className="p-4">
                    <h3 className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-fg-muted mb-3">
                      <Film className="w-3.5 h-3.5" /> Videos ({entries.videos.length})
                    </h3>
                    <div className="flex flex-col gap-2">
                      {entries.videos.map((video) => {
                        // Deleting the video takes its ad with it, so say so
                        // before the click rather than after.
                        const hasAd = !!video.projectId &&
                          entries.ads.some((a) => a.projectId === video.projectId);
                        return (
                        <div
                          key={video.id}
                          className="flex items-center gap-3 p-2.5 rounded-xl bg-overlay border border-hairline"
                        >
                          <div className="min-w-0 flex-1">
                            <p className="text-sm font-medium truncate flex items-center gap-2">
                              <span className="truncate">{video.title}</span>
                              {hasAd && (
                                <span className="shrink-0 flex items-center gap-1 px-1.5 py-0.5 rounded bg-vibe-purple/15 border border-vibe-purple/40 text-[9px] font-bold uppercase tracking-wider text-fg">
                                  <Megaphone className="w-2.5 h-2.5" /> Ad
                                </span>
                              )}
                            </p>
                            <p className="text-[11px] text-fg-muted flex flex-wrap items-center gap-x-2">
                              <span className="font-mono">{video.projectId || "no project id"}</span>
                              <span>· {video.channelName}</span>
                              <span>· {video.status}</span>
                              <span>· {formatUploadTime(video.createdAt)}</span>
                              {/* Only rendered here. This endpoint is behind
                                  the admin allowlist; the public video list
                                  strips the address entirely. */}
                              {video.uploaderIp && (
                                <span className="font-mono">· {video.uploaderIp}</span>
                              )}
                            </p>
                            {video.status === "blocked" && (
                              <p className="mt-1 text-[11px] text-rose-300 flex items-start gap-1.5">
                                <ShieldAlert className="w-3 h-3 mt-0.5 shrink-0" />
                                <span>
                                  <span className="font-semibold uppercase tracking-wider">
                                    {video.moderationCategory || "blocked"}
                                  </span>
                                  {video.moderationReason ? ` — ${video.moderationReason}` : ""}
                                </span>
                              </p>
                            )}
                          </div>
                          <button
                            disabled={busy || !video.projectId}
                            title={
                              video.projectId
                                ? hasAd
                                  ? "Delete this video and its ad"
                                  : "Delete this video"
                                : "Seeded rows have no project id"
                            }
                            onClick={() =>
                              confirmThen(`video:${video.id}`, () =>
                                run(
                                  () => deleteVideo(selected, video.projectId as string),
                                  hasAd
                                    ? `Deleted video ${video.projectId} and its ad`
                                    : `Deleted video ${video.projectId}`
                                )
                              )
                            }
                            className={`shrink-0 flex items-center gap-1.5 p-2 rounded-lg border text-[11px] font-bold cursor-pointer transition-colors disabled:opacity-30 ${
                              confirming === `video:${video.id}`
                                ? "bg-red-600 border-red-600 text-white"
                                : "bg-red-500/10 border-red-500/40 text-red-600 hover:bg-red-500/20"
                            }`}
                          >
                            <Trash2 className="w-4 h-4" />
                            {confirming === `video:${video.id}` &&
                              (hasAd ? "Delete + its ad?" : "Delete?")}
                          </button>
                        </div>
                        );
                      })}
                      {!entries.videos.length && (
                        <p className="text-xs text-fg-muted">No videos.</p>
                      )}
                    </div>
                  </div>

                  <div className="p-4">
                    <h3 className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-fg-muted mb-3">
                      <Megaphone className="w-3.5 h-3.5" /> Ads ({entries.ads.length})
                    </h3>
                    <div className="flex flex-col gap-2">
                      {entries.ads.map((ad) => (
                        <div
                          key={ad.id}
                          className="flex items-center gap-3 p-2.5 rounded-xl bg-overlay border border-hairline"
                        >
                          {ad.imageUrl && (
                            <img
                              src={ad.imageUrl}
                              alt=""
                              className="w-14 h-9 rounded object-cover border border-hairline shrink-0"
                            />
                          )}
                          <div className="min-w-0 flex-1">
                            <p className="text-sm truncate">{ad.message}</p>
                            <p className="text-[11px] text-fg-muted">
                              <span className="font-mono">{ad.projectId}</span>
                              <span> · {ad.active ? "active" : "disabled"}</span>
                              <span> · {ad.imageUrl ? "image" : "text only"}</span>
                            </p>
                          </div>
                          <button
                            disabled={busy}
                            title="Delete this ad"
                            onClick={() =>
                              confirmThen(`ad:${ad.projectId}`, () =>
                                run(
                                  () => deleteAd(selected, ad.projectId),
                                  `Deleted ad ${ad.projectId}`
                                )
                              )
                            }
                            className={`shrink-0 flex items-center gap-1.5 p-2 rounded-lg border text-[11px] font-bold cursor-pointer transition-colors disabled:opacity-30 ${
                              confirming === `ad:${ad.projectId}`
                                ? "bg-red-600 border-red-600 text-white"
                                : "bg-red-500/10 border-red-500/40 text-red-600 hover:bg-red-500/20"
                            }`}
                          >
                            <Trash2 className="w-4 h-4" />
                            {confirming === `ad:${ad.projectId}` && "Delete?"}
                          </button>
                        </div>
                      ))}
                      {!entries.ads.length && (
                        <p className="text-xs text-fg-muted">No ads.</p>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  );
};
