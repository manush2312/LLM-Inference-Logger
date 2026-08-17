/**
 * Observability dashboard.
 *
 * Form was chosen per measure before any colour was picked:
 *
 * - Headline numbers (requests, error rate, p95, ingestion lag) are **stat
 *   tiles**, not charts. A single value over one window has no shape to plot.
 * - Interpolation is `linear`, never `monotone`. A spline through sparse samples
 *   draws a confident curve between points that were never measured: three
 *   buckets rendered as a smooth parabola, implying a rise and fall that did not
 *   happen. Straight segments between marked samples claim only what was sampled.
 * - Latency percentiles are a **line chart** -- change over time, three series,
 *   all in milliseconds so they share one axis. Two y-scales would be the
 *   single most common charting mistake; TTFT and total latency are the same
 *   unit, so they legitimately co-plot.
 * - Throughput is **stacked bars by outcome** -- magnitude over time, split by
 *   state. This uses the reserved status palette rather than series colours,
 *   because success/cancelled/error are states, not identities.
 * - Provider and error breakdowns are **tables**. Several measures across a
 *   handful of rows is a table's job, and a chart would obscure the numbers
 *   someone actually needs to read.
 */

import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Link } from "react-router-dom";
import { useMetrics, useRecentErrors, type TimeBucket } from "../api/metrics";
import { useReducedMotion } from "../hooks/useReducedMotion";
import { RecentCalls } from "../components/RecentCalls";
import "./Dashboard.css";

/**
 * Validated against this app's own dark surface (#171a21), not a reference one:
 * `validate_palette.js "#3987e5,#d95926,#199e70" --mode dark --surface #171a21`
 * — all six checks pass. Assigned in fixed slot order and never cycled, so a
 * series keeps its colour when the set changes.
 */
const SERIES = {
  p50: "var(--series-1)",
  p95: "var(--series-2)",
  ttft: "var(--series-3)",
} as const;

/** Reserved status palette. Always paired with a legend — never colour alone. */
const STATUS = {
  success: "var(--status-good)",
  cancelled: "var(--status-warning)",
  error: "var(--status-critical)",
} as const;

const WINDOWS = [
  { label: "15m", minutes: 15, interval: "minute" },
  { label: "1h", minutes: 60, interval: "minute" },
  { label: "24h", minutes: 1440, interval: "hour" },
  { label: "7d", minutes: 10080, interval: "hour" },
];

