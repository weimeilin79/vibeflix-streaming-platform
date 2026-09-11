import { useEffect, useState } from "react";
import {
  Home, FlaskConical, BookOpen, Menu, X, ExternalLink, Ticket, Award,
} from "lucide-react";
import { navigate } from "../lib/router";
import type { EventCredit } from "../lib/api";

/**
 * CHANGE ME: where the lab and resource links point.
 *
 * These are the only values in this file worth editing. `href` may be an
 * external URL (opens in a new tab) or an internal path starting with "/"
 * (routed in-app, no page reload). A `null` href renders the item disabled,
 * which is the honest state for a lab that has not been published yet.
 */
interface NavItem {
  label: string;
  href: string | null;
  icon: typeof Home;
  /** Grouped under a heading, with a divider above. */
  section?: string;
  /** Runs instead of navigating. Used by Credits, which opens a dialog. */
  onSelect?: () => void;
}

const NAV_ITEMS: NavItem[] = [
  { label: "Home", href: "/", icon: Home },

  // Public codelab URLs. The authoring host (codelabs.devsite.corp.google.com)
  // is reachable only inside Google's network, so linking it here would give
  // every external attendee a dead end.
  {
    label: "Lab 1",
    href: "https://codelabs.developers.google.com/codelabs/vibe-studio-lab/instructions#0",
    icon: FlaskConical,
    section: "Labs",
  },
  {
    label: "Lab 2",
    href: "https://codelabs.developers.google.com/vibetube-ads-agentic-data-engineering/instructions#0",
    icon: FlaskConical,
  },
  { label: "Lab 3", href: null, icon: FlaskConical },

  { label: "Resources", href: null, icon: BookOpen, section: "More" },
];

const isExternal = (href: string) => /^https?:\/\//i.test(href);

interface SidebarProps {
  /** Current showroom code, pinned at the bottom for orientation. */
  code: string;
  /** This room's credit links. Empty hides the Credits item entirely. */
  credits?: EventCredit[];
  /** Opens the credits dialog. */
  onOpenCredits?: () => void;
}

