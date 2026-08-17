import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useConversation, useProviders } from "../hooks/useChat";
import { useStreamingChat } from "../hooks/useStreamingChat";
import { Composer } from "./Composer";
import { MessageBubble } from "./MessageBubble";

export function ChatWindow() {
  // The conversation id lives in the URL, not in component state. That single
  // choice is what makes a page refresh resume the same conversation instead
  // of silently starting a new one.
  const { conversationId } = useParams();
  const navigate = useNavigate();

  const { data: conversation, isLoading } = useConversation(conversationId);
  const { data: providers } = useProviders();
  const stream = useStreamingChat();

  // The selection carries BOTH provider and model, JSON-encoded.
  //
  // Sending only the model name was a real bug: the backend then fell back to
  // the default provider, so picking `llama3.2:1b` was answered by `mock`.
  // JSON rather than a delimited string because model names legitimately
  // contain both `:` (llama3.2:1b) and `/` (meta-llama/llama-4), so any
  // separator risks splitting in the wrong place.
  const [selection, setSelection] = useState<string>("");
  const transcriptRef = useRef<HTMLDivElement>(null);

  const messages = conversation?.messages ?? [];

  useEffect(() => {
    transcriptRef.current?.scrollTo({
      top: transcriptRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages.length, stream.text]);

  // Clear the provisional bubble when switching conversations, so a half-
  // streamed reply cannot bleed into a different transcript.
  useEffect(() => {
    stream.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  function parseSelection(): { provider?: string; model?: string } {
    if (!selection) return {};
    return JSON.parse(selection) as { provider: string; model: string };
  }

  async function handleSend(content: string) {
    const picked = parseSelection();
    const id = await stream.send({
      content,
      conversationId,
      // Both, or neither. Sending a model without its provider is what caused
      // the mis-routing.
      provider: picked.provider,
      model: picked.model,
    });

    if (!conversationId && id) {
      navigate(`/c/${id}`, { replace: true });
    }
  }

  // While streaming, the accumulated text is shown as a provisional bubble.
  // Once the server confirms, the refetched transcript replaces it -- so the
  // rendered conversation is always the persisted one, never a local guess.
  const showProvisional = stream.isStreaming && stream.text.length > 0;

  return (
    <section className="chat">
      <header className="chat__header">
        <h1 className="chat__title">{conversation?.title ?? "New conversation"}</h1>

        <div className="chat__meta">
          {stream.ttftMs !== null && (
            <span className="badge" title="Time to first token">
              TTFT {stream.ttftMs} ms
            </span>
          )}
          {stream.latencyMs !== null && !stream.isStreaming && (
            <span className="badge" title="Total generation time">
              {stream.latencyMs} ms
            </span>
          )}

          <label className="chat__model">
            <span>Model</span>
            <select
              value={selection}
              onChange={(event) => setSelection(event.target.value)}
              disabled={stream.isStreaming}
            >
              <option value="">
                {providers ? `Default (${providers.default})` : "Default"}
              </option>

              {/* One group per provider the server actually has credentials
                  for, built from /providers rather than hardcoded -- so the
                  picker can never offer something that 400s on selection, and
                  adding a provider needs no frontend change at all. */}
              {providers?.items.map((provider) =>
                provider.name === "mock" ? (
                  // The mock's behaviours are spelled out because each one
                  // exists to demonstrate a specific path -- slowness,
                  // failure, cancellation -- and the labels are what make
                  // those reachable from the UI rather than only from tests.
                  <optgroup key={provider.name} label="mock (no network)">
                    {[
                      ["mock", "mock — normal"],
                      ["mock-slow", "mock-slow — slow stream"],
                      ["mock-error", "mock-error — fails mid-response"],
                      ["mock-cancel", "mock-cancel — never ends, press Stop"],
                    ].map(([value, label]) => (
                      <option
                        key={value}
                        value={JSON.stringify({ provider: "mock", model: value })}
                      >
                        {label}
                      </option>
                    ))}
                  </optgroup>
                ) : (
                  <optgroup key={provider.name} label={provider.name}>
                    {provider.models.map((name) => (
                      <option
                        key={name}
                        value={JSON.stringify({ provider: provider.name, model: name })}
                      >
                        {name}
                      </option>
                    ))}
                  </optgroup>
                ),
              )}
            </select>
          </label>
        </div>
      </header>

      <div className="chat__transcript" ref={transcriptRef}>
        {isLoading && <p className="chat__hint">Loading conversation…</p>}

        {!isLoading && messages.length === 0 && !showProvisional && (
          <p className="chat__hint">
            Send a message to begin. Replies stream token by token and report which
            turn they are, so multi-turn context is visible rather than assumed.
          </p>
        )}

        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}

        {showProvisional && (
          <article className="bubble bubble--assistant bubble--streaming">
            <header className="bubble__role">assistant</header>
            <div className="bubble__content">{stream.text}</div>
          </article>
        )}

        {stream.isStreaming && !showProvisional && (
          <p className="chat__hint">Waiting for the first token…</p>
        )}

        {stream.wasCancelled && (
          <p className="chat__notice">
            Generation stopped. Whatever had already arrived was saved, and the call
            was recorded as <code>cancelled</code> — not as an error.
          </p>
        )}

        {stream.error && (
          <p className="chat__error" role="alert">
            {stream.error.code}: {stream.error.message}
          </p>
        )}
      </div>

      <Composer
        onSend={handleSend}
        isStreaming={stream.isStreaming}
        onStop={stream.stop}
      />
    </section>
  );
}
