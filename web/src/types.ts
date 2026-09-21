export type AnyRecord = Record<string, unknown>;

export interface HourlyRisk {
  at: string;
  probability: number;
  label?: string;
}

export interface ResumePoint {
  at: string;
  probability: number;
}

export interface NwsWarning {
  headline?: string;
  severity?: string;
  expiresAt?: string;
  url?: string;
}

export interface WeatherContext {
  stormMotion?: {
    status?: string;
    observedAt?: string;
    observationAgeMinutes?: number;
    bearingDegrees?: number;
    speedMph?: number;
    radialSpeedTowardMph?: number;
    distanceToPolicyBoundaryMiles?: number;
    etaMinutes?: number;
    etaLowerMinutes?: number;
    etaUpperMinutes?: number;
    maxReflectivityDbz?: number;
    modelAdjustmentApplied?: boolean;
  };
  hrrr?: {
    validAt?: string;
    reflectivityDbz?: number;
    precipitationRateKgM2S?: number;
    capeJkg?: number;
    windSpeed10mMs?: number;
  };
  glm?: {
    satellite?: string;
    flashCount?: number;
    durationSeconds?: number;
    flashRatePerMinute?: number;
    flashDensityPerKm2Min?: number;
    validEnd?: string;
  };
}

export interface Game {
  id: string;
  league: 'NFL' | 'College';
  season?: number;
  homeTeam: string;
  awayTeam: string;
  homeAbbr: string;
  awayAbbr: string;
  homeScore?: number;
  awayScore?: number;
  kickoff: string;
  status: string;
  venue: string;
  venueId?: string;
  neutralSite?: boolean;
  city?: string;
  timezone?: string;
  roofType: string;
  officialDelay: boolean;
  modeledDelay: boolean;
  delayStartedAt?: string;
  officialResumeAt?: string;
  generatedAt?: string;
  modelVersion?: string;
  forecastScope?: string;
  delayProbability?: number;
  kickoffDelayProbability?: number;
  inGameDelayProbability?: number;
  expectedDelayMinutes?: number;
  highestRiskWindow?: string;
  hourlyRisk: HourlyRisk[];
  activeDelay: boolean;
  resumeCdf: ResumePoint[];
  resumeP50?: string;
  resumeP75?: string;
  resumeP90?: string;
  probabilityAdditional: Record<number, number>;
  earliestWeatherClear?: string;
  weatherClearCdf?: ResumePoint[];
  lastQualifyingEvent?: string;
  policyTriggerMiles?: number;
  policyMonitorRadiiMiles?: number[];
  policyQuietMinutes?: number;
  policyVerification?: string;
  policyNotes?: string;
  policySource?: string;
  sources: SourceHealth[];
  qualityWarnings: string[];
  nwsWarnings: NwsWarning[];
  nwsWarningsFresh?: boolean;
  weatherContext?: WeatherContext;
  raw?: AnyRecord;
}

export interface SourceHealth {
  name: string;
  status?: string;
  updatedAt?: string;
  ageMinutes?: number;
  freshnessLimitMinutes?: number;
  url?: string;
}

export interface AppData {
  games: Game[];
  sources: SourceHealth[];
  generatedAt?: string;
  version?: string;
  usingSample: boolean;
  lastError?: string;
}
