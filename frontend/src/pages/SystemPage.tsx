import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type BulkImportResult } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Badge, ErrorBox, Spinner } from "../components/ui";

/**
 * Administrative capabilities that had no console surface.
 *
 * **Audit chain verification.** The ledger is hash-chained and append-only, and a
 * verify endpoint has always existed — but the only way to run it was Swagger, which
 * means the integrity guarantee was demonstrable to a developer and invisible to the
 * officer who depends on it. Tamper-evidence nobody can check is a claim, not a
 * control.
 *
 * **Catalogue reconciliation.** The registry is the control plane; the gateway is
 * the reality. This diffs them and reports what changed, without applying anything
 * on its own — the estate is not ours to silently rewrite.
 *
 * **Bulk camera onboarding.** Model 1 requires this as a platform capability, and it
 * previously existed only as a script run on the server. Rows are validated
 * individually against the same rules the seed script uses, so a spreadsheet with two
 * bad lines imports the rest and reports those two by line number and reason.
 */

export default function SystemPage() {
  const { role } = useAuth();
  const isAdmin = role === "admin";
  const qc = useQueryClient();

  const audit = useQuery({
    queryKey: ["audit-verify"],
    queryFn: () => api.auditVerify(),
  });

  const fileRef = useRef<HTMLInputElement>(null);
  const [chosen, setChosen] = useState<File | null>(null);

  const bulkImport = useMutation({
    mutationFn: (f: File) => api.bulkImportCameras(f),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cameras"] });
      qc.invalidateQueries({ queryKey: ["gap-analysis"] });
      setChosen(null);
      if (fileRef.current) fileRef.current.value = "";
    },
  });

  const sync = useMutation({
    mutationFn: () => api.syncCatalogue(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cameras"] }),
  });

  return (
    <div className="h-full overflow-auto p-5 space-y-6">
      <header>
        <h1 className="text-xl font-semibold">System</h1>
        <p className="text-sm text-muted max-w-2xl mt-1">
          Integrity of the evidence ledger, and reconciliation between what the
          registry believes about the estate and what the gateway actually offers.
        </p>
      </header>

      {/* ---------------------------------------------------------------- audit */}
      <section className="panel p-4">
        <div className="flex items-start justify-between gap-4 flex-wrap mb-3">
          <div>
            <h2 className="font-medium">Audit chain</h2>
            <p className="text-xs text-muted max-w-xl mt-0.5">
              Every entry commits to its predecessor:{" "}
              <span className="mono">
                entry_hash = SHA256(prev_hash ‖ canonical_json(entry))
              </span>
              . Re-hashing the chain detects any modification, actor rewrite or
              deletion — including one made directly in the database.
            </p>
          </div>
          <button
            className="btn"
            onClick={() => audit.refetch()}
            disabled={audit.isFetching}
          >
            {audit.isFetching ? "Verifying…" : "Verify now"}
          </button>
        </div>

        {audit.isLoading && <Spinner label="Verifying chain" />}
        {audit.error != null && <ErrorBox error={audit.error} />}
        {audit.data && (
          <>
            <div className="flex items-center gap-3 flex-wrap">
              <Badge tone={audit.data.valid ? "ok" : "bad"}>
                {audit.data.valid ? "chain intact" : "CHAIN BROKEN"}
              </Badge>
              <span className="text-sm text-slate-300 tabular-nums">
                {audit.data.entries_checked} entries checked
              </span>
              <span className="text-xs text-muted">
                verified {new Date(audit.data.verified_at).toLocaleString()}
              </span>
            </div>

            {audit.data.head_hash && (
              <div className="mt-3">
                <div className="label">Head hash</div>
                <div className="mono text-xs break-all text-slate-300">
                  {audit.data.head_hash}
                </div>
              </div>
            )}

            {audit.data.breaks.length > 0 && (
              <div className="mt-3">
                <div className="label text-bad">
                  {audit.data.breaks.length} inconsistency(ies)
                </div>
                <pre className="text-xs bg-ink-900 border border-edge rounded p-2 overflow-x-auto">
                  {JSON.stringify(audit.data.breaks, null, 2)}
                </pre>
              </div>
            )}
          </>
        )}
      </section>

      {/* ------------------------------------------------------------ catalogue */}
      <section className="panel p-4">
        <div className="flex items-start justify-between gap-4 flex-wrap mb-3">
          <div>
            <h2 className="font-medium">Catalogue reconciliation</h2>
            <p className="text-xs text-muted max-w-xl mt-0.5">
              Compares the registry against the gateway catalogue and reports the
              difference. It does not apply anything: a camera vanishing from a
              third-party feed for ten minutes is not authority to delete its
              identity, its history or its evidence.
            </p>
          </div>
          <button
            className="btn"
            onClick={() => sync.mutate()}
            disabled={sync.isPending || !isAdmin}
            title={isAdmin ? undefined : "Requires an admin account"}
          >
            {sync.isPending ? "Comparing…" : "Compare with gateway"}
          </button>
        </div>

        {!isAdmin && (
          <p className="text-xs text-muted">
            Reconciliation requires an admin account.
          </p>
        )}
        {sync.error != null && <ErrorBox error={sync.error} />}
        {sync.data && (
          <>
            <div className="flex items-center gap-3 flex-wrap mb-3">
              <Badge tone={sync.data.catalogue_reachable ? "ok" : "bad"}>
                {sync.data.catalogue_reachable
                  ? "gateway reachable"
                  : "gateway unreachable"}
              </Badge>
              <span className="text-sm text-slate-300 tabular-nums">
                {sync.data.cameras_in_catalogue} in catalogue
              </span>
              <span className="text-xs text-muted tabular-nums">
                {sync.data.unchanged} unchanged
              </span>
            </div>

            <div className="grid gap-3 md:grid-cols-3">
              <DiffList
                label="Only in the catalogue"
                hint="Present on the gateway, absent from the registry"
                tone="ok"
                items={sync.data.added}
              />
              <DiffList
                label="Only in the registry"
                hint="We hold a record the gateway no longer lists"
                tone="warn"
                items={sync.data.removed}
              />
              <DiffList
                label="Properties changed"
                hint="Declared codec, resolution or frame rate differs"
                tone="warn"
                items={sync.data.properties_changed}
              />
            </div>

            {sync.data.note && (
              <p className="text-xs text-muted mt-3">{sync.data.note}</p>
            )}
          </>
        )}
      </section>

      {/* ---------------------------------------------------------- onboarding */}
      <section className="panel p-4">
        <div className="flex items-start justify-between gap-4 flex-wrap mb-3">
          <div>
            <h2 className="font-medium">Bulk camera onboarding</h2>
            <p className="text-xs text-muted max-w-xl mt-0.5">
              Upload a departmental camera list as CSV. Rows are validated one at a
              time against the same rules the seed script uses, so good rows land and
              bad ones come back with the line number and the reason — a spreadsheet
              with two bad rows should not be an all-or-nothing rejection.
            </p>
          </div>
        </div>

        {!isAdmin && (
          <p className="text-xs text-muted">Onboarding requires an admin account.</p>
        )}

        {isAdmin && (
          <>
            <div className="flex items-center gap-3 flex-wrap">
              <input
                ref={fileRef}
                type="file"
                accept=".csv,text/csv"
                className="input"
                onChange={(e) => setChosen(e.target.files?.[0] ?? null)}
              />
              <button
                className="btn btn-primary"
                disabled={!chosen || bulkImport.isPending}
                onClick={() => chosen && bulkImport.mutate(chosen)}
              >
                {bulkImport.isPending ? "Importing…" : "Import cameras"}
              </button>
            </div>
            <p className="text-[10px] text-muted mt-2">
              Expected columns:{" "}
              <span className="mono">
                camera_ref, location_text, lat, lon, geom_source,
                confidence_radius_m, resolved_by, resolved_at
              </span>
              . A camera already placed by manual survey keeps that coordinate — someone
              stood at it, and a spreadsheet did not.
            </p>

            {bulkImport.error != null && (
              <div className="mt-3">
                <ErrorBox error={bulkImport.error} />
              </div>
            )}
            {bulkImport.data && <ImportSummary result={bulkImport.data} />}
          </>
        )}
      </section>
    </div>
  );
}

