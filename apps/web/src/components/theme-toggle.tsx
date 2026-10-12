"use client";

import { Moon, Sun } from "lucide-react";
import { useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";
import { applyTheme, type Theme } from "@/lib/theme";

function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  return () => observer.disconnect();
}

function getSnapshot(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

function getServerSnapshot(): Theme {
  return "dark";
}

export function ThemeToggle() {
  const theme = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  const next: Theme = theme === "dark" ? "light" : "dark";
  const label = `Switch to ${next} theme`;

  return (
    <Button
      variant="ghost"
      size="icon-sm"
      aria-label={label}
      title={label}
      onClick={() => applyTheme(next)}
      className="text-muted-foreground hover:text-foreground"
    >
      {theme === "dark" ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
    </Button>
  );
}
