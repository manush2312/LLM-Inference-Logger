/**
 * Mirrors the backend's response schemas.
 *
 * Hand-written rather than generated: the surface is small, and a generator
 * would be another build step for a reviewer to run. If this grew, generating
 * from the OpenAPI schema FastAPI already serves at /openapi.json would be the
 * upgrade -- see the README.
 */

export type MessageRole = "user" | "assistant" | "system";
export type ConversationStatus = "active" | "cancelled" | "archived";

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  seq: number;
  created_at: string;
}

export interface ConversationSummary {
  id: string;
  title: string | null;
  status: ConversationStatus;
  created_at: string;
  updated_at: string;
}

export interface ConversationDetail extends ConversationSummary {
  messages: Message[];
}

export interface ConversationList {
  items: ConversationSummary[];
  total: number;
}

export interface ChatResponse {
  conversation_id: string;
  user_message: Message;
  assistant_message: Message;
  provider: string;
  model: string;
}

export interface ProviderInfo {
  name: string;
  default_model: string;
  is_default: boolean;
}

export interface ProviderList {
  items: ProviderInfo[];
  default: string;
}

/** The backend's error envelope: `{ error: { code, message, ... } }`. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    [key: string]: unknown;
  };
}
