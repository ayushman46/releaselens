import { SearchX } from "lucide-react";
import Link from "next/link";

import { SiteFooter } from "@/components/marketing/site-footer";
import { SiteHeader } from "@/components/site-header";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";

export default function NotFound() {
  return (
    <div className="flex min-h-dvh flex-col">
      <SiteHeader />
      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-3xl flex-1 px-4 py-20 outline-none sm:px-6">
        <h1 className="sr-only">Page not found</h1>
        <EmptyState
          icon={SearchX}
          title="This page does not exist"
          description="The link may be outdated, or the page has not been built yet. Press ⌘K (Ctrl K) to jump to any page."
          action={
            <Button asChild size="sm">
              <Link href="/">Go to the home page</Link>
            </Button>
          }
          secondaryAction={
            <Button asChild size="sm" variant="ghost">
              <Link href="/app">Open the dashboard</Link>
            </Button>
          }
        />
      </main>
      <SiteFooter />
    </div>
  );
}
