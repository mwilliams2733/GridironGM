import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function StatTile({
  label,
  value,
  sub,
  accent = false,
  icon,
  className,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  accent?: boolean;
  icon?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-1 rounded-lg border border-field-700 bg-field-850/80 p-4",
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <span className="font-display text-[11px] font-medium uppercase tracking-[0.1em] text-field-400">
          {label}
        </span>
        {icon}
      </div>
      <span
        className={cn(
          "font-display text-3xl font-semibold tabular tracking-tight",
          accent ? "text-hash-500" : "text-field-50",
        )}
      >
        {value}
      </span>
      {sub && <span className="text-xs text-field-400">{sub}</span>}
    </div>
  );
}
