# Tickets

- **Test:** `uv run pytest -q` from the repo root. Never with pytest-xdist: the suite
  shares one process with an in-thread server on purpose.
- **PLAN.md** is the source of truth: every change updates it.
- **docs.html** is the user's manual: a change to how the board or the skill behaves
  updates it too.
- **Deploy:** merge the card into master and push. Run the tests (`uv run pytest -q`)
  after the merge and before the push unless the run's deploy brief says the test stage
  already ran them on this exact tree (master had not moved since it merged it in, #156);
  with no such brief, run them. Then look at what the merge changed
  (`git diff --name-only HEAD^1 HEAD`). If it touched `app.py`, `core.py`,
  `log-config.json`, `pyproject.toml` or `uv.lock`, restart the backend 30 s after the
  push with `powershell -NoProfile -File restart-backend.ps1 -Delay 30`: the backend
  loads these once at start. `board.html`, `docs.html` and `docs-card.png` are read from
  disk on every request, so they are live once merged: no restart, still `DEPLOY: OK`. A
  merge that touched only other files (`PLAN.md`, `README.md`, tests, `CLAUDE.md`,
  `skill-*`, `restart-backend.ps1`, `docs_card.py`) deploys nothing: merge, test and
  push as usual, no restart, `DEPLOY: SKIPPED`.
