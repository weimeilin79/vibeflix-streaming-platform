import { CSSProperties, useEffect, useRef, useState } from "react";
import { Link2, Check, Linkedin, Copy } from "lucide-react";

/**
 * Share targets for a single video.
 *
 * The link points at the showroom with the video preselected, so a recipient
 * lands on that video rather than the grid. Copy is offered alongside the
 * social targets because it is the one that works everywhere -- group chats,
 * email, and Slack included.
 */
interface ShareButtonsProps {
  url: string;
  title: string;
  /** Uploader's display name, used to write the post in their voice. */
  authorName?: string;
  /** Video id. Seeds the variant so it matches the server-rendered card. */
  seed?: string;
  /**
   * The room's hashtag, bare and without a leading "#". Appended to the X post
   * and the copied text. Empty for most rooms.
   */
  hashtag?: string;
  /**
   * What was submitted: an ad standing on a placeholder, a real video, or a
   * video that also has an ad. Decides which captions are offered.
   */
  kind?: SubmissionKind;
}

const ANONYMOUS_NAMES = new Set(["", "anonymous vibe", "anonymous"]);

/** Stands in for an uploader who gave no name, so no card reads "Anonymous Vibe". */
const SOMEONE = "Someone";

/**
 * Captions for a submission that is an ad with no video of its own -- the
 * placeholder case. Written around Lab 2, Vibetube Ads: Agentic Data
 * Engineering: an ADK agent that reads BigQuery auction telemetry, drafts a
 * bidding policy, and runs an actor-critic loop until a champion falls out.
 */
const AD_BLURBS = [
  `🤖 I stopped hand-tuning if/else rules and let an agent write the bidding policy instead. "{title}".`,
  `📊 I pointed an ADK agent at BigQuery telemetry and let it argue with itself until the bids got smarter. "{title}".`,
  `🔁 Actor, critic, repeat. I ran the flywheel until a champion policy fell out — "{title}".`,
  `🧪 I let adk eval score the candidates and kept the policy that won. "{title}".`,
  // The last two are third person, for anyone who would rather the post read
  // as a write-up than as a boast. Deliberately name-free: the person posting
  // is the person who made it, and a caption that names them reads oddly in
  // their own feed.
  `⚡ Brittle heuristics, retired. An Agentic Data Engineer queried, drafted, evaluated and went again — "{title}".`,
  `💸 Win the impression, do not overpay for it. An agent learned the difference — "{title}".`,
];

/**
 * Captions for a submission with an actual video. Written around Lab 1, the
 * Vibe Studio Lab: a long-running agent that drafts, pauses for a human
 * judgment, and teaches the channel to make the next one better.
 */
const VIDEO_BLURBS = [
  `🎬 I opened a studio, pressed one button, and let the agent wait well. Out came "{title}".`,
  `🪞 One prompt became a whole production line. I made "{title}" in the Vibe Studio lab.`,
  `🚀 I taught a channel to make the next video better than I would have. First up: "{title}".`,
  `🎧 Human in the loop, agent on the tools. I built "{title}" with Gemini and Google Cloud.`,
  // Third person, name-free, as above.
  `✨ The agent drafted, a human judged, the channel learned. "{title}" is what came back.`,
  `🗺️ A real agent graph, grown out of one prompt and published with three clicks of judgment — "{title}".`,
];

/** What a submission actually is, which decides which captions it gets. */
export type SubmissionKind = "ad" | "video" | "both";

/**
 * Stable index from a seed.
 *
 * Deterministic rather than random so the caption list does not reshuffle
 * between the moment someone reads it and the moment they post.
 */
const pick = (seed: string, count: number): number => {
  let h = 0;
  for (let i = 0; i < seed.length; i += 1) {
    h = (Math.imul(h, 31) + seed.charCodeAt(i)) >>> 0;
  }
  return h % count;
};

/** A stable window of `n` entries, rotated by the seed. */
const rotate = (list: string[], n: number, seed: string): string[] => {
  const start = pick(seed, list.length);
  return Array.from({ length: Math.min(n, list.length) },
                    (_, i) => list[(start + i) % list.length]);
};

