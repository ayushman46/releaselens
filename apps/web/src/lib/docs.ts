import {
  BookOpenCheck,
  FlaskConical,
  GitPullRequest,
  Radio,
  Rocket,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";

export interface DocEntry {
  slug: string;
  title: string;
  summary: string;
  icon: LucideIcon;
  /** The milestone in docs/PLAN.md that ships the feature this page documents. */
  milestone: string;
  /** What the finished page will cover. */
  covers: string[];
}

/** The documentation set. Pages are placeholders until MDX lands; the outline is the plan. */
export const DOCS: DocEntry[] = [
  {
    slug: "quick-start",
    title: "Quick start",
    summary: "From zero to your first risk score and your first regression check.",
    icon: Rocket,
    milestone: "M10",
    covers: [
      "Create an organization and a project",
      "Create API keys with the right scopes",
      "Wire up the GitHub Action, deployment events and OTLP in order",
      "What you will see in the dashboard, and when",
    ],
  },
  {
    slug: "github-setup",
    title: "GitHub setup",
    summary: "Install the GitHub App and add the risk-check Action to your workflow.",
    icon: GitPullRequest,
    milestone: "M5",
    covers: [
      "Installing the GitHub App and the permissions it asks for",
      "Adding the composite Action to a pull-request workflow",
      "Which change facts the Action computes in your runner, and which it never sends",
      "Reading the Check Run: score, band and top factors",
    ],
  },
  {
    slug: "deployment-api",
    title: "Deployment API",
    summary: "Tell ReleaseLens when a release went out: POST /api/v1/deployments.",
    icon: Rocket,
    milestone: "M6",
    covers: [
      "Authentication with the x-releaselens-key header and the deployments:write scope",
      "Request fields and the 202 response with an event_id",
      "Idempotency: retries never create duplicate deployments",
      "Calling it from GitHub Actions, GitLab CI, Argo CD and plain shell",
    ],
  },
  {
    slug: "otel-setup",
    title: "OpenTelemetry setup",
    summary: "Send traces to the OTLP endpoint from your existing Collector or SDKs.",
    icon: Radio,
    milestone: "M7",
    covers: [
      "Collector exporter configuration (OTLP/HTTP, protobuf, gzip)",
      "SDK environment variables for direct export",
      "Resource attributes ReleaseLens relies on: service.name, deployment.environment",
      "Sampling, volume and retention per plan",
    ],
  },
  {
    slug: "methodology",
    title: "Methodology",
    summary: "How risk is scored and how regressions are detected, gated and explained.",
    icon: FlaskConical,
    milestone: "M4 / M8",
    covers: [
      "Training data: the TravisTorrent Java subset and how labels are defined",
      "Features, leakage rules and the SHAP explanations shown with each score",
      "Regression windows, statistical gates and minimum sample sizes",
      "Probable-origin scoring and blast radius on the service graph",
    ],
  },
  {
    slug: "limitations",
    title: "Limitations",
    summary: "What ReleaseLens does not know, and how that shows up in the product.",
    icon: TriangleAlert,
    milestone: "M4 / M12",
    covers: [
      "The model predicts CI failure, not production incidents",
      "Training data ends in 2016; transfer to today's pipelines is measured, not assumed",
      "A probable origin is evidence-ranked, not a proven root cause",
      "When traffic is too low to decide, you see “Insufficient data” instead of a verdict",
    ],
  },
];

export function getDoc(slug: string): DocEntry | undefined {
  return DOCS.find((doc) => doc.slug === slug);
}

export const DOCS_INDEX_ICON = BookOpenCheck;
