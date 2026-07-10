import type { ReactNode } from "react";
import { Nav } from "@/components/Nav";

export function Layout({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-field-950">
      <Nav />
      <main className="mx-auto max-w-[1400px] px-4 py-6 sm:px-6">{children}</main>
    </div>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4 border-b border-field-800 pb-4">
      <div>
        {eyebrow && (
          <div className="mb-1 font-mono text-[11px] uppercase tracking-[0.2em] text-hash-500">
            {eyebrow}
          </div>
        )}
        <h1 className="font-display text-2xl font-semibold uppercase tracking-wide text-field-50">
          {title}
        </h1>
        {description && <p className="mt-1 text-sm text-field-400">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
