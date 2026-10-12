import {
  Activity,
  BookOpen,
  Boxes,
  BrainCircuit,
  ChartColumn,
  CreditCard,
  Gauge,
  Home,
  LayoutDashboard,
  Network,
  Newspaper,
  Rocket,
  Settings,
  type LucideIcon,
} from "lucide-react";

import { DOCS } from "@/lib/docs";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** One line used by the command menu. */
  description: string;
  /** Extra search terms for the command menu. */
  keywords?: string[];
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

/** The authenticated dashboard lives under /app (one matcher for M10's auth middleware). */
export const APP_HOME = "/app";

export const APP_NAV: NavGroup[] = [
  {
    label: "Releases",
    items: [
      {
        href: "/app",
        label: "Overview",
        icon: LayoutDashboard,
        description: "Release health across projects",
        keywords: ["home", "dashboard"],
      },
      {
        href: "/app/deployments",
        label: "Deployments",
        icon: Rocket,
        description: "Deployment events from your CD pipeline",
        keywords: ["deploy", "release", "events"],
      },
      {
        href: "/app/regressions",
        label: "Regressions",
        icon: Activity,
        description: "Before/after trace comparisons per deployment",
        keywords: ["latency", "errors", "probable origin", "blast radius"],
      },
      {
        href: "/app/risk",
        label: "Risk analysis",
        icon: Gauge,
        description: "CI failure risk scores for pull requests",
        keywords: ["predict", "shap", "pull request", "github"],
      },
    ],
  },
  {
    label: "Systems",
    items: [
      {
        href: "/app/services",
        label: "Services",
        icon: Boxes,
        description: "Services reporting OpenTelemetry traces",
        keywords: ["otel", "otlp", "spans"],
      },
      {
        href: "/app/service-map",
        label: "Service map",
        icon: Network,
        description: "Service graph built from real traces",
        keywords: ["graph", "dependencies", "topology"],
      },
      {
        href: "/app/analytics",
        label: "Analytics",
        icon: ChartColumn,
        description: "Delivery analytics and historical findings",
        keywords: ["charts", "trends", "findings"],
      },
    ],
  },
  {
    label: "Configure",
    items: [
      {
        href: "/app/model",
        label: "Model",
        icon: BrainCircuit,
        description: "The CI failure risk model and its limits",
        keywords: ["ml", "model card", "metrics"],
      },
      {
        href: "/app/settings",
        label: "Settings",
        icon: Settings,
        description: "Organization, API keys, integrations, billing",
        keywords: ["api keys", "integrations", "billing", "slack", "github app"],
      },
    ],
  },
];

export const SITE_NAV: NavItem[] = [
  { href: "/", label: "Home", icon: Home, description: "What ReleaseLens does" },
  { href: "/pricing", label: "Pricing", icon: CreditCard, description: "Free, Team and Enterprise plans" },
  { href: "/docs", label: "Docs", icon: BookOpen, description: "Guides and methodology" },
  { href: "/changelog", label: "Changelog", icon: Newspaper, description: "What shipped, milestone by milestone" },
  { href: "/status", label: "System status", icon: Activity, description: "Live API health probes" },
];

export const DOCS_NAV: NavItem[] = DOCS.map((doc) => ({
  href: `/docs/${doc.slug}`,
  label: doc.title,
  icon: doc.icon,
  description: doc.summary,
}));

/**
 * Whether `href` is the current page. The dashboard root only matches exactly, so Overview is not
 * highlighted on every /app/* page; other items also match their sub-pages.
 */
export function isActivePath(pathname: string, href: string): boolean {
  if (href === "/" || href === APP_HOME) return pathname === href;
  return pathname === href || pathname.startsWith(`${href}/`);
}
