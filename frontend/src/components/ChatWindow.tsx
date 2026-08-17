import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { useConversation, useProviders, useSendMessage } from "../hooks/useChat";
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
  const sendMessage = useSendMessage();

  const [model, setModel] = useState<string>("");
  const transcriptRef = useRef<HTMLDivElement>(null);

  const messages = conversation?.messages ?? [];

  useEffect(() => {
    transcriptRef.current?.scrollTo({
      top: transcriptRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages.length, sendMessage.isPending]);

  async function handleSend(content: string) {
    const response = await sendMessage.mutateAsync({
      content,
      conversationId,
      model: model || undefined,
    });

    // A message sent from the "new chat" screen creates a conversation
    // server-side; move the URL onto it so a refresh now resumes here.
    if (!conversationId) {
      navigate(`/c/${response.conversation_id}`, { replace: true });
    }
  }

  const error = sendMessage.error;

  return (
    <section className="chat">
      <header className="chat__header">
        <h1 className="chat__title">{conversation?.title ?? "New conversation"}</h1>

        <label className="chat__model">
          <span>Model</span>
          <select value={model} onChange={(event) => setModel(event.target.value)}>
            <option value="">
              {providers ? `Default (${providers.default})` : "Default"}
            </option>
            {/* Behaviour-selecting mock models, so the failure and latency
                paths are reachable from the UI rather than only from tests. */}
            {providers?.items.some((p) => p.name === "mock") && (
              <optgroup label="mock provider">
                <option value="mock">mock — normal</option>
                <option value="mock-slow">mock-slow — slow stream</option>
                <option value="mock-error">mock-error — fails mid-response</option>
              </optgroup>
            )}
          </select>
        </label>
      </header>

      <div className="chat__transcript" ref={transcriptRef}>
        {isLoading && <p className="chat__hint">Loading conversation…</p>}

        {!isLoading && messages.length === 0 && (
          <p className="chat__hint">
            Send a message to begin. Replies report which turn they are and how much
            history they received, so multi-turn context is visible rather than assumed.
          </p>
        )}

        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}

        {sendMessage.isPending && <p className="chat__hint">Waiting for the model…</p>}

        {error && (
          <p className="chat__error" role="alert">
            {error instanceof ApiError
              ? `${error.code}: ${error.message}`
              : "Something went wrong."}
          </p>
        )}
      </div>

      <Composer onSend={handleSend} disabled={sendMessage.isPending} />
    </section>
  );
}
