#!/usr/bin/env python3
"""Offline regression tests. Run with python -B. Scratch files stay under the configured temporary directory."""

import json
import os
import subprocess
import sys
import tempfile

GATE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "pr-text-gate.py")

def call(payload, state):
    env = dict(os.environ)
    env.pop("HOOK_PR_TEXT_SKIP", None)
    env.pop("HOOK_COMPACTION_AUDITOR", None)
    env["PR_TEXT_GATE_STATE_DIR"] = state
    r = subprocess.run([sys.executable, "-B", GATE], input=json.dumps(payload).encode("utf-8"), capture_output=True, env=env, timeout=30)
    return r.returncode, r.stderr.decode("utf-8", "replace")

def run(tool, command, cwd, tool_cwd=None, state=None, session="s0", agent=None):
    tool_input = {"command": command}
    if tool_cwd:
        tool_input["cwd"] = tool_cwd
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": cwd, "session_id": session}
    if agent:
        payload["agent_id"] = agent
    return call(payload, state)

def harness_read(path, state, session, first=1, count=None, agent=None):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().split("\n")
    total = len(lines)
    count = total - first + 1 if count is None else count
    tool_input = {"file_path": path}
    if first != 1 or count != total:
        tool_input.update({"offset": first, "limit": count})
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": tool_input, "session_id": session,
               "tool_response": {"type": "text", "file": {"filePath": path, "content": "\n".join(lines[first - 1:first - 1 + count]),
                                                          "numLines": count, "startLine": first, "totalLines": total}}}
    if agent:
        payload["agent_id"] = agent
    return call(payload, state)

def project_read(path, state, session, first=1, count=None):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    total = len(lines)
    count = total - first + 1 if count is None else count
    page = lines[first - 1:first - 1 + count]
    last = first + len(page) - 1
    body = {"ok": True, "path": path, "lines": page, "startLine": first, "endLine": last, "truncated": last < total}
    if last < total:
        body["nextStartLine"] = last + 1
    else:
        body["totalLines"] = total
    payload = {"hook_event_name": "PostToolUse", "tool_name": "mcp__project__read", "tool_input": {"filePath": path, "startLine": first},
               "session_id": session, "tool_response": {"content": [{"type": "text", "text": json.dumps(body)}]}}
    return call(payload, state)

