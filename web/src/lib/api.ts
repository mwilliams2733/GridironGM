import type {
  DashboardResponse,
  DraftBoardResponse,
  DraftEspnSyncResponse,
  DraftMyRosterResponse,
  DraftPickRequest,
  DraftPickResponse,
  DraftRecommendationResponse,
  DraftResetRequest,
  DraftResetResponse,
  DraftSimulateRequest,
  DraftSimulateResponse,
  DraftUndoResponse,
  HealthResponse,
  LeagueConfig,
  LeaguesResponse,
  LineupResult,
  PlayersResponse,
  RosterResponse,
  SetMyTeamRequest,
  SetRosterRequest,
  SetRosterResponse,
  SyncResponse,
  SyncStatusResponse,
  TeamsResponse,
  WaiverRankingsResponse,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

/** League every request is scoped to. Set by LeagueProvider on mount and on switch.
 *
 * Kept module-level so `request` can attach it without threading a league id
 * through all ~18 call sites. React Query keys must still include the league id
 * (see useLeague) or a switch would serve the previous league's cached data. */
let activeLeagueId: string | null = null;

export function setActiveLeagueId(id: string | null): void {
  activeLeagueId = id;
}

export function getActiveLeagueId(): string | null {
  return activeLeagueId;
}

function withLeague(path: string): string {
  if (!activeLeagueId) return path;
  // Never override an explicit league_id already on the path.
  if (/[?&]league_id=/.test(path)) return path;
  return `${path}${path.includes("?") ? "&" : "?"}league_id=${encodeURIComponent(activeLeagueId)}`;
}

/** Appends `mock` to a draft path. Kept explicit per call rather than module-level
 *  like the league: mock is a mode of the draft room, not of the whole session,
 *  and defaulting it globally is how a practice draft ends up in real state. */
function withMock(path: string, mock?: boolean): string {
  if (!mock) return path;
  return `${path}${path.includes("?") ? "&" : "?"}mock=true`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${withLeague(path)}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? detail;
    } catch {
      /* ignore non-JSON error body */
    }
    throw new ApiError(res.status, detail || `Request failed (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

export const api = {
  health: () => request<HealthResponse>("/health"),
  config: () => request<LeagueConfig>("/config"),
  /** Not league-scoped — this is the list the switcher is built from. */
  leagues: () => request<LeaguesResponse>("/leagues"),
  sync: (scope = "all") => request<SyncResponse>(`/sync${qs({ scope })}`, { method: "POST" }),
  syncStatus: () => request<SyncStatusResponse>("/sync/status"),

  league: {
    roster: () => request<RosterResponse>("/league/roster"),
    setRoster: (body: SetRosterRequest) =>
      request<SetRosterResponse>("/league/roster", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    teams: () => request<TeamsResponse>("/league/teams"),
    setMyTeam: (body: SetMyTeamRequest) =>
      request<{ ok: boolean }>("/league/my-team", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    players: (params: { search?: string; position?: string; limit?: number } = {}) =>
      request<PlayersResponse>(`/league/players${qs(params)}`),
  },

  draft: {
    board: (limit?: number, mock?: boolean) =>
      request<DraftBoardResponse>(withMock(`/draft/board${qs({ limit })}`, mock)),
    pick: (body: DraftPickRequest, mock?: boolean) =>
      request<DraftPickResponse>(withMock("/draft/pick", mock), {
        method: "POST",
        body: JSON.stringify(body),
      }),
    undo: (mock?: boolean) =>
      request<DraftUndoResponse>(withMock("/draft/undo", mock), { method: "POST" }),
    reset: (body: DraftResetRequest = {}, mock?: boolean) =>
      request<DraftResetResponse>(withMock("/draft/reset", mock), {
        method: "POST",
        body: JSON.stringify(body),
      }),
    simulate: (body: DraftSimulateRequest = {}, mock = true) =>
      request<DraftSimulateResponse>(withMock("/draft/simulate", mock), {
        method: "POST",
        body: JSON.stringify(body),
      }),
    syncEspn: () => request<DraftEspnSyncResponse>("/draft/sync-espn", { method: "POST" }),
    recommendation: (mock?: boolean) =>
      request<DraftRecommendationResponse>(withMock("/draft/recommendation", mock)),
    myRoster: (mock?: boolean) =>
      request<DraftMyRosterResponse>(withMock("/draft/my-roster", mock)),
  },

  waivers: {
    rankings: (week?: number) =>
      request<WaiverRankingsResponse>(`/waivers/rankings${qs({ week })}`),
  },

  lineup: {
    optimal: (params: { week?: number; season?: number } = {}) =>
      request<LineupResult>(`/lineup/optimal${qs(params)}`),
  },

  dashboard: () => request<DashboardResponse>("/dashboard"),
};
