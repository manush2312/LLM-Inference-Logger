/**
 * Drives one streamed turn and exposes it as React state.
 *
 * The `AbortController` is the whole cancellation story on this side: aborting
 * the fetch drops the connection, the server's disconnect watcher notices, and
 * the in-flight model call is cancelled and logged as `cancelled` rather than
 * left running and billing.
 */

import { useCallback, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { streamMessage, type StreamInput } from "../api/stream";
import { queryKeys } from "./useChat";

export interface StreamingState {
  /**
   * The message the user just sent, echoed back for immediate display.
   *
   * Without this the transcript renders only server state, and the composer
   * clears the input the moment you hit Send — so your own message vanishes
   * until the reply finishes and the query refetches. On a slow model that is
   * seconds of staring at an empty screen wondering whether anything happened.
   */
  pendingUserContent: string | null;
  /** Text accumulated so far, rendered as a provisional assistant bubble. */
  text: string;
  isStreaming: boolean;
  ttftMs: number | null;
  latencyMs: number | null;
  error: ApiError | null;
  /** True when the last turn ended because the user stopped it. */
  wasCancelled: boolean;
}

const IDLE: StreamingState = {
  pendingUserContent: null,
  text: "",
  isStreaming: false,
  ttftMs: null,
  latencyMs: null,
  error: null,
  wasCancelled: false,
};

export function useStreamingChat() {
  const queryClient = useQueryClient();
  const [state, setState] = useState<StreamingState>(IDLE);
  const controllerRef = useRef<AbortController | null>(null);

  const stop = useCallback(() => {
    controllerRef.current?.abort();
  }, []);

  const send = useCallback(
    async (input: StreamInput, onConversationId?: (id: string) => void) => {
      const controller = new AbortController();
      controllerRef.current = controller;

      // Echoed before the request is even sent, so the message appears the
      // instant the user hits Send rather than a round trip later.
      setState({ ...IDLE, isStreaming: true, pendingUserContent: input.content });

      let conversationId: string | undefined = input.conversationId;

      try {
        await streamMessage(
          input,
          {
            onStart: (info) => {
              conversationId = info.conversation_id;

              // Reported as soon as the server names it, not after the stream
              // finishes -- that is what lets the caller point its transcript
              // query at the right conversation while tokens are still
              // arriving. The user's message is already committed by now: the
              // service writes it in its own transaction *before* calling the
              // provider, so a refetch here genuinely returns it.
              onConversationId?.(info.conversation_id);
              void queryClient.invalidateQueries({
                queryKey: queryKeys.conversation(info.conversation_id),
              });
            },
            onTimeToFirstToken: (ttftMs) =>
              setState((previous) => ({ ...previous, ttftMs })),
            onChunk: (text) =>
              setState((previous) => ({ ...previous, text: previous.text + text })),
            onDone: (info) =>
              setState((previous) => ({
                ...previous,
                isStreaming: false,
                latencyMs: info.latency_ms,
              })),
            onError: (error) =>
              setState((previous) => ({ ...previous, isStreaming: false, error })),
          },
          controller.signal,
        );
      } catch (error) {
        if (controller.signal.aborted) {
          // Expected: the user pressed Stop. Partial output is already
          // persisted server-side, so the refetch below will show it.
          setState((previous) => ({
            ...previous,
            isStreaming: false,
            wasCancelled: true,
          }));
        } else {
          setState((previous) => ({
            ...previous,
            isStreaming: false,
            error:
              error instanceof ApiError
                ? error
                : new ApiError("network_error", "The stream was interrupted.", 0),
          }));
        }
      } finally {
        controllerRef.current = null;

        // Refetch in every case -- completed, failed, or cancelled. The server
        // is the authority on what was actually persisted, and after a
        // cancellation that is a partial reply the client cannot reconstruct.
        if (conversationId) {
          void queryClient.invalidateQueries({
            queryKey: queryKeys.conversation(conversationId),
          });
        }
        void queryClient.invalidateQueries({ queryKey: queryKeys.conversations });
      }

    },
    [queryClient],
  );

  const reset = useCallback(() => setState(IDLE), []);

  return { ...state, send, stop, reset };
}
