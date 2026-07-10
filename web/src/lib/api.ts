import type {
  DashboardResponse,
  DraftBoardResponse,
  DraftMyRosterResponse,
  DraftPickRequest,
  DraftRecommendationResponse,
  DraftResetRequest,
  HealthResponse,
  LeagueConfig,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
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
    board: (limit?: number) => request<DraftBoardResponse>(`/draft/board${qs({ limit })}`),
    pick: (body: DraftPickRequest) =>
      request<DraftBoardResponse>("/draft/pick", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    undo: () => request<DraftBoardResponse>("/draft/undo", { method: "POST" }),
    reset: (body: DraftResetRequest = {}) =>
      request<DraftBoardResponse>("/draft/reset", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    recommendation: () => request<DraftRecommendationResponse>("/draft/recommendation"),
    myRoster: () => request<DraftMyRosterResponse>("/draft/my-roster"),
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
