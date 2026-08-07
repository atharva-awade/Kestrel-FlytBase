/**
 * API client.
 *
 * Everything the console knows comes through here. Two properties are deliberate:
 *
 * - **Nothing fabricates.** If the backend is down or a session has not been
 *   ingested, components receive `null` and render an honest empty state. A
 *   dashboard that invents plausible numbers when its data source is missing is
 *   worse than one that says so.
 * - **No provider credentials.** Model keys live server-side; the browser talks
 *   only to our own API, which proxies them.
 */

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

/**
 * Where to send a request that must not go through the Next dev proxy.
 *
 * That proxy caps request bodies at 10 MB and truncates anything larger, which
 * surfaces as `socket hang up` and a 500 with no useful message. Video uploads
 * are large by definition, so they go straight to the API origin instead. CORS
 * already permits it, and streaming a 200 MB file through a dev-server proxy
 * would be wasteful even if it worked.
 *
 * Falls back to the same-origin path when no origin is configured, which keeps
 * a deployment behind a single hostname working unchanged.
 */
export const apiOrigin = BASE;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly path: string,
  ) {
    super(message);
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new ApiError(`${res.status} ${res.statusText}`, res.status, path);
  }
  return res.json() as Promise<T>;
}

/** Fetch that degrades to `null` rather than throwing, for optional panels. */
export async function tryGet<T>(path: string): Promise<T | null> {
  try {
    return await req<T>(path);
  } catch {
    return null;
  }
}

// ── types ─────────────────────────────────────────────────────────────────
export type Severity = "info" | "low" | "medium" | "high" | "critical";

export interface AlertLocation {
  lat: number | null;
  lon: number | null;
  zone_id: string | null;
  zone_name: string | null;
  source: "geo-projection" | "zone-centroid" | "drone-position" | "unknown";
  accuracy_m: number | null;
  confidence: number;
  distance_from_dock_m: number | null;
  bearing_from_dock_deg: number | null;
  eta_seconds: number | null;
  recommended_altitude_m: number;
  within_geofence: boolean;
  drone_lat: number | null;
  drone_lon: number | null;
  drone_alt_m: number | null;
  dock_lat: number | null;
  dock_lon: number | null;
}

export interface Evidence {
  kind: string;
  ref_id: string;
  caption: string;
  weight: number;
  detail: Record<string, unknown>;
}

export interface Alert {
  id: string;
  site_id: string;
  rule_id: string;
  rule_name: string;
  severity: Severity;
  title: string;
  narrative: string;
  ts: string;
  zone_id: string | null;
  confidence: number;
  baseline_deviation: number;
  status: string;
  suppressed_reason: string | null;
  mission_id: string | null;
  entity_ids: string[];
  frame_ids: string[];
  evidence: Evidence[];
  location: AlertLocation | null;
}

export interface Detection {
  id: string;
  frame_id: string;
  site_id: string;
  /** Frame timestamp, denormalised onto the detection row for time-ordered queries. */
  ts: string;
  label: string;
  confidence: number;
  x1: number; y1: number; x2: number; y2: number;
  track_id: number | null;
  entity_id: string | null;
  zone_id: string | null;
  lat: number | null;
  lon: number | null;
}

export interface Telemetry {
  ts: string;
  lat: number; lon: number; alt_m: number;
  heading_deg: number; gimbal_pitch_deg: number; gimbal_yaw_deg: number;
  speed_mps: number; battery_pct: number;
  gps_satellites: number; gps_hdop: number;
  wind_mps: number; illuminance_lux: number;
  state: string; signal_pct: number;
  perception_confidence: number;
}

export interface Frame {
  id: string;
  site_id: string;
  seq: number;
  ts: string;
  source: string;
  path: string | null;
  width: number; height: number;
  analysed: number;
  gate_reason: string;
  gate_novelty: number;
  caption: string | null;
  scene: SceneGraph | null;
  telemetry: Telemetry | null;
  detections: Detection[];
}

export interface SceneGraph {
  caption: string;
  objects: { label: string; colour: string | null; kind: string | null; activity: string | null; count: number; confidence: number }[];
  activities: string[];
  lighting: string;
  weather: string | null;
  visibility: string;
  anomalies: string[];
  confidence: number;
  tier: string;
}

export interface Entity {
  id: string;
  site_id: string;
  kind: string;
  label: string;
  descriptor: string;
  attributes: Record<string, string>;
  first_seen: string;
  last_seen: string;
  visit_count: number;
  frame_count: number;
  zones: string[];
  sites: string[];
  threat_score: number;
}

