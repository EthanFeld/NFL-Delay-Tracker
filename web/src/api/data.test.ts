import { describe, expect, it } from 'vitest';
import { normalizeGame } from './data';

describe('scoreboard game normalization', () => {
  it('keeps venue-local outdoor risk breakdown and warning badges', () => {
    const game = normalizeGame({
      id: 'outdoor-game',
      league: 'NFL',
      home_team: 'Home Team',
      away_team: 'Away Team',
      kickoff_utc: '2026-09-20T17:00:00Z',
      status: 'scheduled',
      venue: {
        name: 'Open Field',
        timezone: 'America/New_York',
        roof_type: 'outdoor',
      },
      pregame: {
        delay_probability: 0.3,
        kickoff_delay_probability: 0.2,
        in_game_delay_probability: 0.1,
      },
      quality: { status: 'experimental' },
      generated_at: '2026-09-20T12:00:00Z',
      weather: {
        venue_features: {
          nws_alerts: {
            status: 'ok',
            alerts: [{ headline: 'Severe thunderstorm warning', severity: 'Severe' }],
          },
        },
      },
    });

    expect(game.timezone).toBe('America/New_York');
    expect(game.roofType).toBe('outdoor');
    expect(game.delayProbability).toBe(0.3);
    expect(game.kickoffDelayProbability).toBe(0.2);
    expect(game.inGameDelayProbability).toBe(0.1);
    expect(game.nwsWarnings).toHaveLength(1);
    expect(game.generatedAt).toBe('2026-09-20T12:00:00Z');
  });

  it('marks fixed-roof games as indoor', () => {
    const game = normalizeGame({
      id: 'dome-game',
      league: 'NFL',
      home_team: 'Home Team',
      away_team: 'Away Team',
      kickoff_utc: '2026-09-20T17:00:00Z',
      venue: { name: 'Indoor Stadium', timezone: 'America/Indiana/Indianapolis', roof_type: 'fixed_dome' },
    });

    expect(game.roofType).toBe('fixed_dome');
    expect(game.timezone).toBe('America/Indiana/Indianapolis');
  });

  it('keeps regional forecast scope visible to the game view', () => {
    const game = normalizeGame({
      id: 'week-ahead-game',
      league: 'NFL',
      home_team: 'Home Team',
      away_team: 'Away Team',
      kickoff_utc: '2026-09-27T17:00:00Z',
      status: 'scheduled',
      quality: { forecast_scope: 'regional_outlook' },
    });

    expect(game.forecastScope).toBe('regional_outlook');
  });

  it('normalizes global ensemble conditions without treating them as delay risk', () => {
    const game = normalizeGame({
      id: 'global-outlook-game',
      league: 'NFL',
      home_team: 'Home Team',
      away_team: 'Away Team',
      kickoff_utc: '2026-09-27T20:25:00Z',
      quality: { forecast_scope: 'global_weather_outlook' },
      weather: {
        venue_features: {
          global_weather_outlook: {
            model: 'ecmwf_ifs025_ensemble',
            valid_at: '2026-09-27T21:00:00Z',
            native_resolution_hours: 3,
            grid_resolution_km: 25,
            member_count: 50,
            valid_member_count: 49,
            condition_member_counts: {
              clear_or_cloudy: 32,
              fog: 1,
              precipitation_or_snow: 15,
              other_or_unclassified: 1,
            },
            attribution_url: 'https://open-meteo.com/en/docs/ensemble-api',
          },
        },
      },
      pregame: { delay_probability: 0.99, kickoff_delay_probability: 0.8, in_game_delay_probability: 0.5 },
    });

    expect(game.forecastScope).toBe('global_weather_outlook');
    expect(game.weatherContext?.globalOutlook).toEqual({
      model: 'ecmwf_ifs025_ensemble',
      validAt: '2026-09-27T21:00:00Z',
      nativeResolutionHours: 3,
      gridResolutionKm: 25,
      memberCount: 50,
      validMemberCount: 49,
      conditionMemberCounts: { clearOrCloudy: 32, fog: 1, precipitationOrSnow: 15, otherOrUnclassified: 1 },
      attributionUrl: 'https://open-meteo.com/en/docs/ensemble-api',
    });
    // Normalization preserves source data; App renders condition-only UI for this scope.
    expect(game.delayProbability).toBe(0.99);
  });

  it('normalizes active-delay resume quantiles and additional-resume odds', () => {
    const game = normalizeGame({
      id: 'delayed-game',
      league: 'NFL',
      home_team: 'Home Team',
      away_team: 'Away Team',
      kickoff_utc: '2026-09-20T17:00:00Z',
      venue: { name: 'Open Field', timezone: 'America/New_York', roof_type: 'outdoor' },
      delay: {
        active: true,
        officially_confirmed: true,
        resume_p50: '2026-09-20T17:30:00Z',
        resume_p75: '2026-09-20T17:45:00Z',
        resume_p90: '2026-09-20T18:00:00Z',
        probability_additional_minutes: { 30: 0.6, 180: 0.1 },
      },
    });

    expect(game.activeDelay).toBe(true);
    expect(game.officialDelay).toBe(true);
    expect(game.resumeP50).toBe('2026-09-20T17:30:00Z');
    expect(game.resumeP75).toBe('2026-09-20T17:45:00Z');
    expect(game.resumeP90).toBe('2026-09-20T18:00:00Z');
    expect(game.probabilityAdditional[30]).toBe(0.6);
    expect(game.probabilityAdditional[180]).toBe(0.1);
  });

  it('normalizes observed radar motion separately from lightning', () => {
    const game = normalizeGame({
      id: 'motion-game',
      league: 'NFL',
      home_team: 'Home Team',
      away_team: 'Away Team',
      kickoff_utc: '2026-09-20T17:00:00Z',
      venue: { name: 'Open Field', timezone: 'America/New_York', roof_type: 'outdoor' },
      weather: {
        venue_features: {
          storm_motion: {
            status: 'approaching',
            observed_at: '2026-09-20T17:00:00Z',
            bearing_degrees: 135,
            speed_mph: 28,
            distance_to_policy_boundary_miles: 18,
            eta_lower_minutes: 32,
            eta_upper_minutes: 54,
            max_reflectivity_dbz: 45,
            model_adjustment: { applied: true },
          },
        },
      },
    });

    expect(game.weatherContext?.stormMotion?.status).toBe('approaching');
    expect(game.weatherContext?.stormMotion?.bearingDegrees).toBe(135);
    expect(game.weatherContext?.stormMotion?.etaLowerMinutes).toBe(32);
    expect(game.weatherContext?.stormMotion?.modelAdjustmentApplied).toBe(true);
  });
});
