import { ArrowRight, Check, Minus } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { PricingCards } from "@/components/marketing/pricing-cards";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { PLANS, PRICING_NOTE } from "@/lib/pricing";

export const metadata: Metadata = {
  title: "Pricing",
  description: "Free, Team ($99/month) and Enterprise plans for ReleaseLens.",
};

type Cell = string | boolean;

const ROWS: Array<{ label: string; values: [Cell, Cell, Cell] }> = [
  { label: "Projects", values: ["1", "10", "Custom"] },
  { label: "Services", values: ["3", "50", "Custom"] },
  { label: "Data retention", values: ["7 days", "30 days", "Custom"] },
  { label: "Slack alerts", values: [false, true, true] },
  { label: "GitHub Checks", values: [false, true, true] },
];

function CellValue({ value }: { value: Cell }) {
  if (value === true) {
    return (
      <span className="inline-flex items-center gap-1.5 text-foreground">
        <Check aria-hidden="true" className="size-4 text-brand-text" />
        <span className="sr-only">Included</span>
      </span>
    );
  }
  if (value === false) {
    return (
      <span className="inline-flex items-center text-muted-foreground">
        <Minus aria-hidden="true" className="size-4" />
        <span className="sr-only">Not included</span>
      </span>
    );
  }
  return <span className="text-foreground-secondary">{value}</span>;
}

export default function PricingPage() {
  return (
    <div className="mx-auto max-w-6xl px-4 py-16 sm:px-6 sm:py-20">
      <div className="max-w-2xl">
        <p className="text-xs font-medium tracking-wide text-brand-text uppercase">Pricing</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">Plans for every team size</h1>
        <p className="mt-3 text-base leading-7 text-muted-foreground">
          Start free on one project. Move to Team when ReleaseLens watches the services you ship every day.
        </p>
      </div>

      <Callout tone="info" title="Not on sale yet" className="mt-8 max-w-2xl">
        {PRICING_NOTE}
      </Callout>

      <div className="mt-10">
        <PricingCards headingLevel={2} />
      </div>

      <section aria-labelledby="compare-title" className="mt-16">
        <h2 id="compare-title" className="text-lg font-semibold tracking-tight">
          Compare plans
        </h2>
        {/* `relative` keeps the sr-only cell labels inside the scroll container on narrow screens. */}
        <div className="relative mt-4 overflow-x-auto rounded-xl border bg-panel">
          <table className="w-full min-w-[32rem] text-sm">
            <caption className="sr-only">Plan limits and features</caption>
            <thead>
              <tr className="border-b">
                <th scope="col" className="w-1/4 px-5 py-3 text-left text-xs font-medium text-muted-foreground">
                  <span className="sr-only">Feature</span>
                </th>
                {PLANS.map((plan) => (
                  <th key={plan.id} scope="col" className="px-5 py-3 text-left font-semibold">
                    {plan.name}
                    <span className="mt-0.5 block text-xs font-normal text-muted-foreground">
                      {plan.price}
                      {plan.cadence ?? ""}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {ROWS.map((row) => (
                <tr key={row.label}>
                  <th scope="row" className="px-5 py-3 text-left font-medium text-foreground">
                    {row.label}
                  </th>
                  {row.values.map((value, index) => (
                    <td key={index} className="px-5 py-3">
                      <CellValue value={value} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          Enterprise limits, retention and terms are agreed per customer.
        </p>
      </section>

      <div className="mt-16 flex flex-wrap items-center gap-3">
        <Button asChild>
          <Link href="/docs/quick-start">
            Read the quick start
            <ArrowRight aria-hidden="true" />
          </Link>
        </Button>
        <Button asChild variant="outline">
          <Link href="/#methodology">What the model predicts</Link>
        </Button>
      </div>
    </div>
  );
}
