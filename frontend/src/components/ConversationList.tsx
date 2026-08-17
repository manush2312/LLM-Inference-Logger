import { NavLink, useNavigate, useParams } from "react-router-dom";
import { useConversations, useDeleteConversation } from "../hooks/useChat";

export function ConversationList() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const { data, isLoading } = useConversations();
  const deleteConversation = useDeleteConversation();

  async function handleDelete(id: string, event: React.MouseEvent) {
    // The row is a link; without this the click would also navigate to the
    // conversation being deleted.
    event.preventDefault();
    event.stopPropagation();

    await deleteConversation.mutateAsync(id);

    // Only leave the route if the open conversation is the one removed.
    if (id === conversationId) {
      navigate("/");
    }
  }

  return (
    <nav className="sidebar">
      <div className="sidebar__header">
        <span className="sidebar__title">Conversations</span>
        <button className="btn btn--ghost" onClick={() => navigate("/")}>
          New
        </button>
      </div>

      {isLoading && <p className="sidebar__empty">Loading…</p>}

      {data?.items.length === 0 && (
        <p className="sidebar__empty">No conversations yet. Send a message to start one.</p>
      )}

      <ul className="sidebar__list">
        {data?.items.map((conversation) => (
          <li key={conversation.id}>
            <NavLink
              to={`/c/${conversation.id}`}
              className={({ isActive }) =>
                `sidebar__item${isActive ? " sidebar__item--active" : ""}`
              }
            >
              <span className="sidebar__item-title">
                {conversation.title ?? "Untitled conversation"}
              </span>
              <button
                className="sidebar__delete"
                aria-label={`Delete ${conversation.title ?? "conversation"}`}
                onClick={(event) => void handleDelete(conversation.id, event)}
              >
                ×
              </button>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
