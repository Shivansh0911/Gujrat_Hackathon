import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type WatchlistCreate, type WatchlistEntry } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Badge, Empty, ErrorBox, Spinner } from "../components/ui";

/**
 * The watchlist is the input to every alert this platform raises, and until now it
 * could only be changed by running a seeding script on the server. An operator
 * looking at the Alert Desk could see that a vehicle matched but had no way to see
 * *why* it was being watched, when the authority for watching it expires, or to add
 * a vehicle that had just been reported.
 *
 * Two things here are deliberate rather than incidental.
 *
 * **Expiry is a required field with no default.** An entry without one is a
 * permanent record about a citizen, created by omission. The API rejects a missing
 * or past expiry; this form defaults the input to 30 days out so the easy path is
 * also the correct one, and shows the remaining life of every entry in the table.
 *
 * **Adding requires admin, and the form says so before you fill it in** rather than
 * after the server refuses. An operator can read the watchlist -- they need to, to
 * make sense of an alert -- but not extend it.
 */

/**
 * The same shapes `services/analytics/plate_grammar.py` accepts, mirrored here so the
 * form can refuse a plate before the round trip.
 *
 * The server remains the authority -- this is not a substitute for it, and the API
 * still validates. The point is that being told "GJ01AB1234 is the expected format"
 * while typing is a different experience from a 422 after pressing the button.
 */
const PLATE_STANDARD = /^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,4}$/;
const PLATE_BH = /^\d{2}BH\d{1,4}[A-Z]{1,2}$/;

function plateLooksValid(raw: string): boolean {
  const p = raw.toUpperCase().replace(/[\s-]/g, "");
  return PLATE_STANDARD.test(p) || PLATE_BH.test(p);
}

