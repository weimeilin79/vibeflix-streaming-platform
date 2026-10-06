import { useEffect, useRef, useState } from "react";
import { ArrowRight, Ticket, Sun, Moon, CalendarDays } from "lucide-react";
import { navigate, roomPath } from "../lib/router";
import { EventSummary, fetchEvents, byDateProximity, eventDate } from "../lib/api";
import { Ambience } from "./Ambience";
import { Logo } from "./Logo";
import { Footer } from "./Footer";

const LAST_CODE_KEY = "vibetube:lastEventCode";

export const rememberCode = (code: string) => {
  try {
    localStorage.setItem(LAST_CODE_KEY, code);
  } catch {
    // Private browsing modes reject writes; the gate still works without it.
  }
};

export const recallCode = (): string => {
  try {
    return localStorage.getItem(LAST_CODE_KEY) || "";
  } catch {
    return "";
  }
};

interface GatePageProps {
  theme: "dark" | "light";
  onToggleTheme: () => void;
}

/** Date shown on an event card. Omits the year when it is the current one. */
const formatEventDate = (event: EventSummary): string => {
  const date = eventDate(event);
  if (!date) return "";
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
};

const isToday = (event: EventSummary): boolean => {
  const date = eventDate(event);
  if (!date) return false;
  const now = new Date();
  return date.toDateString() === now.toDateString();
};

// Must stay just under the .logo-launch duration so the room mounts while the
// mark is still blowing out, rather than after an empty beat.
const LAUNCH_MS = 420;