/**
 * Every caption this submission could be posted with.
 *
 * Which set depends on what was actually submitted -- an ad for a project with
 * no video gets Lab 2's data-engineering lines, a real video gets Lab 1's
 * studio lines, and a project with both gets three of each. Rotated by seed
 * rather than shuffled, so the list is stable while someone reads it instead
 * of reshuffling under them on every render.
 */
const buildCaptions = (
  title: string, authorName?: string, kind: SubmissionKind = "video", seed = "",
): string[] => {
  const raw = (authorName ?? "").trim();
  const name = ANONYMOUS_NAMES.has(raw.toLowerCase()) ? SOMEONE : raw;
  const key = seed || title;

  let variants: string[];
  if (kind === "ad") variants = AD_BLURBS;
  else if (kind === "video") variants = VIDEO_BLURBS;
  else variants = [...rotate(VIDEO_BLURBS, 3, key), ...rotate(AD_BLURBS, 3, key)];

  return variants.map((v) => v.split("{name}").join(name).split("{title}").join(title));
};

/** Which caption is preselected. Deterministic, so it does not jump around. */
const defaultCaptionIndex = (title: string, seed: string | undefined, count: number): number =>
  pick(seed || title, count);

/**
 * CHANGE ME: hashtags added to every post, after the room's own.
 *
 * Deliberately short. A wall of tags reads as spam, and LinkedIn actively
 * demotes posts that look like one. Set to [] to post only the room's tag.
 */
const STANDARD_HASHTAGS = ["GoogleCloud", "Gemini", "Vibetube"];

/**
 * The room's tags first, then the standard ones, deduplicated.
 *
 * A room's value is a ready-to-post string that already carries each token's
 * sigil -- "#DevFestNYC @googlecloud" -- normalised server-side by
 * clean_hashtag. It is split rather than prefixed here: re-adding "#" would
 * produce "##DevFestNYC" and would turn an @mention into "#@googlecloud".
 *
 * STANDARD_HASHTAGS are bare words, so those do get a "#".
 */
