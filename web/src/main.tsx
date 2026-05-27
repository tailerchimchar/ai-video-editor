import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

// Self-hosted fonts. Each import injects an @font-face declaration at
// build time so the first paint already has the right family loaded.
import "@fontsource/instrument-serif/400.css";
import "@fontsource-variable/geist";
import "@fontsource-variable/jetbrains-mono";

import "./styles/globals.css";
import { App } from "./App";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Compilation data changes only when the user mutates via this app
      // (or via MCP in another window). 30s is short enough that re-opening
      // a tab refetches; long enough that scrolling around doesn't spam the
      // API. Mutations explicitly invalidate when they need fresh data.
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("missing #root element in index.html");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