export const GatePage = ({ theme, onToggleTheme }: GatePageProps) => {
  const [code, setCode] = useState("");
  const [launching, setLaunching] = useState(false);
  const lastCode = recallCode();
  const launchTimer = useRef<number | undefined>(undefined);

  // Nearest to today first, so whatever is running now leads the list.
  const [events, setEvents] = useState<EventSummary[]>([]);
  useEffect(() => {
    let cancelled = false;
    fetchEvents().then((all) => {
      if (!cancelled) setEvents([...all].sort(byDateProximity));
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Navigating away mid-launch would otherwise leave the timer to fire against
  // an unmounted component.
  useEffect(() => () => window.clearTimeout(launchTimer.current), []);

  const enter = (value: string) => {
    const trimmed = value.trim();
    if (!trimmed || launching) return;
    rememberCode(trimmed);

    const path = roomPath(trimmed);
    const reducedMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)"
    ).matches;

    if (reducedMotion) {
      navigate(path);
      return;
    }

    setLaunching(true);
    launchTimer.current = window.setTimeout(() => navigate(path), LAUNCH_MS);
  };

  return (
    <div className="min-h-screen stage-vignette relative bg-stage text-fg flex flex-col overflow-hidden">
      <Ambience beam />

      <button
        onClick={onToggleTheme}
        aria-label="Toggle theme"
        className="absolute top-5 right-5 z-20 p-2.5 rounded-full bg-card/80 backdrop-blur border border-hairline text-fg-muted hover:text-fg shadow-lg transition-all duration-200 cursor-pointer"
      >
        {theme === "dark" ? (
          <Sun className="w-5 h-5 text-amber-400" />
        ) : (
          <Moon className="w-5 h-5 text-indigo-500" />
        )}
      </button>

      {/* flex-1 centres the gate in whatever space the footer leaves. */}
      <div className="relative z-10 flex-1 flex flex-col items-center justify-center px-4 w-full max-w-md mx-auto text-center">
        {/* One animation per layer: the outer span owns the entrance/exit, the
            inner one the glow pulse, and the logo wrapper the idle drift.
            Stacking them on a single element would let only one `animation`
            declaration survive the cascade. */}
        <h1 className="mb-4">
          <span
            className={`block ${launching ? "logo-launch" : "rise"}`}
            style={launching ? undefined : { animationDelay: "60ms" }}
          >
            <span className="block holo-glow">
              <Logo
                theme={theme}
                className={`w-[300px] md:w-[360px] ${launching ? "" : "logo-float"}`}
              />
            </span>
          </span>
        </h1>

        <div
          className={`w-full flex flex-col items-center ${launching ? "gate-recede" : ""}`}
        >
          <p
            className="text-sm md:text-base text-fg-muted font-medium mb-2 rise"
            style={{ animationDelay: "140ms" }}
          >
            Enter your event code to step into the showroom.
          </p>

          <div
            className="flex items-center gap-3 mb-10 rise"
            style={{ animationDelay: "200ms" }}
          >
            <span className="h-px w-10 bg-gradient-to-r from-transparent to-vibe-blue/50" />
            <span className="text-[10px] uppercase tracking-[0.35em] text-fg-muted/70 font-bold">
              Now Showing
            </span>
            <span className="h-px w-10 bg-gradient-to-l from-transparent to-vibe-purple/50" />
          </div>

          <form
          onSubmit={(e) => {
            e.preventDefault();
            enter(code);
          }}
          className="w-full flex flex-col gap-3"
        >
          <div
            className="relative code-field rise"
            style={{ animationDelay: "260ms" }}
          >
            <div className="code-field__halo" />
            <Ticket className="absolute left-4 top-1/2 -translate-y-1/2 w-5 h-5 text-fg-muted pointer-events-none z-10" />
            <input
              autoFocus
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="Event code"
              aria-label="Event code"
              // No `uppercase` here. Codes are case-sensitive on the server, and the
              // class only transformed what was DISPLAYED -- the value submitted
              // stayed as typed. Someone entering "googlenyc" saw "GOOGLENYC",
              // submitted "googlenyc", and got a 404 they could not see the cause
              // of. The field now shows exactly what will be sent.
              className="relative w-full bg-input border border-hairline rounded-2xl pl-12 pr-4 py-4 text-center text-lg font-display font-bold tracking-[0.2em] text-fg placeholder:text-fg-muted/50 placeholder:tracking-[0.2em] focus:outline-none focus:border-transparent transition-all duration-200"
            />
          </div>

          <button
            type="submit"
            disabled={!code.trim()}
            className="btn-shimmer rise w-full flex items-center justify-center gap-2 py-4 rounded-2xl bg-gradient-to-r from-vibe-blue via-vibe-purple to-vibe-blue bg-[length:200%_100%] hover:bg-[position:100%_0] text-white font-bold shadow-lg shadow-vibe-purple/25 hover:shadow-xl hover:shadow-vibe-purple/40 hover:scale-[1.015] active:scale-[0.99] transition-all duration-300 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:scale-100 disabled:shadow-none"
            style={{ animationDelay: "320ms" }}
          >
            <span className="relative z-10">Enter Showroom</span>
            <ArrowRight className="relative z-10 w-4 h-4 transition-transform duration-300 group-hover:translate-x-1" />
            </button>
          </form>

          {lastCode && (
            <button
              onClick={() => enter(lastCode)}
              className="mt-6 text-xs text-fg-muted hover:text-fg transition-colors cursor-pointer rise"
              style={{ animationDelay: "400ms" }}
            >
              Return to{" "}
              <span className="font-bold tracking-wider text-vibe-purple">{lastCode}</span>
            </button>
          )}

          {/* Every showroom as a tag, nearest date first. Kept to one compact
              row of chips rather than a card list, so the code field stays the
              thing the page is about. */}
          {events.length > 0 && (
            <div className="w-full mt-8 rise" style={{ animationDelay: "460ms" }}>
              <p className="text-[10px] uppercase tracking-[0.3em] text-fg-muted/70 font-bold mb-3">
                Or jump straight in
              </p>

              <div className="flex flex-wrap items-center justify-center gap-2">
                {events.map((event) => (
                  <button
                    key={event.code}
                    onClick={() => enter(event.code)}
                    title={`${event.name} — ${event.code}`}
                    className={`group flex items-center gap-2 pl-3 pr-3 py-1.5 rounded-full bg-card/70 backdrop-blur border transition-all duration-200 cursor-pointer hover:bg-card-hover hover:scale-[1.03] ${
                      isToday(event)
                        ? "border-vibe-blue/45"
                        : "border-hairline hover:border-vibe-purple/45"
                    }`}
                  >
                    {isToday(event) ? (
                      <span className="w-1.5 h-1.5 rounded-full bg-vibe-blue animate-pulse" />
                    ) : (
                      <CalendarDays className="w-3 h-3 text-fg-muted" />
                    )}

                    <span className="text-xs font-bold text-fg">{event.name}</span>

                    {formatEventDate(event) && (
                      <span className="text-[10px] text-fg-muted">
                        {formatEventDate(event)}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      <Footer />
    </div>
  );
};