const buildHashtags = (roomHashtag?: string): string[] => {
  const roomTags = (roomHashtag ?? "")
    .split(/[\s,]+/)
    .filter(Boolean)
    .map((t) => (t.startsWith("@") || t.startsWith("#") ? t : `#${t}`));
  const standard = STANDARD_HASHTAGS.map((t) => `#${t}`);

  // Dedupe on sigil + lowercased body: "#gemini" and "@gemini" are different
  // things, but "#Gemini" and "#gemini" are not.
  const seen = new Set<string>();
  return [...roomTags, ...standard].filter((tag) => {
    const key = tag.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
};

const hashtagLine = (roomHashtag?: string): string =>
  buildHashtags(roomHashtag).join(" ");

/** The pre-filled post text for X, built from the chosen caption. */
const buildShareText = (caption: string, hashtag?: string): string =>
  `${caption}\n\nWatch it on Vibetube 👇\n${hashtagLine(hashtag)}`;

/**
 * The whole post, ready to paste.
 *
 * This exists because LinkedIn cannot be pre-filled: it dropped support for
 * title/summary parameters and builds its card from the page's own metadata,
 * so the only way to get someone's chosen words into their post is to hand
 * them the text. The URL is included, since a pasted post with no link is not
 * much of a share.
 */
const buildFullPost = (caption: string, url: string, hashtag?: string): string =>
  `${caption}\n\n${url}\n\n${hashtagLine(hashtag)}`;

const COPIED_RESET_MS = 2000;

/** X's own mark; lucide still ships the pre-rebrand bird. */
const XIcon = ({ className }: { className?: string }) => (
  <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true">
    <path d="M18.9 1.15h3.68l-8.04 9.19L24 22.85h-7.41l-5.8-7.58-6.64 7.58H.46l8.6-9.83L0 1.15h7.59l5.24 6.93zm-1.29 19.5h2.04L6.49 3.24H4.3z" />
  </svg>
);

export const ShareButtons = ({
  url, title, authorName, seed, hashtag, kind = "video",
}: ShareButtonsProps) => {
  // Which control last confirmed a copy: the post block, or the link button.
  // One flag would light both up at once.
  const [copied, setCopied] = useState<"post" | "link" | null>(null);
  const resetTimer = useRef<number | undefined>(undefined);

  const captions = buildCaptions(title, authorName, kind, seed);
  const [captionIndex, setCaptionIndex] = useState(() =>
    defaultCaptionIndex(title, seed, captions.length),
  );
  const caption = captions[captionIndex] ?? captions[0];

  useEffect(() => () => window.clearTimeout(resetTimer.current), []);

  const fullPost = buildFullPost(caption, url, hashtag);

  const copyText = async (text: string, which: "post" | "link") => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(which);
      window.clearTimeout(resetTimer.current);
      resetTimer.current = window.setTimeout(() => setCopied(null), COPIED_RESET_MS);
      return true;
    } catch {
      // Clipboard access needs a secure context and can be denied outright.
      // The post block is selectable text, so copying by hand still works.
      return false;
    }
  };

  /**
   * LinkedIn opens with the post already on the clipboard, so the composer is
   * one paste away from the words they picked. Copying first rather than
   * afterwards keeps it inside the click's user-gesture, which is what the
   * clipboard API requires -- doing it in a callback after window.open is
   * refused by the browser.
   */
  const openLinkedIn = async (href: string) => {
    await copyText(fullPost, "post");
    window.open(href, "_blank", "noopener,noreferrer");
  };

  const shareText = buildShareText(caption, hashtag);
  const targets = [
    {
      label: "Share on X",
      href: `https://x.com/intent/post?url=${encodeURIComponent(url)}&text=${encodeURIComponent(shareText)}`,
      icon: <XIcon className="w-3.5 h-3.5" />,
      name: "X",
      // X's mark is monochrome, so the chip has to invert against whatever it
      // sits on. This used to be fixed light, on the reasoning that the player
      // modal was dark in both themes -- true until the modal started
      // following the theme, at which point a near-white chip landed on a
      // near-white panel. Driven by CSS custom properties rather than a prop
      // so it inverts wherever this component is used.
      style: {
        background: "var(--x-chip-bg)",
        color: "var(--x-chip-fg)",
      } as CSSProperties,
    },
    {
      // LinkedIn dropped support for title/summary parameters; it reads the
      // page's own metadata, so only the URL is worth sending.
      label: "Share on LinkedIn",
      href: `https://www.linkedin.com/sharing/share-offsite/?url=${encodeURIComponent(url)}`,
      icon: <Linkedin className="w-4 h-4" />,
      name: "LinkedIn",
      style: { background: "#0A66C2", color: "#ffffff" } as CSSProperties,
    },
  ];

  // Solid, brand-coloured fills rather than muted outlines: these were easy to
  // miss when they read as another line of grey text next to the timestamp.
  const buttonBase =
    "flex items-center gap-2 px-4 py-2.5 rounded-xl text-xs font-bold " +
    "shadow-sm hover:shadow-md hover:scale-[1.04] active:scale-[0.98] " +
    "transition-all duration-200 cursor-pointer whitespace-nowrap";

  return (
    <div className="flex flex-col gap-3">
      {/* Caption picker. Drives the X post and the copied text.

          It deliberately does NOT drive LinkedIn: that card is built from the
          page's Open Graph tags, which the server renders before anyone has
          picked anything. Rather than pretend otherwise, the LinkedIn chip
          carries a note saying so. */}
      <div>
        <span className="text-[10px] uppercase font-bold tracking-[0.2em] text-fg-muted">
          Pick your caption
        </span>
        <div className="mt-2 flex flex-col gap-1">
          {captions.map((text, index) => (
            <button
              key={index}
              type="button"
              onClick={() => setCaptionIndex(index)}
              aria-pressed={index === captionIndex}
              className={`flex items-start gap-2.5 text-left px-3 py-2 rounded-xl border text-[11px] leading-snug transition-colors cursor-pointer ${
                index === captionIndex
                  ? "bg-vibe-purple/15 border-vibe-purple/50 text-fg"
                  : "bg-card/60 border-hairline text-fg-muted hover:text-fg hover:border-vibe-purple/30"
              }`}
            >
              <span
                className={`mt-0.5 w-3 h-3 flex-shrink-0 rounded-full border ${
                  index === captionIndex
                    ? "bg-vibe-purple border-vibe-purple"
                    : "border-fg-muted/50"
                }`}
              />
              <span className="min-w-0">{text}</span>
            </button>
          ))}
        </div>
      </div>

      {/* The post, ready to paste. LinkedIn cannot be pre-filled, so this is
          the only route from "the caption I picked" to "the words in my
          post". Shown rather than hidden behind the button so it is obvious
          the text exists and can be edited before posting. */}
      <div>
        <div className="flex items-center justify-between gap-2 mb-1.5">
          <span className="text-[10px] uppercase font-bold tracking-[0.2em] text-fg-muted">
            Your post
          </span>
          <button
            type="button"
            onClick={() => copyText(fullPost, "post")}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[10px] font-bold border transition-colors cursor-pointer ${
              copied === "post"
                ? "bg-emerald-500/15 border-emerald-400/50 text-emerald-300"
                : "bg-card border-hairline text-fg hover:border-vibe-purple/50"
            }`}
          >
            {copied === "post" ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
            <span>{copied === "post" ? "Copied!" : "Copy post"}</span>
          </button>
        </div>
        {/* readOnly rather than disabled: disabled text cannot be selected,
            which would defeat the point if the clipboard call is refused. */}
        <textarea
          readOnly
          value={fullPost}
          // Sized for the longest caption plus the blank lines, the URL and
          // the tag line -- at rows={5} the hashtags were cut off, which is
          // the one part of this that has to be visible to be trusted.
          rows={7}
          onFocus={(e) => e.currentTarget.select()}
          className="w-full resize-none bg-input border border-hairline rounded-xl px-3 py-2 text-[11px] leading-relaxed text-fg font-mono focus:outline-none focus:border-vibe-purple/50"
        />
      </div>

      <div className="flex flex-wrap items-center gap-2.5">
      <span className="text-[10px] uppercase font-bold tracking-[0.2em] text-fg-muted mr-0.5">
        Share
      </span>

      {targets.map((target) =>
        target.name === "LinkedIn" ? (
          <button
            key={target.name}
            type="button"
            onClick={() => openLinkedIn(target.href)}
            aria-label={`${target.label} (copies your post first)`}
            className={buttonBase}
            style={target.style}
          >
            {target.icon}
            <span>{target.name}</span>
          </button>
        ) : (
          <a
            key={target.name}
            href={target.href}
            target="_blank"
            // noreferrer too: opener access is the actual risk, and noopener
            // alone still leaks the referrer.
            rel="noopener noreferrer"
            aria-label={target.label}
            className={buttonBase}
            style={target.style}
          >
            {target.icon}
            <span>{target.name}</span>
          </a>
        ),
      )}

      <button
        type="button"
        onClick={() => copyText(url, "link")}
        aria-label="Copy link to this video"
        className={`${buttonBase} border ${
          copied === "link"
            ? "bg-emerald-500/15 border-emerald-400/50 text-emerald-300"
            : "bg-card border-hairline text-fg hover:border-vibe-purple/50"
        }`}
      >
        {copied === "link" ? <Check className="w-4 h-4" /> : <Link2 className="w-4 h-4" />}
        <span>{copied === "link" ? "Copied!" : "Copy link"}</span>
      </button>

      <span className="w-full text-[10px] text-fg-muted/70">
        LinkedIn cannot be pre-filled, so opening it copies your post — just
        paste into the composer.
      </span>
      </div>
    </div>
  );
};
