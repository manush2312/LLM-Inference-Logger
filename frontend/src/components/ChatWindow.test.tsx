/**
 * Rendering-over-time behaviour of the chat window.
 *
 * These exist because three bugs in a row lived in exactly this seam, and none
 * was visible from reading the code or from the backend test suite:
 *
 * 1. The user's own message vanished on send, reappearing only after the reply
 *    finished.
 * 2. Fixing that moved the navigate earlier, which made a reset effect fire
 *    mid-stream and wipe the tokens as they arrived -- so the reply again
 *    appeared only when complete.
 * 3. Both were about *when* things render relative to the stream, which no
 *    amount of unit-testing the hook in isolation would have caught.
 *
 * So the assertions here are deliberately temporal: something must be on screen
 * *before* the stream completes, not merely afterwards.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChatWindow } from "./ChatWindow";
import * as streamModule from "../api/stream";
import { api } from "../api/client";

const CONVERSATION_ID = "11111111-1111-1111-1111-111111111111";

/** Resolves only when the test says so, so "mid-stream" is a real state. */
function deferred<T = void>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function renderChat() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  // The real route table, not a bare component. This is load-bearing: the
  // regression these tests exist for lives in the interaction between
  // navigating to /c/:conversationId mid-stream and an effect keyed on that
  // param. Rendering ChatWindow without the routes leaves useParams() forever
  // empty, so the effect never re-fires and the test passes against broken code
  // -- which is exactly what happened on the first attempt.
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<ChatWindow />} />
          <Route path="/c/:conversationId" element={<ChatWindow />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  // A brand-new chat: no conversation loaded, one provider available.
  vi.spyOn(api, "listProviders").mockResolvedValue({
    items: [
      { name: "mock", default_model: "mock", is_default: true, models: ["mock"] },
    ],
    default: "mock",
  });
  vi.spyOn(api, "getConversation").mockResolvedValue({
    id: CONVERSATION_ID,
    title: null,
    status: "active",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    // Deliberately empty: the server has not yet been refetched, which is the
    // window both bugs lived in.
    messages: [],
  });
});

async function send(text: string) {
  const { fireEvent } = await import("@testing-library/react");
  const input = await screen.findByPlaceholderText(/send a message/i);
  fireEvent.change(input, { target: { value: text } });
  fireEvent.submit(input.closest("form")!);
}

describe("while a reply is streaming", () => {
  it("shows the user's message immediately, before the reply arrives", async () => {
    const held = deferred();
    vi.spyOn(streamModule, "streamMessage").mockImplementation(
      async (_input, handlers) => {
        handlers.onStart?.({
          conversation_id: CONVERSATION_ID,
          provider: "mock",
          model: "mock",
        });
        await held.promise; // stream is still open
      },
    );

    renderChat();
    await send("what is a WAL?");

    // The assertion that matters: visible while the stream is still open.
    expect(await screen.findByText("what is a WAL?")).toBeInTheDocument();

    held.resolve();
  });

  it("renders tokens as they arrive, not only once complete", async () => {
    const held = deferred();
    let emit: ((text: string) => void) | undefined;

    vi.spyOn(streamModule, "streamMessage").mockImplementation(
      async (_input, handlers) => {
        handlers.onStart?.({
          conversation_id: CONVERSATION_ID,
          provider: "mock",
          model: "mock",
        });
        emit = (text) => handlers.onChunk?.(text);
        await held.promise;
      },
    );

    renderChat();
    await send("hello");

    await waitFor(() => expect(emit).toBeDefined());

    // Two tokens, with the stream still open.
    emit!("Write-ahead ");
    expect(await screen.findByText(/Write-ahead/)).toBeInTheDocument();

    emit!("logging");
    // Partial text accumulates on screen. This is the exact assertion the
    // reset-mid-stream regression would fail: the bubble was being cleared, so
    // nothing appeared until the final refetch.
    await waitFor(() =>
      expect(screen.getByText(/Write-ahead logging/)).toBeInTheDocument(),
    );

    held.resolve();
  });

  it("keeps streaming visible after navigating onto a new conversation", async () => {
    // The regression, isolated. Creating a conversation navigates the router
    // mid-stream; a reset effect keyed on the conversation id then wiped the
    // accumulated tokens. The guard must tell self-navigation apart from the
    // user switching conversations.
    const held = deferred();
    let emit: ((text: string) => void) | undefined;

    vi.spyOn(streamModule, "streamMessage").mockImplementation(
      async (_input, handlers) => {
        // This is what moves the URL from "/" to "/c/<id>".
        handlers.onStart?.({
          conversation_id: CONVERSATION_ID,
          provider: "mock",
          model: "mock",
        });
        emit = (text) => handlers.onChunk?.(text);
        await held.promise;
      },
    );

    renderChat();
    await send("hi");

    await waitFor(() => expect(emit).toBeDefined());
    emit!("partial answer");

    await waitFor(() =>
      expect(screen.getByText(/partial answer/)).toBeInTheDocument(),
    );

    held.resolve();
  });
});

describe("when a stream fails after it has started", () => {
  it("surfaces the in-band error rather than failing silently", async () => {
    vi.spyOn(streamModule, "streamMessage").mockImplementation(
      async (_input, handlers) => {
        handlers.onStart?.({
          conversation_id: CONVERSATION_ID,
          provider: "mock",
          model: "mock",
        });
        handlers.onChunk?.("partial ");
        const { ApiError } = await import("../api/client");
        handlers.onError?.(new ApiError("provider_error", "upstream failed", 200));
      },
    );

    renderChat();
    await send("boom");

    expect(await screen.findByRole("alert")).toHaveTextContent("upstream failed");
  });
});
