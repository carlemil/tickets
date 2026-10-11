# Tickets

- **Test:** `uv run pytest -q` from the repo root. Never with pytest-xdist: the suite
  shares one process with an in-thread server on purpose.
- **PLAN.md** is the source of truth: every change updates it.
- **docs.html** is the user's manual: a change to how the board or the skill behaves
  updates it too.
- **Deploy:** merge the card into master and push. Run the tests (`uv run pytest -q`)
  after the merge and before the push unless the run's deploy brief says the test stage
  already ran them on this exact tree (master had not moved since it merged it in, #156);
  with no such brief, run them. Before the push, list what it brings
  (`git diff --name-only origin/master HEAD`, right for a `--no-ff` or a fast-forward
  merge). `board.html`, `docs.html` and `docs-card.png` are read from disk on every
  request: live once merged, no restart. `PLAN.md`, `README.md`, `CLAUDE.md`,
  `test_*.py`, `conftest.py`, `docs_card.py` and the `skill-*` folders are not used by
  the running backend. Any other changed file (`app.py`, `core.py`, `log-config.json`,
  `pyproject.toml`, `uv.lock`, `restart-backend.ps1`, a new module) is loaded once at
  start: restart the backend 30 s after the push with
  `powershell -NoProfile -File restart-backend.ps1 -Delay 30`. A restart or a changed
  live file is `DEPLOY: OK`; only unused files changed deploys nothing beyond the merge,
  tests and push: no restart, `DEPLOY: SKIPPED` (still with its `tests:` line).
