/**
 * Observability dashboard.
 *
 * Form was chosen per measure before any colour was picked:
 *
 * - Headline numbers (requests, error rate, p95, ingestion lag) are **stat
 *   tiles**, not charts. A single value over one window has no shape to plot.
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
                type="monotone"
                dataKey="p50"
                name="p50 latency"
                stroke={SERIES.p50}
                {...lineProps}
              />
              <Line
                type="monotone"
                dataKey="p95"
                name="p95 latency"
                stroke={SERIES.p95}
                {...lineProps}
              />
              <Line
                type="monotone"
                dataKey="ttft"
                name="p50 TTFT"
                stroke={SERIES.ttft}
                {...lineProps}
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
              <Bar dataKey="success" name="success" stackId="s" fill={STATUS.success} />
              <Bar
                dataKey="cancellations"
                name="cancelled"
                stackId="s"
                fill={STATUS.cancelled}
              />
              <Bar
                dataKey="errors"
                name="error"
                stackId="s"
                fill={STATUS.error}
                radius={[4, 4, 0, 0]}
              />
            </BarChart>
          </ResponsiveContainer>
        </figure>
      </div>

      <section className="panel">
        <h2 className="panel__title">By provider and model</h2>
        <table className="table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Model</th>
              <th className="num">Requests</th>
              <th className="num">Errors</th>
              <th className="num">p50 ms</th>
              <th className="num">p95 ms</th>
              <th className="num">Tokens in</th>
              <th className="num">Tokens out</th>
            </tr>
          </thead>
          <tbody>
            {(data?.providers ?? []).map((row) => (
              <tr key={`${row.provider}/${row.model}`}>
                <td>{row.provider}</td>
                <td>{row.model}</td>
                <td className="num">{row.requests}</td>
                <td className="num">{row.errors}</td>
                <td className="num">{row.p50_latency_ms ?? "—"}</td>
                <td className="num">{row.p95_latency_ms ?? "—"}</td>
                <td className="num">{row.input_tokens}</td>
                <td className="num">{row.output_tokens}</td>
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

      <section className="panel">
        <h2 className="panel__title">Recent errors</h2>
        <table className="table">
          <thead>
            <tr>
              <th>When</th>
              <th>Model</th>
              <th>Type</th>
              <th>Message</th>
              <th className="num">Latency</th>
            </tr>
          </thead>
          <tbody>
            {(errors ?? []).map((row) => (
              <tr key={row.id}>
                <td className="mono">{new Date(row.started_at).toLocaleTimeString()}</td>
                <td>{row.model}</td>
                <td>{row.error_type ?? "—"}</td>
                <td className="truncate">{row.error_message ?? "—"}</td>
                <td className="num">{formatMs(row.latency_ms)}</td>
              </tr>
            ))}
            {errors?.length === 0 && (
              <tr>
                <td colSpan={5} className="table__empty">
                  No errors recorded. Try the <code>mock-error</code> model.
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
  dot: false,
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

function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value} ms`;
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
