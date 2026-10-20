"""Token use per card on the Tickets board, read from Claude Code's own transcripts (#736).

Every stage agent's transcript (`~/.claude/projects/<project>/<session>/subagents/agent-*.jsonl`)
records the usage of each model call. A stage brief's first line names the stage and the card,
`Tickets stage: <stage> #<id> (<model>)`; older briefs ("You are the TEST stage of card #672")
are matched loosely. It calls no model, so measuring costs nothing:

    uv run python skill-start/usage_report.py --split 2026-10-20 [--since 2026-10-01] [--cards]

Cost units weigh each kind of token by its price relative to Sonnet input: cache writes
1.25x (1-hour ones 2x), cache reads 0.1x, output 5x; Opus calls count `--opus` times
(default 5/3) and Haiku `--haiku` times (default 1/3) a Sonnet call. The dispatcher session's
own tokens are not in it: only the stage agents'.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

NEW = re.compile(r"^\s*Tickets stage: ([\w+]+) #(\d+)")
OLD = re.compile(r"\b(plan|develop|test|deploy)\w*\W{0,3}\s+(?:stage|subagent|agent)\b", re.I)
ANY_CARD = re.compile(r"#(\d+)")


def first_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def model_family(model):
    for family in ("opus", "sonnet", "haiku"):
        if family in (model or ""):
            return family
    return "other"


def read_agent(path):
    """One stage agent: its stage, card, model, day and summed usage; None if not a stage."""
    brief, day, models, usage = None, None, collections.Counter(), {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            msg = row.get("message") or {}
            if brief is None and row.get("type") == "user":
                brief = first_text(msg)
                day = (row.get("timestamp") or "")[:10]
            if row.get("type") == "assistant" and msg.get("usage"):
                models[model_family(msg.get("model"))] += 1
                # a message's content blocks come as several lines with the same id and usage
                usage[msg.get("id") or len(usage)] = msg["usage"]
    if not brief:
        return None
    m = NEW.search(brief)
    if m:
        stage, card = m.group(1).lower(), int(m.group(2))
    elif "Tickets board" in brief[:2000] or "Tickets stage" in brief[:2000]:
        m, any_card = OLD.search(brief[:2000]), ANY_CARD.search(brief)
        if not any_card:
            return None
        stage, card = (m.group(1).lower() if m else "unknown"), int(any_card.group(1))
    else:
        return None
    totals = collections.Counter()
    for u in usage.values():
        creation = u.get("cache_creation") or {}
        hour = creation.get("ephemeral_1h_input_tokens") or 0
        totals["input"] += u.get("input_tokens") or 0
        totals["cache_write"] += (u.get("cache_creation_input_tokens") or 0) - hour
        totals["cache_write_1h"] += hour
        totals["cache_read"] += u.get("cache_read_input_tokens") or 0
        totals["output"] += u.get("output_tokens") or 0
    model = models.most_common(1)[0][0] if models else "other"
    return {"stage": stage, "card": card, "model": model, "day": day, "calls": len(usage),
            **totals}


def cost(agent, factors):
    units = (agent["input"] + 1.25 * agent["cache_write"] + 2 * agent["cache_write_1h"]
             + 0.1 * agent["cache_read"] + 5 * agent["output"])
    return units * factors.get(agent["model"], 1)


def tokens(agent):
    return sum(agent[k] for k in ("input", "cache_write", "cache_write_1h", "cache_read", "output"))


def collect(root, since=""):
    agents = []
    for path in glob.glob(os.path.join(root, "*", "*", "subagents", "agent-*.jsonl")):
        agent = read_agent(path)
        if agent and agent["day"] >= since:
            agents.append(agent)
    return agents


def report(agents, factors, split=None, cards=False):
    lines = []
    periods = collections.defaultdict(list)
    for a in agents:
        periods["all" if not split else ("before " if a["day"] < split else "from ") + split].append(a)
    for name in sorted(periods, key=lambda p: (not p.startswith("before"), p)):
        group = periods[name]
        per_card = collections.defaultdict(float)
        for a in group:
            per_card[a["card"]] += cost(a, factors)
        lines.append(f"## {name}: {len(per_card)} cards, {len(group)} stage agents, "
                     f"{sum(per_card.values()) / max(len(per_card), 1) / 1000:,.0f}k cost units per card")
        lines.append("")
        lines.append("| stage | model | agents | tokens per agent | cost units per agent |")
        lines.append("|---|---|---|---|---|")
        rows = collections.defaultdict(list)
        for a in group:
            rows[(a["stage"], a["model"])].append(a)
        for (stage, model), items in sorted(rows.items()):
            lines.append(f"| {stage} | {model} | {len(items)} | "
                         f"{sum(map(tokens, items)) / len(items) / 1000:,.0f}k | "
                         f"{sum(cost(a, factors) for a in items) / len(items) / 1000:,.0f}k |")
        if cards:
            lines.append("")
            lines.append("| card | stages | cost units |")
            lines.append("|---|---|---|")
            for card in sorted(per_card):
                stages = ", ".join(f"{a['stage']} ({a['model']})" for a in group if a["card"] == card)
                lines.append(f"| #{card} | {stages} | {per_card[card] / 1000:,.0f}k |")
        lines.append("")
    return "\n".join(lines) if lines else "no stage agents found"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    p.add_argument("--since", default="", help="first day counted, YYYY-MM-DD")
    p.add_argument("--split", help="compare the cards before this day with those from it")
    p.add_argument("--cards", action="store_true", help="list every card's cost too")
    p.add_argument("--opus", type=float, default=5 / 3, help="an Opus token's price in Sonnet tokens")
    p.add_argument("--haiku", type=float, default=1 / 3, help="a Haiku token's price in Sonnet tokens")
    args = p.parse_args(argv)
    factors = {"sonnet": 1, "opus": args.opus, "haiku": args.haiku}
    print(report(collect(args.root, args.since), factors, args.split, args.cards))


if __name__ == "__main__":
    sys.exit(main())
