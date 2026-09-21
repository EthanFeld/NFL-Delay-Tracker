import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import {
  Activity, AlertTriangle, ArrowDownUp, ArrowLeft, ArrowRight, CalendarDays, CheckCircle2,
  ChevronDown, CloudLightning, Clock3, Info, MapPin, Radio, RefreshCw, Search, ShieldAlert,
  Sun,
} from 'lucide-react';
import { loadAppData, loadGameDetail, pollInterval } from './api/data';
import type { AppData, Game, SourceHealth } from './types';

type LeagueFilter = 'All' | 'NFL' | 'College';
type StatusFilter = 'all' | 'live' | 'upcoming';
type SortOrder = 'kickoff' | 'risk' | 'teams';

const initialData: AppData = { games: [], sources: [], usingSample: false, lastError: 'Waiting for published game data.' };

function dateInZone(date: Date, timezone?: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: timezone || undefined, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date);
    return `${parts.find((p) => p.type === 'year')?.value}-${parts.find((p) => p.type === 'month')?.value}-${parts.find((p) => p.type === 'day')?.value}`;
  } catch {
    return date.toLocaleDateString('en-CA');
  }
}

function formatTime(value?: string, timezone?: string, withZone = true): string {
  if (!value) return 'Time TBD';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: timezone || undefined,
      hour: 'numeric', minute: '2-digit',
      ...(withZone ? { timeZoneName: 'short' as const } : {}),
    }).format(date);
  } catch {
    return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }
}

function compassDirection(bearing?: number): string | undefined {
  if (bearing === undefined || !Number.isFinite(bearing)) return undefined;
  return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(bearing / 45) % 8];
}

function formatFullDate(value?: string, timezone?: string): string {
  if (!value) return 'Date pending';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  try {
    return new Intl.DateTimeFormat('en-US', { timeZone: timezone || undefined, weekday: 'long', month: 'long', day: 'numeric' }).format(date);
  } catch {
    return date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
  }
}

function formatPercent(value?: number): string {
  return value === undefined ? '—' : `${Math.round(value * 100)}%`;
}

function formatAge(source?: SourceHealth, generatedAt?: string): string {
  const timestamp = source?.updatedAt || generatedAt;
  const minutes = source?.ageMinutes ?? (timestamp ? Math.max(0, (Date.now() - new Date(timestamp).getTime()) / 60_000) : undefined);
  if (minutes === undefined || !Number.isFinite(minutes)) return 'Update time unavailable';
  if (minutes < 1) return 'Updated just now';
  if (minutes < 60) return `Updated ${Math.floor(minutes)}m ago`;
  const hours = Math.floor(minutes / 60);
  return `Updated ${hours}h ${Math.floor(minutes % 60)}m ago`;
}

function freshnessTone(source?: SourceHealth, generatedAt?: string): 'good' | 'warn' | 'bad' | 'unknown' {
  const status = source?.status?.toLowerCase();
  const age = source?.ageMinutes ?? (source?.updatedAt || generatedAt ? (Date.now() - new Date(source?.updatedAt || generatedAt || '').getTime()) / 60_000 : undefined);
  if (status && ['stale', 'failed', 'error', 'unavailable', 'degraded'].some((word) => status.includes(word))) return 'bad';
  if (age === undefined || !Number.isFinite(age)) return 'unknown';
  const freshnessLimit = source?.freshnessLimitMinutes ?? 60;
  if (age > freshnessLimit) return 'bad';
  if (age > Math.min(20, freshnessLimit / 3)) return 'warn';
  return 'good';
}

function sourceLabel(name: string): string {
  const labels: Record<string, string> = { mrms: 'Lightning observations', glm: 'Lightning observations', href: 'HREF model', href_calibrated_thunder: 'HREF thunder model', hrrr: 'HRRR model', nws_forecast: 'NWS forecast', nws_alerts: 'NWS warning feed', nfl: 'NFL schedule', nfl_schedule: 'NFL schedule', college: 'College schedule', cfbd: 'College schedule', sports_status: 'Live game status' };
  return labels[name.toLowerCase()] || name.replaceAll('_', ' ');
}

function statusLabel(game: Game): string {
  if (game.officialDelay) return 'Official weather delay';
  if (game.modeledDelay) return 'Modeled weather hold';
  if (['in_progress', 'live', 'inprogress'].includes(game.status)) return 'In progress';
  if (['weather_delay', 'suspended'].includes(game.status)) return 'Weather delay';
  if (game.status === 'completed' || game.status === 'final') return 'Final';
  if (game.status === 'postponed') return 'Postponed';
  if (game.status === 'cancelled') return 'Cancelled';
  return 'Scheduled';
}

function isLive(game: Game): boolean {
  return game.activeDelay || ['in_progress', 'live', 'inprogress', 'weather_delay', 'suspended'].includes(game.status);
}

function isDome(game: Game): boolean {
  return game.roofType === 'fixed_dome' || game.roofType === 'closed' || game.roofType === 'dome';
}

