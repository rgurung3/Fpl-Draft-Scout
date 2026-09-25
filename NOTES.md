# Draft Scout – project notes

Keep this file up to date. Claude reads it at the start of every chat in the
Project, so it's how new chats know where things stand.

## Status
- v1 working: waiver targets, free agents table, league squads
- On GitHub: https://github.com/rgurung3/Fpl-Draft-Scout
- Tests (pytest) and CI (GitHub Actions) added

## How it works
- `app.py` fetches data from the FPL Draft site (league details, who owns
  whom) and the classic FPL site (fixtures + difficulty), then rates every
  player 0–100.
- `static/index.html` is the page you see in the browser.
- Run locally: `python app.py`, then open http://127.0.0.1:5000

## Decisions
- Flask backend, because the Draft site blocks requests made directly from
  a browser page.
- Rating = form, points per game, xGI per 90, minutes share, fixture
  difficulty (next 3 GWs), scaled down for injury doubts. Weights differ by
  position and live in `WEIGHTS` in app.py.
- Waiver suggestions only show when a free agent rates at least 3 points
  higher than my weakest player in the same position.

## Roadmap
1. Trade analyzer
2. AI "why this pick?" explanations (Claude API, key in .env)
3. Deploy to Render so it works on my phone

## Known issues / ideas
- The FPL Draft data feed isn't officially documented and could change.
- Not yet tested against my real league.
