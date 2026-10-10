/**
 * The guided tour and the keyboard reference: the console explaining itself.
 *
 * A demonstration that depends on somebody remembering the running order is a
 * demonstration that goes wrong once. This walks the three jobs the platform exists to
 * do — trace a vehicle, watch a location, plan and audit — against the real screens
 * with the real queries, and says in one line what each screen proves. Nothing here is
 * a mock: every step navigates to the page it is describing, so if a screen is broken
 * the tour shows it broken rather than narrating over it.
 *
 * Dismissible and resumable, in that order of importance. An operator who has seen it
 * must be able to make it go away for good; a judge who dismissed it by accident must
 * be able to get it back. So the control to reopen it is permanent and visible, not a
 * localStorage key somebody has to clear.
 *
 * The shortcut reference opens on `?`, which is where every tool that has one puts it.
 * It is here rather than in a README because a shortcut nobody can discover is a
 * shortcut nobody uses, and §8.9 asks for the reference to be discoverable rather than
 * documented.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

const SEEN_KEY = "setu.tour.seen";
const STEP_KEY = "setu.tour.step";
/** Set by Judge Mode so the tour does not open over the screen it just opened. */
export const JUDGE_KEY = "setu.judge";

type TourStep = {
  path: string;
  job: string;
  title: string;
  /** What is on screen. */
  body: string;
  /** What this screen proves — the sentence a judge should leave with. */
  proves: string;
};

const STEPS: TourStep[] = [
  {
    path: "/coverage",
    job: "Where the state can see",
    title: "Coverage, and its limits",
    body: "Every camera drawn as the area it can actually see, coloured by whether a number plate is readable there. Cameras with no surveyed heading are drawn as domes of positional uncertainty rather than cones.",
    proves:
      "The platform distinguishes what it knows from what it assumes. A cone would claim an orientation nobody recorded, so it does not draw one.",
  },
  {
    path: "/journey",
    job: "Trace a vehicle",
    title: "A route, hop by hop, with its evidence",
    body: "Enter a registration and a purpose. The purpose is written to the audit ledger before the search runs. Each hop carries its crop, its camera, its timestamp and the tier of evidence behind it.",
    proves:
      "A trace is a chain of sightings with evidence attached, not a line on a map. The purpose is recorded first, so the ledger shows why a search happened and not merely that it did.",
  },
  {
    path: "/journey",
    job: "Trace a vehicle",
    title: "When the plate is unreadable",
    body: "Below the confirmed hops, appearance candidates: vehicles matching the seed on colour, class and a 76-dimension fingerprint. Both crops are shown side by side, and the score is broken into its five components.",
    proves:
      "A Candidate is never rendered as a Confirmed. The measured false-link rate at the shipped gate is 0.055; a looser gate measured 0.834 and would fill the list with wrong answers. Nothing here can promote a candidate to a plate read.",
  },
  {
    path: "/journey",
    job: "Trace a vehicle",
    title: "The shape of a movement",
    body: 'Open "Shape of this movement" on the map. Geography runs across, time runs upward. A vertical segment is a vehicle that stayed put; a near-horizontal one is distance crossed with no time spent.',
    proves:
      "Loitering, convoy and impossible travel stop being things you compute and become shapes you recognise. Impossible travel is the visual signature of a cloned or misread plate.",
  },
  {
    path: "/dashboard",
    job: "Watch a location",
    title: "What the estate is doing now",
    body: "Activity, camera health, readiness mix and anomalies in one window. A camera declaring 25 fps and delivering 15 is flagged here rather than trusted.",
    proves:
      "A measurement that was never taken reads as absent, not as zero. That distinction is what stops a dead ingest from being reported as a quiet night.",
  },
  {
    path: "/onboarding",
    job: "Plan and audit",
    title: "Bringing a department on",
    body: "Paste a camera URL and watch the platform probe it: address checks, TCP handshake, transport negotiation, the first decoded frame, declared against measured frame rate, and a readiness grade with its reasoning.",
    proves:
      "Onboarding shows its working. A stage that was never reached is drawn as skipped rather than omitted, because in a list of ticks an absent stage and a passing one look the same.",
  },
  {
    path: "/scale",
    job: "Plan and audit",
    title: "From this estate to 80,000 cameras",
    body: "What the measured per-camera cost implies at state scale, in compute, storage and bandwidth, with the assumptions stated and adjustable.",
    proves:
      "The scale answer is arithmetic from measurements taken on this hardware, not a figure chosen to sound achievable.",
  },
  {
    path: "/system",
    job: "Plan and audit",
    title: "The ledger, and whether it holds",
    body: "Verify the hash chain over every recorded action. Each entry names the actor, the purpose and the subject, and links to the entry before it.",
    proves:
      "Every search, export and adjudication is accountable to a person and a reason. The chain is what makes the rest of the platform's claims checkable rather than asserted.",
  },
];

