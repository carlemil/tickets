# Tickets

- **Test:** `uv run pytest -q` from the repo root. Never with pytest-xdist: the suite
  shares one process with an in-thread server on purpose.
- **PLAN.md** is the source of truth: every change updates it.
- **docs.html** is the user's manual: a change to how the board or the skill behaves
  updates it too.
- **Deploy:** merge the card into master, push, then restart the backend 30 s later with
  `powershell -NoProfile -File restart-backend.ps1 -Delay 30`. Run the tests
  (`uv run pytest -q`) after the merge and before the push unless the run's deploy brief
  says the test stage already ran them on this exact tree (master had not moved since it
  merged it in, #156) or skipped them as docs-only (#163); with no such brief, run them.
