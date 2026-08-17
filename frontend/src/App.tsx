import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { ChatWindow } from "./components/ChatWindow";
import { ConversationList } from "./components/ConversationList";
import "./App.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A chat transcript is only stale when this client changes it, and every
      // mutation invalidates explicitly -- so background refetching would be
      // pure noise.
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="layout">
          <ConversationList />
          <Routes>
            {/* Both routes render the same component: "new chat" is just a
                conversation whose id does not exist yet. */}
            <Route path="/" element={<ChatWindow />} />
            <Route path="/c/:conversationId" element={<ChatWindow />} />
          </Routes>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
