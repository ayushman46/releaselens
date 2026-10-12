import type { ReactNode } from "react";

import { DocsNav } from "@/components/marketing/docs-nav";

export default function DocsLayout({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto grid max-w-6xl grid-cols-1 gap-8 px-4 py-10 sm:px-6 md:grid-cols-[13rem_minmax(0,1fr)] md:gap-12 md:py-14">
      <aside className="min-w-0 md:sticky md:top-20 md:self-start">
        <DocsNav />
      </aside>
      <div className="min-w-0">{children}</div>
    </div>
  );
}
