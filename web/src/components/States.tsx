import type { ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Inbox } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

export function EmptyState({
  title = "No data yet",
  description = "Run a sync to pull the latest league, roster, and market data.",
  action,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
}) {
  const queryClient = useQueryClient();
  const syncMutation = useMutation({
    mutationFn: () => api.sync("all"),
    onSuccess: () => {
      toast.success("Sync complete — data refreshed.");
      queryClient.invalidateQueries();
    },
    onError: (err: Error) => toast.error(`Sync failed: ${err.message}`),
  });

  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-field-700 bg-field-900/40 px-6 py-16 text-center">
      <Inbox className="h-8 w-8 text-field-600" />
      <div className="font-display text-sm font-semibold uppercase tracking-wide text-field-200">
        {title}
      </div>
      <p className="max-w-sm text-sm text-field-400">{description}</p>
      {action ?? (
        <Button onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
          {syncMutation.isPending ? "Syncing…" : "Run sync"}
        </Button>
      )}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-crimson-600/30 bg-crimson-500/5 px-6 py-16 text-center">
      <AlertTriangle className="h-8 w-8 text-crimson-500" />
      <div className="font-display text-sm font-semibold uppercase tracking-wide text-crimson-500">
        Couldn&apos;t load this
      </div>
      <p className="max-w-sm text-sm text-field-400">{message}</p>
      {onRetry && (
        <Button variant="outline" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

export function TableSkeleton({ rows = 8, cols = 6 }: { rows?: number; cols?: number }) {
  return (
    <div className="flex flex-col gap-2 p-4">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex gap-3">
          {Array.from({ length: cols }).map((__, c) => (
            <Skeleton key={c} className="h-6 flex-1" />
          ))}
        </div>
      ))}
    </div>
  );
}

export function CardSkeleton({ className }: { className?: string }) {
  return <Skeleton className={className ?? "h-32 w-full"} />;
}
