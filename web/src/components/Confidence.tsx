import { cn } from "@/lib/utils";

const LEVELS: Record<string, { pct: number; color: string; label: string }> = {
  High: { pct: 90, color: "bg-hash-500", label: "High" },
  Medium: { pct: 60, color: "bg-amber-500", label: "Medium" },
  Low: { pct: 30, color: "bg-crimson-500", label: "Low" },
};

export function ConfidencePill({ level }: { level: string | null | undefined }) {
  const meta = LEVELS[level ?? ""] ?? { pct: 0, color: "bg-field-600", label: level || "—" };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium",
        meta.label === "High" && "border-hash-600/40 bg-hash-500/10 text-hash-500",
        meta.label === "Medium" && "border-amber-600/40 bg-amber-500/10 text-amber-500",
        meta.label === "Low" && "border-crimson-600/40 bg-crimson-500/10 text-crimson-500",
        !LEVELS[level ?? ""] && "border-field-600 bg-field-800 text-field-300",
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", meta.color)} />
      {meta.label}
    </span>
  );
}

export function ConfidenceBar({ level }: { level: string | null | undefined }) {
  const meta = LEVELS[level ?? ""] ?? { pct: 0, color: "bg-field-600", label: level || "—" };
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-field-800">
        <div className={cn("h-full rounded-full", meta.color)} style={{ width: `${meta.pct}%` }} />
      </div>
      <span className="text-xs text-field-400">{meta.label}</span>
    </div>
  );
}
