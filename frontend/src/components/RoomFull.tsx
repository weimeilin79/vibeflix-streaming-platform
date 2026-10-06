import { Users, RefreshCw } from "lucide-react";
import { Ambience } from "./Ambience";
import { Footer } from "./Footer";

interface RoomFullProps {
  code: string;
  onRetry: () => void;
}

/**
 * Shown when a showroom is at its concurrent-viewer capacity.
 *
 * Deliberately offers a retry rather than auto-polling: a full room that
 * everyone's browser hammers on a timer is exactly the load the cap exists to
 * prevent.
 */
export const RoomFull = ({ code, onRetry }: RoomFullProps) => (
  <div className="min-h-screen stage-vignette relative bg-stage text-fg flex flex-col overflow-hidden">
    <Ambience />

    <div className="relative z-10 flex-1 flex flex-col items-center justify-center px-4 text-center max-w-sm mx-auto">
      <div className="relative mb-5 rise">
        <span className="absolute inset-0 rounded-full bg-vibe-purple/20 blur-xl animate-ping" />
        <div className="relative w-16 h-16 rounded-full bg-overlay border border-hairline flex items-center justify-center text-fg-muted">
          <Users className="w-8 h-8" />
        </div>
      </div>

      <h1
        className="font-display text-2xl md:text-3xl font-black tracking-tight text-fg mb-2 rise"
        style={{ animationDelay: "80ms" }}
      >
        This showroom is full
      </h1>

      <p className="text-sm text-fg-muted mb-1 rise" style={{ animationDelay: "140ms" }}>
        Too many people are watching
      </p>
      <p
        // Real case, deliberately: this screen exists to show the code that
        // did not work, and uppercasing it hid the very typo being reported.
        className="font-display font-bold tracking-[0.15em] text-vibe-purple mb-8 break-all rise"
        style={{ animationDelay: "180ms" }}
      >
        {code}
      </p>

      <button
        onClick={onRetry}
        className="flex items-center gap-2 px-5 py-3 rounded-2xl bg-card hover:bg-card-hover border border-hairline hover:border-vibe-purple/40 text-sm font-bold text-fg transition-all duration-200 hover:scale-[1.03] cursor-pointer rise"
        style={{ animationDelay: "240ms" }}
      >
        <RefreshCw className="w-4 h-4" />
        <span>Try again</span>
      </button>

      <p className="text-xs text-fg-muted/70 mt-4 rise" style={{ animationDelay: "300ms" }}>
        A seat frees up shortly after someone leaves.
      </p>
    </div>

    <Footer />
  </div>
);
