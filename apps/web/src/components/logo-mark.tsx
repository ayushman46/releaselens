import type { SVGProps } from "react";

/** ReleaseLens mark: a lens over a baseline tick. Inherits `currentColor`. */
export function LogoMark(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...props}>
      <circle cx="10.5" cy="10.5" r="6.25" stroke="currentColor" strokeWidth="2" />
      <path d="m15.25 15.25 5 5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      <path
        d="M7 11.5h1.75l1.25-2.5 1.5 4 1.25-1.5H14"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
