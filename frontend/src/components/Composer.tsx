import { useState } from "react";

interface ComposerProps {
  onSend: (content: string) => Promise<void>;
  isStreaming: boolean;
  onStop: () => void;
}

export function Composer({ onSend, isStreaming, onStop }: ComposerProps) {
  const [draft, setDraft] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();

    const content = draft.trim();
    if (!content || isStreaming) return;

    // Cleared before awaiting so the input frees up immediately.
    setDraft("");
    await onSend(content);
  }

  return (
    <form className="composer" onSubmit={(event) => void submit(event)}>
      <textarea
        className="composer__input"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          // Enter sends, Shift+Enter inserts a newline.
          if (event.key === "Enter" && !event.shiftKey) {
            void submit(event);
          }
        }}
        placeholder="Send a message…"
        rows={3}
        disabled={isStreaming}
      />

      {/* Stop replaces Send while a reply is in flight, rather than sitting
          beside it. Two enabled actions during streaming would be ambiguous,
          and Send is not a legal action mid-turn anyway. */}
      {isStreaming ? (
        <button className="btn btn--danger" type="button" onClick={onStop}>
          Stop
        </button>
      ) : (
        <button className="btn btn--primary" type="submit" disabled={!draft.trim()}>
          Send
        </button>
      )}
    </form>
  );
}
