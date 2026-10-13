# Accessibility check (M12)

**Date:** 2026-10-13.
**Tool:** axe-core 4.10.2, injected into each page in a Chromium-based browser.
**Rule sets:** `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa` and `best-practice`.
**Data:** signed-in pages ran against a real organisation, the measured fraud demo run, so
tables, charts and the service map were populated rather than empty states.

Lighthouse was not used: it needs a headless Chrome that is not installed here (disk). axe-core
is the engine Lighthouse's accessibility category runs on.

## Results

| Pages | Violations before | Violations after |
|---|---|---|
| Public (8): `/`, `/pricing`, `/docs`, `/changelog`, `/status`, `/login`, `/signup`, `/forgot-password` | 0 | 0 |
| Signed in (17): overview, deployments, deployment detail, regressions, risk, risk detail, services, service map, analytics, model, onboarding, settings (general, members, API keys, alerts, billing, audit log) | 3 rules (below) | **0** |

Fixed:

| Rule (impact) | Where | Fix |
|---|---|---|
| `region` (moderate) | Every dashboard page: the sidebar's brand row was outside any landmark | The desktop rail is an `<aside aria-label="Sidebar">` (`components/shell/app-shell.tsx`) |
| `scrollable-region-focusable` (serious) | Deployment detail: the before/after metrics table scrolls sideways on narrow screens. Risk detail: the full SHAP table scrolls vertically | Both containers are keyboard-focusable named regions (`tabIndex=0`, `role="region"`, `aria-label`), and use the global `:focus-visible` ring |
| `link-in-text-block` (serious) | Service map: the "Health from the latest analysis" link inside a sentence was marked by colour only | Always underlined (WCAG 1.4.1) |

## Not covered by this check

- Automated rules find roughly a third to a half of real accessibility issues. Keyboard-only
  and screen-reader walkthroughs were not done.
- Pages were checked in the default (light) theme. Colour-token contrast for both themes is unit
  tested in `apps/web/src/lib/contrast.test.ts`.
- The Playwright suite (`docs/web/E2E.md`) has not been run, because no browsers are installed
  for it.

## How to repeat

Sign in, open a page, then run this in the browser console:

```js
await new Promise((ok, fail) => {
  const s = document.createElement("script");
  s.src = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js";
  s.onload = ok; s.onerror = fail; document.head.appendChild(s);
});
(await axe.run(document, { runOnly: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"] })).violations;
```
