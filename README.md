# NFL Delay Tracker

NFL Delay Tracker estimates lightning-related weather holds for NFL and NCAA football games. It separates reported delays from model-inferred holds, shows venue policy provenance, and publishes static JSON for a phone-first scoreboard.

Forecasts are experimental estimates, not official safety guidance. Venue and league decisions take precedence. A verified public policy is shown only when a public venue procedure was found; other venues are clearly marked as assumptions or guidance references.

## Current release

The repo has canonical data models, a 165-site NFL/FBS venue registry, schedule adapters, NOAA SPC HREF calibrated thunder probabilities, MRMS lightning and radar reflectivity fields, NWS thunder-probability fallback and active severe-thunderstorm warnings, a global ECMWF conditions fallback, correlated Monte Carlo policy simulation, JSON publication, GitHub workflows, and the React website.

The scoreboard uses [ESPN's current game-score page](https://www.espn.com/nfl/scoreboard) as a visual reference, with game-state cards, team scores, status, and compact matchup browsing. The app is branded NFL Delay Tracker.

SPC HREF CT one-hour calibrated regional thunder probabilities are the preferred near-term forecast input; NWS probability of thunder is fallback when HREF is unavailable and supplies a lower-confidence outlook through seven days. NWS is not blended over HREF by default because its regional probability can inflate venue-delay estimates without local confirmation. Each HREF hour is decoded once for all eligible venues; downloads use retry backoff and model runs older than 18 hours are suppressed. MRMS samples rolling lightning-probability grids and one-minute NLDN cloud-to-ground density inside each venue's configured trigger radius when kickoff is within six hours. NLDN is a regional public proxy; its coverage and network can differ from the venue's operational detector. Active NWS severe-thunderstorm warnings are polygon-matched to outdoor venues and displayed as official alerts; they are informative metadata, not a direct probability uplift. HRRR point samples and GOES GLM total-flash observations are published as supporting storm context and archived for calibration work. They do not change estimates. Forecast scope and missing local coverage are shown with each estimate. Outdoor-game delay probabilities remain experimental and are not calibrated against representative venue outcomes.

When a venue is outside NWS coverage, Open-Meteo's global ECMWF IFS ensemble may provide weather-code member counts as a conditions outlook. Its roughly 25 km grid and 3-hour native step cannot estimate thunderstorms, lightning delay odds, or storm motion. Those games show no numeric delay risk.

Active-delay estimates use fresh MRMS density and future hazard bins when available. During active games, the tracker also links connected MRMS reflectivity objects across recent scans, then estimates bearing, radial motion and constant-speed ETA to the configured policy boundary. A fresh approaching echo can add a small capped hazard pulse only in the 30–180 minute window. Radar echo is not lightning; ETA bounds are sensitivity estimates, and this probability adjustment has not been calibrated. The historical delay corpus contains 29 sourced positive events (25 in the priority range), but lacks archived storm tracks, so backtesting validates only the broad duration prior, not the movement adjustment. Otherwise, the app shows a broad historical duration prior or marks the resume estimate unavailable. MRMS lightning timestamps resolve to the one-minute observation window, not an exact strike time. Compact forecast snapshots are retained across seasons and older daily files are gzip compressed for prospective calibration; historical MRMS replay is unavailable from the rolling public feed.

## Setup

Requires Python 3.12+ and Node.js 20.19+ or 22.12+.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,glm]"
npm install
```

On macOS or Linux, use `python3.12 -m venv .venv` and activate with `source .venv/bin/activate`.

## Run locally

```powershell
python -m nfl_delay_tracker.cli validate-config
python -m nfl_delay_tracker.cli sync-schedules --days 14
python -m nfl_delay_tracker.cli refresh-forecasts --horizon-hours 168 --simulations 20000
python -m nfl_delay_tracker.cli backtest
npm run web:dev
```

`sync-schedules` fetches public NFL and FBS scoreboard data from the backend. `refresh-forecasts` writes individual forecasts, a game index, a manifest and compact daily forecast-issuance archives under `data/`. Pregame snapshots are archived hourly; active delays are archived on each refresh. Older daily files are compressed and retained across seasons, without storing raw weather grids. It refreshes HREF and HRRR by default; pass `--skip-href --skip-hrrr` to reuse cached inputs during frequent live updates. HREF and HRRR contribute only inside their supported 48-hour range; NWS-only 3–7 day outlooks are labeled lower confidence. GOES GLM is sampled each refresh for games near kickoff or in progress. Weather outages are published as degraded or unavailable status; they do not silently become zero risk. `CFBD_API_KEY` may be set in the workflow environment for the authenticated CFBD adapter. Set `NWS_CONTACT_EMAIL` in the workflow secrets to include an identifying contact in NWS requests.

The frontend reads `/data/manifest.json`, `/data/games/index.json`, and per-game JSON from the same site. `VITE_DATA_BASE_URL` can point local builds at another data host when needed.

## Backtesting

```powershell
python -m nfl_delay_tracker.cli backtest
```

The command compares a fixed 40-minute median baseline with an empirical duration prior on sourced historical delay reports. It uses pre-2023 events for training and 2023+ events for chronological holdout. The expanded sample has 22 training and 7 holdout events; overall holdout P50 MAE is 31.0 vs 38.1 minutes, P10-P90 coverage is 71.4% vs 0%, and the Brier score for delays over 60 minutes is 0.2972 vs 0.7143 (empirical vs fixed). For the priority 30-180 minute range, the subset has 19 training and 6 holdout events: P50 MAE is 26.5 vs 41.8 minutes, interval coverage is 83.3% vs 0%, and the over-60-minute Brier score is 0.3089 vs 0.8333. These are selected positive delay events, not a full game denominator; two interruptions from one game are separate observations and may share storm conditions. The backtest scores total delay duration only. It does not measure pregame probability calibration or establish overall forecast skill. Output summary goes to ignored `reports/historical-backtest.json`.

## Development checks

```powershell
ruff check src tests
mypy src/nfl_delay_tracker
pytest
npm run web:test
npm run web:build
```

## Data and workflows

- Edit `config/venues.yaml` and `config/policies.yaml` when venue details change. Run `python -m nfl_delay_tracker.cli validate-config` and `audit-policies` after edits.
- Keep venue policies sourced. The 8-mile/30-minute fallback is explicitly an unverified project assumption, not a universal NCAA or NFL rule.
- Set `CFBD_API_KEY` only as a GitHub Actions secret if using the authenticated provider. Browser assets contain no provider keys.
- Optional `GAME_OVERRIDES_JSON` Actions secret can correct a game. Example: `{"nfl_2026_123":{"official_delay_active":true,"delay_started_at":"2026-09-20T20:15:00Z","source_note":"Team announcement"}}`. Overrides apply after sports-feed status and publish only the validated game state and source note.
- GitHub Actions refresh HREF and HRRR forecasts hourly and live snapshots every five minutes. The frequent workflow reuses cached HREF and HRRR data while refreshing MRMS, GLM and NWS. The scoreboard polls published snapshots every 60 seconds.
- `live-data` contains generated public JSON only. It must not contain weather grids, credentials, source archives, or build instructions.
- GitHub Pages deployment bundles the latest `live-data` JSON with the static app and redeploys after each successful schedule, forecast, or live refresh.
- Set repository **Settings → Pages → Build and deployment → Source** to **GitHub Actions**. GitHub Pages requires a paid plan for a private repository; otherwise the source repository must be public.

## Attribution

Weather data source labels appear with each forecast. NOAA and NWS data use must not imply NOAA endorsement. NFL Delay Tracker is an independent project and is not affiliated with the NFL, NCAA, teams, venues, NOAA, NWS, or ESPN.
