---
name: tickets-plan
description: Plan cards on this folder's Tickets board (http://127.0.0.1:8123) ahead of building, one Opus agent per card — every card in todo and plan, or only the ids given (`640-650`, `#12 15`). A plan with no open questions moves the card to develop, ready for /tickets-start to build and check; a card with open questions waits for your answers. Plans only, never builds. Run by hand as /tickets-plan in a session opened in the repo's folder.
disable-model-invocation: true
---

# Tickets plan: plan cards ahead, on Opus (#736)

You are the dispatcher of one planning run. It plans; it never builds, tests or lands. The
rules it shares with `/tickets-start` are in that skill's `run.md`; this file says only what
differs. The Tickets repo is `git -C ${CLAUDE_SKILL_DIR} rev-parse --show-toplevel` (the
skill folder is a link); read `<tickets repo>/skill-start/run.md` §0–§2, the brief rules,
**Model per stage**, **Card size**, **Slots**, `### plan` (Plan and Post), **Blocked by a
person**, **Writing card descriptions** and **Failure**, and follow them with the
differences below. Work unattended: never ask in chat; what needs a person goes on the card.

## 1. Which cards

The project is the one whose `path` is this folder (run.md §1); none → stop and say how to
set it. `$ARGUMENTS`:
- empty → every card in `todo` and `plan`, in that order, board order within a lane;
- ids or ranges (`640-650`, `#12`, `12 15 20-22`) → just those, whatever their lane is, as
  long as it is `todo` or `plan`; name each one outside those lanes in the summary.

Skip, and name in the summary, run.md §2's skips (waiting for answers, blocked, assigned to a
person, in flight elsewhere, released by an API error), and with no ids given a card that
already has a `plan` and no open questions (a named card is replanned).

## 2. Plan each card

Up to the Slots limit at a time, each card:
1. `update_card(assignee="claude-agent", session="${CLAUDE_SESSION_ID}", lane="plan")` (a
   `todo` card moves to `plan`); `set_activity(card_id=…, doing="planning (<n> in flight)")`.
2. `git -C <repo> pull --ff-only` once per run (fails → tell each agent the code may be
   behind). No worktree: the agent reads the base checkout and writes nothing in it
   (run.md's #204 rule, read-only), scratch files in `<scratchpad>/card-<id>-plan`.
3. One **background** Agent call, `subagent_type: "tickets-stage"` (run.md's fallback rule),
   `model: "opus"` for every card, a **plan-only brief** per run.md's brief rules, first line
   `Tickets stage: plan #<id> (opus)`: the Plan and Post rules, ending `STAGE: PLANNED` on
   NONE. It never builds: no code edits, no commits, no checklist ticks.
4. **Label large ones:** a card with neither `small` nor `large` whose plan's first line says
   `Size: large` → the agent adds the label `large` with its post (`labels=` the card's labels
   plus `large`), so `/tickets-start` builds it on Opus; judged small → no label (Sonnet).

## 3. Per verdict (you)

After the trimmed check (a person's move wins):
- PLANNED → the card is in `develop`, ready to build; unassign it (`assignee=""`) unless a
  person had it, so `/tickets-start` takes it.
- QUESTIONS → comment "open questions: answer them in the card; `/tickets-plan <id>` or the
  next `/tickets-start` replans with them", unassign.
- BLOCKED → the blocker cards (run.md), unassign.
- MOVED, FAILED, a crash, an API error → as run.md says; the card keeps its lane.

Fill each freed slot with the next card. When the last one is done, clear your activity.

## 4. Summary

One line per card: planned → develop (with its size and model to come: `large` → Opus,
else Sonnet), questions, blocked (by what), skipped (why), failed (why). Then: `/tickets-start`
builds and checks the planned cards. No ideas cards: a planning run only plans.
