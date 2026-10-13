import type { ReactNode } from "react";

/** The frame of every auth page: one <h1>, a line of guidance, the form, and footer links. */
export function AuthCard({
  title,
  description,
  children,
  footer,
}: {
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div>
      <h1 className="text-xl font-semibold tracking-tight text-foreground">{title}</h1>
      {description ? <p className="mt-1.5 text-sm leading-6 text-muted-foreground">{description}</p> : null}
      {children ? <div className="mt-6 rounded-xl border bg-panel p-5 shadow-panel sm:p-6">{children}</div> : null}
      {footer ? <div className="mt-5 space-y-2 text-center text-sm text-muted-foreground">{footer}</div> : null}
    </div>
  );
}