def make_repo(root, name, with_rules=True):
    repo = os.path.join(root, name)
    os.makedirs(os.path.join(repo, ".github"))
    subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
    if with_rules:
        with open(os.path.join(repo, "CONTRIBUTING.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write("".join("Rule %d of the contributor guide.\n" % k for k in range(1, 41)))
        with open(os.path.join(repo, ".github", "pull_request_template.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write("## Problem\n\n## Change\n\n## How to test\n")
    return repo

def text_cases(tmp, state, failures):
    work = os.path.join(tmp, "work")
    other = os.path.join(tmp, "other")
    os.makedirs(work)
    os.makedirs(other)
    with open(os.path.join(work, "bad-body.md"), "w", encoding="utf-8") as f:
        f.write("## Problem\n\nThe record is under review-logs/x.md.\n")
    with open(os.path.join(work, "good-body.md"), "w", encoding="utf-8") as f:
        f.write("## Problem\n\nAdd a Connect button for Claude Desktop, Claude Code, Codex and Cursor.\nRound the value; review the model list; seat count is five.\n")
    with open(os.path.join(other, "b.md"), "w", encoding="utf-8") as f:
        f.write("Reviewed in round 4.\n")

    refuse = [
        ("inline body names a model", "PowerShell", 'gh pr create --draft --title "Add a reader" --body "Built by gpt-6-astra"', work, None, "gpt-6-astra"),
        ("body file carries a records path", "Bash", "gh pr edit 12 --body-file bad-body.md", work, None, "review-logs"),
        ("short flags, title carries a numbered round", "Bash", 'gh pr create -t "fix: round 3 findings" -b "ok"', work, None, "round 3"),
        ("bash heredoc body", "Bash", "gh pr create --title \"Add x\" --body \"$(cat <<'EOF'\n## Problem\nREJECT from the second reviewer\nEOF\n)\"", work, None, "REJECT"),
        ("PowerShell here-string body", "PowerShell", "gh pr edit 5 --body @'\n## Problem\n@codex review found it\n'@", work, None, "@codex"),
        ("MCP shell: cwd from the tool input, cd, relative body file", "mcp__project__shell", "Set-Location '" + other + "'; gh pr create --title 'Add x' --body-file .\\b.md", tmp, work, "round 4"),
        ("flag=value form", "Bash", 'gh pr create --title="Add x" --body="claude-opus-5 wrote it"', work, None, "claude-opus-5"),
        ("repo flag before pr, marker in the title", "Bash", 'gh -R owner/repo pr edit 3 --title "EXECUTED probes"', work, None, "EXECUTED"),
        ("version directly after the family name", "Bash", 'gh pr create --title "Add x" --body "ran on qwen3.6-35b"', work, None, "qwen3.6-35b"),
        ("backtick line continuation", "PowerShell", 'gh pr create --title "Add x" `\n  --body "seat 2 said so"', work, None, "seat 2"),
    ]
    allow = [
        ("product names and ordinary words", "Bash", 'gh pr create --title "Add a Connect button for Claude Desktop and Codex" --body "Round the value; review the model list."', work, None),
        ("clean body file", "PowerShell", "gh pr create --draft --title 'Add a reader' --body-file good-body.md", work, None),
        ("a comment is not the body", "Bash", 'gh pr comment 12 --body "@codex review"', work, None),
        ("a read", "Bash", "gh pr view 12 --json body", work, None),
        ("a body the hook cannot see", "PowerShell", 'gh pr create --title "Add x" --body $body', work, None),
        ("not a pull request command", "Bash", 'git commit -m "test: cover round 3 of the retry loop"', work, None),
        ("fill from commits", "Bash", "gh pr create --fill", work, None),
        ("an issue, not a pull request", "Bash", 'gh issue create --title "reader: x" --body "gpt-6-astra"', work, None),
        ("a missing body file", "Bash", "gh pr create --title \"Add x\" --body-file nowhere.md", work, None),
        ("a later command's words are not the body", "Bash", 'gh pr create --title "Clean" --body "clean"; echo "seat 2 round 3"', work, None),
        ("ordinary words that start like a family name", "Bash", 'gh pr create --title "Add x" --body "philosophy-2, claudette-3 and llamas-4 fixtures"', work, None),
        ("separators inside a quoted body", "Bash", 'gh pr create --title "Add x" --body "a; b | c && gpt-compatible clients"', work, None),
    ]
    for name, tool, command, cwd, tool_cwd, needle in refuse:
        code, err = run(tool, command, cwd, tool_cwd, state)
        if code != 2 or needle not in err:
            failures.append(f"TEXT REFUSE {name}: exit {code}, stderr {err[:200]!r}")
    for name, tool, command, cwd, tool_cwd in allow:
        code, err = run(tool, command, cwd, tool_cwd, state)
        if code != 0:
            failures.append(f"TEXT ALLOW {name}: exit {code}, stderr {err[:200]!r}")
    return len(refuse) + len(allow)

def read_cases(tmp, state, failures):
    repo = make_repo(tmp, "repo")
    bare = make_repo(tmp, "bare", with_rules=False)
    twin = make_repo(tmp, "twin")  
    guide = os.path.join(repo, "CONTRIBUTING.md")
    template = os.path.join(repo, ".github", "pull_request_template.md")
    create = 'gh pr create --draft --title "Add a reader" --body "Adds a bounded reader."'
    edit_body = 'gh pr edit 7 --body "Adds a bounded reader."'
    count = [0]

    def expect(name, got, code, needle=None):
        count[0] += 1
        if got[0] != code or (needle and needle not in got[1]):
            failures.append(f"READ {name}: exit {got[0]} (wanted {code}), stderr {got[1][:240]!r}")

    expect("nothing read: create is refused and names the guide", run("Bash", create, repo, state=state, session="r1"), 2, "CONTRIBUTING.md")
    expect("nothing read: an edit of the body is refused", run("PowerShell", edit_body, repo, state=state, session="r1"), 2, "CONTRIBUTING.md")
    expect("nothing read: an edit that writes no text passes", run("Bash", "gh pr edit 7 --add-label docs", repo, state=state, session="r1"), 0)
    expect("a repository with neither file passes", run("Bash", create, bare, state=state, session="r1"), 0)

    harness_read(guide, state, "r2")
    expect("only the guide read: refused, names the template", run("Bash", create, repo, state=state, session="r2"), 2, "pull_request_template.md")
    harness_read(template, state, "r2")
    expect("both read whole by the harness Read: passes", run("Bash", create, repo, state=state, session="r2"), 0)
    expect("another session has read nothing", run("Bash", create, repo, state=state, session="r2-other"), 2, "CONTRIBUTING.md")
    expect("the same bytes in a second worktree count", run("Bash", create, twin, state=state, session="r2"), 0)
    expect("an MCP shell whose cwd is the repository", run("mcp__project__shell", create, tmp, tool_cwd=repo, state=state, session="r2"), 0)

    project_read(guide, state, "r3")
    project_read(template, state, "r3")
    expect("both read whole by a MCP read: passes", run("Bash", create, repo, state=state, session="r3"), 0)

    project_read(guide, state, "r4", first=1, count=10)
    project_read(template, state, "r4")
    expect("a first page only: refused", run("Bash", create, repo, state=state, session="r4"), 2, "CONTRIBUTING.md")
    project_read(guide, state, "r4", first=11)
    expect("pages that chain to the end: passes", run("Bash", create, repo, state=state, session="r4"), 0)

    harness_read(guide, state, "r5", first=5, count=10)
    harness_read(template, state, "r5")
    expect("a harness page from the middle: refused", run("Bash", create, repo, state=state, session="r5"), 2, "CONTRIBUTING.md")

    harness_read(guide, state, "r6", agent="worker-1")
    harness_read(template, state, "r6", agent="worker-1")
    expect("a worker's read does not count for the driver", run("Bash", create, repo, state=state, session="r6"), 2, "CONTRIBUTING.md")
    expect("a worker's read counts for that worker", run("Bash", create, repo, state=state, session="r6", agent="worker-1"), 0)

    with open(guide, "a", encoding="utf-8", newline="\n") as f:
        f.write("Rule 41, added after the read.\n")
    expect("the guide changed after the read: refused", run("Bash", create, repo, state=state, session="r2"), 2, "CONTRIBUTING.md")
    harness_read(guide, state, "r2")
    expect("read again after the change: passes", run("Bash", create, repo, state=state, session="r2"), 0)
    expect("refused text still wins after the read", run("Bash", 'gh pr create --title "Add x" --body "seat 2 said so"', repo, state=state, session="r2"), 2, "seat 2")
    return count[0]

def main():
    failures = []
    tmp = tempfile.mkdtemp(prefix="pr-text-selftest-")
    print("Scratch retained: " + tmp)
    state = os.path.join(tmp, "state")
    n_text = text_cases(tmp, state, failures)
    n_read = read_cases(tmp, state, failures)
    total = n_text + n_read
    if failures:
        print(f"FAIL {len(failures)} of {total}")
        for f in failures:
            print("  " + f)
        return 1
    print(f"PASS {total} of {total} ({n_text} text, {n_read} read) against {GATE}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
