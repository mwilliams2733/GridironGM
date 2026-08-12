import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, setActiveLeagueId } from "./api";
import type { LeagueSummary } from "./types";

const STORAGE_KEY = "gridiron.leagueId";

interface LeagueContextValue {
  leagueId: string | null;
  league: LeagueSummary | null;
  leagues: LeagueSummary[];
  setLeague: (id: string) => void;
  isLoading: boolean;
}

const LeagueContext = createContext<LeagueContextValue | null>(null);

/** Resolves which league the app is showing and keeps the API client in sync.
 *
 * The selection lives in localStorage rather than on the server: config/league.yaml
 * is yours to edit, and the app switching leagues shouldn't rewrite it. The YAML
 * `active:` key is only the default for a browser that hasn't chosen yet. */
export function LeagueProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [leagueId, setLeagueId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch {
      return null;
    }
  });

  // Set before any child query runs, so the first request is already scoped.
  if (typeof window !== "undefined") setActiveLeagueId(leagueId);

  const leaguesQuery = useQuery({
    queryKey: ["leagues"],
    queryFn: api.leagues,
    staleTime: 5 * 60_000,
  });

  const leagues = useMemo(() => leaguesQuery.data?.leagues ?? [], [leaguesQuery.data]);

  // Adopt the server default when nothing is stored, or when a stored id has
  // been removed from league.yaml (a stale id would 404 every request).
  useEffect(() => {
    if (!leaguesQuery.data) return;
    const known = leagues.some((l) => l.id === leagueId);
    if (!known) {
      const fallback = leaguesQuery.data.active ?? leagues[0]?.id ?? null;
      setActiveLeagueId(fallback);
      setLeagueId(fallback);
    }
  }, [leaguesQuery.data, leagues, leagueId]);

  const setLeague = useCallback(
    (id: string) => {
      setActiveLeagueId(id);
      setLeagueId(id);
      try {
        localStorage.setItem(STORAGE_KEY, id);
      } catch {
        /* private mode — selection just won't persist */
      }
      // Every cached response belongs to the previous league.
      queryClient.invalidateQueries();
    },
    [queryClient],
  );

  const value = useMemo<LeagueContextValue>(
    () => ({
      leagueId,
      league: leagues.find((l) => l.id === leagueId) ?? null,
      leagues,
      setLeague,
      isLoading: leaguesQuery.isLoading,
    }),
    [leagueId, leagues, setLeague, leaguesQuery.isLoading],
  );

  return <LeagueContext.Provider value={value}>{children}</LeagueContext.Provider>;
}

export function useLeague(): LeagueContextValue {
  const ctx = useContext(LeagueContext);
  if (!ctx) throw new Error("useLeague must be used inside <LeagueProvider>");
  return ctx;
}

/** Prefix for React Query keys so cached data never leaks across leagues. */
export function useLeagueKey(): string {
  return useLeague().leagueId ?? "none";
}