function ImportSummary({ result }: { result: BulkImportResult }) {
  const clean = result.rejected === 0;
  return (
    <div className="mt-4">
      <div className="flex items-center gap-3 flex-wrap mb-3">
        <Badge tone={clean ? "ok" : "warn"}>
          {result.accepted} of {result.rows_read} accepted
        </Badge>
        {result.rejected > 0 && <Badge tone="bad">{result.rejected} rejected</Badge>}
        <span className="text-xs text-muted tabular-nums">
          {result.created} created · {result.updated} updated
          {result.unset_coordinates > 0 && (
            <> · {result.unset_coordinates} without a coordinate</>
          )}
        </span>
      </div>

      {result.note && <p className="text-xs text-warn mb-3">{result.note}</p>}

      {result.rejections.length > 0 && (
        <div className="overflow-x-auto rounded border border-edge">
          <table className="w-full text-sm">
            <thead className="bg-ink-700 text-muted text-[11px] uppercase tracking-wide">
              <tr>
                <th className="text-right px-3 py-2">Line</th>
                <th className="text-left px-3 py-2">Camera</th>
                <th className="text-left px-3 py-2">Why it was not imported</th>
              </tr>
            </thead>
            <tbody>
              {result.rejections.map((r) => (
                <tr key={r.line} className="border-t border-edge">
                  <td className="px-3 py-2 text-right tabular-nums">{r.line}</td>
                  <td className="px-3 py-2 mono">{r.camera_ref ?? "—"}</td>
                  <td className="px-3 py-2 text-xs text-slate-300">
                    {r.reasons.map((reason, i) => (
                      <div key={i}>{reason}</div>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function DiffList({
  label,
  hint,
  tone,
  items,
}: {
  label: string;
  hint: string;
  tone: "ok" | "warn";
  items: string[];
}) {
  return (
    <div className="rounded border border-edge bg-ink-900 p-3">
      <div className="flex items-center gap-2 mb-1">
        <Badge tone={items.length ? tone : "muted"}>{items.length}</Badge>
        <span className="text-xs text-slate-300">{label}</span>
      </div>
      <div className="text-[10px] text-muted mb-2">{hint}</div>
      {items.length === 0 ? (
        <div className="text-xs text-muted">None</div>
      ) : (
        <div className="mono text-xs text-slate-300 max-h-40 overflow-auto">
          {items.map((i) => (
            <div key={i}>{i}</div>
          ))}
        </div>
      )}
    </div>
  );
}
