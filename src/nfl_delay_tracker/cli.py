"""Command-line interface for NFL Delay Tracker."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from nfl_delay_tracker.backtest import run_backtest
from nfl_delay_tracker.models import (
    DataManifest,
    Game,
    GameForecast,
    Venue,
    WeatherPolicy,
    WeatherSnapshot,
)
from nfl_delay_tracker.pipeline import (
    ROOT,
    refresh_forecasts,
    refresh_live_status,
    sync_schedules,
    validate_configuration,
)


def export_schemas(root: Path = ROOT) -> list[str]:
    schema_dir = root / "schemas"
    schema_dir.mkdir(exist_ok=True)
    models = {
        "game.schema.json": Game,
        "venue.schema.json": Venue,
        "policy.schema.json": WeatherPolicy,
        "weather.schema.json": WeatherSnapshot,
        "forecast.schema.json": GameForecast,
        "live.schema.json": GameForecast,
        "manifest.schema.json": DataManifest,
    }
    for filename, model in models.items():
        (schema_dir / filename).write_text(
            json.dumps(cast(Any, model).model_json_schema(), indent=2), encoding="utf-8"
        )
    return list(models)


def audit_policies(root: Path = ROOT) -> dict[str, object]:
    from nfl_delay_tracker.pipeline import load_registry

    venues, policies = load_registry(root)
    today = datetime.now(UTC).date()
    warnings = []
    for venue in venues:
        policy = policies[venue.policy_id]
        if policy.source_checked_at and (today - policy.source_checked_at).days > 365:
            warnings.append(f"{venue.venue_id}: source review older than one year")
        if policy.verification.value in ("unknown", "venue_assumption"):
            warnings.append(f"{venue.venue_id}: {policy.verification.value}")
    return {"venues": len(venues), "warnings": warnings}


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nfl-delay-tracker")
    commands = parser.add_subparsers(dest="command", required=True)
    sync_parser = commands.add_parser("sync-schedules", help="Refresh NFL and NCAA scoreboards")
    sync_parser.add_argument("--days", type=int, default=14)
    refresh_parser = commands.add_parser("refresh-forecasts", help="Create forecast snapshots")
    refresh_parser.add_argument("--horizon-hours", type=int, default=168)
    refresh_parser.add_argument("--simulations", type=int, default=20_000)
    refresh_parser.add_argument(
        "--skip-href", action="store_true", help="Reuse cached HREF inputs without downloading"
    )
    refresh_parser.add_argument(
        "--skip-hrrr", action="store_true", help="Reuse cached HRRR inputs without downloading"
    )
    commands.add_parser("validate-config", help="Validate venues and venue policies")
    live_parser = commands.add_parser("refresh-live", help="Refresh latest scoreboard status")
    live_parser.add_argument("--days", type=int, default=3)
    commands.add_parser("audit-policies", help="List unverified and stale policy sources")
    commands.add_parser("export-schemas", help="Generate JSON Schema files")
    commands.add_parser("backtest", help="Backtest resume duration prior on sourced past delays")
    args = parser.parse_args(argv)

    if args.command == "sync-schedules":
        result = sync_schedules(days=args.days)
    elif args.command == "refresh-live":
        result = refresh_live_status(days=args.days)
    elif args.command == "refresh-forecasts":
        result = refresh_forecasts(
            forecast_horizon_hours=args.horizon_hours,
            simulation_count=args.simulations,
            refresh_href=not args.skip_href,
            refresh_hrrr=not args.skip_hrrr,
        )
    elif args.command == "validate-config":
        result = validate_configuration()
    elif args.command == "audit-policies":
        result = audit_policies()
    elif args.command == "export-schemas":
        result = {"schemas": export_schemas()}
    else:
        result = run_backtest()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
