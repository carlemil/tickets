"""The token report reads stage agents' transcripts (#736)."""
import importlib.util
import json
import os

spec = importlib.util.spec_from_file_location("usage_report", "skill-start/usage_report.py")
usage_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage_report)

FACTORS = {"sonnet": 1, "opus": 5 / 3, "haiku": 1 / 3}


def write_agent(root, session, name, brief, calls, day="2026-10-20"):
    folder = os.path.join(root, "D--source-Tickets", session, "subagents")
    os.makedirs(folder, exist_ok=True)
    rows = [{"type": "user", "timestamp": day + "T10:00:00Z",
             "message": {"role": "user", "content": brief}}]
    for i, (model, usage) in enumerate(calls):
        msg = {"id": f"msg_{name}_{i}", "model": model, "usage": usage,
               "content": [{"type": "text", "text": "x"}]}
        # Claude Code writes one line per content block, each with the message's usage
        rows += [{"type": "assistant", "message": msg}, {"type": "assistant", "message": msg}]
    with open(os.path.join(folder, f"agent-{name}.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")


def usage(inp=0, write=0, read=0, out=0, hour=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read, "output_tokens": out,
            "cache_creation": {"ephemeral_1h_input_tokens": hour}}


def test_counts_each_message_once_and_reads_the_brief_line(tmp_path):
    write_agent(tmp_path, "s1", "a", "Tickets stage: build #736 (claude-sonnet-5-5)\nYou are ...",
                [("claude-sonnet-5-5", usage(inp=10, write=1000, read=5000, out=200)),
                 ("claude-sonnet-5-5", usage(inp=5, write=0, read=6000, out=100))])
    [agent] = usage_report.collect(str(tmp_path))
    assert (agent["stage"], agent["card"], agent["model"], agent["calls"]) == ("build", 736, "sonnet", 2)
    assert (agent["input"], agent["cache_write"], agent["cache_read"], agent["output"]) == (15, 1000, 11000, 300)
    assert usage_report.cost(agent, FACTORS) == 15 + 1250 + 1100 + 1500


def test_opus_and_hour_cache_writes_weigh_more(tmp_path):
    write_agent(tmp_path, "s1", "a", "Tickets stage: plan #5 (claude-opus-5-5)",
                [("claude-opus-5-5", usage(write=300, hour=300))])
    [agent] = usage_report.collect(str(tmp_path))
    assert (agent["cache_write"], agent["cache_write_1h"]) == (0, 300)
    assert abs(usage_report.cost(agent, FACTORS) - 600 * 5 / 3) < 1e-9


def test_legacy_briefs_and_non_stage_agents(tmp_path):
    write_agent(tmp_path, "s1", "old", "You are the TEST stage of card #672 on the Tickets board.",
                [("claude-opus-4-1", usage(out=1))])
    write_agent(tmp_path, "s1", "dev", "Tickets board: develop subagent for card #600. Build it.",
                [("claude-sonnet-4-5", usage(out=1))])
    write_agent(tmp_path, "s1", "other", "Find every file that imports core.",
                [("claude-haiku-4-5", usage(out=1))])
    write_agent(tmp_path, "s1", "empty", "", [])
    found = sorted((a["stage"], a["card"], a["model"]) for a in usage_report.collect(str(tmp_path)))
    assert found == [("develop", 600, "sonnet"), ("test", 672, "opus")]


def test_since_and_split_report(tmp_path):
    write_agent(tmp_path, "s1", "a", "Tickets stage: test #1 (opus)", [("opus", usage(out=1000))],
                day="2026-10-01")
    write_agent(tmp_path, "s2", "b", "Tickets stage: build #2 (sonnet)", [("sonnet", usage(out=1000))],
                day="2026-10-21")
    write_agent(tmp_path, "s2", "c", "Tickets stage: test #2 (opus)", [("opus", usage(out=1000))],
                day="2026-10-21")
    agents = usage_report.collect(str(tmp_path))
    assert len(usage_report.collect(str(tmp_path), since="2026-10-02")) == 2
    text = usage_report.report(agents, FACTORS, split="2026-10-20", cards=True)
    before, after = text.split("## from 2026-10-20")
    assert "## before 2026-10-20: 1 cards, 1 stage agents" in before
    assert ": 1 cards, 2 stage agents, 13k cost units per card" in after
    assert "| #2 | build (sonnet), test (opus) | 13k |" in after
    assert usage_report.report([], FACTORS) == "no stage agents found"


def test_main_prints_the_report(tmp_path, capsys):
    write_agent(tmp_path, "s1", "a", "Tickets stage: plan+build #9 (opus)", [("opus", usage(out=600))])
    usage_report.main(["--root", str(tmp_path), "--opus", "2"])
    out = capsys.readouterr().out
    assert "| plan+build | opus | 1 | 1k | 6k |" in out