function routeGameId(): string | undefined {
  const match = window.location.hash.match(/^#\/game\/([^/?#]+)/);
  return match ? decodeURIComponent(match[1]) : undefined;
}

function RiskRing({ probability, large = false, label = 'delay risk' }: { probability?: number; large?: boolean; label?: string }) {
  const known = probability !== undefined;
  const p = known ? Math.max(0, Math.min(1, probability)) : 0;
  const color = !known ? 'var(--muted)' : p >= 0.35 ? 'var(--risk-high)' : p >= 0.15 ? 'var(--risk-medium)' : 'var(--risk-low)';
  return (
    <div className={`risk-ring ${large ? 'risk-ring-large' : ''}`} style={{ '--risk-value': `${p * 100}%`, '--ring-color': color } as CSSProperties & Record<`--${string}`, string>} aria-label={known ? `${label} ${formatPercent(probability)}` : `${label} unavailable`}>
      <span>{known ? formatPercent(probability) : '—'}</span>
      <small>{label}</small>
    </div>
  );
}

function GameCard({ game, generatedAt }: { game: Game; generatedAt?: string }) {
  const live = isLive(game);
  const dome = isDome(game);
  const source = game.sources[0];
  const status = statusLabel(game);
  const scheduleTime = formatTime(game.kickoff, game.timezone);
  const scope = game.forecastScope;
  const riskLabel = scope === 'regional_outlook' || scope === 'regional_proxy' ? 'regional' : scope === 'forecast_pending' ? 'outlook pending' : scope === 'archive_missing' ? 'no archive' : scope === 'weather_unavailable' ? 'unavailable' : 'delay risk';
  return (
    <a className={`game-card ${live ? 'game-card-live' : ''}`} href={`#/game/${encodeURIComponent(game.id)}`} aria-label={`${game.awayTeam} at ${game.homeTeam}, ${status}`}>
      <div className="game-card-topline">
        <span className="league-label">{game.league === 'NFL' ? 'NFL' : 'COLLEGE FOOTBALL'}</span>
        <div className="game-card-statuses">
          {game.nwsWarnings.length > 0 && <span className={`nws-warning-chip ${game.nwsWarningsFresh ? '' : 'stale'}`} aria-label={`${game.nwsWarnings.length} NWS severe thunderstorm warning${game.nwsWarnings.length === 1 ? '' : 's'}${game.nwsWarningsFresh ? '' : ', last update may be stale'}`} title={game.nwsWarningsFresh ? 'Active NWS severe thunderstorm warning for this venue' : 'NWS warning in the last published snapshot; feed may have changed'}><AlertTriangle size={10} aria-hidden="true" /> NWS {game.nwsWarnings.length > 1 ? `WARNINGS · ${game.nwsWarnings.length}` : 'WARNING'}{game.nwsWarningsFresh ? '' : ' · STALE'}</span>}
          <span className={`status-badge ${live ? 'status-live' : ''}`}>
            {live ? <Radio size={12} aria-hidden="true" /> : null}{status}
          </span>
        </div>
      </div>
      <div className="game-card-main">
        <div className="matchup">
          <div className="team-row">
            <span className={`team-mark ${game.league === 'NFL' ? 'mark-pro' : 'mark-college'}`}>{game.awayAbbr}</span>
            <span className="team-name">{game.awayTeam}</span>
            {game.awayScore !== undefined && <b className="team-score">{game.awayScore}</b>}
          </div>
          <div className="team-row">
            <span className={`team-mark ${game.league === 'NFL' ? 'mark-pro' : 'mark-college'}`}>{game.homeAbbr}</span>
            <span className="team-name">{game.homeTeam}</span>
            {game.homeScore !== undefined && <b className="team-score">{game.homeScore}</b>}
          </div>
          <div className="venue-line"><MapPin size={13} aria-hidden="true" /> <span>{game.venue}</span></div>
          <div className="time-line"><Clock3 size={13} aria-hidden="true" /> <span>{live ? status : scheduleTime}</span></div>
        </div>
        <div className="card-risk">
          {dome ? (
            <div className="dome-risk"><span className="dome-icon"><Sun size={17} /></span><b>Indoor</b><small>Lightning delay model disabled</small></div>
          ) : game.activeDelay ? (
            <div className="resume-card-metric"><span className="metric-eyebrow">EST. RESUME · P50</span><strong>{formatTime(game.resumeP50, game.timezone, false)}</strong><small>P75 {formatTime(game.resumeP75, game.timezone, false)} · P90 {formatTime(game.resumeP90, game.timezone, false)}</small></div>
          ) : (
            <RiskRing probability={game.delayProbability} label={riskLabel} />
          )}
        </div>
      </div>
      {game.activeDelay ? (
        <div className="card-bottom active-card-bottom">
          <span className={`delay-kind ${game.officialDelay ? 'official' : 'modeled'}`}><CloudLightning size={13} />{game.officialDelay ? 'Reported weather delay' : 'Modeled hold · not officially confirmed'}</span>
          <span className="compact-resume">Resumed by {formatPercent(game.probabilityAdditional[30] === undefined ? undefined : 1 - game.probabilityAdditional[30])} in 30m <ArrowRight size={14} /></span>
        </div>
      ) : (
        <div className="card-bottom">
          <span className="risk-breakdown">{live ? <>New delay <b>{dome ? '—' : formatPercent(game.inGameDelayProbability ?? game.delayProbability)}</b></> : <>Kickoff <b>{dome ? '—' : formatPercent(game.kickoffDelayProbability)}</b><i /> In-game <b>{dome ? '—' : formatPercent(game.inGameDelayProbability)}</b></>}</span>
          <span className="updated-note"><span className={`fresh-dot ${freshnessTone(source, game.generatedAt || generatedAt)}`} />{formatAge(source, game.generatedAt || generatedAt)}</span>
        </div>
      )}
    </a>
  );
}

function RiskTimeline({ game }: { game: Game }) {
  if (game.hourlyRisk.length === 0) return <div className="empty-timeline">Hourly risk data has not been published for this forecast.</div>;
  const width = 720;
  const height = 186;
  const left = 35;
  const right = 12;
  const top = 14;
  const bottom = 32;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const points = game.hourlyRisk.map((point, index) => ({
    ...point,
    x: left + (game.hourlyRisk.length < 2 ? plotWidth / 2 : (index / (game.hourlyRisk.length - 1)) * plotWidth),
    y: top + (1 - Math.min(1, point.probability)) * plotHeight,
  }));
  const line = points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point.y}`).join(' ');
  const area = `${line} L ${points.at(-1)?.x || left} ${top + plotHeight} L ${points[0]?.x || left} ${top + plotHeight} Z`;
  return (
    <div className="timeline-wrap">
      <div className="timeline-scale"><span>100%</span><span>50%</span><span>0%</span></div>
      <svg className="risk-timeline" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`Hourly weather delay hazard. ${points.map((p) => `${formatTime(p.at, game.timezone, false)} ${formatPercent(p.probability)}`).join(', ')}`}>
        {[0, 0.5, 1].map((fraction) => <line key={fraction} x1={left} y1={top + fraction * plotHeight} x2={width - right} y2={top + fraction * plotHeight} className="chart-grid" />)}
        <path d={area} className="timeline-area" />
        <path d={line} className="timeline-line" />
        {points.map((point) => <circle key={point.at} cx={point.x} cy={point.y} r="4" className="timeline-dot"><title>{formatTime(point.at, game.timezone)} · {formatPercent(point.probability)}</title></circle>)}
        {points.filter((_, index) => index === 0 || index === points.length - 1 || index % Math.ceil(points.length / 5) === 0).map((point) => <text key={`${point.at}-label`} x={point.x} y={height - 9} className="chart-label" textAnchor="middle">{formatTime(point.at, game.timezone, false)}</text>)}
      </svg>
    </div>
  );
}

function ResumeChart({ game }: { game: Game }) {
  const rows = game.resumeCdf;
  if (rows.length < 2) {
    const probabilities = [30, 45, 60, 90, 120, 150, 180].map((minute) => ({ minute, probability: game.probabilityAdditional[minute] === undefined ? undefined : 1 - game.probabilityAdditional[minute] }));
    if (!probabilities.some((row) => row.probability !== undefined)) return <div className="empty-timeline">Resume-time distribution has not been published.</div>;
    return <div className="resume-bars" aria-label="Probability the game resumes by each time from now">
      {probabilities.map((row) => row.probability === undefined ? null : <div className="resume-bar-row" key={row.minute}><span>+{row.minute} min</span><div className="bar-track"><div className="bar-fill" style={{ width: `${row.probability * 100}%` }} /></div><b>{formatPercent(row.probability)}</b></div>)}
    </div>;
  }
  const width = 720;
  const height = 210;
  const left = 34;
  const top = 12;
  const bottom = 31;
  const plotWidth = width - left - 14;
  const plotHeight = height - top - bottom;
  const start = new Date(rows[0].at).getTime();
  const end = new Date(rows.at(-1)!.at).getTime();
  const span = Math.max(1, end - start);
  const points = rows.map((row) => ({ ...row, x: left + ((new Date(row.at).getTime() - start) / span) * plotWidth, y: top + (1 - row.probability) * plotHeight }));
  const line = points.map((point, index) => `${index ? 'L' : 'M'} ${point.x} ${point.y}`).join(' ');
  const area = `${line} L ${points.at(-1)?.x || left} ${top + plotHeight} L ${left} ${top + plotHeight} Z`;
  return (
    <div className="timeline-wrap resume-chart-wrap">
      <div className="timeline-scale"><span>100%</span><span>50%</span><span>0%</span></div>
      <svg className="risk-timeline resume-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`Chance of resumption by time. ${points.map((p) => `${formatTime(p.at, game.timezone, false)} ${formatPercent(p.probability)}`).join(', ')}`}>
        {[0, 0.5, 1].map((fraction) => <line key={fraction} x1={left} y1={top + fraction * plotHeight} x2={width - 14} y2={top + fraction * plotHeight} className="chart-grid" />)}
        <path d={area} className="resume-area" /><path d={line} className="resume-line" />
        {points.filter((_, index) => index % Math.max(1, Math.floor(points.length / 7)) === 0 || index === points.length - 1).map((point) => <circle key={point.at} cx={point.x} cy={point.y} r="4" className="resume-dot"><title>{formatTime(point.at, game.timezone)} · {formatPercent(point.probability)}</title></circle>)}
        {[0, 0.5, 1].map((fraction) => <text key={`label-${fraction}`} x={left + fraction * plotWidth} y={height - 8} className="chart-label" textAnchor={fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle'}>{formatTime(new Date(start + fraction * span).toISOString(), game.timezone, false)}</text>)}
      </svg>
    </div>
  );
}

function StormMotionSummary({ motion }: { motion: NonNullable<Game['weatherContext']>['stormMotion'] }) {
  if (!motion) return null;
  const statusLabels: Record<string, string> = {
    approaching: 'Approaching the configured boundary',
    moving_away: 'Moving away from the venue',
    inside_policy: 'Reflectivity echo overlaps the configured boundary',
    uncertain: 'Direction is uncertain',
    no_history: 'Tracking radar echo; another scan is needed',
    no_echoes: 'No qualifying radar echo within 60 miles',
    stale: 'Radar motion sample is stale',
    ambiguous: 'Storm track association is uncertain',
    unavailable: 'Storm movement is unavailable',
  };
  const etaLabel = motion.etaLowerMinutes !== undefined && motion.etaUpperMinutes !== undefined
    ? `${Math.round(motion.etaLowerMinutes)}–${Math.round(motion.etaUpperMinutes)} min`
    : motion.etaMinutes !== undefined ? `${Math.round(motion.etaMinutes)} min` : '—';
  const direction = compassDirection(motion.bearingDegrees);
  return <div className="storm-motion-summary">
    <div className="storm-motion-heading"><b>Observed storm movement</b><span>{motion.observedAt ? formatTime(motion.observedAt) : 'No timestamp'}</span></div>
    <p>{statusLabels[motion.status || ''] || 'Storm movement unavailable'}</p>
    <div className="policy-stats">
      <div><span>Motion</span><b>{direction && motion.speedMph !== undefined ? `${direction} · ${Math.round(motion.speedMph)} mph` : '—'}</b></div>
      <div><span>Boundary distance</span><b>{motion.distanceToPolicyBoundaryMiles === undefined ? '—' : `${motion.distanceToPolicyBoundaryMiles.toFixed(1)} mi`}</b></div>
      <div><span>Estimated arrival</span><b>{etaLabel}</b></div>
      <div><span>Echo peak</span><b>{motion.maxReflectivityDbz === undefined ? '—' : `${motion.maxReflectivityDbz.toFixed(0)} dBZ`}</b></div>
    </div>
    {motion.modelAdjustmentApplied && <p className="motion-calibration-note">This fresh track contributed a small experimental adjustment to the 30–180 minute delay estimate.</p>}
    <p className="muted-copy">Radar echo movement is not a lightning observation. ETA bounds are sensitivity estimates, not calibrated confidence intervals.</p>
  </div>;
}

function SourceList({ sources, generatedAt }: { sources: SourceHealth[]; generatedAt?: string }) {
  if (!sources.length) return <p className="muted-copy">Provider timestamps are not included in the current public snapshot.</p>;
  return <div className="source-list">
    {sources.map((source, index) => <div className="source-item" key={`${source.name}-${index}`}>
      <span className={`fresh-dot ${freshnessTone(source, generatedAt)}`} />
      <span className="source-name">{sourceLabel(source.name)}</span>
      <span className="source-time">{formatAge(source, generatedAt)}</span>
      {source.status && <span className={`source-status ${freshnessTone(source, generatedAt)}`}>{source.status}</span>}
    </div>)}
  </div>;
}

function PolicyRingView({ game }: { game: Game }) {
  const trigger = game.policyTriggerMiles;
  const radii = [...new Set([...(game.policyMonitorRadiiMiles || []), ...(trigger === undefined ? [] : [trigger])])]
    .filter((radius) => Number.isFinite(radius) && radius > 0)
    .sort((left, right) => right - left);
  const outerRadius = Math.max(1, ...radii);
  const scale = 145 / outerRadius;
  const description = trigger === undefined
    ? 'Venue coordinates or policy radius are unavailable.'
    : `Venue-centered policy circles: trigger ${trigger} miles; monitoring ${radii.filter((radius) => radius !== trigger).join(', ') || 'radii unavailable'} miles.`;
  return <div className="policy-ring-view">
    <svg viewBox="0 0 360 360" role="img" aria-label={description}>
      <circle className="ring-map-background" cx="180" cy="180" r="156" />
      {radii.map((radius) => <g key={radius}>
        <circle
          className={`policy-radius-ring ${radius === trigger ? 'trigger-ring' : 'monitor-ring'}`}
          cx="180"
          cy="180"
          r={radius * scale}
        />
        <text className="ring-mile-label" x="180" y={180 - radius * scale + 12}>{radius} mi</text>
      </g>)}
      <circle className="stadium-marker-halo" cx="180" cy="180" r="12" />
      <circle className="stadium-marker" cx="180" cy="180" r="6" />
      <text className="stadium-marker-label" x="180" y="204" textAnchor="middle">STADIUM</text>
    </svg>
    <div className="policy-ring-caption">
      <b>{trigger === undefined ? 'Policy geometry unavailable' : `Trigger radius ${trigger} mi`}</b>
      <span>{description}</span>
    </div>
  </div>;
}

function DetailPage({ game, globalSources, generatedAt, onBack }: { game: Game; globalSources: SourceHealth[]; generatedAt?: string; onBack: () => void }) {
  const active = game.activeDelay;
  const liveRisk = isLive(game) && !active;
  const regionalRisk = game.forecastScope === 'regional_outlook' || game.forecastScope === 'regional_proxy';
  const sourceList = game.sources.length ? game.sources : globalSources;
  const dome = isDome(game);
  const dataWarnings = game.qualityWarnings;
  const clearCdf = game.weatherClearCdf || [];
  const clearCdfStart = clearCdf.length ? new Date(clearCdf[0].at).getTime() : NaN;
  const clearCdfRows = [15, 30, 45, 60, 90, 120, 180].flatMap((minutes) => {
    const point = clearCdf.find((row) => new Date(row.at).getTime() >= clearCdfStart + minutes * 60_000);
    return point ? [{ minutes, probability: point.probability }] : [];
  });
  const resumeRangeRows = [30, 45, 60, 90, 120, 150, 180].flatMap((minutes) => {
    const probability = game.probabilityAdditional[minutes];
    return probability === undefined ? [] : [{ minutes, probability: 1 - probability }];
  });
  return (
    <main className="detail-page">
      <button className="back-link" onClick={onBack}><ArrowLeft size={16} /> Scoreboard</button>
      <section className="detail-hero">
        <div className="detail-hero-heading">
          <span className="league-label">{game.league === 'NFL' ? 'NFL' : 'COLLEGE FOOTBALL'} <span className="dot-separator">·</span> {formatFullDate(game.kickoff, game.timezone)}</span>
          <span className={`status-badge ${isLive(game) ? 'status-live' : ''}`}>{isLive(game) && <Radio size={12} />}{statusLabel(game)}</span>
        </div>
        <div className="detail-matchup">
          <div className="detail-team"><span className={`team-mark team-mark-large ${game.league === 'NFL' ? 'mark-pro' : 'mark-college'}`}>{game.awayAbbr}</span><div><span className="team-side">AWAY</span><h1>{game.awayTeam}</h1></div><b className="detail-score">{game.awayScore ?? '—'}</b></div>
          <span className="at-divider">AT</span>
          <div className="detail-team"><span className={`team-mark team-mark-large ${game.league === 'NFL' ? 'mark-pro' : 'mark-college'}`}>{game.homeAbbr}</span><div><span className="team-side">HOME</span><h1>{game.homeTeam}</h1></div><b className="detail-score">{game.homeScore ?? '—'}</b></div>
        </div>
        <div className="detail-venue-line"><MapPin size={15} />{game.venue}{game.city ? ` · ${game.city}` : ''}{game.neutralSite && <b className="neutral-site-chip">Neutral site</b>}<span className="dot-separator">·</span>{dome ? 'Fixed dome' : `${game.roofType.replaceAll('_', ' ')} venue`}</div>
        <div className="time-dual"><span><Clock3 size={14} /> Venue time <b>{formatTime(game.kickoff, game.timezone)}</b></span><span>Your local time <b>{formatTime(game.kickoff)}</b></span></div>
      </section>

      {game.nwsWarnings.length > 0 && <div className={`warning-strip nws-warning-strip ${game.nwsWarningsFresh ? '' : 'stale'}`}>
        <AlertTriangle size={17} aria-hidden="true" />
        <div>
          <b>{game.nwsWarningsFresh ? 'Active NWS severe thunderstorm warning for this venue' : 'NWS warning in the last published snapshot'}</b>
          {game.nwsWarnings.map((warning, index) => <span key={`${warning.url || warning.headline || 'nws-warning'}-${index}`}>
            {warning.headline || 'Severe thunderstorm warning'}{warning.expiresAt ? ` · Expires ${formatTime(warning.expiresAt, game.timezone)}` : ''}{warning.severity ? ` · ${warning.severity}` : ''}{warning.url ? <> · <a href={warning.url} target="_blank" rel="noreferrer">View NWS alert</a></> : null}
          </span>)}
          {!game.nwsWarningsFresh && <span>The alert feed may have changed since this snapshot.</span>}
        </div>
      </div>}
      {dataWarnings.length > 0 && <div className="warning-strip"><AlertTriangle size={17} /><div><b>Forecast quality notice</b><span>{dataWarnings.join(' · ')}</span></div></div>}
      {dome ? (
        <section className="dome-notice"><div className="notice-icon"><Sun size={23} /></div><div><h2>Indoor — lightning delay model disabled</h2><p>This stadium's fixed roof protects play from lightning. Weather conditions outside may still affect travel or venue operations.</p></div></section>
      ) : active ? (
        <section className="forecast-panel active-forecast">
          <div className="panel-heading"><div><span className="section-kicker"><CloudLightning size={14} /> ACTIVE WEATHER DELAY</span><h2>Estimated game resumption</h2></div><span className={`delay-kind ${game.officialDelay ? 'official' : 'modeled'}`}>{game.officialDelay ? 'Officially reported weather delay' : 'Modeled weather hold · not officially confirmed'}</span></div>
          <p className="panel-description">Chance that play has resumed by each time. Weather clearance and football restart are separate; operational restart time is included where data allows.</p>
          <div className="resume-estimates">
            <div className="resume-estimate primary"><span>P50 · MOST LIKELY</span><b>{formatTime(game.resumeP50, game.timezone)}</b></div>
            <div className="resume-estimate"><span>P75</span><b>{formatTime(game.resumeP75, game.timezone)}</b></div>
            <div className="resume-estimate"><span>P90</span><b>{formatTime(game.resumeP90, game.timezone)}</b></div>
          </div>
          {resumeRangeRows.length > 0 && <div className="resume-range-summary">
            <div className="chart-heading"><b>30–180 minute outlook</b><span>Chance play resumes by each time</span></div>
            <div className="resume-range-grid">
              {resumeRangeRows.map((row) => <div key={row.minutes}><span>+{row.minutes} min</span><b>{formatPercent(row.probability)}</b></div>)}
            </div>
          </div>}
          <div className="chart-heading"><b>Probability game resumes by time</b><span>Current forecast</span></div>
          <ResumeChart game={game} />
          {clearCdfRows.length > 0 && <div className="weather-clear-cdf">
            <div className="chart-heading"><b>Probability weather is clear by time</b><span>All-clear estimate</span></div>
            <div className="policy-stats">
              {clearCdfRows.map((row) => <div key={row.minutes}><span>+{row.minutes} min</span><b>{formatPercent(row.probability)}</b></div>)}
            </div>
          </div>}
          <div className="resume-facts">
            <div><span>Earliest weather all-clear</span><b>{formatTime(game.earliestWeatherClear, game.timezone)}</b></div>
            <div><span>Latest modeled qualifying lightning</span><b>{formatTime(game.lastQualifyingEvent, game.timezone)}</b></div>
          </div>
          <p className="reset-note"><Info size={15} /> Another qualifying strike could reset the quiet-period clock.</p>
        </section>
      ) : (
        <section className="forecast-panel pregame-forecast">
          <div className="panel-heading"><div><span className="section-kicker"><Activity size={14} /> {liveRisk ? 'LIVE GAME RISK' : 'PREGAME FORECAST'}</span><h2>{liveRisk ? 'New weather delay' : 'Weather delay risk'}</h2></div><span className="forecast-asof">Forecast {formatAge(undefined, game.generatedAt || generatedAt).toLowerCase()}</span></div>
          <div className="risk-detail-top">
            <div className="risk-primary"><RiskRing probability={game.delayProbability} large label={regionalRisk ? 'regional' : 'delay risk'} /><p>{regionalRisk ? 'Regional thunder inputs; this is an experimental scenario, not calibrated stadium delay odds.' : liveRisk ? 'Chance of a new lightning-related delay from now through game end.' : 'Chance of at least one lightning-related delay affecting this game.'}</p></div>
            {liveRisk ? <div className="risk-splits"><div><span>New in-game delay</span><b>{formatPercent(game.inGameDelayProbability ?? game.delayProbability)}</b></div><div><span>Expected delay from now</span><b>{game.expectedDelayMinutes === undefined ? '—' : `${Math.round(game.expectedDelayMinutes)} min`}</b></div><div><span>Kickoff delay</span><b>Not applicable</b></div></div> : <div className="risk-splits"><div><span>Kickoff delay</span><b>{formatPercent(game.kickoffDelayProbability)}</b></div><div><span>In-game delay</span><b>{formatPercent(game.inGameDelayProbability)}</b></div><div><span>Expected total delay</span><b>{game.expectedDelayMinutes === undefined ? '—' : `${Math.round(game.expectedDelayMinutes)} min`}</b></div></div>}
          </div>
          {game.highestRiskWindow && <div className="highest-window"><span>Highest-risk window</span><b>{game.highestRiskWindow}</b></div>}
          <div className="chart-heading"><b>{regionalRisk ? 'Regional thunder outlook' : 'Hourly weather delay hazard'}</b><span>Venue local time</span></div>
          <RiskTimeline game={game} />
        </section>
      )}

      {!active && game.officialResumeAt && <section className="info-panel official-resume-panel">
        <div className="small-panel-title"><CheckCircle2 size={17} /><h2>Official resume time</h2></div>
        <p className="panel-subcopy">Reported play resumed at {formatTime(game.officialResumeAt, game.timezone)}.</p>
      </section>}

      <div className="detail-grid">
        <section className="info-panel">
          <div className="small-panel-title"><ShieldAlert size={17} /><h2>Venue policy</h2></div>
          <div className="policy-primary"><span>Verification</span><b className={`verification ${game.policyVerification || 'unknown'}`}>{(game.policyVerification || 'unknown').replaceAll('_', ' ')}</b></div>
          <div className="policy-stats"><div><span>Trigger radius</span><b>{game.policyTriggerMiles === undefined ? 'Not published' : `${game.policyTriggerMiles} mi`}</b></div><div><span>Quiet interval</span><b>{game.policyQuietMinutes === undefined ? 'Not published' : `${game.policyQuietMinutes} min`}</b></div></div>
          {game.policyNotes && <p className="policy-note">{game.policyNotes}</p>}
          {game.policySource && <a className="source-link" href={game.policySource} target="_blank" rel="noreferrer">Policy source <ArrowRight size={13} /></a>}
          <p className="muted-copy">The tracker never treats a general league guideline as a verified venue policy.</p>
        </section>
        <section className="info-panel">
          <div className="small-panel-title"><RefreshCw size={17} /><h2>Forecast freshness</h2></div>
          <p className="panel-subcopy">{game.modelVersion ? `Model ${game.modelVersion}` : 'Model version unavailable'} <span className="dot-separator">·</span> {formatAge(undefined, game.generatedAt || generatedAt)}</p>
          <SourceList sources={sourceList} generatedAt={game.generatedAt || generatedAt} />
        </section>
      </div>

      {!dome && <section className="info-panel proximity-panel">
        <div className="small-panel-title"><MapPin size={17} /><h2>Lightning policy geometry</h2></div>
        <PolicyRingView game={game} />
        <p className="muted-copy">Circle sizes use the configured mile radii. Lightning summaries and radar echo tracks are separate public observations.</p>
      </section>}

      {game.weatherContext && <section className="info-panel weather-context-panel">
        <div className="small-panel-title"><CloudLightning size={17} /><h2>Storm context</h2></div>
        {game.weatherContext.stormMotion && <StormMotionSummary motion={game.weatherContext.stormMotion} />}
        <div className="policy-stats">
          {game.weatherContext.hrrr && <>
            <div><span>HRRR reflectivity</span><b>{game.weatherContext.hrrr.reflectivityDbz?.toFixed(1) ?? '—'} dBZ</b></div>
            <div><span>HRRR rain rate</span><b>{game.weatherContext.hrrr.precipitationRateKgM2S === undefined ? '—' : `${(game.weatherContext.hrrr.precipitationRateKgM2S * 3600).toFixed(1)} mm/h`}</b></div>
            <div><span>HRRR CAPE</span><b>{game.weatherContext.hrrr.capeJkg?.toFixed(0) ?? '—'} J/kg</b></div>
            <div><span>HRRR 10 m wind</span><b>{game.weatherContext.hrrr.windSpeed10mMs?.toFixed(1) ?? '—'} m/s</b></div>
          </>}
          {game.weatherContext.glm && <>
            <div><span>{game.weatherContext.glm.satellite || 'GOES GLM'} flashes</span><b>{game.weatherContext.glm.flashCount ?? '—'} / {game.weatherContext.glm.durationSeconds ?? 0}s</b></div>
            <div><span>GLM flash rate</span><b>{game.weatherContext.glm.flashRatePerMinute?.toFixed(1) ?? '—'} / min</b></div>
            <div><span>GLM observation ended</span><b>{formatTime(game.weatherContext.glm.validEnd, game.timezone)}</b></div>
          </>}
        </div>
        <p className="muted-copy">HRRR and GLM are supporting context. Only a fresh approaching MRMS radar track can add the capped experimental 30–180 minute hazard adjustment.</p>
      </section>}

      <section className="safety-notice"><div className="safety-icon"><Info size={19} /></div><div><b>Experimental estimate, not official safety guidance</b><p>This tracker provides experimental weather-policy estimates. It is not affiliated with the NFL, NCAA, NOAA, any team, or venue. Official team and venue announcements take precedence over every model estimate. NOAA data attribution: weather and lightning observations are sourced from public NOAA data products where available; no NOAA endorsement is implied.</p></div></section>
    </main>
  );
}

function App() {
  const [data, setData] = useState<AppData>(initialData);
  const [league, setLeague] = useState<LeagueFilter>('All');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [sort, setSort] = useState<SortOrder>('kickoff');
  const [query, setQuery] = useState('');
  const [selectedDate, setSelectedDate] = useState(() => dateInZone(new Date()));
  const [routeId, setRouteId] = useState<string | undefined>(routeGameId);
  const [detail, setDetail] = useState<Game | undefined>();
  const [refreshing, setRefreshing] = useState(false);
  const versionRef = useRef<string | undefined>();
  const requestRef = useRef<AbortController | undefined>();
  const searchRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async (initial = false) => {
    if (requestRef.current && !initial) requestRef.current.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    if (!initial) setRefreshing(true);
    try {
      const update = await loadAppData(initial ? undefined : versionRef.current, controller.signal);
      if (update.version) versionRef.current = update.version;
      if (update.games.length > 0 || initial) {
        setData((old) => ({ ...old, ...update, games: update.games.length ? update.games : old.games }));
      } else {
        setData((old) => ({ ...old, ...update, games: old.games }));
      }
    } catch {
      // Keep the most recent snapshot visible when a refresh fails.
    } finally {
      if (!controller.signal.aborted) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh(true);
    const timer = window.setInterval(() => void refresh(), pollInterval);
    const onHashChange = () => setRouteId(routeGameId());
    const onSearchShortcut = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const isTyping = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target?.isContentEditable;
      if (event.key === '/' && !isTyping) {
        event.preventDefault();
        searchRef.current?.focus();
      } else if (event.key === 'Escape' && document.activeElement === searchRef.current) {
        setQuery('');
        searchRef.current?.blur();
      }
    };
    window.addEventListener('hashchange', onHashChange);
    window.addEventListener('keydown', onSearchShortcut);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('hashchange', onHashChange);
      window.removeEventListener('keydown', onSearchShortcut);
      requestRef.current?.abort();
    };
  }, [refresh]);

  useEffect(() => {
    if (!routeId) {
      setDetail(undefined);
      return;
    }
    const existing = data.games.find((game) => game.id === routeId);
    if (existing) setDetail(existing);
    const controller = new AbortController();
    void loadGameDetail(routeId, controller.signal, versionRef.current).then((loaded) => {
      if (loaded && !controller.signal.aborted) {
        setDetail(() => {
          const summary = data.games.find((game) => game.id === routeId);
          if (!summary) return loaded;
          return {
            ...summary,
            ...loaded,
            homeTeam: loaded.homeTeam === 'Home team' ? summary.homeTeam : loaded.homeTeam,
            awayTeam: loaded.awayTeam === 'Away team' ? summary.awayTeam : loaded.awayTeam,
            venue: loaded.venue === 'Venue not listed' ? summary.venue : loaded.venue,
            sources: loaded.sources.length ? loaded.sources : summary.sources,
            raw: loaded.raw,
          };
        });
      }
    });
    return () => controller.abort();
  }, [routeId, data.games]);

  const now = new Date();
  const games = useMemo(() => {
    const searchTerm = query.trim().toLowerCase();
    return data.games.filter((game) => {
      if (league !== 'All' && game.league !== league) return false;
      if (dateInZone(new Date(game.kickoff), game.timezone) !== selectedDate) return false;
      if (statusFilter === 'live' && !isLive(game)) return false;
      if (statusFilter === 'upcoming' && isLive(game)) return false;
      if (searchTerm && ![game.homeTeam, game.awayTeam, game.venue, game.city, game.homeAbbr, game.awayAbbr].some((part) => part?.toLowerCase().includes(searchTerm))) return false;
      return true;
    }).sort((a, b) => {
      if (sort === 'risk') return (b.delayProbability ?? -1) - (a.delayProbability ?? -1);
      if (sort === 'teams') return `${a.awayTeam} ${a.homeTeam}`.localeCompare(`${b.awayTeam} ${b.homeTeam}`);
      return new Date(a.kickoff).getTime() - new Date(b.kickoff).getTime();
    });
  }, [data.games, league, query, selectedDate, sort, statusFilter]);

  const selectedDateLabel = useMemo(() => {
    const date = new Date(`${selectedDate}T12:00:00`);
    if (selectedDate === dateInZone(now)) return 'Today';
    return date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  }, [selectedDate, now]);

  const adjustDate = (offset: number) => {
    const date = new Date(`${selectedDate}T12:00:00`);
    date.setDate(date.getDate() + offset);
    setSelectedDate(dateInZone(date));
  };
  const backToBoard = () => { window.location.hash = '/'; };

  if (routeId && detail) return <div className="app-shell"><SiteHeader onHome={backToBoard} /><DetailPage game={detail} globalSources={data.sources} generatedAt={data.generatedAt} onBack={backToBoard} /><SiteFooter /></div>;

  return (
    <div className="app-shell">
      <SiteHeader onHome={backToBoard} />
      <main className="board-page">
        <section className="board-hero">
          <div className="hero-copy"><div className="eyebrow"><span className="hero-pulse" /> WEATHER WATCH <span className="dot-separator">·</span> {now.getFullYear()} SEASON</div><h1>Game day,<br /><span>weather aware.</span></h1><p>Track lightning delay risk and estimated resume times across football.</p></div>
          <div className="hero-stat"><span className="stat-icon"><Activity size={18} /></span><div><b>{data.games.filter(isLive).length.toString().padStart(2, '0')}</b><span>games live now</span></div><span className="hero-stat-divider" /><div><b>{data.games.filter((game) => game.activeDelay).length.toString().padStart(2, '0')}</b><span>weather holds</span></div></div>
          <div className="hero-orbit hero-orbit-one" /><div className="hero-orbit hero-orbit-two" /><div className="hero-lightning"><CloudLightning size={82} strokeWidth={1.15} /></div>
        </section>

        {data.lastError && <div className="demo-notice warning-demo"><AlertTriangle size={16} /><span><b>Live game data unavailable.</b> No example scores or forecasts are shown. {data.lastError}</span></div>}

        <div className="board-controls">
          <div className="board-controls-title"><div><span className="section-kicker">SCOREBOARD</span><h2>Upcoming & live games</h2></div><div className="last-updated"><span className={`fresh-dot ${refreshing ? 'updating' : 'good'}`} />{refreshing ? 'Updating' : formatAge(undefined, data.generatedAt)}</div></div>
          <div className="filter-row">
            <div className="segmented leagues" role="group" aria-label="Filter by league">
              {(['All', 'NFL', 'College'] as LeagueFilter[]).map((item) => <button key={item} className={league === item ? 'selected' : ''} onClick={() => setLeague(item)}>{item === 'College' ? 'College football' : item}</button>)}
            </div>
            <div className="date-controls" aria-label="Game date">
              <button className="icon-button" onClick={() => adjustDate(-1)} aria-label="Previous day"><ArrowLeft size={17} /></button>
              <label className="date-picker-label"><CalendarDays size={15} /><span>{selectedDateLabel}</span><input aria-label="Select date" type="date" value={selectedDate} onChange={(event) => setSelectedDate(event.target.value)} /></label>
              <button className="icon-button" onClick={() => adjustDate(1)} aria-label="Next day"><ArrowRight size={17} /></button>
            </div>
          </div>
          <div className="filter-row secondary-filters">
            <div className="segmented small-segmented" role="group" aria-label="Filter game status">
              <button className={statusFilter === 'all' ? 'selected' : ''} onClick={() => setStatusFilter('all')}>All games <span>{data.games.length}</span></button>
              <button className={statusFilter === 'live' ? 'selected' : ''} onClick={() => setStatusFilter('live')}>Live <span>{data.games.filter(isLive).length}</span></button>
              <button className={statusFilter === 'upcoming' ? 'selected' : ''} onClick={() => setStatusFilter('upcoming')}>Upcoming</button>
            </div>
            <div className="list-tools">
              <label className="search-box"><Search size={16} /><input ref={searchRef} placeholder="Search teams or venues" value={query} onChange={(event) => setQuery(event.target.value)} /><kbd>/</kbd></label>
              <label className="sort-select"><ArrowDownUp size={14} /><span className="sr-only">Sort games</span><select value={sort} onChange={(event) => setSort(event.target.value as SortOrder)}><option value="kickoff">Kickoff</option><option value="risk">Delay risk</option><option value="teams">Teams A–Z</option></select><ChevronDown size={13} /></label>
            </div>
          </div>
        </div>

        <div className="games-heading"><div><h2>{selectedDateLabel} <span>· {games.length} {games.length === 1 ? 'game' : 'games'}</span></h2><p>All kickoff times shown in each venue's local timezone</p></div><span className="watch-label"><span className="fresh-dot good" /> Live updates every 60 seconds</span></div>

        {games.length ? <section className="game-grid" aria-label="Football games">{games.map((game) => <GameCard game={game} generatedAt={data.generatedAt} key={game.id} />)}</section> : (
          <section className="empty-state"><div className="empty-icon"><CalendarDays size={24} /></div><h3>{data.lastError ? 'No live scoreboard snapshot' : 'No games match this view'}</h3><p>{data.lastError ? 'The scoreboard will populate after schedule data is published.' : 'Try a different day, league, or search term.'}</p>{!data.lastError && <button className="text-button" onClick={() => { setQuery(''); setStatusFilter('all'); setLeague('All'); }}>Clear filters</button>}</section>
        )}

        <section className="board-info-grid">
          <div className="board-info-card"><span className="board-info-icon"><CloudLightning size={18} /></span><div><b>Delay risk</b><p>Probability of at least one lightning policy hold during the game.</p></div></div>
          <div className="board-info-card"><span className="board-info-icon green"><CheckCircle2 size={18} /></span><div><b>Official vs modeled</b><p>We label reported delays separately from weather holds inferred by the model.</p></div></div>
          <div className="board-info-card"><span className="board-info-icon amber"><ShieldAlert size={18} /></span><div><b>Venue policy matters</b><p>Policy source and verification status are shown on each game page.</p></div></div>
        </section>
        <section className="board-safety"><Info size={16} /><p><b>Experimental estimates only.</b> This tracker is not an official safety system. Follow team and venue instructions; those announcements take precedence.</p><a href="#safety" onClick={(event) => { event.preventDefault(); document.querySelector('.board-info-grid')?.scrollIntoView({ behavior: 'smooth' }); }}>Safety & data <ArrowRight size={13} /></a></section>
      </main>
      <SiteFooter />
    </div>
  );
}

function SiteHeader({ onHome }: { onHome: () => void }) {
  return <header className="site-header"><div className="header-inner"><a href="#/" className="brand" onClick={onHome} aria-label="NFL Delay Tracker home"><span className="brand-mark"><CloudLightning size={20} fill="currentColor" /></span><span>NFL <b>Delay Tracker</b></span></a><nav className="header-nav" aria-label="Main navigation"><a href="#/" className="active">Scoreboard</a><a href="#/about-safety" onClick={(event) => { event.preventDefault(); document.querySelector('.board-info-grid')?.scrollIntoView({ behavior: 'smooth' }); }}>How it works</a></nav><div className="header-live"><span className="live-signal" /> PUBLIC DATA <span className="header-dot">·</span> EXPERIMENTAL</div></div></header>;
}

function SiteFooter() {
  return <footer className="site-footer"><div className="footer-inner"><a href="#/" className="footer-brand"><span className="brand-mark"><CloudLightning size={17} /></span> NFL Delay Tracker</a><span>Experimental estimates · Not official safety guidance</span><span>NOAA data attribution · No NOAA endorsement</span></div></footer>;
}

export default App;
