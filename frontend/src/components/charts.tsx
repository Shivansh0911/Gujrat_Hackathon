/**
 * Small SVG charts, written here rather than pulled from a library.
 *
 * Recharts or Chart.js would do this and would be licence-clean. They are not here
 * because the dashboard needs four shapes — a bar series, a sparkline, a horizontal
 * bar list and a stat tile — and each is a dozen lines of SVG. A charting library's
 * value is its axis logic, its animation and its interaction model; this console wants
 * none of those enough to justify 50-100 kB on a cold load over a mobile connection,
 * which is how a judge will open it between sessions.
 *
 * The rules all four follow, which are the ones a chart library usually gets right and
 * hand-rolled charts usually get wrong:
 *
 *   Colour comes from the theme tokens, never from a literal. So every chart is
 *   correct in both themes without a second palette, and the map-recolour class of bug
 *   cannot recur here.
 *
 *   Figures are tabular. A value that changes must not shift its neighbours.
 *
 *   Every chart is labelled for a screen reader and degrades to a table-like summary,
 *   because an SVG with no accessible name is a blank to anyone not looking at it.
 *
 *   An empty series renders an empty state, not an axis with nothing on it. "No data
 *   yet" and "a flat line at zero" are different findings.
 */

import type { ReactNode } from "react";

/** Theme-token colour for a series. Never a hex literal. */
export const SERIES = {
  accent: "rgb(var(--accent))",
  ok: "rgb(var(--ok))",
  warn: "rgb(var(--warn))",
  bad: "rgb(var(--bad))",
  muted: "rgb(var(--muted))",
  confirmed: "rgb(var(--tier-confirmed))",
  probable: "rgb(var(--tier-probable))",
  candidate: "rgb(var(--tier-candidate))",
} as const;

export type SeriesColour = keyof typeof SERIES;

function EmptyChart({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center h-full min-h-[4rem] text-muted t-small border border-dashed border-edge rounded">
      {label}
    </div>
  );
}

/** A value with its unit, set so the digits cannot jitter. */
export function Stat({
  label,
  value,
  unit,
  hint,
  tone = "accent",
  trend,
}: {
  label: string;
  value: number | string | null | undefined;
  unit?: string;
  hint?: string;
  tone?: SeriesColour;
  /** Change against the baseline, as a fraction. Rendered with a direction glyph. */
  trend?: number | null;
}) {
  const missing = value === null || value === undefined;
  return (
    <div className="panel p-3 flex flex-col gap-0.5">
      <span className="t-micro uppercase tracking-wider text-muted">{label}</span>
      <span className="flex items-baseline gap-1.5">
        <span
          className="text-2xl font-semibold num tracking-tight"
          style={{ color: missing ? "rgb(var(--muted))" : SERIES[tone] }}
        >
          {/* An em dash rather than 0. "Not measured" and "measured as zero" are
              different facts and must not look identical. */}
          {missing ? "—" : typeof value === "number" ? value.toLocaleString("en-IN") : value}
        </span>
        {unit ? <span className="t-small text-fg3">{unit}</span> : null}
        {typeof trend === "number" ? (
          <span
            className="t-micro num"
            style={{ color: trend > 0 ? SERIES.warn : SERIES.ok }}
            title="against the same hour last week"
          >
            {trend > 0 ? "▲" : "▼"} {Math.abs(trend * 100).toFixed(0)}%
          </span>
        ) : null}
      </span>
      {hint ? <span className="t-micro text-fg3">{hint}</span> : null}
    </div>
  );
}

/** A column series over time. */
export function BarSeries({
  data,
  label,
  colour = "accent",
  height = 120,
  formatX,
}: {
  data: { x: string; y: number }[];
  label: string;
  colour?: SeriesColour;
  height?: number;
  formatX?: (x: string) => string;
}) {
  if (!data.length) return <EmptyChart label={`No ${label.toLowerCase()} in this window`} />;

  const max = Math.max(...data.map((d) => d.y), 1);
  // A minimum column count, so a window holding one or two buckets renders one or
  // two columns rather than stretching them across the whole width. Without it a
  // single bucket fills the chart and reads as "everything", which is the opposite
  // of what one observation means.
  const columns = Math.max(data.length, 12);
  const width = Math.max(columns * 14, 180);
  const barWidth = Math.max(width / columns - 3, 2);

  return (
    <figure className="w-full">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="w-full"
        style={{ height }}
        role="img"
        aria-label={`${label}. ${data.length} buckets, peak ${max}.`}
      >
        {/* A faint baseline rather than a full grid: the comparison is between bars,
            and gridlines at this size are noise. */}
        <line
          x1="0"
          y1={height - 0.5}
          x2={width}
          y2={height - 0.5}
          stroke="rgb(var(--edge))"
          strokeWidth="1"
        />
        {data.map((d, i) => {
          const h = (d.y / max) * (height - 4);
          return (
            <rect
              key={d.x}
              x={i * (width / columns)}
              y={height - h}
              width={barWidth}
              height={h}
              fill={SERIES[colour]}
              opacity={0.85}
            >
              <title>{`${formatX ? formatX(d.x) : d.x}: ${d.y.toLocaleString("en-IN")}`}</title>
            </rect>
          );
        })}
      </svg>
      <figcaption className="t-micro text-muted mt-1 flex justify-between">
        <span>{formatX ? formatX(data[0].x) : data[0].x}</span>
        <span className="num">peak {max.toLocaleString("en-IN")}</span>
        <span>{formatX ? formatX(data[data.length - 1].x) : data[data.length - 1].x}</span>
      </figcaption>
    </figure>
  );
}

