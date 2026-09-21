# NFL Delay Tracker website

React, TypeScript, and Vite frontend for the NFL Delay Tracker scoreboard.

## Run locally

```sh
npm install
npm run dev
```

`npm run build` creates the static site in `dist/`.

## Data and hosting

The site requests `/data/manifest.json`, `/data/games/index.json`, and individual `/data/games/{game_id}.json` snapshots. Set `VITE_DATA_BASE_URL` to an alternate public data host root when needed. Keep the trailing path rooted above `data/`.

Set `VITE_BASE_PATH` when building for a GitHub Pages project URL, for example `/NFL-Delay-Tracker/`. The hash router works when the site is hosted below the domain root. With no published JSON files, NFL Delay Tracker shows an empty scoreboard and does not invent scores or forecasts.

The browser only reads public JSON. Provider credentials belong in trusted backend workflows and must never be added to this frontend.
