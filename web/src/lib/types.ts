// ---------------------------------------------------------------------------
// Types mirroring the API contracts in docs/SPEC-PASS3-SONNET.md §A.
// The backend serializes DataFrames via `records()` (NaN -> null), so numeric
// fields on row types are `number | null` throughout.
// ---------------------------------------------------------------------------

export type Num = number | null;

// --- /api/config -----------------------------------------------------------

export interface LeagueConfig {
  league: {
    name: string;
    platform: string;
    teams: number;
    season: number;
    history_seasons: number;
  };
  scoring: Record<string, unknown>;
  roster: {
    starters: Record<string, number>; // e.g. { QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, K: 1, DST: 1 }
    flex_eligible: string[];
    bench: number;
    ir: number;
  };
  draft: {
    type: "snake" | "auction" | "linear";
    rounds: number;
    my_slot: number | null;
  };
  waivers: {
    system: "faab" | "priority";
    faab_budget: number;
  };
  espn: {
    league_id: string | number | null;
    year: number;
  };
}

export interface HealthResponse {
  status: string;
}

export interface SyncResponse {
  results: Record<string, unknown>;
}

export interface SyncStatusResponse {
  status: SyncStatus | null;
}

export interface SyncStatus {
  last_synced?: string | null;
  [key: string]: unknown;
}

// --- shared row shapes -------------------------------------------------

export interface Player {
  player_id: string;
  name: string;
  position: string;
  team?: string | null;
  bye?: Num;
  [key: string]: unknown;
}

/** A row from vorp_board / draft board / recommendation lists. */
export interface VorpRow {
  player_id: string;
  name: string;
  position: string;
  team?: string | null;
  proj: Num;
  vorp: Num;
  tier: Num;
  bye: Num;
  adp: Num;
  adp_delta: Num;
  rationale?: string | null;
  drafted?: boolean;
  [key: string]: unknown;
}

export interface WaiverRow {
  player_id: string;
  name: string;
  position: string;
  team?: string | null;
  faab_bid: Num;
  confidence: "High" | "Medium" | "Low" | string;
  breakout_flags: string[];
  suggested_drop?: string | null;
  rationale?: string | null;
  [key: string]: unknown;
}

// --- league.py ---------------------------------------------------------

export interface RosterResponse {
  mode: "espn" | "manual" | "none";
  team: {
    team_id?: string | number | null;
    name?: string | null;
    players?: Player[];
    [key: string]: unknown;
  } | null;
}

export interface SetRosterRequest {
  players: { name: string; position: string; team?: string }[];
}

export interface SetRosterResponse {
  resolved: Player[];
  unresolved: string[];
}

export type TeamsResponse = Record<string, unknown>[];

export interface SetMyTeamRequest {
  team_id: string | number;
}

export type PlayersResponse = (Player & {
  adp?: Num;
  proj?: Num;
})[];

// --- draft.py ------------------------------------------------------------

export interface DraftBoardResponse {
  board: VorpRow[];
  drafted_count: number;
  current_pick: number;
  my_next_pick: number | null;
  runs: Record<string, number>;
  tier_depth: Record<string, number>;
  [key: string]: unknown;
}

export interface DraftPickRequest {
  player_id: string;
  by_me: boolean;
}

export interface DraftResetRequest {
  my_slot?: number | null;
}

export interface DraftRecommendationResponse {
  recommended: VorpRow | null;
  alternatives: VorpRow[];
}

export interface DraftMyRosterResponse {
  picks: (VorpRow & { slot?: string | null })[];
}

// --- waivers.py ------------------------------------------------------------

export type WaiverRankingsResponse = WaiverRow[];

// --- lineup.py ------------------------------------------------------------

export interface LineupSlot {
  slot: string;
  player: (Player & { proj?: Num; confidence?: string | null }) | null;
}

export interface CloseCall {
  slot: string;
  starter: string;
  alternative: string;
  margin: Num;
  [key: string]: unknown;
}

export interface LineupResult {
  starters: LineupSlot[];
  bench: (Player & { proj?: Num })[];
  current_total: Num;
  optimal_total: Num;
  delta: Num;
  close_calls: CloseCall[];
  week: number;
  season: number;
}

// --- dashboard.py ------------------------------------------------------------

export interface DashboardResponse {
  my_team: RosterResponse["team"] | null;
  optimal_lineup: Partial<LineupResult> | null;
  top_waivers: WaiverRow[];
  standings: Record<string, unknown>[];
  odds_board: Record<string, unknown>[];
  sync_status: SyncStatus | null;
  warning?: string | null;
}