/** A compact trend line with an emphasised endpoint. */
export function Sparkline({
  values,
  label,
  colour = "accent",
  width = 120,
  height = 28,
}: {
  values: number[];
  label: string;
  colour?: SeriesColour;
  width?: number;
  height?: number;
}) {
  if (values.length < 2) return <span className="t-micro text-muted">not enough data</span>;

  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const points = values.map((v, i) => [i * step, height - ((v - min) / span) * height] as const);
  const path = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const [lastX, lastY] = points[points.length - 1];

  return (
    <svg
      width={width}
      height={height}
      role="img"
      aria-label={`${label}: ${values.length} points, latest ${values[values.length - 1]}`}
    >
      {/* An area fill under the line reads the magnitude as well as the shape. */}
      <path
        d={`${path} L${width},${height} L0,${height} Z`}
        fill={SERIES[colour]}
        opacity={0.12}
      />
      <path d={path} fill="none" stroke={SERIES[colour]} strokeWidth="1.5" />
      <circle cx={lastX} cy={lastY} r="2.5" fill={SERIES[colour]} />
    </svg>
  );
}

/** A ranked horizontal bar list — the right shape for "busiest cameras". */
export function BarList({
  rows,
  label,
  colour = "accent",
  formatValue,
}: {
  rows: { key: string; label: string; value: number; badge?: ReactNode }[];
  label: string;
  colour?: SeriesColour;
  formatValue?: (v: number) => string;
}) {
  if (!rows.length) return <EmptyChart label={`No ${label.toLowerCase()} yet`} />;
  const max = Math.max(...rows.map((r) => r.value), 1);

  return (
    <ul className="flex flex-col gap-1" aria-label={label}>
      {rows.map((row) => (
        <li key={row.key} className="relative">
          {/* The bar is a background behind the label rather than beside it, which
              keeps the text left-aligned and scannable at a glance. */}
          <div
            className="absolute inset-y-0 left-0 rounded-sm"
            style={{
              width: `${(row.value / max) * 100}%`,
              background: SERIES[colour],
              opacity: 0.14,
            }}
            aria-hidden="true"
          />
          <div className="relative flex items-center justify-between gap-2 px-1.5 py-1">
            <span className="t-small truncate flex items-center gap-1.5">
              {row.label}
              {row.badge}
            </span>
            <span className="t-small num text-fg2">
              {formatValue ? formatValue(row.value) : row.value.toLocaleString("en-IN")}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}

/** A proportional bar for a class mix or a tier breakdown. */
export function StackedBar({
  segments,
  label,
}: {
  segments: { key: string; value: number; colour: SeriesColour; label: string }[];
  label: string;
}) {
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  if (!total) return <EmptyChart label={`No ${label.toLowerCase()} yet`} />;

  return (
    <div>
      <div
        className="flex h-3 rounded overflow-hidden border border-edge"
        role="img"
        aria-label={`${label}: ${segments.map((s) => `${s.label} ${s.value}`).join(", ")}`}
      >
        {segments
          .filter((s) => s.value > 0)
          .map((s) => (
            <div
              key={s.key}
              style={{ width: `${(s.value / total) * 100}%`, background: SERIES[s.colour] }}
              title={`${s.label}: ${s.value} (${((s.value / total) * 100).toFixed(0)}%)`}
            />
          ))}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 mt-1.5">
        {segments
          .filter((s) => s.value > 0)
          .map((s) => (
            <span key={s.key} className="t-micro text-fg3 flex items-center gap-1">
              <span
                className="inline-block w-2 h-2 rounded-sm"
                style={{ background: SERIES[s.colour] }}
                aria-hidden="true"
              />
              {s.label}
              <span className="num">{((s.value / total) * 100).toFixed(0)}%</span>
            </span>
          ))}
      </div>
    </div>
  );
}

/** A skeleton shaped like the content it replaces, not a spinner. */
export function SkeletonChart({ height = 120 }: { height?: number }) {
  return <div className="skeleton w-full" style={{ height }} aria-hidden="true" />;
}

export function SkeletonStat() {
  return (
    <div className="panel p-3 flex flex-col gap-2">
      <div className="skeleton h-2.5 w-20" />
      <div className="skeleton h-7 w-16" />
    </div>
  );
}

export function SkeletonRows({ rows = 6 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-1" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton h-6 w-full" />
      ))}
    </div>
  );
}
