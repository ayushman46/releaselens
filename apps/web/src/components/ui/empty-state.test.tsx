import { render, screen } from "@testing-library/react";
import { Rocket } from "lucide-react";
import Link from "next/link";
import { describe, expect, it } from "vitest";

import { EmptyState } from "@/components/ui/empty-state";

describe("EmptyState", () => {
  it("renders the icon, a heading, the guidance and the actions", () => {
    const { container } = render(
      <EmptyState
        icon={Rocket}
        title="Send your first deployment event"
        description="Your pipeline tells ReleaseLens when a release went out."
        action={<Link href="/docs/deployment-api">Read the guide</Link>}
        secondaryAction={<Link href="/app/services">Set up traces</Link>}
      >
        <pre>curl …</pre>
      </EmptyState>,
    );

    expect(screen.getByRole("heading", { level: 2, name: "Send your first deployment event" })).toBeInTheDocument();
    expect(screen.getByText("Your pipeline tells ReleaseLens when a release went out.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Read the guide" })).toHaveAttribute("href", "/docs/deployment-api");
    expect(screen.getByRole("link", { name: "Set up traces" })).toBeInTheDocument();
    expect(screen.getByText("curl …")).toBeInTheDocument();

    const icon = container.querySelector("svg");
    expect(icon).toHaveAttribute("aria-hidden", "true");
  });

  it("supports a level-3 heading for nested use and omits empty action rows", () => {
    const { container } = render(
      <EmptyState icon={Rocket} title="No API keys yet" description="Keys arrive in M10." headingLevel={3} variant="bare" />,
    );
    expect(screen.getByRole("heading", { level: 3, name: "No API keys yet" })).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
    expect(container.querySelector('[data-slot="empty-state"]')).not.toHaveClass("border-dashed");
  });
});
