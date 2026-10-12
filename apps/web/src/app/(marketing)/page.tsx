import {
  ArrowRight,
  Check,
  Gauge,
  GitPullRequest,
  Minus,
  Radio,
  Rocket,
  ScanSearch,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";

import { LoopDiagram } from "@/components/marketing/loop-diagram";
import { PricingCards } from "@/components/marketing/pricing-cards";
import { ScreenshotPlaceholder } from "@/components/marketing/screenshot-placeholder";
import { Section } from "@/components/marketing/section";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { KeyValueList } from "@/components/ui/key-value";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PRICING_NOTE } from "@/lib/pricing";
import { deploymentCurl, GITHUB_ACTION_YAML, otelCollectorYaml } from "@/lib/snippets";

export default function LandingPage() {
  return (
    <>
      <Hero />
      <Questions />
      <Section
        id="loop"
        eyebrow="How it works"
        title="One loop, from pull request to production and back"
        description="Every step runs on data your tools already produce. Only one step, deploying, stays entirely yours."
      >
        <LoopDiagram />
      </Section>
      <Integrations />
      <Methodology />
      <Section
        id="pricing"
        eyebrow="Pricing"
        title="Priced per team, not per seat"
        description={PRICING_NOTE}
      >
        <PricingCards />
        <div className="mt-6">
          <Button asChild variant="ghost" size="sm">
            <Link href="/pricing">
              Compare plans
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        </div>
      </Section>
      <FinalCta />
    </>
  );
}

function Hero() {
  return (
    <section aria-labelledby="hero-title" className="relative overflow-hidden">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-[480px] opacity-40 [mask-image:linear-gradient(to_bottom,black,transparent)]"
        style={{
          backgroundImage:
            "linear-gradient(var(--border) 1px, transparent 1px), linear-gradient(90deg, var(--border) 1px, transparent 1px)",
          backgroundSize: "56px 56px",
          backgroundPosition: "center -1px",
        }}
      />
      <div className="relative mx-auto max-w-6xl px-4 pt-16 pb-12 sm:px-6 sm:pt-24">
        <Link
          href="/changelog"
          className="inline-flex h-7 items-center gap-2 rounded-full border bg-panel pr-3 pl-2.5 text-xs text-foreground-secondary shadow-panel hover:text-foreground"
        >
          <Wrench aria-hidden="true" className="size-3.5 text-muted-foreground" />
          In development
          <span aria-hidden="true" className="h-3 w-px bg-border" />
          <span className="text-muted-foreground">Read the changelog</span>
          <ArrowRight aria-hidden="true" className="size-3 text-muted-foreground" />
        </Link>

        <h1
          id="hero-title"
          className="mt-6 max-w-5xl text-4xl leading-[1.08] font-semibold tracking-tight text-balance sm:text-5xl lg:text-[3.5rem]"
        >
          <span className="block">See the risk before you merge.</span>{" "}
          <span className="block text-muted-foreground">Find the regression after you deploy.</span>
        </h1>

        <p className="mt-6 max-w-2xl text-base leading-7 text-pretty text-foreground-secondary sm:text-lg sm:leading-8">
          ReleaseLens scores the CI failure risk of each change from historical delivery data. After
          each deployment it compares OpenTelemetry traces from before and after the release, then
          flags regressions, their probable origin and the services affected. It plugs into the
          GitHub, CI/CD and OpenTelemetry setup you already run.
        </p>

        <div className="mt-8 flex flex-wrap items-center gap-3">
          <Button asChild size="lg">
            <Link href="/docs/quick-start">
              Read the quick start
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
          <Button asChild size="lg" variant="outline">
            <Link href="/app">Open the dashboard preview</Link>
          </Button>
        </div>

        <ScreenshotPlaceholder
          className="mt-16"
          label="Regression detail: before/after comparison, probable origin and blast radius"
          caption="Placeholder. Real screenshots replace it once the dashboard shows live data."
        />
      </div>
    </section>
  );
}

