import "@fontsource/source-sans-3/latin-400.css";
import "@fontsource/source-sans-3/latin-600.css";
import "@fontsource/source-sans-3/latin-700.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp } from "antd";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { isClientError } from "./api";
import { ThemeProvider } from "./theme";

const queryClient = new QueryClient({
  defaultOptions: {
    // a 4xx (e.g. unknown repo) won't fix itself on retry
    queries: { retry: (n, e) => !isClientError(e) && n < 1, refetchOnWindowFocus: false },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <AntApp>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </AntApp>
      </ThemeProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
