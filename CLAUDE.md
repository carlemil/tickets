# Tickets

- **Test:** `uv run pytest -q` from the repo root. Never with pytest-xdist: the suite
  shares one process with an in-thread server on purpose. The test stage runs it in full
  (its only exception is the docs-only check, #163). The develop stage runs only the files
  that cover its change (test map, #192; a path with no row, or a new module, runs the
  full suite):

  | changed | develop runs |
  |---|---|
  | `core.py`, `app.py` | full suite (every test file imports `core`; the board and e2e tests drive `app` through the server) |
  | `log-config.json`, `restart-backend.ps1` | `test_app.py` |
  | `board.html` | `test_app.py test_board.py test_e2e.py` |
  | `docs.html` | `test_app.py test_board.py test_idle_gate.py` |
  | `docs-card.png`, `docs_card.py` | `test_app.py test_board.py` |
  | `skill-start/` | `test_idle_gate.py` |
  | other `skill-*/` | the docs-only check, else `test_idle_gate.py` |
  | a `test_*.py` | that file |
  | `conftest.py`, `pyproject.toml`, `uv.lock`, `.gitignore`, a new module | full suite |
  | `*.md` | the docs-only check, else the test files its #163 grep hits |

- **PLAN.md** is the source of truth: every change updates it.
- **docs.html** is the user's manual: a change to how the board or the skill behaves
  updates it too.
- **Deploy:** merge the card into master and push. Run the tests (`uv run pytest -q`)
  after the merge and before the push unless the run's deploy brief says the test stage
  already ran them on this exact tree (master had not moved since it merged it in, #156)
  or skipped them as docs-only (#163); with no such brief, run them. Before the push,
  list what it brings
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
