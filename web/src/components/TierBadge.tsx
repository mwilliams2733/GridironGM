import { cn } from "@/lib/utils";

const TIER_COLORS: Record<number, string> = {
  1: "bg-tier-1 text-field-950",
  2: "bg-tier-2 text-field-950",
  3: "bg-tier-3 text-field-950",
  4: "bg-tier-4 text-field-950",
  5: "bg-tier-5 text-field-950",
};

export function TierBadge({ tier }: { tier: number | null | undefined }) {
  if (tier === null || tier === undefined) {
    return (
      <span className="inline-flex h-5 min-w-5 items-center justify-center rounded px-1.5 font-mono text-[11px] font-medium text-field-500">
        —
      </span>
    );
  }
  const color = TIER_COLORS[tier] ?? "bg-tier-6 text-field-50";
  return (
    <span
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded px-1.5 font-mono text-[11px] font-semibold",
        color,
      )}
      title={`Tier ${tier}`}
    >
      T{tier}
    </span>
  );
}

/** Depth chart rank, e.g. RB1 / WR2. Starters read neutral; anything below the
 * top of the position group is flagged, since that's the preseason signal that
 * actually moves value. A missing rank means "not synced", not "buried". */
export function DepthBadge({
  rank,
  position,
}: {
  rank: number | null | undefined;
  position?: string | null;
}) {
  if (rank === null || rank === undefined) {
    return <span className="font-mono text-[11px] text-field-600">—</span>;
  }
  const label = `${position ?? ""}${rank}`;
  const tone =
    rank === 1
      ? "border-hash-600/40 text-hash-500"
      : rank === 2
        ? "border-amber-600/40 text-amber-500"
        : "border-field-600 text-field-500";
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center justify-center rounded border px-1.5 font-mono text-[11px] font-semibold",
        tone,
      )}
      title={
        rank === 1
          ? "Starter on the current depth chart"
          : `No. ${rank} at his position on the current depth chart`
      }
    >
      {label}
    </span>
  );
}

const POSITION_COLORS: Record<string, string> = {
  QB: "border-sky-500/40 text-sky-500",
  RB: "border-hash-600/40 text-hash-500",
  WR: "border-amber-600/40 text-amber-500",
  TE: "border-tier-3/40 text-tier-3",
  K: "border-field-500 text-field-300",
  DST: "border-crimson-600/40 text-crimson-500",
};

export function PositionBadge({ position }: { position: string }) {
  const color = POSITION_COLORS[position] ?? "border-field-500 text-field-300";
  return (
    <span
      className={cn(
        "inline-flex h-5 min-w-9 items-center justify-center rounded border px-1 font-mono text-[11px] font-semibold",
        color,
      )}
    >
      {position}
    </span>
  );
}

export function AdpDeltaBadge({ delta }: { delta: number | null | undefined }) {
  if (delta === null || delta === undefined || Number.isNaN(delta)) {
    return <span className="font-mono text-xs text-field-500">—</span>;
  }
  const falling = delta > 0;
  const flat = Math.abs(delta) < 0.5;
  return (
    <span
      className={cn(
        "font-mono text-xs tabular",
        flat ? "text-field-400" : falling ? "text-hash-500" : "text-crimson-500",
      )}
    >
      {flat ? "even" : falling ? `▼ falling ${delta.toFixed(1)}` : `▲ reach ${Math.abs(delta).toFixed(1)}`}
    </span>
  );
}
