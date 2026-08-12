import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { StatTile } from "@/components/StatTile";
import { EmptyState, ErrorState, CardSkeleton } from "@/components/States";
import { PositionBadge } from "@/components/TierBadge";
import { ConfidencePill } from "@/components/Confidence";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmt1, fmtSigned1 } from "@/lib/utils";

function num(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  return null;
}
function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export function Dashboard() {
  const dashboardQuery = useQuery({ queryKey: ["dashboard"], queryFn: api.dashboard });

  if (dashboardQuery.isLoading) {
    return (
      <div>
        <PageHeader eyebrow="Home Base" title="Dashboard" description="Loading this week's picture…" />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <CardSkeleton key={i} className="h-24" />
          ))}
        </div>
        <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
          <CardSkeleton className="h-80 lg:col-span-2" />
          <CardSkeleton className="h-80" />
        </div>
      </div>
    );
  }

  if (dashboardQuery.isError) {
    return (
      <div>
        <PageHeader eyebrow="Home Base" title="Dashboard" />
        <ErrorState
          message={(dashboardQuery.error as Error).message}
          onRetry={() => dashboardQuery.refetch()}
        />
      </div>
    );
  }

  const data = dashboardQuery.data!;
  const roster = data.my_team?.team?.roster ?? [];
  const hasTeam = roster.length > 0;
  const optimal = data.optimal_lineup;
  const waivers = data.top_waivers ?? [];
  const standings = data.standings ?? [];
  const oddsBoard = data.odds_board ?? [];

  // odds_board is already one row per team (2 per matchup), so map rather than flatMap.
  const chartData = oddsBoard
    .map((g) => {
      const label = str(g.team);
      const total = num(g.implied_total);
      return label && total !== null ? { label, total } : null;
    })
    .filter((r): r is { label: string; total: number } => r !== null)
    .sort((a, b) => b.total - a.total)
    .slice(0, 16);

  return (
    <div>
      <PageHeader
        eyebrow="Week in Review"
        title="Dashboard"
        description={data.warning ?? "Everything you need before kickoff, in one call."}
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Optimal Total"
          value={fmt1(num(optimal?.optimal_total))}
          sub="This week's ceiling lineup"
          accent
        />
        <StatTile
          label="Current Total"
          value={fmt1(num(optimal?.current_total))}
          sub="Your set lineup"
        />
        <StatTile
          label="Delta"
          value={fmtSigned1(num(optimal?.delta))}
          sub="Points left on the bench"
        />
        <StatTile
          label="Roster Mode"
          value={!hasTeam ? "None" : data.my_team?.mode === "espn" ? "Linked" : "Manual"}
          sub={hasTeam ? `${roster.length} players` : "Connect a roster"}
        />
      </div>

      <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Vegas Board — Implied Team Totals</CardTitle>
          </CardHeader>
          <CardContent>
            {chartData.length === 0 ? (
              <EmptyState
                title="No odds loaded"
                description="Sync to pull this week's Vegas lines and implied totals."
              />
            ) : (
              <ResponsiveContainer width="100%" height={320}>
                <BarChart data={chartData} layout="vertical" margin={{ left: 8, right: 24 }}>
                  <CartesianGrid horizontal={false} stroke="var(--color-field-800)" />
                  <XAxis
                    type="number"
                    tick={{ fill: "var(--color-field-400)", fontSize: 11 }}
                    stroke="var(--color-field-700)"
                    domain={[0, "dataMax + 3"]}
                  />
                  <YAxis
                    type="category"
                    dataKey="label"
                    width={44}
                    tick={{ fill: "var(--color-field-300)", fontSize: 11, fontFamily: "var(--font-mono)" }}
                    stroke="var(--color-field-700)"
                  />
                  <Tooltip
                    cursor={{ fill: "rgba(255,255,255,0.03)" }}
                    contentStyle={{
                      background: "var(--color-field-850)",
                      border: "1px solid var(--color-field-700)",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    formatter={(value) => [Number(value).toFixed(1), "Implied pts"]}
                  />
                  <Bar dataKey="total" radius={[0, 4, 4, 0]} maxBarSize={16}>
                    {chartData.map((d, i) => (
                      <Cell
                        key={i}
                        fill={d.total >= 24 ? "var(--color-hash-500)" : "var(--color-field-500)"}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Top Waiver Targets</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {waivers.length === 0 ? (
              <div className="p-4">
                <EmptyState
                  title="No waiver data"
                  description="Sync free-agent data to see ranked adds."
                />
              </div>
            ) : (
              <ul className="divide-y divide-field-800">
                {waivers.slice(0, 5).map((w) => (
                  <li key={w.player_id} className="flex items-center justify-between gap-2 px-4 py-3">
                    <div className="flex items-center gap-2">
                      <PositionBadge position={w.pos} />
                      <div>
                        <div className="text-sm font-medium text-field-100">{w.name}</div>
                        <div className="text-xs text-field-500">{w.team ?? "FA"}</div>
                      </div>
                    </div>
                    <div className="flex items-center gap-3">
                      <span className="font-mono text-xs tabular text-hash-500">
                        ${num(w.faab_bid) ?? 0}
                      </span>
                      <ConfidencePill level={w.confidence} />
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>My Team</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {!hasTeam ? (
              <div className="p-4">
                <EmptyState
                  title="No roster connected"
                  description="Sync ESPN or add a manual roster to see your team here."
                />
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Player</TableHead>
                    <TableHead>Pos</TableHead>
                    <TableHead>Team</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {roster.map((p) => (
                    <TableRow key={p.player_id}>
                      <TableCell className="font-medium text-field-100">{p.name}</TableCell>
                      <TableCell>
                        <PositionBadge position={p.position} />
                      </TableCell>
                      <TableCell className="text-field-400">{p.team ?? "—"}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Standings</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {standings.length === 0 ? (
              <div className="p-4">
                <EmptyState
                  title="No standings"
                  description="Sync your ESPN league to load standings."
                />
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Team</TableHead>
                    <TableHead>W-L</TableHead>
                    <TableHead>PF</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {standings.map((t, i) => (
                    <TableRow key={i}>
                      <TableCell className="font-medium text-field-100">
                        {str(t.name ?? t.team_name ?? `Team ${i + 1}`)}
                      </TableCell>
                      <TableCell className="tabular text-field-300">
                        {num(t.wins) ?? 0}-{num(t.losses) ?? 0}
                      </TableCell>
                      <TableCell className="tabular text-field-300">
                        {fmt1(num(t.points_for ?? t.pf))}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

