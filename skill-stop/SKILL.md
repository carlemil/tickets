---
name: tickets-stop
description: Stop the Tickets loop that /tickets-start started in this session, so the board is no longer worked on a timer. Use when the user asks to stop, cancel or pause the tickets loop.
---

# Stop the Tickets loop

1. CronList (load it with ToolSearch `select:CronList,CronDelete` if it is deferred). The
   loop's jobs are the ones whose prompt starts with `Tickets loop tick.`
2. CronDelete each of them. None → say no Tickets loop is running in this session (a loop
   lives only in the session that started it: stop it there, or close that session).
3. Cards already in flight are left to finish: their background agents report as usual
   and the run wraps up. Say how many are still working, if any; ask before stopping
   them — a card cut off mid-stage keeps its lane and worktree, and the next `/tickets`
   picks it up again.
4. Reply in one line: the loop is stopped (job ids), and `/tickets-start` starts it
   again.
