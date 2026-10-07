---
name: tickets-run-loop
description: Keep working this project's Tickets board on a timer — /tickets every N minutes (default 15m) for as long as this session stays open, via /loop. Run by hand as /tickets-run-loop [interval, e.g. 10m or 1h] in a session opened in the project's folder.
disable-model-invocation: true
---

# Tickets on a loop

`/tickets` refuses to be started by the model (`disable-model-invocation`), so `/loop`
cannot call it by name. Instead each tick reads the skill's file and follows it — same
run, same rules. Read that way its placeholders are not filled in, so the tick prompt
fills them (the session id and the skill folder); the prompt below names them in words
because anything written as a placeholder here is filled with this skill's values. The loop lives in this session only: closing it stops the loop.

Interval: `$ARGUMENTS` if it is one (like `10m`, `30m`, `1h`), else `15m`. Anything
shorter than `5m` → use `5m` and say so: a run takes minutes.

Invoke the Skill tool with skill `loop` and args `<interval> <tick prompt>`, the tick
prompt being exactly:

> Tickets loop tick. Read ~/.claude/skills/tickets/SKILL.md and do one run of it in this
> folder, following it exactly, with these differences: where it puts the session-id
> placeholder into `session=`, use ${CLAUDE_SESSION_ID}; where it names the skill-folder
> placeholder, use ~/.claude/skills/tickets. If cards from an earlier tick are still in
> flight in this session (background subagents not yet reported), do not start a new
> run: answer only "tickets: previous run still working" and stop. If the queue is empty,
> do not write ideas or a summary: answer only "tickets: nothing to do" and stop.

Then tell the user in two lines: the board is worked every <interval> while this session
is open, and how to stop it (ask to stop the loop, or close the session).
