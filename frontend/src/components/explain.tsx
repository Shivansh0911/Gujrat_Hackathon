/**
 * Self-explanation: the honesty posture expressed as interface rather than as a
 * document.
 *
 * Every screen answers three questions in one line each — what it is for, what data
 * sits behind it, and what its known limits are. The limits line is the one that
 * matters and the one most products omit: a screen that shows a number without saying
 * what the number cannot tell you invites a conclusion it does not support.
 *
 * Collapsed by default so it costs an operator nothing, expanded with one key, and
 * remembered per screen. Not a tooltip: a limit discovered by hovering is a limit
 * discovered after the decision.
 */

import { useEffect, useState } from "react";

const KEY = "setu.explain.open";

export function WhatAmILookingAt({
  what,
  data,
  limits,
}: {
  what: string;
  data: string;
  limits: string;
}) {
  const [open, setOpen] = useState(false);

  // The preference is global rather than per page: an operator who wants the context
  // wants it everywhere, and one who does not should not have to dismiss it eleven
  // times.
  useEffect(() => {
    try {
      setOpen(localStorage.getItem(KEY) === "1");
    } catch {
      /* site data blocked; the default stands */
    }
  }, []);

  const toggle = () => {
    setOpen((v) => {
      try {
        localStorage.setItem(KEY, v ? "0" : "1");
      } catch {
        /* the choice simply will not persist */
      }
      return !v;
    });
  };

  return (
    <div className="border-b border-edge bg-ink-800">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="w-full px-4 py-1 flex items-center gap-2 text-left hover:bg-ink-700 transition-colors"
      >
        <span className="t-micro uppercase tracking-wider text-muted">
          What am I looking at?
        </span>
        <span className="t-micro text-muted" aria-hidden="true">
          {open ? "▴" : "▾"}
        </span>
        {!open ? (
          <span className="t-micro text-fg3 truncate">{what}</span>
        ) : null}
      </button>

      {open ? (
        <dl className="px-4 pb-2.5 grid gap-1.5 sm:grid-cols-3">
          <div>
            <dt className="t-micro uppercase tracking-wider text-muted">This screen</dt>
            <dd className="t-small text-fg2">{what}</dd>
          </div>
          <div>
            <dt className="t-micro uppercase tracking-wider text-muted">Behind it</dt>
            <dd className="t-small text-fg2">{data}</dd>
          </div>
          <div>
            <dt className="t-micro uppercase tracking-wider text-muted">
              What it cannot tell you
            </dt>
            <dd className="t-small text-fg2">{limits}</dd>
          </div>
        </dl>
      ) : null}
    </div>
  );
}
