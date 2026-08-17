import { useQuery } from "@tanstack/react-query";

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
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
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
