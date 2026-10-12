import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Providers } from "@/components/providers";
import { SiteHeader } from "@/components/site-header";
import { themeInitScript } from "@/lib/theme";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "ReleaseLens — Release intelligence for engineering teams",
    template: "%s · ReleaseLens",
  },
  description:
    "Release risk prediction before merge and regression detection after deploy, built on your existing GitHub, CI/CD and OpenTelemetry.",
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
        <Providers>
          <div className="flex min-h-dvh flex-col">
            <SiteHeader />
            <main className="flex-1">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
