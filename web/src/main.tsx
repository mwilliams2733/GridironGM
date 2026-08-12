import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";
import "./index.css";
import App from "./App.tsx";
import { queryClient } from "./lib/queryClient";
import { LeagueProvider } from "./lib/league";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <LeagueProvider>
        <BrowserRouter>
          <App />
          <Toaster
            theme="dark"
            position="bottom-right"
            toastOptions={{
              style: {
                background: "var(--color-field-850)",
                border: "1px solid var(--color-field-700)",
                color: "var(--color-field-100)",
              },
            }}
          />
        </BrowserRouter>
      </LeagueProvider>
    </QueryClientProvider>
  </StrictMode>,
);
