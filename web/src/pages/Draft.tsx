import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Download, Redo2, RotateCcw, Search, Undo2, Zap } from "lucide-react";
import { api } from "@/lib/api";
import { useLeague, useLeagueKey } from "@/lib/league";
import type { VorpRow } from "@/lib/types";
import { PageHeader } from "@/components/Layout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { TierBadge, PositionBadge, AdpDeltaBadge } from "@/components/TierBadge";
import { EmptyState, ErrorState, TableSkeleton, CardSkeleton } from "@/components/States";
import { cn, fmt1 } from "@/lib/utils";

const POSITIONS = ["ALL", "QB", "RB", "WR", "TE", "K", "DST"];

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function RecommendationHero({
  row,
  onDraft,
  pending,
}: {
  row: VorpRow;
  onDraft: (id: string) => void;
  pending: boolean;
}) {
  return (
    <div className="relative overflow-hidden rounded-lg border border-hash-600/40 bg-gradient-to-br from-hash-500/10 via-field-850 to-field-850 p-5">
      <div className="absolute right-4 top-4 font-mono text-[10px] uppercase tracking-[0.2em] text-hash-500">
        On the clock
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <PositionBadge position={row.position} />
        <TierBadge tier={row.tier} />
        <h2 className="font-display text-2xl font-semibold text-field-50">{row.name}</h2>
        <span className="text-sm text-field-400">{row.team ?? "FA"}</span>
      </div>
      <div className="mt-3 flex flex-wrap gap-6">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-wide text-field-500">Proj</div>
          <div className="tabular font-display text-xl text-field-50">{fmt1(row.proj)}</div>
        </div>
        <div>
          <div className="font-mono text-[10px] uppercase tracking-wide text-field-500">VORP</div>
          <div className="tabular font-display text-xl text-hash-500">{fmt1(row.vorp)}</div>
        </div>
        <div>
          <div className="font-mono text-[10px] uppercase tracking-wide text-field-500">ADP</div>
          <div className="tabular font-display text-xl text-field-50">{fmt1(row.adp)}</div>
        </div>
        <div>
          <div className="font-mono text-[10px] uppercase tracking-wide text-field-500">Bye</div>
          <div className="tabular font-display text-xl text-field-50">{row.bye ?? "—"}</div>
        </div>
      </div>
      {row.rationale && <p className="mt-3 text-sm text-field-300">{row.rationale}</p>}
      <Button className="mt-4" onClick={() => onDraft(row.player_id)} disabled={pending}>
        <Zap className="h-4 w-4" />
        Draft {row.name.split(" ")[0]}
      </Button>
    </div>
  );
}

function AlternativeCard({
  row,
  onDraft,
  pending,
}: {
  row: VorpRow;
  onDraft: (id: string) => void;
  pending: boolean;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-md border border-field-700 bg-field-900 p-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <PositionBadge position={row.position} />
          <TierBadge tier={row.tier} />
        </div>
        <span className="tabular font-mono text-xs text-hash-500">{fmt1(row.vorp)} vorp</span>
      </div>
      <div className="text-sm font-medium text-field-100">{row.name}</div>
      <div className="flex items-center justify-between">
        <span className="text-xs text-field-500">{row.team ?? "FA"} · proj {fmt1(row.proj)}</span>
        <Button size="sm" variant="outline" onClick={() => onDraft(row.player_id)} disabled={pending}>
          Draft
        </Button>
      </div>
    </div>
  );
}

