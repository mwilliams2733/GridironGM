import { Fragment, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, TrendingUp } from "lucide-react";
import { api } from "@/lib/api";
import { useLeagueKey } from "@/lib/league";
import { PageHeader } from "@/components/Layout";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { PositionBadge } from "@/components/TierBadge";
import { ConfidencePill } from "@/components/Confidence";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/States";
import { cn } from "@/lib/utils";

export function Waivers() {
  const leagueKey = useLeagueKey();
  const [week, setWeek] = useState<string>("current");
  const [expanded, setExpanded] = useState<string | null>(null);

  const weekParam = week === "current" ? undefined : Number(week);
  const rankingsQuery = useQuery({
    queryKey: ["waivers", leagueKey, weekParam],
    queryFn: () => api.waivers.rankings(weekParam),
  });

  const rows = rankingsQuery.data?.rankings ?? [];
  const weekOptions = useMemo(() => ["current", ...Array.from({ length: 18 }, (_, i) => String(i + 1))], []);

  return (
    <div>
      <PageHeader
        eyebrow="Free Agency"
        title="Waivers"
        description="Ranked adds by rest-of-season upgrade, with suggested FAAB bids."
        actions={
          <Select value={week} onValueChange={setWeek}>
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {weekOptions.map((w) => (
                <SelectItem key={w} value={w}>
                  {w === "current" ? "This week" : `Week ${w}`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />

      {rankingsQuery.isLoading ? (
        <Card>
          <TableSkeleton rows={10} cols={7} />
        </Card>
      ) : rankingsQuery.isError ? (
        <ErrorState
          message={(rankingsQuery.error as Error).message}
          onRetry={() => rankingsQuery.refetch()}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No waiver rankings"
          description="Sync free-agent data (ESPN or ADP fallback) to rank this week's adds."
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Player</TableHead>
                  <TableHead>Pos</TableHead>
                  <TableHead>FAAB</TableHead>
                  <TableHead>Confidence</TableHead>
                  <TableHead>Breakout signals</TableHead>
                  <TableHead>Suggested drop</TableHead>
                  <TableHead></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((r) => {
                  const isOpen = expanded === r.player_id;
                  return (
                    <Fragment key={r.player_id}>
                      <TableRow
                        className="cursor-pointer"
                        onClick={() => setExpanded(isOpen ? null : r.player_id)}
                      >
                        <TableCell className="font-medium text-field-100">
                          {r.name}
                          <span className="ml-2 text-xs text-field-500">{r.team ?? "FA"}</span>
                        </TableCell>
                        <TableCell>
                          <PositionBadge position={r.pos} />
                        </TableCell>
                        <TableCell className="tabular font-mono text-hash-500">
                          ${r.faab_bid ?? 0}
                        </TableCell>
                        <TableCell>
                          <ConfidencePill level={r.confidence} />
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-wrap gap-1">
                            {r.breakout_flags?.length ? (
                              r.breakout_flags.map((f) => (
                                <Badge key={f} variant="hash">
                                  <TrendingUp className="h-3 w-3" />
                                  {f}
                                </Badge>
                              ))
                            ) : (
                              <span className="text-xs text-field-500">—</span>
                            )}
                          </div>
                        </TableCell>
                        <TableCell className="text-field-400">{r.suggested_drop ?? "—"}</TableCell>
                        <TableCell>
                          <ChevronDown
                            className={cn(
                              "h-4 w-4 text-field-500 transition-transform",
                              isOpen && "rotate-180",
                            )}
                          />
                        </TableCell>
                      </TableRow>
                      {isOpen && (
                        <TableRow className="hover:bg-transparent">
                          <TableCell colSpan={7} className="whitespace-normal bg-field-900/60 text-sm text-field-300">
                            {r.rationale ?? "No rationale provided."}
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