const QUESTIONS: Array<{
  phase: string;
  icon: LucideIcon;
  question: string;
  answers: string[];
  milestone: string;
}> = [
  {
    phase: "Before deploy",
    icon: Gauge,
    question: "Based on historical delivery data, how risky does this change look, and why?",
    answers: [
      "A CI failure risk score from 0 to 100, with a band",
      "The factors that raised and lowered it, from SHAP",
      "CI status and duration next to the score, never mixed into it",
    ],
    milestone: "Ships in M5",
  },
  {
    phase: "After deploy",
    icon: ScanSearch,
    question:
      "Did this deployment introduce a regression, where did it probably originate, and which services were affected?",
    answers: [
      "Latency and error changes per service, statistically gated",
      "The probable origin, ranked from trace evidence",
      "The blast radius across your service graph",
    ],
    milestone: "Ships in M8–M9",
  },
];

function Questions() {
  return (
    <Section
      id="problem"
      eyebrow="The problem"
      title="Two questions every release raises"
      description="Teams usually answer them with gut feel before a merge and a wall of dashboards after a deploy. ReleaseLens answers both with evidence, and says so when the evidence is too thin."
    >
      <div className="grid gap-4 md:grid-cols-2">
        {QUESTIONS.map((item) => {
          const Icon = item.icon;
          return (
            <article key={item.phase} className="flex flex-col rounded-xl border bg-panel p-6 shadow-panel sm:p-7">
              <div className="flex items-center justify-between gap-3">
                <p className="inline-flex items-center gap-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                  <Icon aria-hidden="true" className="size-4 text-brand-text" />
                  {item.phase}
                </p>
                <Badge>{item.milestone}</Badge>
              </div>
              <h3 className="mt-5 text-lg leading-7 font-medium tracking-tight text-balance">
                “{item.question}”
              </h3>
              <ul className="mt-6 space-y-2.5 border-t pt-5 text-sm">
                {item.answers.map((answer) => (
                  <li key={answer} className="flex items-start gap-2.5 text-foreground-secondary">
                    <Check aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                    {answer}
                  </li>
                ))}
              </ul>
            </article>
          );
        })}
      </div>
    </Section>
  );
}

const INTEGRATIONS = [
  {
    value: "github",
    icon: GitPullRequest,
    title: "GitHub App + Action",
    milestone: "M5",
    description:
      "A composite Action computes change facts inside your own runner and asks for a risk score. The GitHub App posts it back as a Check Run.",
  },
  {
    value: "deployments",
    icon: Rocket,
    title: "Deployment-event API",
    milestone: "M6",
    description:
      "One authenticated POST from your CD pipeline per release. Retries are safe: events are idempotent.",
  },
  {
    value: "otlp",
    icon: Radio,
    title: "OTLP endpoint",
    milestone: "M7",
    description:
      "Point your OpenTelemetry Collector or SDKs at ReleaseLens. The service graph is built from these traces.",
  },
] as const;

function Integrations() {
  return (
    <Section
      id="integrations"
      eyebrow="Integration"
      title="Plugs into what you already run"
      description="Three integration points, each a few lines of configuration. ReleaseLens never deploys code and never needs credentials for your production infrastructure."
    >
      <div className="grid gap-8 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <ul className="space-y-3">
          {INTEGRATIONS.map((item) => {
            const Icon = item.icon;
            return (
              <li key={item.value} className="flex gap-4 rounded-xl border bg-panel p-4 shadow-panel">
                <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border bg-subtle">
                  <Icon aria-hidden="true" className="size-4 text-brand-text" />
                </span>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-sm font-semibold">{item.title}</h3>
                    <Badge>Preview · {item.milestone}</Badge>
                  </div>
                  <p className="mt-1 text-sm leading-6 text-muted-foreground">{item.description}</p>
                </div>
              </li>
            );
          })}
        </ul>

        <Tabs defaultValue="github" className="min-w-0">
          <TabsList aria-label="Integration snippets" className="w-full">
            {INTEGRATIONS.map((item) => (
              <TabsTrigger key={item.value} value={item.value}>
                {item.title}
              </TabsTrigger>
            ))}
          </TabsList>
          <TabsContent value="github">
            <CodeBlock code={GITHUB_ACTION_YAML} title=".github/workflows/releaselens.yml" />
          </TabsContent>
          <TabsContent value="deployments">
            <CodeBlock code={deploymentCurl()} title="deploy.sh" />
          </TabsContent>
          <TabsContent value="otlp">
            <CodeBlock code={otelCollectorYaml()} title="otel-collector.yaml" />
          </TabsContent>
          <p className="mt-3 text-xs leading-5 text-muted-foreground">
            Contract previews from the architecture docs. Each endpoint ships in the milestone shown; field
            names can still change until then.
          </p>
        </Tabs>
      </div>
    </Section>
  );
}

