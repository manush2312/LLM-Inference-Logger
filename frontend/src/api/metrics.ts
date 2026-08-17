import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

export interface TimeBucket {
  bucket: string;
  requests: number;
  errors: number;
  cancellations: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  p50_ttft_ms: number | null;
}

export interface ProviderBreakdown {
  provider: string;
  model: string;
  requests: number;
  errors: number;
  cancellations: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  p95_ttft_ms: number | null;
  input_tokens: number;
  output_tokens: number;
}

export interface Totals {
  requests: number;
  errors: number;
  cancellations: number;
  error_rate: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  p95_ttft_ms: number | null;
  input_tokens: number;
  output_tokens: number;
}

export interface IngestionHealth {
  last_ingested_at: string | null;
  lag_seconds: number | null;
  logs_total: number;
  raw_events_total: number;
  raw_events_pending: number;
  raw_events_failed: number;
  is_stalled: boolean;
}

export interface MetricsSummary {
  window_minutes: number;
  interval: string;
  totals: Totals;
  series: TimeBucket[];
  providers: ProviderBreakdown[];
  ingestion: IngestionHealth;
}

/** One inference, in full. Wider than ErrorLog: this view answers questions
 *  about cost and latency attribution, not just about what broke. */
export interface LogRow {
  id: string;
  conversation_id: string | null;
  message_id: string | null;
  provider: string;
  model: string;
  status: string;
  streamed: boolean;
  started_at: string;
  completed_at: string;
  ingested_at: string;
  latency_ms: number | null;
  ttft_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  finish_reason: string | null;
  error_type: string | null;
  error_message: string | null;
  input_preview: string | null;
  output_preview: string | null;
  raw_metadata: Record<string, unknown>;
}

export interface LogPage {
  items: LogRow[];
  next_before: string | null;
  next_before_id: string | null;
}

export interface ErrorLog {
  id: string;
  provider: string;
  model: string;
  status: string;
  started_at: string;
  latency_ms: number | null;
  ttft_ms: number | null;
  error_type: string | null;
  error_message: string | null;
  input_preview: string | null;
  output_preview: string | null;
  raw_metadata: Record<string, unknown>;
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`);
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return (await response.json()) as T;
}

export function useMetrics(windowMinutes: number, interval: string) {
  return useQuery({
    queryKey: ["metrics", windowMinutes, interval],
    queryFn: () =>
      get<MetricsSummary>(
        `/metrics/summary?window_minutes=${windowMinutes}&interval=${interval}`,
      ),
    // Ingestion runs asynchronously, so the dashboard is never the freshest
    // view of the chat path. Polling keeps lag visible as it grows rather than
    // only on manual reload.
    refetchInterval: 5000,
  });
}

export function useRecentErrors() {
  return useQuery({
    queryKey: ["metrics", "errors"],
    queryFn: () => get<ErrorLog[]>("/metrics/errors?limit=15"),
    refetchInterval: 10_000,
  });
}

export interface LogFilters {
  provider?: string;
  status?: string;
}

/**
 * Per-call log browsing, paginated by keyset.
 *
 * `useInfiniteQuery` rather than `useQuery` because the cursor is a pair --
 * (started_at, id) -- and the server decides when there is no next page by
 * returning nulls. Threading that through manual state would duplicate what the
 * infinite-query cursor protocol already does.
 *
 * Not polled. The aggregate panels refresh every 5s because they are a live
 * view; this is a reader that someone is scrolling, and repointing page 1 under
 * them mid-read would be hostile.
 */
export function useLogs(filters: LogFilters, pageSize = 25) {
  return useInfiniteQuery({
    queryKey: ["metrics", "logs", filters, pageSize],
    initialPageParam: null as { before: string; before_id: string } | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: String(pageSize) });
      if (filters.provider) params.set("provider", filters.provider);
      if (filters.status) params.set("status", filters.status);
      if (pageParam) {
        params.set("before", pageParam.before);
        params.set("before_id", pageParam.before_id);
      }
      return get<LogPage>(`/metrics/logs?${params.toString()}`);
    },
    getNextPageParam: (last) =>
      last.next_before && last.next_before_id
        ? { before: last.next_before, before_id: last.next_before_id }
        : undefined,
  });
}
