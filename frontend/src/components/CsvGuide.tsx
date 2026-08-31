import { useState } from "react";

/**
 * What to put in the spreadsheet, written for the person who has to make it.
 *
 * The previous version of this was one line naming eight columns in a monospace
 * run-on. That is a reference for somebody who already knows the shape of the data.
 * The people who actually assemble these lists are department clerks working from an
 * asset register, and for them `geom_source` and `confidence_radius_m` are not
 * self-explanatory -- the second in particular reads as a camera setting rather than a
 * statement about how well the position is known, which is exactly the misreading that
 * puts a fabricated coordinate into the registry.
 *
 * So: plain language, one row per column, required ones marked, a worked example, and
 * a template that opens in Excel. Collapsed by default, because the operators who
 * already know this should not have to scroll past it to reach the controls.
 */

type Column = {
  name: string;
  required: boolean;
  plain: string;
  example: string;
};

const COLUMNS: Column[] = [
  {
    name: "camera_ref",
    required: true,
    plain:
      "The department's own identifier for the camera. Must be unique — a row whose reference already exists is rejected rather than silently overwriting a camera somebody else onboarded.",
    example: "AMC-TRF-014",
  },
  {
    name: "name",
    required: true,
    plain:
      "What an operator would call it. This appears on the map and in evidence exports, so write the junction, not an asset code.",
    example: "Paldi Cross Roads, west approach",
  },
  {
    name: "location_text",
    required: false,
    plain:
      "The address or landmark in words. It is what the district is worked out from, so a real place name is worth more here than a plot number.",
    example: "Paldi, Ahmedabad",
  },
  {
    name: "lat / lon",
    required: false,
    plain:
      "Decimal degrees — 23.0126 and 72.5714, not degrees-minutes-seconds. Leave both blank if nobody has established where the camera actually is. A blank is honest; a guess is not, and it quietly corrupts every route the camera appears in.",
    example: "23.0126 / 72.5714",
  },
  {
    name: "geom_source",
    required: false,
    plain:
      "How the position was obtained. SURVEY if somebody stood at the camera, GEOCODED if it came from looking up the address, DISTRICT_CENTROID if it is only known to be somewhere in that district, UNSET if there is no position at all.",
    example: "SURVEY",
  },
  {
    name: "confidence_radius_m",
    required: false,
    plain:
      "How far the true position could be from the coordinate, in metres. A surveyed camera might be 10; an address lookup 500; a district centroid 5000. This is not a camera setting — it is how much the coordinate can be trusted, and the Coverage page reports on it.",
    example: "10",
  },
  {
    name: "resolved_by / resolved_at",
    required: false,
    plain:
      "Who established the position, and when (YYYY-MM-DD). Provenance, so a coordinate can later be questioned by a person instead of treated as fact.",
    example: "R. Patel / 2026-08-14",
  },
];

const TEMPLATE_ROWS = [
  "camera_ref,name,location_text,lat,lon,geom_source,confidence_radius_m,resolved_by,resolved_at",
  'AMC-TRF-014,"Paldi Cross Roads, west approach","Paldi, Ahmedabad",23.0126,72.5714,SURVEY,10,R. Patel,2026-08-14',
  'AMC-TRF-015,"Nehru Bridge, north end","Ellisbridge, Ahmedabad",23.0225,72.5714,GEOCODED,500,,',
  'RJT-TRF-002,"Bus Port main gate","Rajkot",,,UNSET,,,',
];

export default function CsvGuide() {
  const [open, setOpen] = useState(false);

  function downloadTemplate() {
    // A BOM, so Excel opens the file as UTF-8 instead of mangling any non-ASCII
    // place name into mojibake. Gujarati and Hindi names are the normal case here.
    const blob = new Blob(["﻿" + TEMPLATE_ROWS.join("\r\n") + "\r\n"], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "setu-camera-import-template.csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="mt-3">
      <div className="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          className="btn text-xs py-1"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? "Hide instructions" : "How to prepare the file"}
        </button>
        <button type="button" className="btn text-xs py-1" onClick={downloadTemplate}>
          Download CSV template
        </button>
        <span className="text-[10px] text-muted">
          Open it in Excel, replace the example rows, save as CSV.
        </span>
      </div>

      {open && (
        <div className="mt-3 space-y-3">
          <p className="text-[11px] text-muted leading-snug max-w-3xl">
            One camera per row, with a header row of column names on top. Only{" "}
            <span className="mono">camera_ref</span> and{" "}
            <span className="mono">name</span> are required. A list with no coordinates
            yet is still worth importing: a camera SETU knows about but cannot place is
            reported as a coverage gap somebody can act on, whereas a camera nobody has
            entered is simply invisible.
          </p>

          <div className="overflow-x-auto">
            <table className="text-[11px] w-full min-w-[34rem]">
              <thead>
                <tr className="text-muted text-left">
                  <th className="py-1 pr-3 font-medium">Column</th>
                  <th className="py-1 pr-3 font-medium">What to put in it</th>
                  <th className="py-1 font-medium">Example</th>
                </tr>
              </thead>
              <tbody>
                {COLUMNS.map((c) => (
                  <tr key={c.name} className="border-t border-edge align-top">
                    <td className="py-1.5 pr-3 whitespace-nowrap">
                      <span className="mono text-slate-200">{c.name}</span>
                      {c.required && (
                        <div className="text-[9px] uppercase tracking-wide text-warn mt-0.5">
                          required
                        </div>
                      )}
                    </td>
                    <td className="py-1.5 pr-3 text-muted leading-snug">{c.plain}</td>
                    <td className="py-1.5 mono text-muted whitespace-nowrap">
                      {c.example}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="bg-ink-900 rounded p-3 space-y-1.5">
            <div className="text-[11px] font-medium text-slate-200">
              What happens when a row is wrong
            </div>
            <p className="text-[11px] text-muted leading-snug">
              Nothing is all-or-nothing. Every row is checked on its own: the good ones
              are imported, and each bad one comes back with its line number and the
              reason. Fix those lines and upload the file again.
            </p>
            <p className="text-[11px] text-muted leading-snug">
              Re-uploading a row that already imported is safe — it is matched on{" "}
              <span className="mono">camera_ref</span> and reported as a duplicate
              rather than added twice. And a camera already placed by manual survey
              keeps that coordinate: somebody stood at it, and a spreadsheet did not.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
