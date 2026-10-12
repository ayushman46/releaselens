import Link from "next/link";

import { LogoMark } from "@/components/logo-mark";
import { NavLink } from "@/components/nav-link";
import { ThemeToggle } from "@/components/theme-toggle";

export function SiteHeader() {
  return (
    <header className="sticky top-0 z-40 border-b bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/65">
      <div className="mx-auto flex h-12 max-w-5xl items-center gap-4 px-4 sm:px-6">
        <Link
          href="/"
          className="flex items-center gap-2 rounded-md text-sm font-semibold tracking-tight outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
        >
          <LogoMark className="size-5" />
          ReleaseLens
        </Link>
        <nav aria-label="Main" className="flex items-center gap-1">
          <NavLink href="/status">Status</NavLink>
        </nav>
        <div className="ml-auto">
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
