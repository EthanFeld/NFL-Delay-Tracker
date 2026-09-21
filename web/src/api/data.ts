import type { AnyRecord, AppData, Game, HourlyRisk, NwsWarning, ResumePoint, SourceHealth } from '../types';

const UPDATE_INTERVAL = 60_000;
const configuredBase = (import.meta.env.VITE_DATA_BASE_URL as string | undefined)?.trim();
const rootBase = configuredBase || import.meta.env.BASE_URL || '/';
const base = rootBase.endsWith('/') ? rootBase : `${rootBase}/`;

function asRecord(value: unknown): AnyRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as AnyRecord : {};
}

function first(record: AnyRecord, ...keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key];
  }
  return undefined;
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

function numeric(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return undefined;
}

function probability(value: unknown): number | undefined {
  const parsed = numeric(value);
  if (parsed === undefined) return undefined;
  return Math.max(0, Math.min(1, parsed > 1 ? parsed / 100 : parsed));
}

function boolean(value: unknown): boolean {
  return value === true || value === 1 || value === 'true';
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function abbreviation(name: string): string {
  return name.split(/\s+/).map((part) => part[0]).join('').slice(-3).toUpperCase();
}

function isoOrString(value: unknown): string {
  if (typeof value === 'number') return new Date(value < 10_000_000_000 ? value * 1000 : value).toISOString();
  return text(value);
}

function normalizeSource(name: string, value: unknown): SourceHealth {
  const item = asRecord(value);
  const updatedAt = isoOrString(first(item, 'updated_at', 'last_success', 'last_updated', 'observed_at', 'timestamp'));
  const ageMinutes = numeric(first(item, 'age_minutes', 'age_min'));
  const freshnessLimitMinutes = numeric(first(item, 'freshness_limit_minutes'));
  return {
    name: text(first(item, 'name', 'provider', 'source'), name),
    status: text(first(item, 'status', 'state', 'health')),
    updatedAt: updatedAt || undefined,
    ageMinutes,
    freshnessLimitMinutes,
    url: text(first(item, 'url', 'source_url')) || undefined,
  };
}

function normalizeSources(value: unknown): SourceHealth[] {
  if (Array.isArray(value)) return value.map((item, i) => normalizeSource(`Source ${i + 1}`, item));
  const record = asRecord(value);
  return Object.entries(record).map(([name, item]) => normalizeSource(name, item));
}

function normalizeHours(value: unknown): HourlyRisk[] {
  return list(value).flatMap((item): HourlyRisk[] => {
    const row = asRecord(item);
    const at = isoOrString(first(row, 'at', 'timestamp', 'time', 'hour', 'valid_time', 'valid_at'));
    const risk = probability(first(row, 'probability', 'hazard_probability', 'delay_probability', 'hazard', 'risk'));
    return at && risk !== undefined
      ? [{ at, probability: risk, label: text(first(row, 'label', 'window')) || undefined }]
      : [];
  });
}

function normalizeResumeCdf(value: unknown): ResumePoint[] {
  const now = Date.now();
  return list(value).map((item) => {
    const row = asRecord(item);
    const timeValue = first(row, 'at', 'timestamp', 'resume_at', 'time', 'minutes_from_now', 'minutes');
    let at = isoOrString(timeValue);
    if (typeof timeValue === 'number' && !String(timeValue).includes('e') && timeValue < 10_000) {
      at = new Date(now + timeValue * 60_000).toISOString();
    }
    const p = probability(first(row, 'probability', 'cdf', 'resumption_probability', 'resume_probability'));
    return at && p !== undefined ? { at, probability: p } : undefined;
  }).filter((row): row is ResumePoint => Boolean(row));
}

export function normalizeGame(input: unknown, manifestSources: SourceHealth[] = []): Game {
  const root = asRecord(input);
  const game = asRecord(first(root, 'game', 'matchup') ?? root);
  const pregame = asRecord(first(root, 'pregame', 'forecast', 'delay_forecast', 'risk'));
  const delay = asRecord(first(root, 'delay', 'active_delay'));
  const venue = asRecord(first(root, 'venue', 'stadium'));
  const weather = asRecord(first(root, 'weather'));
  const venueFeatures = asRecord(first(weather, 'venue_features') ?? first(root, 'venue_features'));
  const nwsAlerts = asRecord(first(venueFeatures, 'nws_alerts'));
  const policy = asRecord(first(root, 'policy', 'weather_policy'));
  const quality = asRecord(first(root, 'quality', 'forecast_quality', 'data_quality'));
  const homeTeam = text(first(game, 'home_team', 'homeTeam', 'home', 'home_name'), 'Home team');
  const awayTeam = text(first(game, 'away_team', 'awayTeam', 'away', 'away_name'), 'Away team');
  const id = text(first(game, 'game_id', 'id', 'slug'), `${awayTeam}-${homeTeam}`.toLowerCase().replace(/[^a-z0-9]+/g, '-'));
  const leagueName = text(first(game, 'league', 'competition', 'level'), 'NFL').toLowerCase();
  const status = text(first(game, 'status', 'game_status', 'state'), 'unknown').toLowerCase();
  const eventDate = isoOrString(first(game, 'kickoff_utc', 'kickoff', 'start_time', 'scheduled_start', 'game_time', 'date'));
  const activeDelay = boolean(first(delay, 'active', 'model_delay_active', 'official_delay_active'))
    || boolean(first(game, 'official_delay', 'official_delay_active', 'model_delay_active'))
    || boolean(first(root, 'official_delay_active', 'model_delay_active', 'active_delay'))
    || status === 'weather_delay' || status === 'suspended';
  const roofType = text(first(venue, 'roof_type', 'roof', 'roof_status') ?? first(game, 'roof_type', 'roof'), 'unknown').toLowerCase();
  const delayProbability = probability(first(pregame, 'delay_probability', 'probability_any_delay', 'any_delay_probability'));
  const additional = asRecord(first(delay, 'probability_additional', 'probability_additional_minutes', 'probability_of_another_delay', 'additional_delay_probabilities'));
  const probabilities: Record<number, number> = {};
  for (const minute of [15, 30, 45, 60, 90, 120, 150, 180]) {
    const value = first(delay, `probability_additional_${minute}_min`, `probability_additional_${minute}_minutes`, `probability_another_${minute}_min`) ?? additional[minute] ?? additional[String(minute)];
    const p = probability(value);
    if (p !== undefined) probabilities[minute] = p;
  }
  const byProvider = normalizeSources(first(root, 'sources', 'source_status', 'source_health') ?? first(quality, 'sources', 'source_status', 'source_health'));
  const warnings = list(first(quality, 'warnings', 'degradation_reasons', 'issues', 'missing_sources')).map((x) => text(x)).filter(Boolean);
  const qualityText = text(first(quality, 'status', 'mode', 'forecast_mode'));
  const qualityMessage = text(first(quality, 'message', 'notes'));
  const nwsAlertStatus = text(first(nwsAlerts, 'status')).toLowerCase();
  const nwsWarnings = list(first(nwsAlerts, 'alerts')).flatMap((item): NwsWarning[] => {
    const alert = asRecord(item);
    const expiresAt = isoOrString(first(alert, 'expires_at', 'expires')) || undefined;
    if (expiresAt && new Date(expiresAt).getTime() <= Date.now()) return [];
    const rawUrl = text(first(alert, 'web_url', 'url'));
    return [{
      headline: text(first(alert, 'headline')) || undefined,
      severity: text(first(alert, 'severity')) || undefined,
      expiresAt,
      url: rawUrl.startsWith('https://') ? rawUrl : undefined,
    }];
  });
  const hrrrContext = asRecord(first(venueFeatures, 'hrrr_point'));
  const glmContext = asRecord(first(venueFeatures, 'glm_observation'));
  const motionContext = asRecord(first(venueFeatures, 'storm_motion'));
  const globalOutlookContext = asRecord(first(venueFeatures, 'global_weather_outlook'));
  const globalConditionCounts = asRecord(first(globalOutlookContext, 'condition_member_counts'));
  const motionAdjustment = asRecord(first(motionContext, 'model_adjustment'));
  const weatherContext = {
    globalOutlook: Object.keys(globalOutlookContext).length ? {
      model: text(first(globalOutlookContext, 'model')) || undefined,
      validAt: isoOrString(first(globalOutlookContext, 'valid_at')) || undefined,
      nativeResolutionHours: numeric(first(globalOutlookContext, 'native_resolution_hours')),
      gridResolutionKm: numeric(first(globalOutlookContext, 'grid_resolution_km')),
      memberCount: numeric(first(globalOutlookContext, 'member_count')),
      validMemberCount: numeric(first(globalOutlookContext, 'valid_member_count')),
      conditionMemberCounts: {
        clearOrCloudy: numeric(first(globalConditionCounts, 'clear_or_cloudy')),
        fog: numeric(first(globalConditionCounts, 'fog')),
        precipitationOrSnow: numeric(first(globalConditionCounts, 'precipitation_or_snow')),
        otherOrUnclassified: numeric(first(globalConditionCounts, 'other_or_unclassified')),
      },
      attributionUrl: (() => {
        const url = text(first(globalOutlookContext, 'attribution_url'));
        return url.startsWith('https://') ? url : undefined;
      })(),
    } : undefined,
    stormMotion: Object.keys(motionContext).length ? {
      status: text(first(motionContext, 'status')) || undefined,
      observedAt: isoOrString(first(motionContext, 'observed_at')) || undefined,
      observationAgeMinutes: numeric(first(motionContext, 'observation_age_minutes')),
      bearingDegrees: numeric(first(motionContext, 'bearing_degrees')),
      speedMph: numeric(first(motionContext, 'speed_mph')),
      radialSpeedTowardMph: numeric(first(motionContext, 'radial_speed_toward_mph')),
      distanceToPolicyBoundaryMiles: numeric(first(motionContext, 'distance_to_policy_boundary_miles')),
      etaMinutes: numeric(first(motionContext, 'eta_minutes')),
      etaLowerMinutes: numeric(first(motionContext, 'eta_lower_minutes')),
      etaUpperMinutes: numeric(first(motionContext, 'eta_upper_minutes')),
      maxReflectivityDbz: numeric(first(motionContext, 'max_reflectivity_dbz')),
      modelAdjustmentApplied: boolean(first(motionAdjustment, 'applied')),
    } : undefined,
    hrrr: Object.keys(hrrrContext).length ? {
      validAt: isoOrString(first(hrrrContext, 'valid_at')) || undefined,
      reflectivityDbz: numeric(first(hrrrContext, 'reflectivity_dbz')),
      precipitationRateKgM2S: numeric(first(hrrrContext, 'precipitation_rate_kg_m2_s')),
      capeJkg: numeric(first(hrrrContext, 'cape_j_kg')),
      windSpeed10mMs: numeric(first(hrrrContext, 'wind_speed_10m_ms')),
    } : undefined,
    glm: Object.keys(glmContext).length ? {
      satellite: text(first(glmContext, 'satellite')) || undefined,
      flashCount: numeric(first(glmContext, 'flash_count')),
      durationSeconds: numeric(first(glmContext, 'duration_seconds')),
      flashRatePerMinute: numeric(first(glmContext, 'flash_rate_per_minute')),
      flashDensityPerKm2Min: numeric(first(glmContext, 'flash_density_per_km2_min')),
      validEnd: isoOrString(first(glmContext, 'valid_end')) || undefined,
    } : undefined,
  };
  if (boolean(first(quality, 'degraded')) && qualityMessage) warnings.unshift(qualityMessage);
  if (qualityText && ['degraded', 'stale', 'unavailable'].some((value) => qualityText.toLowerCase().includes(value))) warnings.unshift(`Forecast data status: ${qualityText}`);
  for (const note of list(first(delay, 'notes')).map((x) => text(x)).filter(Boolean)) warnings.push(note);
  return {
    id,
    league: leagueName.includes('college') || leagueName.includes('ncaa') || leagueName.includes('cfb') || leagueName.includes('fbs') ? 'College' : 'NFL',
    season: numeric(first(game, 'season')),
    homeTeam,
    awayTeam,
    homeAbbr: text(first(game, 'home_abbreviation', 'home_abbr', 'home_short_name'), abbreviation(homeTeam)),
    awayAbbr: text(first(game, 'away_abbreviation', 'away_abbr', 'away_short_name'), abbreviation(awayTeam)),
    homeScore: numeric(first(game, 'home_score', 'home_points')),
    awayScore: numeric(first(game, 'away_score', 'away_points')),
    kickoff: eventDate || new Date().toISOString(),
    status,
    venue: text(first(venue, 'name', 'venue_name') ?? first(game, 'venue_name', 'venue'), 'Venue not listed'),
    venueId: text(first(venue, 'venue_id', 'id') ?? first(game, 'venue_id')) || undefined,
    neutralSite: boolean(first(game, 'neutral_site', 'neutralSite')),
    city: text(first(venue, 'city', 'location') ?? first(game, 'venue_city', 'city')) || undefined,
    timezone: text(first(venue, 'timezone', 'tz') ?? first(game, 'timezone', 'venue_timezone')) || undefined,
    roofType,
    officialDelay: boolean(first(delay, 'officially_confirmed', 'official_delay_active')) || boolean(first(game, 'official_delay', 'official_delay_active')) || boolean(first(root, 'official_delay_active')),
    modeledDelay: (boolean(first(delay, 'model_delay_active', 'modeled', 'active')) || boolean(first(game, 'model_delay_active')) || boolean(first(root, 'model_delay_active'))) && !boolean(first(delay, 'officially_confirmed')),
    delayStartedAt: isoOrString(first(delay, 'started_at', 'delay_started_at') ?? first(game, 'delay_started_at')) || undefined,
    officialResumeAt: isoOrString(first(game, 'official_resume_at', 'officialResumeAt')) || undefined,
    generatedAt: isoOrString(first(root, 'generated_at', 'updated_at') ?? first(quality, 'generated_at')) || undefined,
    modelVersion: text(first(root, 'model_version', 'model_version_id')) || undefined,
    forecastScope: text(first(quality, 'forecast_scope')) || undefined,
    delayProbability,
    kickoffDelayProbability: probability(first(pregame, 'kickoff_delay_probability', 'probability_kickoff_delay')),
    inGameDelayProbability: probability(first(pregame, 'in_game_delay_probability', 'probability_in_game_delay')),
    expectedDelayMinutes: numeric(first(pregame, 'expected_total_delay_minutes', 'expected_delay_minutes')),
    highestRiskWindow: text(first(pregame, 'highest_risk_window', 'risk_window')) || undefined,
    hourlyRisk: normalizeHours(first(pregame, 'hourly_delay_hazard', 'hourly_risk', 'hourly_hazard', 'risk_timeline')),
    activeDelay,
    resumeCdf: normalizeResumeCdf(first(delay, 'resume_cdf', 'resume_distribution', 'resumption_cdf')),
    resumeP50: isoOrString(first(delay, 'resume_p50', 'resume_p50_at', 'p50')) || undefined,
    resumeP75: isoOrString(first(delay, 'resume_p75', 'resume_p75_at', 'p75')) || undefined,
    resumeP90: isoOrString(first(delay, 'resume_p90', 'resume_p90_at', 'p90')) || undefined,
    probabilityAdditional: probabilities,
    earliestWeatherClear: isoOrString(first(delay, 'earliest_weather_clear_at', 'earliest_weather_clear')) || undefined,
    weatherClearCdf: normalizeResumeCdf(first(delay, 'weather_clear_cdf')),
    lastQualifyingEvent: isoOrString(first(delay, 'last_qualifying_event_at', 'last_qualifying_event', 'latest_qualifying_event_at')) || undefined,
    policyTriggerMiles: numeric(first(policy, 'trigger_radius_miles', 'trigger_radius', 'policy_radius_miles')),
    policyMonitorRadiiMiles: list(first(policy, 'monitor_radii_miles', 'monitor_radii')).map(numeric).filter((value): value is number => value !== undefined),
    policyQuietMinutes: numeric(first(policy, 'quiet_period_minutes', 'quiet_minutes', 'minimum_quiet_period_minutes')),
    policyVerification: text(first(policy, 'verification', 'verification_class', 'status')) || undefined,
    policyNotes: text(first(policy, 'notes', 'summary')) || undefined,
    policySource: text(first(policy, 'source_url', 'source', 'url')) || undefined,
    sources: byProvider.length ? byProvider : manifestSources,
    qualityWarnings: warnings,
    nwsWarnings,
    nwsWarningsFresh: nwsAlertStatus === 'ok',
    weatherContext: weatherContext.globalOutlook || weatherContext.stormMotion || weatherContext.hrrr || weatherContext.glm ? weatherContext : undefined,
    raw: root,
  };
}

function unwrapGameIndex(payload: unknown): unknown[] {
  if (Array.isArray(payload)) return payload;
  const record = asRecord(payload);
  const value = first(record, 'games', 'items', 'results', 'game_ids');
  return Array.isArray(value) ? value : [];
}

async function getJson(path: string, signal?: AbortSignal, version?: string): Promise<unknown> {
  const requestUrl = new URL(path, new URL(base, window.location.origin));
  if (version) requestUrl.searchParams.set('v', version);
  const response = await fetch(requestUrl, {
    signal,
    headers: { Accept: 'application/json' },
    cache: 'no-cache',
  });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

export async function loadAppData(previousVersion?: string, signal?: AbortSignal): Promise<AppData> {
  let manifest: AnyRecord = {};
  let manifestError = '';
  try {
    manifest = asRecord(await getJson('data/manifest.json', signal));
  } catch (error) {
    manifestError = error instanceof Error ? error.message : 'Manifest unavailable';
  }
  const version = text(first(manifest, 'version', 'generated_at', 'updated_at', 'manifest_version')) || undefined;
  const manifestSources = normalizeSources(first(manifest, 'sources', 'source_health', 'providers'));
  const shouldReloadIndex = !previousVersion || !version || version !== previousVersion;
  if (!shouldReloadIndex) {
    return { games: [], sources: manifestSources, generatedAt: isoOrString(first(manifest, 'generated_at', 'updated_at')) || undefined, version, usingSample: false };
  }

  try {
    const indexPayload = await getJson('data/games/index.json', signal, version);
    const index = asRecord(indexPayload);
    const gamesList = unwrapGameIndex(indexPayload);
    const games = (await Promise.all(gamesList.map(async (item) => {
      if (typeof item === 'string') return loadGameDetail(item, signal, version);
      return normalizeGame(item, manifestSources);
    }))).filter((game): game is Game => Boolean(game));
    const sourceData = normalizeSources(first(index, 'sources', 'source_health'));
    return {
      games,
      sources: sourceData.length ? sourceData : manifestSources,
      generatedAt: isoOrString(first(index, 'generated_at', 'updated_at') ?? first(manifest, 'generated_at', 'updated_at')) || undefined,
      version: text(first(index, 'version', 'manifest_version')) || version,
      usingSample: games.length === 0,
      lastError: games.length ? undefined : manifestError || 'The published index has no games yet.',
    };
  } catch (error) {
    return {
      games: [],
      sources: manifestSources,
      generatedAt: isoOrString(first(manifest, 'generated_at', 'updated_at')) || undefined,
      version,
      usingSample: false,
      lastError: error instanceof Error ? error.message : manifestError || 'Live game data is not available yet.',
    };
  }
}

export async function loadGameDetail(id: string, signal?: AbortSignal, version?: string): Promise<Game | undefined> {
  try {
    const payload = await getJson(`data/games/${encodeURIComponent(id)}.json`, signal, version);
    return normalizeGame(payload);
  } catch {
    if (signal?.aborted) return undefined;
    return undefined;
  }
}

export const pollInterval = UPDATE_INTERVAL;