export const SHORTCUTS: { keys: string; what: string }[] = [
  { keys: "Ctrl / ⌘ + K", what: "Command palette — jump to a page, a camera or a plate" },
  { keys: "?", what: "This reference" },
  { keys: "Esc", what: "Close the palette, this reference, or the tour" },
  { keys: "→ / ←", what: "Next and previous step, while the tour is open" },
  { keys: "↑ / ↓", what: "Move through results in the command palette" },
  { keys: "Enter", what: "Open the selected result" },
];

/** The shortcut reference. Opens on `?`, which is where it is everywhere else. */
export function ShortcutsReference({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 grid place-items-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
      onClick={onClose}
    >
      <div
        className="panel p-4 w-full max-w-md bg-ink-800"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className="t-section mb-3">Keyboard shortcuts</h2>
        <dl className="flex flex-col gap-1.5">
          {SHORTCUTS.map((s) => (
            <div key={s.keys} className="flex items-baseline gap-3">
              <dt className="mono t-micro px-1.5 py-0.5 rounded border border-edge bg-ink-700 shrink-0 min-w-[6.5rem] text-center">
                {s.keys}
              </dt>
              <dd className="t-small text-fg2">{s.what}</dd>
            </div>
          ))}
        </dl>
        <button type="button" className="btn t-small mt-4 w-full" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}

export function GuidedTour({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [index, setIndex] = useState(() => {
    const stored = Number(localStorage.getItem(STEP_KEY));
    return Number.isFinite(stored) && stored > 0 && stored < STEPS.length ? stored : 0;
  });

  const step = STEPS[index];

  const go = useCallback(
    (next: number) => {
      const clamped = Math.max(0, Math.min(STEPS.length - 1, next));
      setIndex(clamped);
      try {
        localStorage.setItem(STEP_KEY, String(clamped));
      } catch {
        // A console in a private window still gets a working tour; it just will not
        // remember where it was.
      }
    },
    [],
  );

  const finish = useCallback(() => {
    try {
      localStorage.setItem(SEEN_KEY, "1");
      localStorage.removeItem(STEP_KEY);
    } catch {
      /* see above */
    }
    onClose();
  }, [onClose]);

  // Navigate when the reader moves between steps -- never on the initial open.
  //
  // Navigating on open made the tour hijack whatever the app was doing. Judge Mode
  // signs in and routes straight to a prepared trace on /journey; the tour mounted a
  // moment later, saw step 1, and sent the browser to /coverage. The button whose
  // entire purpose is to open the scored capability appeared to do nothing, and the
  // cause was two features navigating at once rather than either one being broken.
  //
  // So the first render adopts the screen the reader is already on, and only Next,
  // Back and a resumed step move them.
  const navigatedFor = useRef<number | null>(null);
  useEffect(() => {
    if (!open || !step) return;
    if (navigatedFor.current === null) {
      navigatedFor.current = index; // adopt, do not redirect
      return;
    }
    if (navigatedFor.current !== index) {
      navigatedFor.current = index;
      navigate(step.path);
    }
  }, [open, step, index, navigate]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowRight") go(index + 1);
      if (event.key === "ArrowLeft") go(index - 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, index, go, onClose]);

  if (!open || !step) return null;

  const last = index === STEPS.length - 1;

  return (
    // Anchored rather than modal: the point is to look at the screen behind it. A
    // tour that covers the thing it is describing is a slide deck.
    //
    // Vertically centred on the right rather than in the bottom corner. In the corner
    // it sat below most people's natural line of sight and under the two floating
    // buttons, and the first thing a reviewer is meant to read was the thing they did
    // not notice. Centred it is unmissable and still leaves the navigation, the page
    // header and the left two-thirds of every screen clear.
    //
    // Capped at the viewport height with its own scroll, so a long step on a short
    // laptop screen cannot push its own controls out of reach.
    <div
      className="fixed right-4 top-1/2 -translate-y-1/2 z-40 w-[min(26rem,calc(100vw-2rem))]
                 max-h-[calc(100vh-2rem)] overflow-y-auto panel bg-ink-800 shadow-lg"
      role="dialog"
      aria-label="Guided tour"
    >
      <div className="px-3 pt-3 flex items-baseline justify-between gap-2">
        <span className="t-micro uppercase tracking-wider text-accent">{step.job}</span>
        <span className="t-micro num text-muted tabular-nums">
          {index + 1} / {STEPS.length}
        </span>
      </div>

      <div className="px-3 pb-3">
        <h2 className="t-section mt-1">{step.title}</h2>
        <p className="t-small text-fg2 mt-1">{step.body}</p>
        <p className="t-small mt-2 pt-2 border-t border-edge">
          <span className="t-micro uppercase tracking-wider text-muted block mb-0.5">
            What this proves
          </span>
          {step.proves}
        </p>
      </div>

      {/* Progress as segments, one per step, so the length of what remains is visible
          rather than implied by a percentage. */}
      <div className="flex gap-0.5 px-3" aria-hidden="true">
        {STEPS.map((s, i) => (
          <span
            key={s.title}
            className="h-0.5 flex-1 rounded"
            style={{
              background: i <= index ? "rgb(var(--accent))" : "rgb(var(--edge))",
            }}
          />
        ))}
      </div>

      <div className="flex gap-1.5 p-3">
        <button type="button" className="btn t-small" onClick={finish}>
          Dismiss
        </button>
        <span className="flex-1" />
        <button
          type="button"
          className="btn t-small"
          disabled={index === 0}
          onClick={() => go(index - 1)}
        >
          Back
        </button>
        <button
          type="button"
          className="btn btn-primary t-small"
          onClick={() => (last ? finish() : go(index + 1))}
        >
          {last ? "Done" : "Next"}
        </button>
      </div>
    </div>
  );
}

/**
 * Owns both overlays and the keys that open them.
 *
 * The tour opens by itself on a first visit and never again, but the button that
 * reopens it is permanent — a judge who dismissed it in the first ten seconds should
 * not have to clear site data to get it back.
 */
export function TourAndShortcuts() {
  const [tourOpen, setTourOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);

  useEffect(() => {
    let seen = "1";
    let judging = false;
    try {
      seen = localStorage.getItem(SEEN_KEY) ?? "";
      // Judge Mode asked for one specific screen. Opening a tour over it is the
      // opposite of helpful, so that path suppresses the auto-open for the session.
      judging = sessionStorage.getItem(JUDGE_KEY) === "1";
    } catch {
      // Treat an unreadable store as "already seen". Showing the tour on every single
      // load in a private window would be worse than not showing it at all.
    }
    if (!seen && !judging) setTourOpen(true);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.isContentEditable;
      if (typing) return;
      if (event.key === "?") {
        event.preventDefault();
        setShortcutsOpen((on) => !on);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      <GuidedTour open={tourOpen} onClose={() => setTourOpen(false)} />
      <ShortcutsReference open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
      {!tourOpen ? (
        <div className="fixed bottom-3 right-3 z-30 flex gap-1.5">
          <button
            type="button"
            className="btn t-micro"
            onClick={() => setShortcutsOpen(true)}
            title="Keyboard shortcuts (?)"
          >
            Shortcuts
          </button>
          <button type="button" className="btn t-micro" onClick={() => setTourOpen(true)}>
            Guided tour
          </button>
        </div>
      ) : null}
    </>
  );
}
