/**
 * The per-call log browser.
 *
 * The behaviour worth locking down is not the table markup -- it is that this
 * view shows *successful* calls. The dashboard already had an errors panel; the
 * gap it filled was that an ordinary successful inference was invisible in the
 * UI and reachable only from psql.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RecentCalls } from "./RecentCalls";

const ROW = {
  id: "11111111-1111-1111-1111-111111111111",
  conversation_id: "22222222-2222-2222-2222-222222222222",
  message_id: "33333333-3333-3333-3333-333333333333",
  provider: "groq",
  model: "openai/gpt-oss-20b",
  status: "success",
  streamed: true,
  started_at: new Date().toISOString(),
  completed_at: new Date().toISOString(),
  ingested_at: new Date().toISOString(),
  latency_ms: 1054,
  ttft_ms: 430,
  input_tokens: 78,
  output_tokens: 451,
  finish_reason: "stop",
  error_type: null,
  error_message: null,
  input_preview: "My email is [REDACTED_EMAIL]",
  output_preview: "Hello, what's up?",
  raw_metadata: { reasoning_tokens: 26, provider_request_id: "chatcmpl-abc123" },
};

function mockFetch(page: Record<string, unknown>) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue({
    ok: true,
    json: async () => page,
  } as Response);
}

function renderCalls() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RecentCalls providers={["groq", "mock"]} />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.restoreAllMocks());

describe("recent calls", () => {
  it("lists successful calls, not only failures", async () => {
    mockFetch({ items: [ROW], next_before: null, next_before_id: null });
    renderCalls();
    expect(await screen.findByText("groq")).toBeInTheDocument();
    expect(screen.getByText("success")).toBeInTheDocument();
  });

  it("shows the redacted previews only once a row is expanded", async () => {
    mockFetch({ items: [ROW], next_before: null, next_before_id: null });
    renderCalls();

    const toggle = await screen.findByRole("button", { expanded: false });
    // Collapsed: the preview is the reason to look, but it must not be shown for
    // every row at once or the table stops being scannable.
    expect(screen.queryByText(/REDACTED_EMAIL/)).not.toBeInTheDocument();

    fireEvent.click(toggle);

    expect(await screen.findByText(/REDACTED_EMAIL/)).toBeInTheDocument();
    expect(screen.getByText("stop")).toBeInTheDocument();
  });

  it("surfaces reasoning tokens and the provider request id", async () => {
    // Both live in raw_metadata and were being discarded at the API edge.
    // reasoning_tokens is the only thing that explains a short answer reporting
    // hundreds of output tokens; provider_request_id is what a vendor's support
    // asks for.
    mockFetch({ items: [ROW], next_before: null, next_before_id: null });
    renderCalls();

    fireEvent.click(await screen.findByRole("button", { expanded: false }));

    expect(await screen.findByText("26")).toBeInTheDocument();
    expect(screen.getByText("chatcmpl-abc123")).toBeInTheDocument();
    // Derived, not served: generation speed excludes the wait for first token.
    expect(screen.getByText(/tok\/s/)).toBeInTheDocument();
  });

  it("does not crash when the API omits raw_metadata", async () => {
    // The rolling-deploy case: a new bundle served while an old pod still
    // answers. Reaching into an absent object took down the whole dashboard, not
    // just the row.
    const { raw_metadata: _omitted, ...withoutMeta } = ROW;
    mockFetch({ items: [withoutMeta], next_before: null, next_before_id: null });
    renderCalls();

    fireEvent.click(await screen.findByRole("button", { expanded: false }));

    expect(await screen.findByText(/REDACTED_EMAIL/)).toBeInTheDocument();
  });

  it("sends the compound cursor when loading older pages", async () => {
    // Both halves of the cursor must go back, or pagination silently skips every
    // row sharing the boundary timestamp -- the bug this endpoint was fixed for.
    const spy = mockFetch({
      items: [ROW],
      next_before: "2026-08-17T21:00:00Z",
      next_before_id: ROW.id,
    });
    renderCalls();

    fireEvent.click(await screen.findByRole("button", { name: /load older/i }));

    await waitFor(() => expect(spy.mock.calls.length).toBeGreaterThan(1));
    const url = String(spy.mock.calls.at(-1)?.[0]);
    expect(url).toContain("before=2026-08-17T21%3A00%3A00Z");
    expect(url).toContain(`before_id=${ROW.id}`);
  });

  it("passes the provider filter to the server rather than filtering locally", async () => {
    // Local filtering would only ever filter the page already loaded, which for a
    // keyset-paginated table is a subset that looks authoritative and is not.
    const spy = mockFetch({ items: [ROW], next_before: null, next_before_id: null });
    renderCalls();
    await screen.findByText("groq");

    fireEvent.change(screen.getByLabelText(/filter by provider/i), {
      target: { value: "mock" },
    });

    await waitFor(() =>
      expect(String(spy.mock.calls.at(-1)?.[0])).toContain("provider=mock"),
    );
  });
});