export interface SiteStatus {
  site_id: string;
  name: string;
  lat: number; lon: number;
  country: string; country_name: string;
  kind: string;
  simulated: boolean;
  drone_state: string;
  battery_pct: number;
  active_alerts: number;
  peak_severity: Severity | null;
  threat_score: number;
  entities_today: number;
  last_seen: string | null;
  online: boolean;
}

export interface CountryBucket {
  country: string;
  country_name: string;
  sites: number;
  alerts: number;
  simulated_sites: number;
  by_severity: Record<Severity, number>;
  threat: number;
  site_ids: string[];
}

export interface FleetResponse {
  summary: {
    sites: number; live_sites: number; simulated_sites: number;
    online: number; active_alerts: number; airborne: number; charging: number;
    mean_battery: number; peak_threat: number; countries: number; note: string;
  };
  sites: SiteStatus[];
  by_country: CountryBucket[];
}

export interface Rule {
  id: string; name: string; description: string;
  severity: Severity; enabled: boolean; origin: string;
  tags: string[]; conditions: string[];
  visual_predicate: string | null;
  cooldown_seconds: number;
  yaml: string; fires: number;
}

export interface Mission {
  id: string; site_id: string; alert_id: string | null;
  rationale: string; status: string; created_ts: string;
  steps: {
    kind: string; target: { lat: number; lon: number } | null;
    zone_id: string | null; entity_id: string | null;
    altitude_m: number; radius_m: number; duration_s: number; note: string;
  }[];
  feasibility: {
    feasible: boolean; battery_required_pct: number; battery_available_pct: number;
    distance_m: number; duration_s: number; within_geofence: boolean;
    wind_ok: boolean; altitude_ok: boolean; daylight: boolean;
    blockers: string[]; warnings: string[];
  };
}

export interface SearchHit {
  frame_id: string; ts: string; caption: string;
  zone_id: string | null; labels: string[]; score: number;
  sources: string[];
  ranks: { structured: number | null; caption: number | null; visual: number | null };
}

export interface SearchResult {
  plan: Record<string, unknown> & { intent: string; reasoning: string };
  plan_steps: string[];
  hits: SearchHit[];
  counts: Record<string, number>;
  /** Retrievers that could not run, and why. Empty on a healthy search. */
  degraded?: Record<string, string>;
  complete?: boolean;
  took_ms: number;
}

export interface AskTurn {
  question: string;
  answer: string;
  intent: string;
  tool_calls: { tool: string; arguments: Record<string, unknown>; result: Record<string, unknown> }[];
  citations: string[];
  pending_confirmation: { tool: string; arguments: Record<string, unknown>; consequence: string; message: string } | null;
  verified: boolean;
  verification_note: string;
  ms: number;
}

export interface Health {
  status: string;
  mode: string;
  requested_mode: string;
  roster: Record<string, string>;
  providers: { provider: string; available: boolean; circuit_open: boolean }[];
  cassettes: { hits: number; misses: number; count_on_disk: number };
  storage: Record<string, unknown>;
  ledger: Record<string, unknown>;
  runs_without_api_key: boolean;
}

// ── endpoints ─────────────────────────────────────────────────────────────
export interface Clip {
  slug: string;
  title: string;
  width: number;
  height: number;
  fps: number;
  duration_s: number;
  primary: boolean;
  uploaded?: boolean;
  location?: { lat: number; lon: number } | null;
  attribution?: string;
  licence?: string;
  indexed: boolean;
  video_url: string;
}

/** One detection in a playback index. Boxes are normalised 0-1 of frame size, so
 *  the overlay scales to whatever the `<video>` element happens to be. */
export interface PlaybackDet {
  x1: number; y1: number; x2: number; y2: number;
  label: string;
  conf: number;
  track: number | null;
  zone: string | null;
}

export interface PlaybackFrame {
  t: number;
  analysed: boolean;
  gate_reason: string;
  dets: PlaybackDet[];
}

export interface PlaybackAlert {
  t: number;
  id: string;
  severity: string;
  title: string;
  rule_id: string;
  confidence: number;
  zone_id: string | null;
  location: Record<string, any> | null;
}

