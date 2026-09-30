# Working on Draft Scout

Draft Scout is a Python/Flask web app that helps me win my FPL Draft league.
In Draft, each player can only be owned by one manager per league, so the app
is about free agents, waivers, trades and comparing squads within my league,
not prices or classic FPL transfers. NOTES.md has the current status,
decisions, known issues and roadmap: read it before starting a task.

## How to help me

- I'm still learning. Explain what you changed and why in plain language,
  and list exactly which files changed.
- Before writing code for a new feature, explain the design (what the server
  works out, what the page shows, what gets tested) and wait for me to agree.
  Small fixes don't need this.
- Keep the existing style and structure. Don't reorganise code I didn't ask about.
- Add or update tests whenever logic changes. Before finishing, run
  `ruff check .` and `pytest` and make sure both pass.
- At the end of each task, update NOTES.md (status, how it works, decisions,
  known issues, roadmap) and the README if what the app does changed.
- Never put API keys, tokens or passwords in code. Use environment variables
  and .env (which must stay in .gitignore).
- If a task needs a file whose name starts with a dot, point it out.

## How the project is laid out

- `app.py`: Flask backend and all the logic (ratings, waivers, trades, league
  race). Logic belongs here so it can be tested.
- `static/index.html`: the page. It only draws what the server sends.
- Settings are constants at the top of `app.py` (WEIGHTS, SEASON_WEIGHTS,
  LOOKAHEAD, MIN_MINUTES, SCALE_PERCENTILE, MIN_GAIN, TRADE_MIN_GAIN, etc.).
  New settings go there too.
- `tests/test_app.py`: pytest, using fake FPL data. Tests must never call the
  real FPL site.
- `check_league.py`: checks a real league from the terminal. It needs the real
  Draft site, so I run it on my own computer (cloud sessions can't reach it).
- `.github/workflows/ci.yml`: GitHub Actions runs ruff and pytest on every push.

## Things to keep in mind

- My team ID is 276914 (the entry_id, the number after /entry/ on the Draft
  site). My league, Ballahulics, is head-to-head.
- Draft matches and standings name managers by league entry ID, not entry_id;
  league_entries maps one to the other.
- The live site is https://fpl-draft-scout.onrender.com on Render's free plan.
  It redeploys when main changes, so only merge to main when tests pass.
- The Draft site sits behind Cloudflare. A 403 from it means it's blocking us.
- ruff is pinned in requirements-dev.txt on purpose. Only change the version
  deliberately.
