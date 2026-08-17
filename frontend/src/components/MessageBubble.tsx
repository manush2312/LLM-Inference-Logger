import type { Message } from "../api/types";

export function MessageBubble({ message }: { message: Message }) {
  return (
    <article className={`bubble bubble--${message.role}`}>
      <header className="bubble__role">{message.role}</header>
      <div className="bubble__content">{message.content}</div>
    </article>
  );
}