const PREDICTS = [
  "Whether a change is likely to fail CI (the build fails or errors), as a score from 0 to 100 with a band.",
  "It learns from 128,819 historical CI builds of 402 open-source Java projects (TravisTorrent, 2011–2015).",
  "Every score comes with SHAP explanations: the factors that raised it and the ones that lowered it.",
  "CI pass/fail and duration appear next to the score, never as inputs, because they would leak the answer.",
];

const DOES_NOT = [
  "It is not an outage or incident probability. Failing CI is a different outcome from breaking production.",
  "A score is not presented as a probability unless calibration testing justifies it.",
  "Transfer from 2011–2015 Travis-era projects to today's pipelines is unproven. Cross-project and cold-start evaluations measure it instead of assuming it.",
  "After deploy it names a probable origin from trace evidence, never a proven root cause. Too little traffic gives “Insufficient data”, not a verdict.",
];

function Methodology() {
  return (
    <Section
      id="methodology"
      eyebrow="Methodology"
      title="What the model predicts, and what it doesn't"
      description="A risk score is only useful if you know exactly what it measures. Here is the whole claim, including its limits."
    >
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border bg-panel p-6 shadow-panel">
          <h3 className="text-sm font-semibold">What it predicts</h3>
          <ul className="mt-4 space-y-3 text-sm leading-6">
            {PREDICTS.map((line) => (
              <li key={line} className="flex gap-3 text-foreground-secondary">
                <Check aria-hidden="true" className="mt-1 size-4 shrink-0 text-brand-text" />
                {line}
              </li>
            ))}
          </ul>
        </div>
        <div className="rounded-xl border bg-panel p-6 shadow-panel">
          <h3 className="text-sm font-semibold">What it doesn&apos;t</h3>
          <ul className="mt-4 space-y-3 text-sm leading-6">
            {DOES_NOT.map((line) => (
              <li key={line} className="flex gap-3 text-foreground-secondary">
                <Minus aria-hidden="true" className="mt-1 size-4 shrink-0 text-muted-foreground" />
                {line}
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="rounded-xl border bg-panel px-6 py-2 shadow-panel">
          <h3 className="sr-only">Numbers we will publish</h3>
          <KeyValueList
            labelWidth="sm:grid-cols-[minmax(0,1fr)_auto]"
            items={[
              { label: "Model quality against baselines (PR-AUC)", value: <Pending milestone="M4" /> },
              { label: "Calibration", value: <Pending milestone="M4" /> },
              { label: "Regression detection on the demo scenarios", value: <Pending milestone="M12" /> },
            ]}
          />
        </div>
        <div className="flex flex-col justify-center rounded-xl border border-dashed p-6">
          <p className="text-sm leading-6 text-foreground-secondary">
            No numbers appear on this site until they are measured. Each one will be published with its
            sample size and the evaluation it came from.
          </p>
          <div className="mt-4">
            <Button asChild variant="outline" size="sm">
              <Link href="/docs/methodology">
                Read the methodology
                <ArrowRight aria-hidden="true" />
              </Link>
            </Button>
          </div>
        </div>
      </div>
    </Section>
  );
}

function Pending({ milestone }: { milestone: string }) {
  return <span className="text-sm text-muted-foreground">Measured in {milestone}</span>;
}

function FinalCta() {
  return (
    <section aria-labelledby="cta-title" className="border-t">
      <div className="mx-auto max-w-6xl px-4 py-16 sm:px-6 sm:py-24">
        <div className="flex flex-col items-start justify-between gap-6 rounded-2xl border bg-panel p-8 shadow-panel sm:p-10 md:flex-row md:items-center">
          <div className="max-w-xl">
            <h2 id="cta-title" className="text-2xl font-semibold tracking-tight">
              Start with the quick start
            </h2>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              See how the pieces fit together, then wire each one up as it ships. Sign-up opens in M10.
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <Button asChild>
              <Link href="/docs/quick-start">
                Read the quick start
                <ArrowRight aria-hidden="true" />
              </Link>
            </Button>
            <Button asChild variant="outline">
              <Link href="/app">Open the dashboard preview</Link>
            </Button>
          </div>
        </div>
      </div>
    </section>
  );
}