export interface PlaybackIndex {
  clip: string;
  title: string;
  width: number;
  height: number;
  fps: number;
  duration_s: number;
  sampled_fps: number;
  sampled_frames: number;
  detector: string;
  detector_device: string;
  mean_detect_ms: number;
  gate: { analysed: number; skipped: number; efficiency: number };
  telemetry: string;
  site_id: string;
  built_at: string;
  frames: PlaybackFrame[];
  alerts: PlaybackAlert[];
  tracks: Record<string, { label: string; first_t: number; last_t: number; frames: number }>;
  note: string;
}

export const api = {
  health: () => tryGet<Health>("/api/health"),
  stats: () => tryGet<Record<string, any>>("/api/stats"),
  tools: () => tryGet<{ tools: any[]; permissions: { auto: string[]; confirm: string[] }; classes: Record<string, string[]> }>("/api/tools"),

  sites: () => tryGet<{ count: number; sites: any[] }>("/api/sites"),
  site: (id: string) => tryGet<any>(`/api/sites/${id}`),
  fleet: () => tryGet<FleetResponse>("/api/fleet"),
  correlations: () => tryGet<{ count: number; matches: any[]; patterns: any[]; note: string }>("/api/fleet/correlations"),

  // ── playback ────────────────────────────────────────────────────────────
  clips: () => tryGet<{ count: number; clips: Clip[] }>("/api/clips"),
  playback: (clip: string) => tryGet<PlaybackIndex>(`/api/playback/${clip}`),
  footageUrl: (clip: string) => `${BASE}/api/footage/${clip}.mp4`,

  frames: (siteId = "plant-01", limit = 60) =>
    tryGet<{ count: number; frames: Frame[] }>(`/api/frames?site_id=${siteId}&limit=${limit}`),
  frameImage: (frameId: string) => `${BASE}/api/frames/${frameId}/image`,

  alerts: (siteId?: string, limit = 50) =>
    tryGet<{ count: number; alerts: Alert[] }>(
      `/api/alerts?limit=${limit}${siteId ? `&site_id=${siteId}` : ""}`),

  entities: (siteId = "plant-01", limit = 100) =>
    tryGet<{ count: number; entities: Entity[] }>(`/api/entities?site_id=${siteId}&limit=${limit}`),
  entity: (id: string) => tryGet<{ entity: Entity; sightings: Detection[]; count: number }>(`/api/entities/${id}`),

  missions: (siteId = "plant-01") => tryGet<{ count: number; missions: Mission[] }>(`/api/missions?site_id=${siteId}`),
  memory: (siteId = "plant-01", level?: string) =>
    tryGet<{ count: number; nodes: any[] }>(`/api/memory?site_id=${siteId}${level ? `&level=${level}` : ""}`),
  rules: () => tryGet<{ count: number; rules: Rule[] }>("/api/rules"),
  ledger: (limit = 100) => tryGet<{ verification: any; stats: any; entries: any[] }>(`/api/ledger?limit=${limit}`),
  scenarios: () => tryGet<{ count: number; scenarios: any[] }>("/api/scenarios"),
  evals: () => tryGet<Record<string, any>>("/api/evals"),
  architecture: (topic = "overview") =>
    tryGet<{ topic: string; explanation: string; topics: string[]; limitations: string }>(`/api/architecture?topic=${topic}`),

  search: (q: string, limit = 24) =>
    tryGet<SearchResult>(`/api/search?q=${encodeURIComponent(q)}&limit=${limit}`),

  ask: (question: string, selection?: Record<string, unknown>) =>
    req<AskTurn>("/api/ask", { method: "POST", body: JSON.stringify({ question, selection }) }),

  confirm: (tool: string, args: Record<string, unknown>, approve: boolean) =>
    req<{ ok: boolean; executed: boolean; result?: unknown }>("/api/ask/confirm", {
      method: "POST",
      body: JSON.stringify({ tool, arguments: args, approve }),
    }),

  brief: () => tryGet<{ brief: string; generated_at: string }>("/api/brief"),

  askStreamUrl: `${BASE}/api/ask/stream`,
  sessionStreamUrl: `${BASE}/api/session/stream`,
};

/**
 * Read a POST server-sent-event stream.
 *
 * EventSource cannot POST, and both the agent and the live session need a body,
 * so the SSE framing is parsed manually off a fetch body reader.
 */
export async function postStream(
  url: string,
  body: unknown,
  onEvent: (data: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new ApiError(`stream failed: ${res.status}`, res.status, url);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const line = part.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      try {
        onEvent(JSON.parse(line.slice(6)));
      } catch {
        /* a truncated frame is not worth failing the whole stream over */
      }
    }
  }
}
