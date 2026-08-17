/**
 * Per-call log browser.
 *
 * The dashboard could answer "how is the system behaving" in aggregate and "what
 * broke" for failures, and had no answer for "show me that one call". In a system
 * whose product *is* the inference log, the individual row is the thing, and it
 * was reachable only by opening psql.
 *
 * A row expands rather than navigating: the previews are the reason to look, and
 * losing the surrounding rows to read one of them makes comparison -- which is
 * the actual task -- impossible.
 */

import { useState } from "react";
import { type LogRow, useLogs } from "../api/metrics";

const STATUSES = ["", "success", "error", "cancelled"] as const;

export function RecentCalls({ providers }: { providers: string[] }) {
  const [provider, setProvider] = useState("");
  const [status, setStatus] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  const { data, isLoading, fetchNextPage, hasNextPage, isFetchingNextPage } = useLogs({
    provider: provider || undefined,
    status: status || undefined,
  });

  const rows = data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <section className="panel">
      <div className="calls__head">
        <h2 className="panel__title">Recent calls</h2>
        {/* Filters in one row above the table. */}
        <div className="calls__filters">
          <select
            className="calls__select"
            value={provider}
            onChange={(event) => setProvider(event.target.value)}
            aria-label="Filter by provider"
          >
            <option value="">All providers</option>
            {providers.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <select
            className="calls__select"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            aria-label="Filter by status"
          >
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {value === "" ? "All outcomes" : value}
              </option>
            ))}
          </select>
        </div>
      </div>

      <table className="table">
        <thead>
          <tr>
            <th>When</th>
            <th>Provider</th>
            <th>Model</th>
            <th>Status</th>
            <th className="num">Latency</th>
            <th className="num">TTFT</th>
            <th className="num">Tokens</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <Row
              key={row.id}
              row={row}
              isOpen={expanded === row.id}
              onToggle={() => setExpanded(expanded === row.id ? null : row.id)}
            />
          ))}
          {!isLoading && rows.length === 0 && (
            <tr>
              <td className="table__empty" colSpan={7}>
                No calls match these filters.
              </td>
            </tr>
          )}
          {isLoading && (
            <tr>
              <td className="table__empty" colSpan={7}>
                Loading…
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {hasNextPage && (
        <button
          className="calls__more"
          type="button"
          onClick={() => fetchNextPage()}
          disabled={isFetchingNextPage}
        >
          {isFetchingNextPage ? "Loading…" : "Load older"}
        </button>
      )}
    </section>
  );
}

function Row({
  row,
  isOpen,
  onToggle,
}: {
  row: LogRow;
  isOpen: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr className="calls__row" onClick={onToggle}>
        {/* A real button, so the row is reachable by keyboard and announces its
            state, rather than a click handler on a <tr> that a screen reader has
            no way to describe. */}
        <td className="mono">
          <button className="calls__toggle" type="button" aria-expanded={isOpen}>
            <span aria-hidden="true">{isOpen ? "▾" : "▸"}</span>{" "}
            {new Date(row.started_at).toLocaleTimeString()}
          </button>
        </td>
        <td>{row.provider}</td>
        <td className="truncate">{row.model}</td>
        <td>
          <span className={`badge badge--${row.status}`}>{row.status}</span>
        </td>
        <td className="num">{ms(row.latency_ms)}</td>
        <td className="num">{ms(row.ttft_ms)}</td>
        <td className="num mono">
          {row.input_tokens ?? 0}/{row.output_tokens ?? 0}
        </td>
      </tr>
      {isOpen && (
        <tr>
          <td colSpan={7} className="calls__detail">
            <dl className="calls__meta">
              <Meta label="Streamed">{row.streamed ? "yes" : "no"}</Meta>
              <Meta label="Finish reason">{row.finish_reason ?? "—"}</Meta>
              <Meta label="Conversation">
                {row.conversation_id ? (
                  // Errors and cancellations often have no conversation at all,
                  // which is worth showing rather than rendering a dead link.
                  <a href={`/c/${row.conversation_id}`}>open</a>
                ) : (
                  "—"
                )}
              </Meta>
              <Meta label="Log id">
                <span className="mono">{row.id}</span>
              </Meta>
            </dl>

            {row.error_message && (
              <p className="calls__error" role="alert">
                <strong>{row.error_type ?? "error"}:</strong> {row.error_message}
              </p>
            )}

            <Preview label="Input" text={row.input_preview} />
            <Preview label="Output" text={row.output_preview} />
            <p className="calls__note">
              Previews are redacted and truncated before the event leaves the API
              process, so this is the stored record — not the raw text.
            </p>
          </td>
        </tr>
      )}
    </>
  );
}

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="calls__metaitem">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Preview({ label, text }: { label: string; text: string | null }) {
  return (
    <div className="calls__preview">
      <h3>{label}</h3>
      {/* pre-wrap: a preview is the user's own text and its line breaks carry
          meaning. Not markdown-rendered -- this is a record of what was stored,
          and formatting it would obscure exactly what redaction did. */}
      <pre>{text || "(empty)"}</pre>
    </div>
  );
}

function ms(value: number | null) {
  if (value === null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value} ms`;
}
