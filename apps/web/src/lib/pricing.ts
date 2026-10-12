export interface Plan {
  id: "free" | "team" | "enterprise";
  name: string;
  price: string;
  cadence?: string;
  tagline: string;
  limits: { projects: string; services: string; retention: string };
  /** Listed before the limits (e.g. "Everything in Team"). */
  includes?: string;
  features: string[];
  highlighted?: boolean;
}

/** Planned launch pricing. Sign-up and billing arrive in M10; prices may change before launch. */
export const PLANS: Plan[] = [
  {
    id: "free",
    name: "Free",
    price: "$0",
    tagline: "Try the loop on one project.",
    limits: { projects: "1 project", services: "3 services", retention: "7-day retention" },
    features: [],
  },
  {
    id: "team",
    name: "Team",
    price: "$99",
    cadence: "/month",
    tagline: "For teams of 10–200 engineers shipping every day.",
    limits: { projects: "10 projects", services: "50 services", retention: "30-day retention" },
    features: ["Slack alerts", "GitHub Checks"],
    highlighted: true,
  },
  {
    id: "enterprise",
    name: "Enterprise",
    price: "Custom",
    tagline: "Limits, retention and terms agreed with you.",
    limits: { projects: "Custom projects", services: "Custom services", retention: "Custom retention" },
    includes: "Everything in Team",
    features: [],
  },
];

export const PRICING_NOTE =
  "Planned launch pricing. Sign-up and billing open in M10, and prices may change before launch.";
