import { Suspense, lazy } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { ChatWindow } from "./components/ChatWindow";
import { ConversationList } from "./components/ConversationList";
import "./App.css";

// Split out: Recharts is ~400 kB, and the chat path never loads it. Without
// this, every first paint of the chat UI pays for a dashboard it may never open.
const Dashboard = lazy(() =>
  import("./pages/Dashboard").then((m) => ({ default: m.Dashboard })),
);

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
            <Route
              path="/dashboard"
              element={
                <Suspense fallback={<p className="chat__hint">Loading dashboard…</p>}>
                  <Dashboard />
                </Suspense>
              }
            />
          </Routes>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
