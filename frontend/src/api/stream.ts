/**
 * SSE client for `POST /chat/stream`.
 *
 * `EventSource` is not usable here: it only issues GET requests and cannot send
 * a JSON body. So the stream is read from `fetch`'s `ReadableStream` and the
 * SSE framing is parsed by hand -- which also gives us the `AbortController`
 * that cancellation depends on.
 */

import { ApiError } from "./client";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

export interface StreamStart {
  conversation_id: string;
  provider: string;
  model: string;
}

export interface StreamDone {
  message_id: string;
  latency_ms: number;
}

export interface StreamHandlers {
  onStart?: (info: StreamStart) => void;
  /** Fires once, the moment the first token arrives -- separate from content. */
  onTimeToFirstToken?: (ttftMs: number) => void;
  onChunk?: (text: string) => void;
  onDone?: (info: StreamDone) => void;
  /** A failure reported in-band, after the 200 headers were already sent. */
  onError?: (error: ApiError) => void;
}

export interface StreamInput {
  content: string;
  conversationId?: string;
  provider?: string;
  model?: string;
}

export async function streamMessage(
  input: StreamInput,
  handlers: StreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`${BASE_URL}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({
      content: input.content,
      conversation_id: input.conversationId ?? null,
      provider: input.provider ?? null,
      model: input.model ?? null,
    }),
  });

  // Only pre-stream failures (validation, unknown conversation) arrive as a
  // non-200. Anything that goes wrong after the first byte is an `error`
  // frame instead, because the status line is already committed.
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      body?.error?.code ?? "unknown_error",
      body?.error?.message ?? `Request failed with status ${response.status}`,
      response.status,
    );
  }

  if (!response.body) {
    throw new ApiError("no_stream", "The server returned no stream body.", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Frames are delimited by a blank line. The final element is whatever
      // arrived mid-frame, so it stays in the buffer until the rest lands --
      // network packets do not respect message boundaries.
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        dispatch(frame, handlers);
      }
    }
  } finally {
    // Releasing the lock lets an aborted fetch tear the connection down, which
    // is what the server observes as a client disconnect.
    reader.releaseLock();
  }
}

function dispatch(frame: string, handlers: StreamHandlers): void {
  let event = "";
  let data = "";

  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7);
    else if (line.startsWith("data: ")) data = line.slice(6);
  }

  if (!event || !data) return;

  const payload = JSON.parse(data);

  switch (event) {
    case "start":
      handlers.onStart?.(payload as StreamStart);
      break;
    case "ttft":
      handlers.onTimeToFirstToken?.(payload.ttft_ms as number);
      break;
    case "chunk":
      handlers.onChunk?.(payload.text as string);
      break;
    case "done":
      handlers.onDone?.(payload as StreamDone);
      break;
    case "error":
      handlers.onError?.(new ApiError(payload.code, payload.message, 200));
      break;
    default:
      // Unknown event types are ignored rather than fatal, so the server can
      // add frames without breaking older clients.
      break;
  }
}
