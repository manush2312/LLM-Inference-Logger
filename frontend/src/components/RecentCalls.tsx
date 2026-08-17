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
  // Defaulted rather than assumed present. The API always sends it, but backend
  // and frontend roll independently: during a deploy the new bundle can be served
  // while an old pod is still answering, and reaching into an absent object threw
  // -- taking the whole dashboard down with it, not just this row. A test now
  // pins that, having found it the hard way.
  const meta: Record<string, unknown> = row.raw_metadata ?? {};

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
            {/* Grouped rather than a flat list of fields. Timing, cost and
                identity answer different questions, and the panel was previously
                showing four fields while the row's most useful numbers -- the
                token split and anything the vendor returned -- were either
                unlabelled in the collapsed row or discarded at the API edge. */}
            <div className="calls__groups">
              <Group title="Timing">
                <Meta label="Latency">{ms(row.latency_ms)}</Meta>
                <Meta label="Time to first token">{ms(row.ttft_ms)}</Meta>
                {/* Generation speed excludes TTFT on purpose: waiting for a
                    provider to start is not the same as how fast it writes, and
                    averaging them together hides which one is slow. */}
                <Meta label="Generation">{tokensPerSecond(row)}</Meta>
                <Meta label="Ingestion lag">{lag(row)}</Meta>
              </Group>

              <Group title="Tokens">
                <Meta label="Input">{row.input_tokens ?? "—"}</Meta>
                <Meta label="Output">{row.output_tokens ?? "—"}</Meta>
                {/* Only present on reasoning models, and the only thing that
                    explains a three-word answer reporting hundreds of output
                    tokens. Billed, and invisible without this. */}
                {typeof meta.reasoning_tokens === "number" && (
                  <Meta label="of which reasoning">
                    {meta.reasoning_tokens as number}
                  </Meta>
                )}
                <Meta label="Total">{total(row)}</Meta>
              </Group>

              <Group title="Call">
                <Meta label="Streamed">{row.streamed ? "yes" : "no"}</Meta>
                <Meta label="Finish reason">{row.finish_reason ?? "—"}</Meta>
                <Meta label="Started">
                  {new Date(row.started_at).toLocaleString()}
                </Meta>
                <Meta label="Conversation">
                  {row.conversation_id ? (
                    // Errors and cancellations often have no conversation at all,
                    // which is worth showing rather than rendering a dead link.
                    <a href={`/c/${row.conversation_id}`}>open</a>
                  ) : (
                    "—"
                  )}
                </Meta>
              </Group>

              <Group title="Identifiers">
                <Meta label="Log id">
                  <code>{row.id}</code>
                </Meta>
                {/* What a provider's support will ask for. Captured all along and
                    previously thrown away before it reached the UI. */}
                {typeof meta.provider_request_id === "string" && (
                  <Meta label="Provider request id">
                    <code>{meta.provider_request_id as string}</code>
                  </Meta>
                )}
                {/* message_id is deliberately not shown. It is always NULL:
                    the assistant message is written after the call returns, so
                    publishing its id would race the worker into a foreign-key
                    violation (see Known limitations in the README). A row reading
                    "none" on every single call is noise pretending to be data. */}
              </Group>
            </div>

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

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="calls__group">
      <h3 className="calls__grouptitle">{title}</h3>
      <dl className="calls__meta">{children}</dl>
    </section>
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

/** Output tokens per second of *generation*, excluding the wait for first token. */
function tokensPerSecond(row: LogRow): string {
  const { output_tokens: out, latency_ms: total, ttft_ms: ttft } = row;
  if (!out || total === null || ttft === null) return "—";
  const generating = (total - ttft) / 1000;
  if (generating <= 0.05) return "—"; // too short to divide by meaningfully
  return `${(out / generating).toFixed(1)} tok/s`;
}

function total(row: LogRow): string {
  if (row.input_tokens === null && row.output_tokens === null) return "—";
  return String((row.input_tokens ?? 0) + (row.output_tokens ?? 0));
}

/** How long this row waited between the model finishing and the worker writing it. */
function lag(row: LogRow): string {
  const delta = new Date(row.ingested_at).getTime() - new Date(row.completed_at).getTime();
  if (Number.isNaN(delta)) return "—";
  return delta < 1000 ? `${Math.max(0, Math.round(delta))} ms` : `${(delta / 1000).toFixed(1)} s`;
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