/** Local datetime string for an <input type="datetime-local">, N days out. */
function localDateTimeIn(days: number): string {
  const d = new Date(Date.now() + days * 86_400_000);
  // Shift by the timezone offset so the picker shows local time, not UTC.
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

const SEVERITIES = ["low", "medium", "high", "critical"] as const;

const SEVERITY_TONE: Record<string, "bad" | "warn" | "ok" | "muted"> = {
  critical: "bad",
  high: "bad",
  medium: "warn",
  low: "muted",
};

function daysUntil(iso: string): number {
  return Math.ceil((new Date(iso).getTime() - Date.now()) / 86_400_000);
}

/** Thirty days out: long enough to be useful, short enough to be a real bound. */
function defaultExpiry(): string {
  return localDateTimeIn(30);
}

const EMPTY_FORM: WatchlistCreate = {
  plate_normalised: "",
  watchlist_name: "",
  authority: "",
  case_ref: "",
  priority: 50,
  severity: "medium",
  notes: "",
  valid_to: "",
};

export default function WatchlistPage() {
  const { role } = useAuth();
  const isAdmin = role === "admin";
  const qc = useQueryClient();

  const [includeExpired, setIncludeExpired] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<WatchlistCreate>({
    ...EMPTY_FORM,
    valid_to: defaultExpiry(),
  });

  const { data, isLoading, error } = useQuery({
    queryKey: ["watchlist", includeExpired],
    queryFn: () => api.watchlist(includeExpired),
  });

  const add = useMutation({
    mutationFn: (body: WatchlistCreate) => api.addWatchlistEntry(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["watchlist"] });
      setForm({ ...EMPTY_FORM, valid_to: defaultExpiry() });
      setShowForm(false);
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteWatchlistEntry(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  const entries = useMemo(
    () => [...(data ?? [])].sort((a, b) => b.priority - a.priority),
    [data],
  );

  const set = <K extends keyof WatchlistCreate>(k: K, v: WatchlistCreate[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const plateTouched = form.plate_normalised.length > 0;
  const plateOk = plateLooksValid(form.plate_normalised);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    add.mutate({
      ...form,
      plate_normalised: form.plate_normalised.toUpperCase().replace(/\s+/g, ""),
      // A local datetime from the picker carries no zone; the API wants an instant.
      valid_to: new Date(form.valid_to).toISOString(),
      case_ref: form.case_ref || null,
      notes: form.notes || null,
    });
  }

  return (
    <div className="h-full overflow-auto p-5">
      <header className="mb-4 flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold">Watchlist</h1>
          <p className="text-sm text-muted max-w-2xl mt-1">
            Every alert on the desk begins here. An entry authorises the platform to
            report sightings of one vehicle, to one named authority, until a stated
            date — after which it stops matching on its own.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <label className="text-xs text-muted flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={includeExpired}
              onChange={(e) => setIncludeExpired(e.target.checked)}
            />
            Show expired
          </label>
          {isAdmin ? (
            <button className="btn" onClick={() => setShowForm((v) => !v)}>
              {showForm ? "Cancel" : "Add vehicle"}
            </button>
          ) : (
            <span className="text-xs text-muted">
              Adding requires an admin account
            </span>
          )}
        </div>
      </header>

      {showForm && isAdmin && (
        <form
          onSubmit={submit}
          className="mb-5 rounded border border-edge bg-ink-800 p-4 grid gap-3 md:grid-cols-3"
        >
          <Field
            label="Registration"
            hint={
              plateTouched && !plateOk
                ? "Not a valid Indian registration — expected GJ01AB1234, or 22BH1234A for a BH-series plate"
                : "As it would be read, without spaces"
            }
          >
            <input
              className={`input mono uppercase ${
                plateTouched && !plateOk ? "border-bad/70" : ""
              }`}
              required
              minLength={4}
              value={form.plate_normalised}
              onChange={(e) => set("plate_normalised", e.target.value)}
              placeholder="GJ01AB1234"
            />
          </Field>
          <Field label="Watchlist" hint="Why this vehicle is listed">
            <input
              className="input"
              required
              value={form.watchlist_name}
              onChange={(e) => set("watchlist_name", e.target.value)}
              placeholder="Stolen Vehicles"
            />
          </Field>
          <Field label="Authority" hint="Who authorised the listing">
            <input
              className="input"
              required
              value={form.authority}
              onChange={(e) => set("authority", e.target.value)}
              placeholder="Gujarat Police, Crime Branch"
            />
          </Field>
          <Field label="Case reference">
            <input
              className="input mono"
              value={form.case_ref ?? ""}
              onChange={(e) => set("case_ref", e.target.value)}
              placeholder="FIR 123/2026"
            />
          </Field>
          <Field label="Severity">
            <select
              className="input"
              value={form.severity}
              onChange={(e) => set("severity", e.target.value)}
            >
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
          <Field label={`Priority — ${form.priority}`} hint="Orders the alert desk">
            <input
              type="range"
              min={0}
              max={100}
              value={form.priority}
              onChange={(e) => set("priority", Number(e.target.value))}
            />
          </Field>
          <Field
            label="Expires"
            hint="Required. An entry with no end date is a permanent record about a citizen."
          >
            <input
              type="datetime-local"
              className="input"
              required
              // `min` disables past dates in the picker itself. Previously the only
              // signal was a 422 after submitting, which taught the operator nothing
              // about what was wrong until the work was already done.
              min={localDateTimeIn(0)}
              value={form.valid_to}
              onChange={(e) => set("valid_to", e.target.value)}
            />
            <div className="flex gap-1 mt-1.5">
              {[
                ["7 days", 7],
                ["30 days", 30],
                ["90 days", 90],
              ].map(([label, days]) => (
                <button
                  key={label as string}
                  type="button"
                  onClick={() => set("valid_to", localDateTimeIn(days as number))}
                  className="text-[10px] px-2 py-0.5 rounded border border-edge bg-ink-700
                             hover:bg-ink-600 text-muted hover:text-fg transition-colors"
                >
                  {label}
                </button>
              ))}
            </div>
          </Field>
          <Field label="Notes" hint="Context for the officer who sees the alert">
            <input
              className="input"
              value={form.notes ?? ""}
              onChange={(e) => set("notes", e.target.value)}
            />
          </Field>
          <div className="flex items-end">
            <button
              className="btn btn-primary w-full"
              disabled={add.isPending || !plateOk}
              title={plateOk ? undefined : "Enter a valid registration first"}
            >
              {add.isPending ? "Adding…" : "Add to watchlist"}
            </button>
          </div>
          {add.error != null && (
            <div className="md:col-span-3">
              <ErrorBox error={add.error} />
            </div>
          )}
        </form>
      )}

      {isLoading && <Spinner label="Loading watchlist" />}
      {error != null && <ErrorBox error={error} />}
      {!isLoading && error == null && entries.length === 0 && (
        <Empty
          title="No watchlist entries"
          detail={
            includeExpired
              ? "Nothing has ever been listed on this instance."
              : "Nothing is currently listed. Expired entries are hidden — tick “Show expired” to see them."
          }
        />
      )}

      {entries.length > 0 && (
        <div className="overflow-x-auto rounded border border-edge">
          <table className="w-full text-sm">
            <thead className="bg-ink-700 text-muted text-[11px] uppercase tracking-wide">
              <tr>
                <th className="text-left px-3 py-2">Registration</th>
                <th className="text-left px-3 py-2">Watchlist</th>
                <th className="text-left px-3 py-2">Authority</th>
                <th className="text-left px-3 py-2">Case</th>
                <th className="text-right px-3 py-2">Priority</th>
                <th className="text-left px-3 py-2">Severity</th>
                <th className="text-left px-3 py-2">Expires</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <Row
                  key={e.id}
                  entry={e}
                  canRemove={isAdmin}
                  removing={remove.isPending}
                  onRemove={() => remove.mutate(e.id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-muted mt-4 max-w-3xl">
        Adding an entry is written to the audit ledger before it takes effect, with the
        actor who added it. Matching is confusion-aware, so a listed{" "}
        <span className="mono">0</span> will still be found in a read of{" "}
        <span className="mono">O</span> — the Alert Desk shows which characters were
        treated as equivalent for any given match.
      </p>
    </div>
  );
}

function Row({
  entry,
  canRemove,
  removing,
  onRemove,
}: {
  entry: WatchlistEntry;
  canRemove: boolean;
  removing: boolean;
  onRemove: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const days = daysUntil(entry.valid_to);
  const expired = days <= 0;
  return (
    <tr className={`border-t border-edge ${expired ? "opacity-55" : ""}`}>
      <td className="px-3 py-2 mono">{entry.plate_normalised ?? "—"}</td>
      <td className="px-3 py-2">{entry.watchlist_name}</td>
      <td className="px-3 py-2 text-fg2">{entry.authority ?? "—"}</td>
      <td className="px-3 py-2 mono text-xs">{entry.case_ref ?? "—"}</td>
      <td className="px-3 py-2 text-right tabular-nums">{entry.priority}</td>
      <td className="px-3 py-2">
        <Badge tone={SEVERITY_TONE[entry.severity] ?? "muted"}>{entry.severity}</Badge>
      </td>
      <td className="px-3 py-2 text-xs">
        {expired ? (
          <Badge tone="muted">expired</Badge>
        ) : (
          <span className={days <= 7 ? "text-warn" : "text-fg2"}>
            {days} day{days === 1 ? "" : "s"} left
          </span>
        )}
      </td>
      <td className="px-3 py-2 text-right whitespace-nowrap">
        {canRemove &&
          (confirming ? (
            <span className="inline-flex items-center gap-1.5">
              <span className="text-[11px] text-muted">Remove?</span>
              <button
                className="btn text-[11px] py-0.5 px-2 border-bad/60 text-bad hover:bg-bad/10"
                disabled={removing}
                onClick={onRemove}
              >
                {removing ? "Removing…" : "Yes, remove"}
              </button>
              <button
                className="btn text-[11px] py-0.5 px-2"
                onClick={() => setConfirming(false)}
              >
                Cancel
              </button>
            </span>
          ) : (
            <button
              className="btn text-[11px] py-0.5 px-2 text-muted hover:text-bad"
              onClick={() => setConfirming(true)}
              title="Remove this entry. The removal is written to the audit ledger."
            >
              Remove
            </button>
          ))}
      </td>
    </tr>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <div className="text-xs text-fg2 mb-1">{label}</div>
      {children}
      {hint && <div className="text-[10px] text-muted mt-1">{hint}</div>}
    </label>
  );
}
