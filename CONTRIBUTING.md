# Contributing to NFL Delay Tracker

Keep forecast claims proportional to available data. Preserve source timestamps, policy verification status, and missing-provider information whenever changing the pipeline.

## Local workflow

- Install Python and web dependencies using the commands in `README.md`.
- Run `python -m nfl_delay_tracker.cli validate-config` after venue or policy edits.
- Run Python lint, type checks, unit tests, and the static web build before opening a pull request.
- For model changes, run `python -m nfl_delay_tracker.cli backtest` and include the holdout metrics and sample size in the change description.

## Data rules

- Never commit credentials, raw weather grids, or generated live-data snapshots.
- Do not label a venue assumption as verified without a public source.
- Keep game-delay labels linked to a source and distinguish kickoff delays, in-game suspensions, and unknown durations.
- Do not call an absent media report a confirmed no-delay outcome.

## Model review

Use chronological splits. Report calibration and uncertainty coverage alongside point error. Game-level lightning delays are rare, so include sample counts and avoid claiming production readiness from a small backtest.