export function Dashboard() {
  const [windowIndex, setWindowIndex] = useState(1);
  const active = WINDOWS[windowIndex];
  const { data, isLoading, error } = useMetrics(active.minutes, active.interval);
  const { data: errors } = useRecentErrors();
  // Above the early return: hooks must run in the same order on every render, and
  // there is a `return` for the error state below. Honours the OS reduced-motion
  // preference for the mount tween, and is also what makes the charts capturable
  // headlessly -- see the hook for why.
  const animate = !useReducedMotion();

  if (error) {
    return (
      <section className="dash">
        <p className="chat__error">Could not load metrics. Is the API running?</p>
      </section>
    );
  }

  const totals = data?.totals;
  const ingestion = data?.ingestion;
  const series = (data?.series ?? []).map(toChartRow);

  return (
    <section className="dash">
      <header className="dash__header">
        <h1 className="dash__title">Inference metrics</h1>
        <div className="dash__filters">
          {/* Filters in one row above the charts. */}
          {WINDOWS.map((option, index) => (
            <button
              key={option.label}
              className={`chip${index === windowIndex ? " chip--active" : ""}`}
              onClick={() => setWindowIndex(index)}
            >
              {option.label}
            </button>
          ))}
          <Link className="btn btn--ghost" to="/">
            Back to chat
          </Link>
        </div>
      </header>

      {isLoading && !data && <p className="chat__hint">Loading metrics…</p>}

      <div className="tiles">
        <StatTile label="Requests" value={totals?.requests ?? 0} />
        <StatTile
          label="Error rate"
          value={formatPercent(totals?.error_rate)}
          tone={(totals?.error_rate ?? 0) > 0.05 ? "critical" : "neutral"}
        />
        <StatTile label="Cancelled" value={totals?.cancellations ?? 0} />
        <StatTile label="p95 latency" value={formatMs(totals?.p95_latency_ms)} />
        <StatTile label="p95 TTFT" value={formatMs(totals?.p95_ttft_ms)} />
        <StatTile
          label="Tokens (in / out)"
          value={`${totals?.input_tokens ?? 0} / ${totals?.output_tokens ?? 0}`}
        />
      </div>

      {/*
        The ingestion panel exists because of a real outage: a deleted Redis
        consumer group left the worker alive, passing its liveness probe, and
        ingesting nothing. Every chat-path panel looked perfectly healthy
        throughout. Lag is time since the last successful write, so it grows
        regardless of why ingestion stopped.
      */}
      <section
        className={`panel ingestion${ingestion?.is_stalled ? " ingestion--stalled" : ""}`}
      >
        <div className="ingestion__headline">
          <span className="panel__title">Ingestion pipeline</span>
          <span className={`pill pill--${ingestionTone(ingestion)}`}>
            {/* Icon + label, so the state never rests on colour alone. */}
            {ingestionIcon(ingestion)} {ingestionLabel(ingestion)}
          </span>
        </div>
        <dl className="ingestion__stats">
          <Stat label="Lag" value={formatLag(ingestion?.lag_seconds)} />
          <Stat label="Logs ingested" value={ingestion?.logs_total ?? 0} />
          <Stat label="Raw events" value={ingestion?.raw_events_total ?? 0} />
          <Stat label="Pending" value={ingestion?.raw_events_pending ?? 0} />
          <Stat
            label="Failed"
            value={ingestion?.raw_events_failed ?? 0}
            tone={(ingestion?.raw_events_failed ?? 0) > 0 ? "critical" : "neutral"}
          />
        </dl>
      </section>

      <div className="charts">
        <figure className="panel">
          <figcaption className="panel__title">
            Latency percentiles <span className="panel__unit">ms</span>
          </figcaption>
          <ResponsiveContainer width="100%" height={240}>
            <LineChart data={series} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--grid)" vertical={false} />
              <XAxis dataKey="label" {...axisProps} />
              {/* One axis. All three series are milliseconds. */}
              <YAxis {...axisProps} width={48} />
              <Tooltip {...tooltipProps} />
              <Legend {...legendProps} />
              <Line
                type="linear"
                dataKey="p50"
                name="p50 latency"
                stroke={SERIES.p50}
                {...lineProps}
                isAnimationActive={animate}
              />
              <Line
                type="linear"
                dataKey="p95"
                name="p95 latency"
                stroke={SERIES.p95}
                {...lineProps}
                isAnimationActive={animate}
              />
              <Line
                type="linear"
                dataKey="ttft"
                name="p50 TTFT"
                stroke={SERIES.ttft}
                {...lineProps}
                isAnimationActive={animate}
              />
            </LineChart>
          </ResponsiveContainer>
        </figure>

        <figure className="panel">
          <figcaption className="panel__title">
            Throughput by outcome <span className="panel__unit">requests</span>
          </figcaption>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={series} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--grid)" vertical={false} />
              <XAxis dataKey="label" {...axisProps} />
              <YAxis {...axisProps} width={48} allowDecimals={false} />
              <Tooltip {...tooltipProps} cursor={{ fill: "rgba(255,255,255,0.04)" }} />
              <Legend {...legendProps} />
              {/* 4px rounded data-end on the topmost segment only, anchored to
                  the baseline; 2px surface gap between stacked segments. */}
              <Bar dataKey="success" name="success" stackId="s" fill={STATUS.success} isAnimationActive={animate} />
              <Bar
                dataKey="cancellations"
                name="cancelled"
                stackId="s"
                fill={STATUS.cancelled}
                isAnimationActive={animate}
              />
              <Bar
                dataKey="errors"
                name="error"
                stackId="s"
                fill={STATUS.error}
                isAnimationActive={animate}
                radius={[4, 4, 0, 0]}
              />
            </BarChart>
          </ResponsiveContainer>
        </figure>
      </div>

      <section className="panel">
        <h2 className="panel__title">By provider and model</h2>
        {/* Ten flat columns of equally-weighted numbers was the readability
            problem: a real error rendered exactly as loudly as a zero, so there
            was nothing to catch the eye. Three changes fix it.

            Identity collapses into one cell (provider over model) rather than two
            columns -- it is one thing, and splitting it cost width that the
            numbers needed. Latency and tokens get grouped headers, so it is
            visible at a glance which numbers belong together. And zeros are
            muted while non-zero errors and cancellations take the reserved status
            colour, so the only coloured thing on screen is the thing worth
            looking at. */}
        <table className="table table--metrics">
          <colgroup>
            <col />
            <col span={3} className="group" />
            <col span={3} className="group group--alt" />
            <col className="group" />
          </colgroup>
          <thead>
            <tr className="table__grouprow">
              <th />
              <th colSpan={3}>Outcomes</th>
              <th colSpan={3}>Latency</th>
              <th>Tokens</th>
            </tr>
            <tr>
              <th>Provider / model</th>
              <th className="num">Req</th>
              <th className="num">Err</th>
              <th className="num">Cxl</th>
              <th className="num">p50</th>
              <th className="num">p95</th>
              {/* Per provider, because TTFT is the number that differs most
                  between them: a thinking model can spend tens of seconds
                  before its first visible token. */}
              <th className="num">p95 TTFT</th>
              <th className="num">in / out</th>
            </tr>
          </thead>
          <tbody>
            {[...(data?.providers ?? [])]
              // Busiest first. The provider carrying the traffic is the one the
              // reader is looking for, and alphabetical order buries it.
              .sort((a, b) => b.requests - a.requests)
              .map((row) => (
                <tr key={`${row.provider}/${row.model}`}>
                  <td className="ident">
                    <span className="ident__name">{row.provider}</span>
                    <span className="ident__sub" title={row.model}>
                      {row.model}
                    </span>
                  </td>
                  <td className="num">{row.requests}</td>
                  <Count value={row.errors} tone="error" />
                  <Count value={row.cancellations} tone="cancelled" />
                  <td className="num">{formatMs(row.p50_latency_ms)}</td>
                  <td className="num">{formatMs(row.p95_latency_ms)}</td>
                  <td className="num">{formatMs(row.p95_ttft_ms)}</td>
                  <td className="num mono">
                    {row.input_tokens}
                    <span className="sep"> / </span>
                    {row.output_tokens}
                  </td>
                </tr>
              ))}
            {data?.providers.length === 0 && (
              <tr>
                <td colSpan={8} className="table__empty">
                  No calls in this window.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {/* Deduped: the breakdown is keyed by provider *and* model, so a provider
          serving two models would otherwise appear twice in the filter. */}
      <RecentCalls
        providers={[...new Set((data?.providers ?? []).map((p) => p.provider))].sort()}
      />

      <section className="panel">
        <h2 className="panel__title">Recent errors</h2>
        {/* The message was the one field worth reading and the only one being
            truncated to a single clipped line. It now wraps and gets the width,
            paid for by dropping the redundant `Model` column -- the error type
            was already carrying more information than the model name. */}
        <table className="table table--errors">
          <thead>
            <tr>
              <th>When</th>
              <th>Provider / model</th>
              <th>Failure</th>
              <th className="num">Latency</th>
            </tr>
          </thead>
          <tbody>
            {(errors ?? []).map((row) => (
              <tr key={row.id}>
                {/* Relative time reads faster for "is this happening now?", which
                    is the actual question. The exact timestamp stays available on
                    hover rather than being dropped. */}
                <td className="mono nowrap" title={new Date(row.started_at).toLocaleString()}>
                  {formatAgo(row.started_at)}
                </td>
                <td className="ident">
                  <span className="ident__name">{row.provider}</span>
                  <span className="ident__sub" title={row.model}>
                    {row.model}
                  </span>
                </td>
                <td className="failure">
                  <span className="badge badge--error">{row.error_type ?? "error"}</span>
                  <span className="failure__msg">{row.error_message ?? "—"}</span>
                </td>
                <td className="num">{formatMs(row.latency_ms)}</td>
              </tr>
            ))}
            {errors?.length === 0 && (
              <tr>
                <td colSpan={4} className="table__empty">
                  No errors in this window.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </section>
  );
}

// --- Presentation helpers --------------------------------------------------

const axisProps = {
  stroke: "var(--axis)",
  tick: { fill: "var(--text-muted)", fontSize: 11 },
  tickLine: false,
  axisLine: { stroke: "var(--axis)" },
} as const;

const lineProps = {
  strokeWidth: 2,
  // Small dots mark where samples actually are. With `dot: false` and a sparse
  // series -- which is the normal case for a low-traffic window -- the line gave
  // no clue whether it was drawn through 3 points or 300.
  dot: { r: 2, strokeWidth: 0 },
  // Bigger than the mark, so the hover target is comfortable.
  activeDot: { r: 4, strokeWidth: 2, stroke: "var(--surface)" },
  connectNulls: false,
} as const;

const legendProps = {
  wrapperStyle: { fontSize: 12, color: "var(--text-muted)" },
  iconType: "plainline",
  iconSize: 12,
} as const;

const tooltipProps = {
  contentStyle: {
    background: "var(--surface-raised)",
    border: "1px solid var(--border)",
    borderRadius: 8,
    fontSize: 12,
  },
  labelStyle: { color: "var(--text)" },
  itemStyle: { color: "var(--text-muted)" },
} as const;

interface ChartRow {
  label: string;
  requests: number;
  success: number;
  errors: number;
  cancellations: number;
  p50: number | null;
  p95: number | null;
  ttft: number | null;
}

function toChartRow(bucket: TimeBucket): ChartRow {
  return {
    label: new Date(bucket.bucket).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    }),
    requests: bucket.requests,
    // Derived rather than served: successes are whatever is left once the two
    // non-success outcomes are removed, so the stack always sums to the total.
    success: bucket.requests - bucket.errors - bucket.cancellations,
    errors: bucket.errors,
    cancellations: bucket.cancellations,
    p50: bucket.p50_latency_ms,
    p95: bucket.p95_latency_ms,
    ttft: bucket.p50_ttft_ms,
  };
}

