/**
 * Typed API client.
 *
 * One place that knows about HTTP. Components call these functions and receive
 * parsed data or a thrown `ApiError` -- they never touch `fetch`, status
 * codes, or JSON parsing.
 */

import type {
  ApiErrorBody,
  ChatResponse,
  ConversationDetail,
  ConversationList,
  ProviderList,
} from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

/**
 * Carries the backend's machine-readable `code` alongside the message.
 *
 * Branching on `code` rather than parsing prose is what lets the UI treat
 * "provider not configured" differently from "the model itself failed" without
 * string-matching error text that may change.
 */
export class ApiError extends Error {
  // Declared as fields rather than constructor parameter properties: the
  // project builds with `erasableSyntaxOnly`, which rejects any TypeScript
  // syntax that emits runtime code.
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;

  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    // A network failure has no status and no body, so it needs its own code
    // rather than being reported as an unparseable server error.
    throw new ApiError("network_error", "Could not reach the server.", 0);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const body = await response.json().catch(() => null);

  if (!response.ok) {
    const error = (body as ApiErrorBody | null)?.error;
    throw new ApiError(
      error?.code ?? "unknown_error",
      error?.message ?? `Request failed with status ${response.status}`,
      response.status,
    );
  }

  return body as T;
}

export const api = {
  listConversations: () => request<ConversationList>("/conversations"),

  getConversation: (id: string) => request<ConversationDetail>(`/conversations/${id}`),

  deleteConversation: (id: string) =>
    request<void>(`/conversations/${id}`, { method: "DELETE" }),

  listProviders: () => request<ProviderList>("/providers"),

  sendMessage: (input: {
    content: string;
    conversationId?: string;
    provider?: string;
    model?: string;
  }) =>
    request<ChatResponse>("/chat", {
      method: "POST",
      body: JSON.stringify({
        content: input.content,
        conversation_id: input.conversationId ?? null,
        provider: input.provider ?? null,
        model: input.model ?? null,
      }),
    }),
};
