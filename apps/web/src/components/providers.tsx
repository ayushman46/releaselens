"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: 1,
      },
    },
  });
}

export function Providers({ children }: { children: ReactNode }) {
  // One client per browser session; useState keeps it stable across re-renders.
  const [queryClient] = useState(makeQueryClient);
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