function StatTile({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  tone?: "neutral" | "critical";
}) {
  return (
    <div className="tile">
      <span className="tile__label">{label}</span>
      <span className={`tile__value tile__value--${tone}`}>{value}</span>
    </div>
  );
}

function Stat({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  tone?: "neutral" | "critical";
}) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd className={tone === "critical" ? "stat--critical" : undefined}>{value}</dd>
    </div>
  );
}

/** A count where zero is the boring case.
 *
 * Muting zeros is what makes a non-zero error visible at a glance -- a column of
 * equally-bright digits has no signal in it. The number stays rendered either
 * way, so the state is never carried by colour alone. */
function Count({ value, tone }: { value: number; tone: "error" | "cancelled" }) {
  if (value === 0) return <td className="num num--zero">0</td>;
  return <td className={`num num--${tone}`}>{value}</td>;
}

function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value} ms`;
}

/** "2m ago" rather than a wall-clock time.
 *
 * The question asked of an error list is almost always "is this still happening",
 * and a relative age answers it without the reader doing arithmetic against the
 * current time. */
function formatAgo(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

function formatPercent(value: number | undefined): string {
  if (value === undefined) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function formatLag(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

type Ingestion = { is_stalled: boolean; lag_seconds: number | null } | undefined;

function ingestionTone(ingestion: Ingestion): string {
  if (!ingestion || ingestion.lag_seconds === null) return "idle";
  return ingestion.is_stalled ? "critical" : "good";
}

function ingestionIcon(ingestion: Ingestion): string {
  const tone = ingestionTone(ingestion);
  return tone === "critical" ? "✕" : tone === "good" ? "✓" : "•";
}

function ingestionLabel(ingestion: Ingestion): string {
  const tone = ingestionTone(ingestion);
  if (tone === "idle") return "no events yet";
  return tone === "critical" ? "stalled" : "healthy";
}