export function Draft() {
  const queryClient = useQueryClient();
  const leagueKey = useLeagueKey();
  const { league } = useLeague();
  const [search, setSearch] = useState("");
  const [position, setPosition] = useState("ALL");
  const [sortKey, setSortKey] = useState<"vorp" | "proj" | "adp" | "tier">("vorp");
  const [slotInput, setSlotInput] = useState("1");
  // null = follow the snake order. Override only for trades / out-of-order entry.
  const [assignSlot, setAssignSlot] = useState<number | null>(null);

  const configQuery = useQuery({ queryKey: ["config", leagueKey], queryFn: api.config });
  const boardQuery = useQuery({ queryKey: ["draft-board", leagueKey], queryFn: () => api.draft.board(300) });
  const recQuery = useQuery({ queryKey: ["draft-rec", leagueKey], queryFn: api.draft.recommendation });
  const myRosterQuery = useQuery({ queryKey: ["draft-my-roster", leagueKey], queryFn: api.draft.myRoster });

  const invalidateAll = () => {
    queryClient.invalidateQueries({ queryKey: ["draft-board"] });
    queryClient.invalidateQueries({ queryKey: ["draft-rec"] });
    queryClient.invalidateQueries({ queryKey: ["draft-my-roster"] });
  };

  const pickMutation = useMutation({
    mutationFn: ({ player_id, slot }: { player_id: string; slot?: number }) =>
      api.draft.pick({ player_id, slot }),
    onSuccess: (data) => {
      const p = data.pick;
      if (p) {
        toast.success(
          p.by_me
            ? `${p.name} — your pick (${p.overall} overall).`
            : `${p.name} → Team ${p.slot} (${p.overall} overall).`,
        );
      } else {
        toast.success("Pick logged.");
      }
      invalidateAll();
    },
    onError: (err: Error) => toast.error(`Pick failed: ${err.message}`),
  });

  /** Log a pick. Omitting the slot lets the server assign it from the snake order. */
  const draftPlayer = (player_id: string) =>
    pickMutation.mutate({ player_id, slot: assignSlot ?? undefined });

  const espnSyncMutation = useMutation({
    mutationFn: api.draft.syncEspn,
    onSuccess: (r) => {
      if (r.added === 0) {
        toast("ESPN has no new picks.");
      } else {
        toast.success(`Pulled ${r.added} pick${r.added === 1 ? "" : "s"} from ESPN.`);
      }
      if (r.unresolved.length > 0) {
        toast.warning(
          `${r.unresolved.length} ESPN player${r.unresolved.length === 1 ? "" : "s"} couldn't be matched — enter manually.`,
        );
      }
      invalidateAll();
    },
    onError: (err: Error) => toast.error(`ESPN sync failed: ${err.message}`),
  });

  const undoMutation = useMutation({
    mutationFn: api.draft.undo,
    onSuccess: () => {
      toast("Last pick undone.");
      invalidateAll();
    },
    onError: (err: Error) => toast.error(`Undo failed: ${err.message}`),
  });

  const resetMutation = useMutation({
    mutationFn: (my_slot?: number) => api.draft.reset(my_slot ? { my_slot } : {}),
    onSuccess: () => {
      toast.success("Draft reset.");
      invalidateAll();
    },
    onError: (err: Error) => toast.error(`Reset failed: ${err.message}`),
  });

  const board = boardQuery.data?.board ?? [];
  const filtered = useMemo(() => {
    let rows = board;
    if (position !== "ALL") rows = rows.filter((r) => r.position === position);
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      rows = rows.filter((r) => r.name.toLowerCase().includes(q));
    }
    return [...rows].sort((a, b) => {
      if (sortKey === "adp") return num(a.adp) - num(b.adp);
      if (sortKey === "tier") return num(a.tier) - num(b.tier);
      return num(b[sortKey]) - num(a[sortKey]);
    });
  }, [board, position, search, sortKey]);

  const needsSetup =
    configQuery.data?.draft.my_slot == null && boardQuery.data?.my_next_pick == null;

  const runs = boardQuery.data?.runs ?? {};
  const activeRuns = Object.entries(runs).filter(([, count]) => count >= 3);
  // tier_depth is position -> tier -> remaining count. The actionable signal is the
  // cliff: how many players are left in the best tier still on the board at each
  // position, which is what the red/amber thresholds below are calibrated against.
  const tierDepth = Object.entries(boardQuery.data?.tier_depth ?? {}).reduce<
    Record<string, { tier: number; count: number }>
  >((acc, [pos, tiers]) => {
    const best = Object.entries(tiers)
      .map(([tier, count]) => ({ tier: Number(tier), count: Number(count) }))
      .filter((t) => Number.isFinite(t.tier) && t.count > 0)
      .sort((a, b) => a.tier - b.tier)[0];
    if (best) acc[pos] = best;
    return acc;
  }, {});

  const teams = boardQuery.data?.teams ?? [];
  const onTheClock = boardQuery.data?.on_the_clock ?? null;
  const mySlot = boardQuery.data?.my_slot ?? null;
  const currentRound = boardQuery.data?.current_round ?? null;
  // Which team the next logged pick lands on: the override if set, else the snake.
  const effectiveSlot = assignSlot ?? onTheClock;
  const isMyPick = effectiveSlot != null && effectiveSlot === mySlot;
  const assignLabel =
    effectiveSlot == null
      ? "the next team"
      : `Team ${effectiveSlot}${effectiveSlot === mySlot ? " (you)" : ""}`;

  if (configQuery.isLoading || boardQuery.isLoading) {
    return (
      <div>
        <PageHeader eyebrow="Live Board" title="Draft Room" />
        <CardSkeleton className="h-40" />
        <div className="mt-4">
          <TableSkeleton rows={10} cols={7} />
        </div>
      </div>
    );
  }

  if (boardQuery.isError) {
    return (
      <div>
        <PageHeader eyebrow="Live Board" title="Draft Room" />
        <ErrorState message={(boardQuery.error as Error).message} onRetry={() => boardQuery.refetch()} />
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        eyebrow="Live Board"
        title="Draft Room"
        description={
          boardQuery.data
            ? `${boardQuery.data.league.name} · ${boardQuery.data.league.teams} teams · pick ${boardQuery.data.current_pick} (rd ${currentRound}) · ${boardQuery.data.drafted_count} drafted · your next pick #${boardQuery.data.my_next_pick ?? "—"}`
            : undefined
        }
        actions={
          <>
            {league?.espn_configured && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => espnSyncMutation.mutate()}
                disabled={espnSyncMutation.isPending}
                title="Pull picks made in the ESPN draft room"
              >
                <Download className={cn("h-3.5 w-3.5", espnSyncMutation.isPending && "animate-pulse")} />
                {espnSyncMutation.isPending ? "Syncing…" : "ESPN"}
              </Button>
            )}
            <Button variant="ghost" size="sm" onClick={() => undoMutation.mutate()} disabled={undoMutation.isPending}>
              <Undo2 className="h-3.5 w-3.5" />
              Undo
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => resetMutation.mutate(undefined)}
              disabled={resetMutation.isPending}
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Reset
            </Button>
          </>
        }
      />

      {!needsSetup && onTheClock != null && (
        <div
          className={cn(
            "mb-6 flex flex-wrap items-center gap-3 rounded-lg border px-4 py-3",
            isMyPick
              ? "border-hash-600/50 bg-hash-500/10"
              : "border-field-700 bg-field-900",
          )}
        >
          <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-field-500">
            On the clock
          </span>
          <span
            className={cn(
              "font-display text-lg font-semibold",
              isMyPick ? "text-hash-500" : "text-field-100",
            )}
          >
            {isMyPick ? "Your pick" : `Team ${onTheClock}`}
          </span>

          <label className="ml-auto flex items-center gap-2 text-xs text-field-400">
            Log pick to
            <select
              value={assignSlot ?? ""}
              onChange={(e) => setAssignSlot(e.target.value === "" ? null : Number(e.target.value))}
              className="h-8 rounded-md border border-field-600 bg-field-900 px-2 font-mono text-xs text-field-100 focus:border-hash-500 focus:outline-none"
            >
              <option value="">Auto — Team {onTheClock}</option>
              {teams.map((t) => (
                <option key={t.slot} value={t.slot}>
                  Team {t.slot}
                  {t.is_me ? " (you)" : ""}
                </option>
              ))}
            </select>
          </label>
          {assignSlot != null && (
            <Button variant="ghost" size="sm" onClick={() => setAssignSlot(null)}>
              Back to auto
            </Button>
          )}
        </div>
      )}

      {needsSetup && (
        <Card className="mb-6 border-hash-600/40">
          <CardHeader>
            <CardTitle>Set your draft slot</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-3">
            <p className="text-sm text-field-400">
              Pick your position (1–{configQuery.data?.league.teams ?? 12}) in the snake order to get
              personalized recommendations and roster tracking.
            </p>
            <Input
              type="number"
              min={1}
              max={configQuery.data?.league.teams ?? 12}
              value={slotInput}
              onChange={(e) => setSlotInput(e.target.value)}
              className="w-20"
            />
            <Button onClick={() => resetMutation.mutate(Number(slotInput))} disabled={resetMutation.isPending}>
              Start draft
            </Button>
          </CardContent>
        </Card>
      )}

      {activeRuns.length > 0 && (
        <div className="mb-6 flex flex-wrap items-center gap-2 rounded-md border border-amber-600/40 bg-amber-500/5 px-3 py-2 text-sm text-amber-500">
          <Redo2 className="h-4 w-4" />
          Positional run:
          {activeRuns.map(([pos, count]) => (
            <Badge key={pos} variant="amber">
              {count} {pos} in last 8 picks
            </Badge>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1fr_320px]">
        <div className="flex flex-col gap-6">
          {recQuery.isLoading ? (
            <CardSkeleton className="h-40" />
          ) : recQuery.data?.recommended ? (
            <div>
              <RecommendationHero
                row={recQuery.data.recommended}
                onDraft={draftPlayer}
                pending={pickMutation.isPending}
              />
              {recQuery.data.alternatives.length > 0 && (
                <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  {recQuery.data.alternatives.map((r) => (
                    <AlternativeCard
                      key={r.player_id}
                      row={r}
                      onDraft={draftPlayer}
                      pending={pickMutation.isPending}
                    />
                  ))}
                </div>
              )}
            </div>
          ) : (
            <EmptyState title="No recommendation" description="Sync ADP + projections to populate the board." />
          )}

          <Card>
            <CardHeader className="flex-wrap gap-3">
              <CardTitle>Available Players</CardTitle>
              <div className="flex flex-1 flex-wrap items-center justify-end gap-2">
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-field-500" />
                  <Input
                    placeholder="Search players…"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    className="w-48 pl-7"
                  />
                </div>
                <Select value={position} onValueChange={setPosition}>
                  <SelectTrigger className="w-24">
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
              </div>
            </CardHeader>
            <CardContent className="max-h-[640px] overflow-y-auto p-0">
              {filtered.length === 0 ? (
                <div className="p-4">
                  <EmptyState title="No players match" description="Adjust search or position filter." />
                </div>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Player</TableHead>
                      <TableHead>Pos</TableHead>
                      <TableHead role="button" onClick={() => setSortKey("tier")} className="cursor-pointer">
                        Tier
                      </TableHead>
                      <TableHead role="button" onClick={() => setSortKey("proj")} className="cursor-pointer">
                        Proj
                      </TableHead>
                      <TableHead role="button" onClick={() => setSortKey("vorp")} className="cursor-pointer">
                        VORP
                      </TableHead>
                      <TableHead role="button" onClick={() => setSortKey("adp")} className="cursor-pointer">
                        ADP
                      </TableHead>
                      <TableHead>Trend</TableHead>
                      <TableHead>Bye</TableHead>
                      <TableHead></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filtered.map((r) => (
                      <TableRow key={r.player_id}>
                        <TableCell className="font-medium text-field-100">{r.name}</TableCell>
                        <TableCell>
                          <PositionBadge position={r.position} />
                        </TableCell>
                        <TableCell>
                          <TierBadge tier={r.tier} />
                        </TableCell>
                        <TableCell className="tabular text-field-300">{fmt1(r.proj)}</TableCell>
                        <TableCell className="tabular font-medium text-hash-500">{fmt1(r.vorp)}</TableCell>
                        <TableCell className="tabular text-field-300">{fmt1(r.adp)}</TableCell>
                        <TableCell>
                          <AdpDeltaBadge delta={r.adp_delta} />
                        </TableCell>
                        <TableCell className="tabular text-field-400">{r.bye ?? "—"}</TableCell>
                        <TableCell>
                          <Button
                            size="sm"
                            variant={isMyPick ? "default" : "subtle"}
                            onClick={() => draftPlayer(r.player_id)}
                            disabled={pickMutation.isPending}
                            title={`Assign to ${assignLabel}`}
                          >
                            Draft
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="flex flex-col gap-6">
          <Card>
            <CardHeader>
              <CardTitle>Tier Depth</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              {Object.keys(tierDepth).length === 0 ? (
                <p className="text-xs text-field-500">No tier data yet.</p>
              ) : (
                Object.entries(tierDepth).map(([pos, { tier, count }]) => (
                  <div key={pos} className="flex items-center gap-2">
                    <span className="w-10 font-mono text-xs text-field-400">{pos}</span>
                    <span className="w-8 font-mono text-[10px] text-field-500">T{tier}</span>
                    <div className="h-2 flex-1 overflow-hidden rounded-full bg-field-800">
                      <div
                        className={cn(
                          "h-full rounded-full",
                          count <= 1 ? "bg-crimson-500" : count <= 3 ? "bg-amber-500" : "bg-hash-500",
                        )}
                        style={{ width: `${Math.min(100, count * 12)}%` }}
                      />
                    </div>
                    <span className="w-5 text-right font-mono text-xs tabular text-field-300">{count}</span>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>My Roster</CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              {myRosterQuery.isLoading ? (
                <TableSkeleton rows={6} cols={2} />
              ) : (myRosterQuery.data?.picks.length ?? 0) === 0 ? (
                <div className="p-4">
                  <EmptyState
                    title="No picks yet"
                    description="Drafted players assigned to you will fill in here."
                    action={<span />}
                  />
                </div>
              ) : (
                <ul className="divide-y divide-field-800">
                  {myRosterQuery.data!.picks.map((p) => (
                    <li key={p.player_id} className="flex items-center justify-between px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <span className="w-11 font-mono text-[10px] uppercase text-field-500">
                          {p.slot ?? p.position}
                        </span>
                        <span className="text-sm text-field-100">{p.name}</span>
                      </div>
                      <PositionBadge position={p.position} />
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {teams.length > 0 && (
        <Card className="mt-6">
          <CardHeader>
            <CardTitle>Rosters by team</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
              {teams.map((t) => (
                <div
                  key={t.slot}
                  className={cn(
                    "rounded-md border p-3",
                    t.is_me
                      ? "border-hash-600/50 bg-hash-500/5"
                      : t.slot === onTheClock
                        ? "border-field-500 bg-field-900"
                        : "border-field-700 bg-field-900",
                  )}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span
                      className={cn(
                        "font-display text-xs font-semibold uppercase tracking-wide",
                        t.is_me ? "text-hash-500" : "text-field-200",
                      )}
                    >
                      {t.name}
                    </span>
                    <span className="font-mono text-[10px] text-field-500">
                      {t.picks.length} pick{t.picks.length === 1 ? "" : "s"}
                    </span>
                  </div>

                  {t.picks.length === 0 ? (
                    <p className="mt-2 text-xs text-field-600">—</p>
                  ) : (
                    <ul className="mt-2 flex flex-col gap-1">
                      {t.picks.map((p) => (
                        <li key={p.player_id} className="flex items-center gap-1.5">
                          <span className="w-6 font-mono text-[10px] text-field-600">
                            {p.overall}
                          </span>
                          <PositionBadge position={p.position ?? "?"} />
                          <span className="truncate text-xs text-field-200">{p.name}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
