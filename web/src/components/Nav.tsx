import { NavLink } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Radio } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const links = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/draft", label: "Draft Room" },
  { to: "/waivers", label: "Waivers" },
  { to: "/lineup", label: "Start / Sit" },
];

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "never synced";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "never synced";
  const diffMs = Date.now() - then;
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export function Nav() {
  const queryClient = useQueryClient();
  const statusQuery = useQuery({
    queryKey: ["sync-status"],
    queryFn: api.syncStatus,
    refetchInterval: 60_000,
  });

  const syncMutation = useMutation({
    mutationFn: () => api.sync("all"),
    onSuccess: () => {
      toast.success("Sync complete — data refreshed.");
      queryClient.invalidateQueries();
    },
    onError: (err: Error) => {
      toast.error(`Sync failed: ${err.message}`);
    },
  });

  // /sync/status returns one entry per scope; the header shows the most recent of them.
  const lastSynced = (statusQuery.data?.status ?? []).reduce<string | undefined>((latest, s) => {
    if (!s.last_synced) return latest;
    return !latest || s.last_synced > latest ? s.last_synced : latest;
  }, undefined);

  return (
    <header className="sticky top-0 z-40 border-b border-field-700 bg-field-950/95 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-[1400px] items-center gap-6 px-4 sm:px-6">
        <div className="flex items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded bg-hash-500 font-display text-sm font-bold text-field-950">
            G
          </span>
          <span className="font-display text-base font-semibold uppercase tracking-[0.14em] text-field-50">
            Gridiron<span className="text-hash-500">GM</span>
          </span>
        </div>

        <nav className="flex items-center gap-1">
          {links.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.end}
              className={({ isActive }) =>
                cn(
                  "rounded px-3 py-1.5 font-display text-xs font-medium uppercase tracking-wide transition-colors",
                  isActive
                    ? "bg-field-800 text-hash-500"
                    : "text-field-400 hover:bg-field-900 hover:text-field-100",
                )
              }
            >
              {l.label}
            </NavLink>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          <div className="hidden items-center gap-1.5 text-xs text-field-400 sm:flex">
            <Radio className="h-3 w-3 text-hash-500" />
            <span className="tabular">{timeAgo(lastSynced)}</span>
          </div>
          <button
            onClick={() => syncMutation.mutate()}
            disabled={syncMutation.isPending}
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-field-600 bg-field-900 px-3 font-display text-xs font-medium uppercase tracking-wide text-field-100 transition-colors hover:border-hash-500 hover:text-hash-500 disabled:opacity-50"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", syncMutation.isPending && "animate-spin")} />
            {syncMutation.isPending ? "Syncing…" : "Sync"}
          </button>
        </div>
      </div>
    </header>
  );
}
