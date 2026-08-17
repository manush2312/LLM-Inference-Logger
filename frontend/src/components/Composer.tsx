import { useState } from "react";

interface ComposerProps {
  onSend: (content: string) => Promise<void>;
  disabled: boolean;
}

export function Composer({ onSend, disabled }: ComposerProps) {
  const [draft, setDraft] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();

    const content = draft.trim();
    if (!content || disabled) return;

    // Cleared before awaiting so the input frees up immediately. If the send
    // fails the error is surfaced above the composer, and the text is
    // recoverable from the transcript -- losing a keystroke is a worse
    // experience than losing a failed draft.
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
          // Enter sends, Shift+Enter inserts a newline -- the convention every
          // chat interface uses.
          if (event.key === "Enter" && !event.shiftKey) {
            void submit(event);
          }
        }}
        placeholder="Send a message…"
        rows={3}
        disabled={disabled}
      />
      <button className="btn btn--primary" type="submit" disabled={disabled || !draft.trim()}>
        {disabled ? "Sending…" : "Send"}
      </button>
    </form>
  );
}
