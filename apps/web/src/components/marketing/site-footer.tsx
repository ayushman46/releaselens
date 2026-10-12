import Link from "next/link";

import { LogoMark } from "@/components/logo-mark";
import { DOCS } from "@/lib/docs";

const COLUMNS = [
  {
    title: "Product",
    links: [
      { href: "/#loop", label: "How it works" },
      { href: "/pricing", label: "Pricing" },
      { href: "/changelog", label: "Changelog" },
      { href: "/status", label: "System status" },
    ],
  },
  {
    title: "Docs",
    links: DOCS.slice(0, 4).map((doc) => ({ href: `/docs/${doc.slug}`, label: doc.title })),
  },
  {
    title: "Honesty",
    links: [
      { href: "/docs/methodology", label: "Methodology" },
      { href: "/docs/limitations", label: "Limitations" },
      { href: "/#methodology", label: "What the model predicts" },
    ],
  },
];

export function SiteFooter() {
  return (
    <footer className="border-t">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-12 sm:px-6 md:grid-cols-[minmax(0,1.4fr)_repeat(3,minmax(0,1fr))]">
        <div className="max-w-xs">
          <Link href="/" className="inline-flex items-center gap-2 rounded-md text-sm font-semibold tracking-tight">
            <LogoMark className="size-5" />
            ReleaseLens
          </Link>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">
            Release intelligence for engineering teams. In development: features ship milestone by
            milestone, and the changelog says which.
          </p>
        </div>
        {COLUMNS.map((column) => (
          <nav key={column.title} aria-label={column.title}>
            <p className="text-xs font-medium text-foreground">{column.title}</p>
            <ul className="mt-3 space-y-2">
              {column.links.map((link) => (
                <li key={link.href}>
                  <Link href={link.href} className="rounded-sm text-sm text-muted-foreground hover:text-foreground">
                    {link.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        ))}
      </div>
      <div className="border-t">
        <p className="mx-auto max-w-6xl px-4 py-5 text-xs leading-5 text-muted-foreground sm:px-6">
          Historical training data: TravisTorrent (Beller, Gousios &amp; Zaidman, MSR 2017,{" "}
          <a
            href="https://doi.org/10.1109/MSR.2017.24"
            className="underline underline-offset-2 hover:text-foreground"
            rel="noreferrer"
          >
            doi:10.1109/MSR.2017.24
          </a>
          ), build table re-published on Figshare under CC BY 4.0.
        </p>
      </div>
    </footer>
  );
}
