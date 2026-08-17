import type { Message } from "../api/types";
import { Markdown } from "./Markdown";

export function MessageBubble({ message }: { message: Message }) {
  // Only assistant output is treated as markdown. What the user typed is shown
  // back exactly as typed -- someone asking "what does ** mean in Python?"
  // should not watch their own question turn bold, and round-tripping user input
  // through a parser is a transformation with no upside.
  const isAssistant = message.role === "assistant";
  return (
    <article className={`bubble bubble--${message.role}`}>
      <header className="bubble__role">{message.role}</header>
      {isAssistant ? (
        <Markdown content={message.content} />
      ) : (
        <div className="bubble__content">{message.content}</div>
      )}
    </article>
  );
}