export const Sidebar = ({ code, credits = [], onOpenCredits }: SidebarProps) => {
  const [open, setOpen] = useState(false);

  // Credits sits directly above Lab 1 and only exists when the organiser has
  // set links for this room -- a nav entry that opens an empty dialog is worse
  // than no entry. It is deliberately outside the "Labs" group: it is not a
  // lab, so it carries the section heading that Lab 1 would otherwise start.
  const items: NavItem[] = credits.length
    ? [
        NAV_ITEMS[0],
        { label: "Credits", href: null, icon: Award, onSelect: onOpenCredits },
        { ...NAV_ITEMS[1], section: "Labs" },
        ...NAV_ITEMS.slice(2),
      ]
    : NAV_ITEMS;

  // Escape closes the mobile drawer; without it the only way out is the X,
  // which is easy to miss on a small screen.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  /**
   * @param stacked YouTube's narrow rail puts the label beneath the icon.
   *                The drawer has room for the conventional side-by-side form.
   */
  const renderItem = (item: NavItem, stacked: boolean) => {
    const Icon = item.icon;
    const base = stacked
      ? "group flex flex-col items-center justify-center gap-1.5 w-full py-3.5 px-1 rounded-xl transition-all duration-150"
      : "group flex items-center gap-4 w-full px-3 py-2.5 rounded-xl text-sm font-medium transition-all duration-150";
    const iconSize = stacked ? "w-6 h-6" : "w-5 h-5";
    const labelClass = stacked
      ? "text-[10px] font-medium leading-none text-center"
      : "truncate";

    if (item.onSelect) {
      return (
        <button
          onClick={() => {
            setOpen(false);
            item.onSelect?.();
          }}
          title={item.label}
          className={`${base} text-fg-muted hover:text-fg hover:bg-overlay cursor-pointer ${
            stacked ? "" : "text-left"
          }`}
        >
          <Icon className={`${iconSize} flex-shrink-0`} />
          <span className={labelClass}>{item.label}</span>
        </button>
      );
    }

    if (!item.href) {
      return (
        <span
          aria-disabled="true"
          title={`${item.label} — coming soon`}
          className={`${base} text-fg-muted/35 cursor-not-allowed`}
        >
          <Icon className={`${iconSize} flex-shrink-0`} />
          <span className={labelClass}>{item.label}</span>
          {!stacked && (
            <span className="ml-auto text-[9px] uppercase tracking-wider">soon</span>
          )}
        </span>
      );
    }

    const shared = `${base} text-fg-muted hover:text-fg hover:bg-overlay cursor-pointer`;

    if (isExternal(item.href)) {
      return (
        <a
          href={item.href}
          target="_blank"
          rel="noopener noreferrer"
          title={item.label}
          className={shared}
        >
          <Icon className={`${iconSize} flex-shrink-0`} />
          <span className={labelClass}>{item.label}</span>
          {!stacked && (
            <ExternalLink className="ml-auto w-3.5 h-3.5 opacity-0 group-hover:opacity-60 transition-opacity" />
          )}
        </a>
      );
    }

    return (
      <button
        onClick={() => {
          setOpen(false);
          navigate(item.href as string);
        }}
        title={item.label}
        className={`${shared} ${stacked ? "" : "text-left"}`}
      >
        <Icon className={`${iconSize} flex-shrink-0`} />
        <span className={labelClass}>{item.label}</span>
      </button>
    );
  };

  const panel = (stacked: boolean) => (
    <nav
      className={`flex flex-col h-full ${stacked ? "px-1.5 py-3 gap-0.5" : "px-3 py-6 gap-1"}`}
      aria-label="Main"
    >
      {items.map((item, index) => (
        <div key={`${item.label}-${index}`}>
          {item.section && (
            stacked ? (
              // A caps heading does not fit 72px; a rule alone carries the
              // grouping, exactly as YouTube does it.
              <div className="my-1.5 border-t border-hairline" />
            ) : (
              <div className="mt-4 mb-1 pt-4 border-t border-hairline">
                <p className="px-3 pb-1 text-[10px] uppercase font-bold tracking-[0.2em] text-fg-muted/60">
                  {item.section}
                </p>
              </div>
            )
          )}
          {renderItem(item, stacked)}
        </div>
      ))}

      <div className={`mt-auto border-t border-hairline ${stacked ? "pt-2" : "pt-4"}`}>
        {stacked ? (
          <div
            className="flex flex-col items-center gap-1 py-2 text-fg-muted"
            title={`Showroom ${code}`}
          >
            <Ticket className="w-4 h-4" />
            <span className="text-[9px] font-bold tracking-wider truncate max-w-full px-1 text-fg">
              {code}
            </span>
          </div>
        ) : (
          <div className="flex items-center gap-3 px-3 py-2 text-fg-muted">
            <Ticket className="w-4 h-4 flex-shrink-0" />
            <div className="min-w-0">
              <p className="text-[9px] uppercase tracking-[0.2em] font-bold text-fg-muted/60">
                Showroom
              </p>
              <p className="text-xs font-bold tracking-wider truncate text-fg">{code}</p>
            </div>
          </div>
        )}
      </div>
    </nav>
  );

  return (
    <>
      {/* Trigger lives in the top bar on desktop; on small screens it floats
          because there is no room for it beside the logo. */}
      <button
        onClick={() => setOpen(true)}
        aria-label="Open menu"
        className="lg:hidden p-2 rounded-full text-fg-muted hover:text-fg hover:bg-overlay transition-colors cursor-pointer"
      >
        <Menu className="w-5 h-5" />
      </button>

      {/* Desktop rail, tucked under the fixed top bar. */}
      <aside className="hidden lg:block fixed left-0 top-16 bottom-0 w-[72px] z-30 bg-stage/80 backdrop-blur-xl border-r border-hairline">
        {panel(true)}
      </aside>

      {/* Mobile drawer */}
      {open && (
        <div className="lg:hidden fixed inset-0 z-50 animate-fade-in">
          <div
            className="absolute inset-0 bg-black/70 backdrop-blur-sm"
            onClick={() => setOpen(false)}
          />
          <aside className="absolute left-0 top-0 bottom-0 w-64 bg-stage border-r border-hairline shadow-2xl">
            <button
              onClick={() => setOpen(false)}
              aria-label="Close menu"
              className="absolute top-5 right-3 p-2 rounded-full text-fg-muted hover:text-fg hover:bg-overlay transition-colors cursor-pointer"
            >
              <X className="w-5 h-5" />
            </button>
            {panel(false)}
          </aside>
        </div>
      )}
    </>
  );
};
