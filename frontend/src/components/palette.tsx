/**
 * Command palette — Ctrl/Cmd-K.
 *
 * A control room operator repeats a handful of moves all shift: get to a camera, trace
 * a plate, open the alert desk. Each of those is currently two or three clicks through
 * a sidebar, and an officer who has done it four hundred times should be able to do it
 * without looking.
 *
 * It is also the discoverable home for the keyboard shortcuts, because a shortcut
 * nobody can find is a shortcut nobody uses.
 *
 * Deliberately simple: no fuzzy-match library, no command registry abstraction. The
 * command set is a dozen entries and a substring match over their labels, which is
 * both enough and auditable.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";

type Command = {
  id: string;
  label: string;
  hint?: string;
  group: "Go to" | "Camera" | "Action";
  run: () => void;
};

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  // Cameras are already cached by every other screen, so searching them here costs
  // nothing. `enabled` keeps the palette from being the thing that fetches them.
  const cameras = useQuery({
    queryKey: ["cameras"],
    queryFn: () => api.cameras(),
    enabled: open,
    staleTime: 60_000,
  });

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
    setActive(0);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // Ctrl-K on Windows and Linux, Cmd-K on macOS. Both, because a control room is
      // whatever hardware the department bought.
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((v) => !v);
        return;
      }
      if (event.key === "Escape" && open) close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const commands = useMemo<Command[]>(() => {
    const pages: [string, string, string][] = [
      ["/coverage", "Coverage", "where the estate can see"],
      ["/map", "GIS Map", "camera registry"],
      ["/journey", "Journey", "trace a registration"],
      ["/dashboard", "Dashboard", "traffic, alerts and health"],
      ["/alerts", "Alert Desk", "live watchlist matches"],
      ["/control-room", "Control Room", "multi-camera wall"],
      ["/health", "Health", "feed diagnostics and ANPR grading"],
      ["/gaps", "Coverage gaps", "blind spots by district"],
      ["/watchlist", "Watchlist", "vehicles being watched"],
      ["/zones", "Zones", "intrusion polygons"],
      ["/scale", "Scale Planner", "sizing for 80,000 cameras"],
      ["/system", "System", "audit chain and catalogue"],
      ["/demo", "Demo", "own-feed footage and its reads"],
    ];

    const out: Command[] = pages.map(([path, label, hint]) => ({
      id: `page:${path}`,
      label,
      hint,
      group: "Go to",
      run: () => navigate(path),
    }));

    for (const camera of cameras.data ?? []) {
      out.push({
        id: `cam:${camera.id}`,
        label: camera.name,
        hint: camera.camera_ref,
        group: "Camera",
        run: () => navigate(`/map?camera=${camera.id}`),
      });
    }

    // A plate typed into the palette goes straight to a trace. The single most common
    // thing anybody will do with this system, and on evaluation day literally the task.
    const plate = query.trim().toUpperCase().replace(/[\s-]/g, "");
    if (/^[A-Z]{2}[0-9A-Z]{4,10}$/.test(plate)) {
      out.unshift({
        id: `trace:${plate}`,
        label: `Trace ${plate}`,
        hint: "journey across the camera network",
        group: "Action",
        run: () => navigate(`/journey?plate=${plate}`),
      });
    }

    return out;
  }, [cameras.data, navigate, query]);

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return commands.slice(0, 12);
    return commands
      .filter(
        (c) =>
          c.label.toLowerCase().includes(needle) ||
          (c.hint ?? "").toLowerCase().includes(needle) ||
          c.group === "Action",
      )
      .slice(0, 20);
  }, [commands, query]);

  if (!open) return null;

  const grouped = results.reduce<Record<string, Command[]>>((acc, c) => {
    (acc[c.group] ??= []).push(c);
    return acc;
  }, {});

  let index = -1;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[12vh] px-4 bg-black/40"
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      onClick={close}
    >
      <div
        className="panel w-full max-w-lg overflow-hidden"
        onClick={(event) => event.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setActive(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setActive((a) => Math.min(a + 1, results.length - 1));
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              setActive((a) => Math.max(a - 1, 0));
            } else if (event.key === "Enter" && results[active]) {
              event.preventDefault();
              results[active].run();
              close();
            }
          }}
          placeholder="Search a camera, type a registration, or jump to a screen…"
          aria-label="Command"
          className="w-full bg-transparent border-0 border-b border-edge px-3.5 py-2.5 t-body focus:outline-none"
        />

        <div className="max-h-[50vh] overflow-y-auto py-1" role="listbox">
          {results.length === 0 ? (
            <p className="px-3.5 py-3 t-small text-muted">
              Nothing matches. Try a camera name, a screen, or a registration like
              GJ01AB1234.
            </p>
          ) : (
            Object.entries(grouped).map(([group, items]) => (
              <div key={group}>
                <div className="px-3.5 pt-2 pb-1 t-micro uppercase tracking-wider text-muted">
                  {group}
                </div>
                {items.map((command) => {
                  index += 1;
                  const isActive = index === active;
                  return (
                    <button
                      key={command.id}
                      role="option"
                      aria-selected={isActive}
                      onClick={() => {
                        command.run();
                        close();
                      }}
                      className={`w-full text-left px-3.5 py-1.5 flex items-baseline gap-2 ${
                        isActive ? "bg-accent/15" : "hover:bg-ink-700"
                      }`}
                    >
                      <span className="t-small">{command.label}</span>
                      {command.hint ? (
                        <span className="t-micro text-muted mono truncate">{command.hint}</span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>

        <div className="px-3.5 py-1.5 border-t border-edge flex gap-3 t-micro text-muted">
          <span>
            <kbd className="mono">↑↓</kbd> move
          </span>
          <span>
            <kbd className="mono">↵</kbd> open
          </span>
          <span>
            <kbd className="mono">esc</kbd> close
          </span>
          <span className="ml-auto">
            <kbd className="mono">ctrl</kbd>+<kbd className="mono">k</kbd> anywhere
          </span>
        </div>
      </div>
    </div>
  );
}
