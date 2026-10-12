import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { Providers } from "@/components/providers";
import { CommandMenuProvider } from "@/components/shell/command-menu";
import { themeInitScript } from "@/lib/theme";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "ReleaseLens — Release intelligence for engineering teams",
    template: "%s · ReleaseLens",
  },
  description:
    "CI failure risk before merge and regression detection after deploy, built on your existing GitHub, CI/CD and OpenTelemetry.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#0b0c0e" },
    { media: "(prefers-color-scheme: light)", color: "#fbfbfc" },
  ],
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    // Dark-first: the server always renders `dark`; the inline script switches to light
    // before first paint when the visitor chose it, hence suppressHydrationWarning.
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className="min-h-dvh">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-[60] focus:rounded-md focus:border focus:bg-raised focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-foreground focus:shadow-raised"
        >
          Skip to content
        </a>
        <Providers>
          <CommandMenuProvider>{children}</CommandMenuProvider>
        </Providers>
      </body>
    </html>
  );
}
