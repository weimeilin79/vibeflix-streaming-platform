import { useEffect } from "react";
import { Award, ExternalLink, X } from "lucide-react";
import type { EventCredit } from "../lib/api";

/**
 * The sponsor / attribution links behind the Credits item in a room's nav.
 *
 * Only reachable when the room has at least one credit, so this never renders
 * an empty state -- the nav item that opens it does not exist otherwise.
 *
 * Every link is external and opens in a new tab. The URLs are validated
 * server-side as http(s) before they are stored (see clean_credits in
 * backend/main.py); `rel="noopener noreferrer"` is the second half of that,
 * since a credit still points somewhere this site does not control.
 */
interface CreditsModalProps {
  credits: EventCredit[];
  eventName: string;
  onClose: () => void;
}

export const CreditsModal = ({ credits, eventName, onClose }: CreditsModalProps) => {
  // Escape closes, matching the other dialogs in the app.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-label="Credits"
    >
      <div
        className="absolute inset-0 bg-black/75 backdrop-blur-sm"
        onClick={onClose}
      />

      <div className="relative w-full max-w-md bg-card border border-hairline rounded-2xl shadow-2xl overflow-hidden">
        <div className="flex items-start gap-3 p-5 border-b border-hairline">
          <div className="p-2 rounded-xl bg-gradient-to-br from-vibe-blue/20 to-vibe-purple/20 border border-hairline">
            <Award className="w-5 h-5 text-vibe-purple" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-semibold text-fg leading-tight">Credits</h2>
            <p className="text-xs text-fg-muted mt-0.5 truncate">{eventName}</p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="p-1.5 -mr-1 -mt-1 rounded-full text-fg-muted hover:text-fg hover:bg-overlay transition-colors cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <ul className="p-2.5 max-h-[60vh] overflow-y-auto">
          {credits.map((credit, index) => (
            <li key={`${credit.url}-${index}`}>
              <a
                href={credit.url}
                target="_blank"
                rel="noopener noreferrer"
                className="group flex items-center gap-3 px-3 py-2.5 rounded-xl hover:bg-overlay transition-colors"
              >
                <span className="flex-1 min-w-0 text-sm font-medium text-fg truncate">
                  {credit.name}
                </span>
                <ExternalLink className="w-3.5 h-3.5 flex-shrink-0 text-fg-muted opacity-50 group-hover:opacity-100 transition-opacity" />
              </a>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
};
