import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowRight, Plus, Trash2, TriangleAlert } from "lucide-react";
import { api } from "@/lib/api";
import { useLeagueKey } from "@/lib/league";
import { PageHeader } from "@/components/Layout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { PositionBadge } from "@/components/TierBadge";
import { ConfidenceBar } from "@/components/Confidence";
import { EmptyState, ErrorState, CardSkeleton } from "@/components/States";
import { fmt1, fmtSigned1, cn } from "@/lib/utils";

const POSITIONS = ["QB", "RB", "WR", "TE", "K", "DST"];

function ManualRosterDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const queryClient = useQueryClient();
  const [rows, setRows] = useState<{ name: string; position: string; team: string }[]>([
    { name: "", position: "RB", team: "" },
  ]);
  const [nameQuery, setNameQuery] = useState("");
  const [activeRow, setActiveRow] = useState<number | null>(null);

  const suggestQuery = useQuery({
    queryKey: ["player-search", nameQuery],
    queryFn: () => api.league.players({ search: nameQuery, limit: 8 }),
    enabled: nameQuery.trim().length >= 2,
  });

  const saveMutation = useMutation({
    mutationFn: () =>
      api.league.setRoster({
        players: rows.filter((r) => r.name.trim()).map((r) => ({ ...r, team: r.team || undefined })),
      }),
    onSuccess: (res) => {
      if (res.unresolved.length > 0) {
        toast.warning(`Saved. ${res.unresolved.length} player(s) couldn't be matched: ${res.unresolved.join(", ")}`);
      } else {
        toast.success("Roster saved.");
      }
      queryClient.invalidateQueries();
      onOpenChange(false);
    },
    onError: (err: Error) => toast.error(`Couldn't save roster: ${err.message}`),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Enter your roster</DialogTitle>
          <DialogDescription>
            No linked league. Type each player's name — we'll match against the player database.
          </DialogDescription>
        </DialogHeader>

        <div className="flex max-h-80 flex-col gap-2 overflow-y-auto">
          {rows.map((row, i) => (
            <div key={i} className="relative flex items-center gap-2">
              <Input
                placeholder="Player name"
                value={row.name}
                onChange={(e) => {
                  const v = e.target.value;
                  setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, name: v } : r)));
                  setNameQuery(v);
                  setActiveRow(i);
                }}
                onFocus={() => setActiveRow(i)}
                className="flex-1"
              />
              <Select
                value={row.position}
                onValueChange={(v) =>
                  setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, position: v } : r)))
                }
              >
                <SelectTrigger className="w-20">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {POSITIONS.map((p) => (
                    <SelectItem key={p} value={p}>
                      {p}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Input
                placeholder="Team"
                value={row.team}
                onChange={(e) =>
                  setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, team: e.target.value } : r)))
                }
                className="w-16"
              />
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setRows((prev) => prev.filter((_, idx) => idx !== i))}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>

              {activeRow === i && nameQuery.trim().length >= 2 && (suggestQuery.data?.players.length ?? 0) > 0 && (
                <div className="absolute left-0 top-10 z-10 w-64 rounded-md border border-field-700 bg-field-850 shadow-xl">
                  {suggestQuery.data!.players.slice(0, 6).map((p) => (
                    <button
                      key={p.player_id}
                      type="button"
                      className="flex w-full items-center justify-between px-3 py-1.5 text-left text-sm text-field-200 hover:bg-field-800"
                      onClick={() => {
                        setRows((prev) =>
                          prev.map((r, idx) =>
                            idx === i
                              ? { name: p.name, position: p.position, team: p.team ?? "" }
                              : r,
                          ),
                        );
                        setNameQuery("");
                        setActiveRow(null);
                      }}
                    >
                      <span>{p.name}</span>
                      <span className="text-xs text-field-500">
                        {p.position} · {p.team ?? "FA"}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() => setRows((prev) => [...prev, { name: "", position: "RB", team: "" }])}
        >
          <Plus className="h-3.5 w-3.5" />
          Add player
        </Button>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending}>
            {saveMutation.isPending ? "Saving…" : "Save roster"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function Lineup() {
  const leagueKey = useLeagueKey();
  const [week, setWeek] = useState<string>("current");
  const [modalOpen, setModalOpen] = useState(false);

  const rosterQuery = useQuery({ queryKey: ["league-roster", leagueKey], queryFn: api.league.roster });
  const weekParam = week === "current" ? undefined : Number(week);
  const lineupQuery = useQuery({
    queryKey: ["lineup-optimal", leagueKey, weekParam],
    queryFn: () => api.lineup.optimal({ week: weekParam }),
    enabled: rosterQuery.data?.mode !== "none",
  });

  const weekOptions = useMemo(() => ["current", ...Array.from({ length: 18 }, (_, i) => String(i + 1))], []);

  if (rosterQuery.isLoading) {
    return (
      <div>
        <PageHeader eyebrow="This Week" title="Start / Sit" />
        <CardSkeleton className="h-64" />
      </div>
    );
  }

  if (rosterQuery.data?.mode === "none") {
    return (
      <div>
        <PageHeader eyebrow="This Week" title="Start / Sit" />
        <EmptyState
          title="No roster connected"
          description="Enter your roster manually to get optimal-lineup recommendations, or sync ESPN from the nav."
          action={<Button onClick={() => setModalOpen(true)}>Enter roster</Button>}
        />
        <ManualRosterDialog open={modalOpen} onOpenChange={setModalOpen} />
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        eyebrow="This Week"
        title="Start / Sit"
        description="Optimal lineup vs. your current lineup, side by side."
        actions={
          <Select value={week} onValueChange={setWeek}>
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {weekOptions.map((w) => (
                <SelectItem key={w} value={w}>
                  {w === "current" ? "Next week" : `Week ${w}`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />

      {lineupQuery.isLoading ? (
        <CardSkeleton className="h-96" />
      ) : lineupQuery.isError ? (
        <ErrorState message={(lineupQuery.error as Error).message} onRetry={() => lineupQuery.refetch()} />
      ) : !lineupQuery.data || lineupQuery.data.starters.length === 0 ? (
        <EmptyState
          title="No lineup to optimize"
          description="Sync projections and your roster to see this week's optimal lineup."
        />
      ) : (
        <>
          <div
            className={cn(
              "mb-6 flex items-center gap-3 rounded-md border px-4 py-3",
              (lineupQuery.data.delta ?? 0) > 0.5
                ? "border-hash-600/40 bg-hash-500/5"
                : "border-field-700 bg-field-900",
            )}
          >
            <ArrowRight className="h-4 w-4 text-hash-500" />
            <span className="text-sm text-field-200">
              Your current lineup leaves{" "}
              <strong className="tabular text-hash-500">{fmtSigned1(lineupQuery.data.delta)}</strong>{" "}
              points on the bench vs. the optimal set.
            </span>
          </div>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_280px]">
            <div>
              <Card>
                <CardHeader>
                  <CardTitle>Optimal Lineup — Week {lineupQuery.data.week}</CardTitle>
                  <span className="tabular font-mono text-sm text-hash-500">
                    {fmt1(lineupQuery.data.optimal_total)} pts
                  </span>
                </CardHeader>
                <CardContent className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  {lineupQuery.data.starters.map((s) => (
                    <div
                      key={s.slot}
                      className="flex items-center justify-between gap-3 rounded-md border border-field-700 bg-field-900 p-3"
                    >
                      <div className="flex items-center gap-2">
                        <span className="w-10 font-mono text-[10px] uppercase text-field-500">{s.slot}</span>
                        {s.player ? (
                          <div>
                            <div className="text-sm font-medium text-field-100">{s.player.name}</div>
                            <div className="flex items-center gap-1.5 text-xs text-field-500">
                              <PositionBadge position={s.player.position} />
                              {s.player.team ?? "—"}
                            </div>
                          </div>
                        ) : (
                          <span className="text-sm text-field-500">Empty</span>
                        )}
                      </div>
                      <div className="flex flex-col items-end gap-1">
                        <span className="tabular text-sm font-semibold text-field-100">
                          {fmt1(s.player?.proj_points)}
                        </span>
                        <ConfidenceBar level={s.player?.confidence} />
                      </div>
                    </div>
                  ))}
                </CardContent>
              </Card>

              <Card className="mt-6">
                <CardHeader>
                  <CardTitle>Bench</CardTitle>
                </CardHeader>
                <CardContent className="flex flex-wrap gap-2">
                  {lineupQuery.data.bench.length === 0 ? (
                    <span className="text-xs text-field-500">Empty bench.</span>
                  ) : (
                    lineupQuery.data.bench.map((p) => (
                      <div
                        key={p.player_id}
                        className="flex items-center gap-2 rounded-md border border-field-700 bg-field-900 px-3 py-1.5"
                      >
                        <PositionBadge position={p.position} />
                        <span className="text-sm text-field-200">{p.name}</span>
                        <span className="tabular text-xs text-field-500">{fmt1(p.proj_points)}</span>
                      </div>
                    ))
                  )}
                </CardContent>
              </Card>
            </div>

            <Card className="h-fit">
              <CardHeader>
                <CardTitle>Close Calls</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {lineupQuery.data.close_calls.length === 0 ? (
                  <span className="text-xs text-field-500">No razor-thin decisions this week.</span>
                ) : (
                  lineupQuery.data.close_calls.map((c, i) => (
                    <div key={i} className="rounded-md border border-amber-600/30 bg-amber-500/5 p-3">
                      <div className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-amber-500">
                        <TriangleAlert className="h-3.5 w-3.5" />
                        {c.slot}
                      </div>
                      <div className="mt-1 text-sm text-field-200">
                        {c.starter} <span className="text-field-500">over</span> {c.alt}
                      </div>
                      <div className="mt-0.5 tabular text-xs text-field-400">
                        margin {fmt1(c.margin)} pts
                      </div>
                    </div>
                  ))
                )}
              </CardContent>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
