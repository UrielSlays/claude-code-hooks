#!/usr/bin/env python3
"""Offline regression tests. Run with python -B. Scratch files stay under the configured temporary directory."""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

FIXTURE_ROOT = tempfile.mkdtemp(prefix="compaction-documents-")

def fixture_path(relative):
    return os.path.join(FIXTURE_ROOT, *relative.split("/"))

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "compaction-reread-gate.py")
TEN = [
    fixture_path('reference/project/WORKFLOW.md'),
    fixture_path('source/sample-app/TOOL-GUIDE.md'),
    fixture_path('reference/project/REVIEW-CHECKLIST.md'),
    fixture_path('reference/project/RETRY-POLICY.md'),
    fixture_path('reference/project/REUSE-POLICY.md'),
    fixture_path('source/sample-app/docs/dependency-inventory.md'),
    fixture_path('reference/project/DESIGN-INDEX.md'),
    fixture_path('reference/project/ESCALATION-POLICY.md'),
    fixture_path('reference/project/CLEANUP.md'),
    fixture_path('reference/project/reviewer-guide.md'),
]
SESSION = "selftest-session-0001"
CWD = fixture_path('source/sample-app')

def setup_fixtures():
    guide = TEN[1]
    operations = [fixture_path("reference/operations/" + name) for name in
                  ("TASK-LEDGER.md", "TASK-BOARD.md", "REFERENCE-INDEX.md")] + [guide]
    workshop = [fixture_path("reference/workshop/" + name) for name in
                ("WORKSHOP-RULES.md", "VALIDATION.md", "TASK-CARD.md", "PROJECT-INDEX.md")] + [guide]
    for path in set(TEN + operations + workshop):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(f"Reference rule {n}: inspect the source before making a claim.\n" for n in range(1, 81)))
    config = {"rosters": {
        "operations": {"roots": [fixture_path("reference/operations")], "docs": operations,
            "feeds": [fixture_path("reference/operations/activity/feed"), fixture_path("reference/operations/activity/updates")],
            "lists": [fixture_path("reference/operations/activity")], "after": "read the FEEDS and record outstanding tasks"},
        "workshop": {"roots": [fixture_path("reference/workshop")], "docs": workshop,
            "lists": [fixture_path("reference/workshop/backlog")], "after": "list backlog/ and continue the task"},
        "project": {"roots": [fixture_path("reference/project")], "docs": TEN,
            "source_roots": [fixture_path("source")], "source_parents": ["source"],
            "source_names": ["sample-app", "cli-app", "desktop-app", "service-app"],
            "drive_prefixes": ["work-"], "label": "the ten reference documents",
            "after": "check the work since the last read and fix whatever has drifted"}
    }}
    config_path = fixture_path("rosters.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    os.environ["COMPACTION_GATE_CONFIG"] = config_path
    os.environ.pop("HOOK_GATE_CHECK_ROSTERS", None)

setup_fixtures()

def runner_T() -> int:
    # A synthetic legacy timeout. The hook must ignore legacy audit state.
    return 900

failures = []

def run(payload: dict, state_dir: str, extra=None, drop=()):
    env = dict(os.environ, COMPACTION_GATE_STATE_DIR=state_dir)

    env["COMPACTION_AUDIT_STATE_DIR"] = os.path.join(state_dir, "audit")
    env.pop("HOOK_AUDITOR_TIMEOUT_S", None)
    env.update(extra or {})
    for k in drop:
        env.pop(k, None)
    payload = dict(payload)
    if payload.get("transcript_path") == r"C:\x\y\session.jsonl":

        payload["transcript_path"] = os.path.join(state_dir, (payload.get("session_id") or "no-session") + "-parent.jsonl")
        if not os.path.exists(payload["transcript_path"]):
            open(payload["transcript_path"], "a", encoding="utf-8").close()
        if payload.get("hook_event_name") == "SessionStart" and payload.get("source") == "compact" and not payload.get("agent_id"):
            append_parent_boundary(payload["transcript_path"], sessionId=payload.get("session_id", ""))
    p = subprocess.run([sys.executable, "-B", GATE], input=json.dumps(payload).encode("utf-8"), capture_output=True, env=env)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

def append_parent_boundary(path: str, **overrides) -> str:
    record = {"type": "system", "subtype": "compact_boundary", "sessionId": SESSION,
              "isSidechain": False, "uuid": str(uuid.uuid4()), "timestamp": "2000-01-02T12:00:00Z"}
    record.update(overrides)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record["uuid"]

def base(event: str, **kw) -> dict:
    d = {"session_id": SESSION, "transcript_path": r"C:\x\y\session.jsonl", "cwd": CWD, "hook_event_name": event}
    d.update(kw)
    return d

def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  RED  ") + name + ("" if cond else "  — " + detail))
    if not cond:
        failures.append(name)

def whole_read_response(path: str) -> str:
    
    try:
        with open(path, "rb") as f:
            n = len(f.read())
    except Exception:
        n = 10000
    return json.dumps({"path": path, "sizeBytes": n, "content": "x" * min(n, 2000)})

LANE = fixture_path('source/sample-app/docs/session/task-record.md')

def marker_now(marker: str):
    try:
        with open(marker, encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else None
    except Exception:
        return None

def phase_of(marker: str):
    m = marker_now(marker)
    return m.get("phase") if m else None

def clear_context(out: str) -> str:
    for line in reversed((out or "").strip().splitlines()):
        try:
            return json.loads(line)["hookSpecificOutput"]["additionalContext"]
        except Exception:
            continue
    return ""

def boundary_in(text: str):
    mm = re.search(r"^COMPACTION-BOUNDARY: (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)$", text or "", re.M)
    return mm.group(1) if mm else None

def stem(p: str) -> str:
    return os.path.splitext(os.path.basename(p.replace("\\", "/")))[0]

def block(boundary, docs, skip_boundary=False, no_pointer=None, name_only=None, drop=None, work=True, work_pointer=True, work_style="- **WORK SINCE THE READ:** ") -> str:
    lines = []
    if not skip_boundary:
        lines.append(f"COMPACTION-BOUNDARY: {boundary or 'NO-BOUNDARY-PRINTED'}")
    lines += ["", "Per-document drift check:"]
    for i, d in enumerate(docs):
        s = stem(d)
        if s == drop:
            continue
        if s == no_pointer:
            lines.append(f"- {s} — applied (the stop rule, by memory of its place)")
        elif s == name_only:
            lines.append(f"- `{os.path.basename(d)}` — applied")
        elif i % 4 == 3:
            lines.append(f"- {s} — drifted — `{os.path.basename(d)}:{10 + i}` re-anchored, the lane record corrected")
        else:
            lines.append(f"- {s} — applied — `{os.path.basename(d)}:{10 + i}` §the stop rule")
    if work:
        lines.append(work_style + ("`git rev-parse origin/main` = `01234567`; `rg compact_boundary session.jsonl` printed the boundary" if work_pointer else "a shell and a grep, nothing named"))
    return "\n".join(lines) + "\n"

def missing_line(err: str, n: int) -> str:
    mm = re.search(r"^MISSING \(" + str(n) + r"\)[^\n]*", err or "", re.M)
    return mm.group(0) if mm else ""

def worker_compaction_regressions() -> None:
    for identity, fields in (
        ("agent id", {"agent_id": "worker-regression", "agent_type": "worker"}),
        ("worker transcript", {"transcript_path": r"C:\x\y\session\subagents\agent-worker-regression.jsonl"}),
    ):
        state = tempfile.mkdtemp(prefix="compaction-gate-selftest-worker-")
        marker = os.path.join(state, SESSION + ".json")
        worker = base("SessionStart", source="compact", **fields)
        rc, out, err = run(worker, state)
        check(f"worker compact ({identity}): cannot create a parent marker",
              rc == 0 and not os.path.exists(marker) and not out,
              f"rc={rc} marker={os.path.exists(marker)} out={out[:100]}")

        run(base("SessionStart", source="compact"), state)
        original = marker_now(marker)
        doc = next(iter(original["docs"]))
        original["docs"][doc] = 123
        original["cover"] = {doc: {"total": 100, "spans": [[1, 30]]}}
        original["created"] = int(time.time()) - 60
        original["boundary"] = "2000-01-01T00:00:00Z"
        with open(marker, "w", encoding="utf-8") as f:
            json.dump(original, f)
        with open(marker, "rb") as f:
            before = f.read()
        rc, out, err = run(worker, state)
        with open(marker, "rb") as f:
            after = f.read()
        check(f"worker compact ({identity}): preserves parent partial progress byte for byte",
              rc == 0 and after == before and not out, f"rc={rc} changed={after != before}")

        original["phase"] = "await-check"
        original["reads_done"] = 456
        with open(marker, "w", encoding="utf-8") as f:
            json.dump(original, f)
        with open(marker, "rb") as f:
            before = f.read()
        rc, out, err = run(worker, state)
        with open(marker, "rb") as f:
            after = f.read()
        check(f"worker compact ({identity}): preserves parent await-check byte for byte",
              rc == 0 and after == before and not out, f"rc={rc} changed={after != before}")

        run(base("SessionStart", source="compact"), state)
        for path in TEN:
            run(base("PostToolUse", tool_name="mcp__project__read",
                     tool_input={"filePath": path},
                     tool_response={"content": [{"type": "text", "text": whole_read_response(path)}]}),
                state, drop=("HOOK_GATE_CHECK_ROSTERS",))
        check(f"parent reads after {identity}: clear the gate before worker compact",
              not os.path.exists(marker))
        rc, out, err = run(worker, state)
        check(f"worker compact ({identity}): cannot rearm a cleared parent",
              rc == 0 and not os.path.exists(marker) and not out,
              f"rc={rc} marker={os.path.exists(marker)}")

        rc, out, err = run(base("SessionStart", source="compact"), state)
        armed = marker_now(marker)
        check(f"parent compact after {identity}: still arms its own gate",
              rc == 0 and bool(armed) and "COMPACTION GATE" in out, f"rc={rc}")
        with open(marker, "w", encoding="utf-8") as f:
            json.dump(original, f)
        rc, out, err = run(base("SessionStart", source="compact"), state)
        reset = marker_now(marker)
        check(f"parent compact after {identity}: resets progress and await-check",
              rc == 0 and bool(reset) and all(v is None for v in reset["docs"].values())
              and "cover" not in reset and "phase" not in reset and "reads_done" not in reset
              and reset.get("boundary") != original["boundary"], f"rc={rc} marker={reset}")

def parent_boundary_regressions() -> None:
    state = tempfile.mkdtemp(prefix="compaction-gate-selftest-boundary-")
    marker = os.path.join(state, SESSION + ".json")
    transcript = os.path.join(state, "explicit-parent.jsonl")
    open(transcript, "w", encoding="utf-8").close()
    compact = base("SessionStart", source="compact", transcript_path=transcript)
    pre = base("PreToolUse", tool_name="Bash", tool_input={"command": "git status"}, transcript_path=transcript)
    rc, out, err = run(compact, state)
    check("hidden worker: no parent boundary cannot arm", rc == 0 and not os.path.exists(marker) and not out)

    append_parent_boundary(transcript)
    rc, out, err = run(compact, state)
    check("parent transcript boundary: compact arms", rc == 0 and os.path.exists(marker) and "COMPACTION GATE" in out)
    m = marker_now(marker)
    doc = next(iter(m["docs"]))
    m["docs"][doc] = 123
    m["cover"] = {doc: {"total": 100, "spans": [[1, 30]]}}
    with open(marker, "w", encoding="utf-8") as f:
        json.dump(m, f)
    before = PathBytes(marker)
    rc, out, err = run(compact, state)
    check("hidden worker: unchanged parent boundary preserves partial progress",
          rc == 0 and PathBytes(marker) == before and not out)
    rc, out, err = run(pre, state)
    check("parent cursor: no new data preserves progress and enforces current gate",
          rc == 2 and PathBytes(marker) == before)
    m["phase"] = "await-check"
    m["reads_done"] = 456
    with open(marker, "w", encoding="utf-8") as f:
        json.dump(m, f)
    before = PathBytes(marker)
    rc, out, err = run(compact, state)
    check("hidden worker: unchanged parent boundary preserves await-check",
          rc == 0 and PathBytes(marker) == before and not out)

    append_parent_boundary(transcript)
    rc, out, err = run(compact, state)
    m = marker_now(marker)
    check("parent transcript: new boundary resets prior phase and progress",
          rc == 0 and bool(m) and "phase" not in m and "cover" not in m
          and all(v is None for v in m["docs"].values()))
    for path in TEN:
        run(base("PostToolUse", transcript_path=transcript, tool_name="mcp__project__read",
                 tool_input={"filePath": path},
                 tool_response={"content": [{"type": "text", "text": whole_read_response(path)}]}),
            state, drop=("HOOK_GATE_CHECK_ROSTERS",))
    check("parent transcript: reads clear marker", not os.path.exists(marker))
    rc, out, err = run(compact, state)
    check("hidden worker: handled boundary cannot rearm after clear",
          rc == 0 and not os.path.exists(marker) and not out)
    rc, out, err = run(pre, state)
    check("parent cursor: handled boundary remains open on next tool", rc == 0 and not os.path.exists(marker))

    with open(transcript, "a", encoding="utf-8") as f:
        f.write("malformed json\n")
    append_parent_boundary(transcript, agentId="worker-a", isSidechain=True)
    append_parent_boundary(transcript, sessionId="another-session")
    rc, out, err = run(pre, state)
    check("parent cursor: malformed and non-parent records cannot arm", rc == 0 and not os.path.exists(marker))

    record = {"type": "system", "subtype": "compact_boundary", "sessionId": SESSION,
              "isSidechain": False, "uuid": str(uuid.uuid4())}
    line = json.dumps(record)
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(line[:len(line)//2])
    rc, out, err = run(pre, state)
    check("parent cursor: partial boundary line cannot arm", rc == 0 and not os.path.exists(marker))
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(line[len(line)//2:] + "\n")
    rc, out, err = run(pre, state)
    check("parent pre-tool: newly completed deferred boundary arms and blocks",
          rc == 2 and os.path.exists(marker))

    from concurrent.futures import ThreadPoolExecutor
    append_parent_boundary(transcript)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run(compact, state), range(4)))
    check("parent boundary: concurrent delivery arms exactly once",
          all(rc == 0 for rc, _, _ in results)
          and sum("COMPACTION GATE" in out for _, out, _ in results) == 1)
    m = marker_now(marker)
    m["docs"][next(iter(m["docs"]))] = 123
    with open(marker, "w", encoding="utf-8") as f:
        json.dump(m, f)
    before = PathBytes(marker)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run(compact, state), range(4)))
    check("parent boundary: concurrent repeated delivery preserves progress",
          all(rc == 0 and not out for rc, out, _ in results) and PathBytes(marker) == before)
    missing = dict(compact, transcript_path=os.path.join(state, "missing.jsonl"), session_id=SESSION + "-missing")
    rc, out, err = run(missing, state)
    check("parent boundary: missing transcript fails open", rc == 0 and not os.path.exists(os.path.join(state, SESSION + "-missing.json")))

def PathBytes(path: str):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None

def main() -> int:
    worker_compaction_regressions()
    parent_boundary_regressions()
    state = tempfile.mkdtemp(prefix="compaction-gate-selftest-")
    marker = os.path.join(state, SESSION + ".json")

    rc, out, err = subprocess.run([sys.executable, "-B", GATE], input=b"not json", capture_output=True,
                                  env=dict(os.environ, COMPACTION_GATE_STATE_DIR=state)).returncode, "", ""
    check("garbage stdin fails OPEN (exit 0)", rc == 0, f"rc={rc}")

    state0 = tempfile.mkdtemp(prefix="compaction-gate-selftest-default-")
    marker0 = os.path.join(state0, SESSION + ".json")
    nocheck = ("HOOK_GATE_CHECK_ROSTERS",)
    rc, out, err = run(base("SessionStart", source="compact"), state0, drop=nocheck)
    check("default: a compaction arms the gate", rc == 0 and os.path.exists(marker0), f"rc={rc} err={err[:160]}")
    check("default: the arm message asks for the read, then the check and the fix — no table words",
          "read whole" in out and "fix whatever has drifted" in out and "applied" not in out and "drift check" not in out, out[:300])
    for path in TEN:
        rc, out, err = run(base("PostToolUse", tool_name="mcp__project__read", tool_input={"filePath": path}, tool_response={"content": [{"type": "text", "text": whole_read_response(path)}]}), state0, drop=nocheck)
    check("default: ten whole reads RELEASE the marker (no await-check phase)", not os.path.exists(marker0), f"marker={os.path.exists(marker0)} phase={phase_of(marker0)}")
    check("default: the clear says check and fix, and asks for no table",
          "COMPACTION GATE CLEARED" in out and "fix whatever has drifted" in out and "applied" not in out and "docs/session" not in out, out[:300])
    rc, out, err = run(base("PreToolUse", tool_name="Agent", tool_input={"prompt": "x"}), state0, drop=nocheck)
    check("default: after the release an Agent dispatch is allowed", rc == 0, f"rc={rc} err={err[:160]}")
    
    os.environ["HOOK_GATE_CHECK_ROSTERS"] = "project"

    rc, out, err = run(base("SessionStart", source="startup"), state)
    check("SessionStart startup: exit 0, no marker", rc == 0 and not os.path.exists(marker), f"rc={rc} marker={os.path.exists(marker)}")

    rc, out, err = run(base("SessionStart", source="compact"), state)
    check("SessionStart compact: exit 0", rc == 0, f"rc={rc} err={err[:200]}")
    check("SessionStart compact: marker written", os.path.exists(marker))
    check("SessionStart compact: stdout names the gate and the ten", "COMPACTION GATE" in out and "10" in out, out[:200])

    rc, out, err = run(base("PreToolUse", tool_name="Bash", tool_input={"command": "git status"}), state)
    check("armed: Bash refused (exit 2)", rc == 2, f"rc={rc}")
    check("armed: refusal names the marker path", marker.replace("\\", "/").lower() in err.replace("\\", "/").lower() or SESSION in err, err[:300])
    check("armed: refusal names an unread doc", "REVIEW-CHECKLIST.md" in err, err[:300])

    for tool, ti in (("Agent", {"prompt": "x"}), ("Write", {"file_path": r"C:\x\a.md", "content": "y"}), ("mcp__project__shell", {"command": "ls"}), ("mcp__project__read_lines", {"filePath": r"C:\x\design.md", "startLine": 1})):
        rc, out, err = run(base("PreToolUse", tool_name=tool, tool_input=ti), state)
        check(f"armed: {tool} refused", rc == 2, f"rc={rc}")

    rc, out, err = run(base("PreToolUse", tool_name="ToolSearch", tool_input={"query": "select:mcp__project__read"}), state)
    check("armed: ToolSearch allowed", rc == 0, f"rc={rc} err={err[:120]}")
    rc, out, err = run(base("PreToolUse", tool_name="mcp__project__read", tool_input={"filePath": TEN[2].replace("\\", "/")}), state)
    check("armed: MCP read of a canon file allowed (forward slashes)", rc == 0, f"rc={rc} err={err[:120]}")
    rc, out, err = run(base("PreToolUse", tool_name="Read", tool_input={"file_path": TEN[0]}), state)
    check("armed: harness Read of a canon file allowed", rc == 0, f"rc={rc} err={err[:120]}")

    rc, out, err = run(base("PreToolUse", tool_name="Bash", tool_input={"command": "git status"}, agent_id="a123", agent_type="worker"), state)
    check("armed: a subagent's Bash allowed", rc == 0, f"rc={rc}")

    rc, out, err = run(base("PostToolUse", tool_name="mcp__project__read_lines", tool_input={"filePath": TEN[0], "startLine": 1, "maxLines": 5}, tool_response={"content": [{"type": "text", "text": "{}"}]}), state)
    m = json.load(open(marker, encoding="utf-8"))
    check("partial read_lines ticks nothing", all(v is None for v in m["docs"].values()), json.dumps(m["docs"])[:200])
    rc, out, err = run(base("PostToolUse", tool_name="mcp__project__read", tool_input={"filePath": TEN[0]}, tool_response={"content": [{"type": "text", "text": whole_read_response(TEN[0])}]}, agent_id="a123"), state)
    m = json.load(open(marker, encoding="utf-8"))
    check("a subagent's whole read ticks nothing", all(v is None for v in m["docs"].values()))
    rc, out, err = run(base("PostToolUse", tool_name="mcp__project__read", tool_input={"filePath": TEN[0]}, tool_response={"content": [{"type": "text", "text": whole_read_response(TEN[0])}]}), state)
    m = json.load(open(marker, encoding="utf-8"))
    ticked = [k for k, v in m["docs"].items() if v is not None]
    check("a whole MCP read ticks exactly its doc", len(ticked) == 1 and ticked[0].lower().endswith("workflow.md"), json.dumps(ticked))
    
    rc, out, err = run(base("PreToolUse", tool_name="Bash", tool_input={"command": "git status"}), state)
    check("after one read: Bash still refused, 9 unread named", rc == 2 and "9" in err, f"rc={rc} err={err[:200]}")

    for i, path in enumerate(TEN[1:], start=1):
        if i % 3 == 0:
            tool, ti = "Read", {"file_path": path}
            resp = "x" * 5000
        elif i % 3 == 1:
            total = sum(1 for _ in open(path, "rb"))
            tool, ti = "mcp__project__read_lines", {"filePath": path, "startLine": 1, "endLine": total}
            resp = {"content": [{"type": "text", "text": json.dumps({"lineCount": total, "lines": ["x"] * 3})}]}
        else:
            tool, ti = "mcp__project__read", {"filePath": path.replace("\\", "/")}
            resp = {"content": [{"type": "text", "text": whole_read_response(path)}]}
        rc, out, err = run(base("PostToolUse", tool_name=tool, tool_input=ti, tool_response=resp), state)
    
    check("ten whole reads ⇒ the read phase clears INTO await-check (marker kept, phase await-check)", phase_of(marker) == "await-check", f"marker={'present' if os.path.exists(marker) else 'absent'} phase={phase_of(marker)}")
    check("the clear is announced (additionalContext)", "COMPACTION GATE CLEARED" in out, out[:300])
    clear_out = out
    rc, out, err = run(base("PreToolUse", tool_name="Bash", tool_input={"command": "git status"}), state)
    check("after the ten: Bash allowed", rc == 0, f"rc={rc} err={err[:120]}")

    ctx = clear_context(clear_out)
    b = boundary_in(ctx)
    check("8b. the clear carries the exact line COMPACTION-BOUNDARY: <ISO-8601 UTC>", b is not None, ctx[:300])
    check("8b. the clear names the block shape (docs/session/, applied/drifted, backticked pointer, WORK SINCE THE READ:)",
          all(s in ctx for s in ("docs/session/", "applied", "drifted", "WORK SINCE THE READ:", "`")), ctx[:300])
    mk = marker_now(marker)
    docs10 = list(mk["docs"]) if mk else list(TEN)
    check("8b. the marker carries the boundary the clear printed", bool(mk) and b is not None and mk.get("boundary") == b, f"marker={'present' if mk else 'absent'} boundary={mk.get('boundary') if mk else None} printed={b}")

    def p2() -> bool:
        return phase_of(marker) == "await-check"

    pre = p2()
    rc, out, err = run(base("PreToolUse", tool_name="Agent", tool_input={"prompt": "x", "subagent_type": "worker"}), state)
    check("(a) await-check: Agent REFUSED (exit 2)", pre and rc == 2, f"phase={pre} rc={rc}")
    check("(a) await-check: the refusal names the phase, the boundary line, the block shape and the release",
          pre and "await-check" in err and b is not None and f"COMPACTION-BOUNDARY: {b}" in err and "WORK SINCE THE READ:" in err and "docs/session/" in err
          and (marker.replace("\\", "/").lower() in err.replace("\\", "/").lower()), err[:400])
    
    CONN2 = "mcp__00000000-0000-4000-8000-000000000001__"
    refused = [("Workflow", {"script": "x"}), ("SendMessage", {"to": "a1", "message": "x"}),
               ("Write", {"file_path": r"C:\x\a.md", "content": "y"}), ("Edit", {"file_path": r"C:\x\a.md", "old_string": "a", "new_string": "b"}),
               ("NotebookEdit", {"notebook_path": r"C:\x\n.ipynb", "new_source": "x"})]
    for srv in ("mcp__project__", "mcp__claude_ai_project__"):
        refused += [(srv + "write", {"filePath": r"C:\x\a.md", "content": "y"}), (srv + "create_file", {"filePath": r"C:\x\b.md", "content": "y"}),
                    (srv + "edit", {"filePath": r"C:\x\a.md", "expectedText": "a", "replacementText": "b"}),
                    (srv + "append_to_file", {"filePath": r"C:\x\a.md", "content": "y"}), (srv + "browser_type", {"sessionId": "s", "text": "y"}),
                    (srv + "browser_click", {"sessionId": "s", "text": "OK"}), (srv + "delete_file", {"filePath": r"C:\x\a.md"}),
                    (srv + "move", {"sourcePath": r"C:\x\a.md", "destinationPath": r"C:\x\b.md"})]
    refused += [(CONN2 + "append_to_file", {"filePath": r"C:\x\a.md", "content": "y"}), (CONN2 + "write", {"filePath": r"C:\x\a.md", "content": "y"})]
    for tool, ti in refused:
        pre = p2()
        rc, out, err = run(base("PreToolUse", tool_name=tool, tool_input=ti), state)
        check(f"await-check: {tool} REFUSED", pre and rc == 2, f"phase={pre} rc={rc}")
    
    pre = p2()
    rc, out, err = run(base("PreToolUse", tool_name="mcp__project__append_to_file", tool_input={"filePath": r"C:\x\notes.md", "content": block(b, docs10)}), state)
    check("(h) await-check: project append_to_file to a non-docs/session path REFUSED (even with a conforming block)", pre and rc == 2, f"phase={pre} rc={rc}")
    pre = p2()
    rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": r"C:\x\mydocs\session\board.md", "content": block(b, docs10)}), state)
    check("await-check: a write to …/mydocs/session/ (not a docs/ segment) REFUSED", pre and rc == 2, f"phase={pre} rc={rc}")
    for tool, ti in (("mcp__project__delete_file", {"filePath": LANE}), ("mcp__project__move", {"sourcePath": r"C:\x\a.md", "destinationPath": LANE})):
        pre = p2()
        rc, out, err = run(base("PreToolUse", tool_name=tool, tool_input=ti), state)
        check(f"await-check: {tool} INTO docs/session/ REFUSED (only a write/edit/append is the exception)", pre and rc == 2, f"phase={pre} rc={rc}")
    
    for tool, ti in (("Read", {"file_path": r"C:\x\design.md"}), ("mcp__project__read_lines", {"filePath": r"C:\x\design.md", "startLine": 1}),
                     ("mcp__project__read", {"filePath": LANE}), ("Grep", {"pattern": "compact_boundary"}), ("Glob", {"pattern": "*.md"}),
                     ("Bash", {"command": "git rev-parse origin/main"}), ("PowerShell", {"command": "Get-Date"}), ("ToolSearch", {"query": "select:Read"}),
                     ("mcp__project__shell", {"command": "git log -1"}), ("mcp__project__browser_connect", {"cdpUrl": "http://127.0.0.1:9223"}),
                     ("mcp__project__browser_evaluate", {"sessionId": "s", "expression": "1"}), ("mcp__project__browser_wait", {"sessionId": "s"})):
        pre = p2()
        rc, out, err = run(base("PreToolUse", tool_name=tool, tool_input=ti), state)
        check(f"(g) await-check: {tool} allowed", pre and rc == 0, f"phase={pre} rc={rc} err={err[:120]}")
    check("(g) await-check: allowed calls do not release the marker", p2(), f"phase={phase_of(marker)}")
    
    for tool, ti in (("Agent", {"prompt": "x"}), ("Write", {"file_path": r"C:\x\a.md", "content": "y"}), ("mcp__project__append_to_file", {"filePath": r"C:\x\a.md", "content": "y"})):
        pre = p2()
        rc, out, err = run(base("PreToolUse", tool_name=tool, tool_input=ti, agent_id="a123", agent_type="worker"), state)
        check(f"(f) await-check: a subagent's {tool} allowed", pre and rc == 0, f"phase={pre} rc={rc}")
    
    pre = p2()
    rc, out, err = run(base("PostToolUse", tool_name="mcp__project__read", tool_input={"filePath": TEN[0]}, tool_response={"content": [{"type": "text", "text": whole_read_response(TEN[0])}]}), state)
    check("(i) await-check: a whole roster read does not release or re-clear the marker", pre and p2() and "COMPACTION GATE CLEARED" not in out, f"phase={phase_of(marker)} out={out[:120]}")
    
    pre = p2()
    rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": LANE, "content": block(b, docs10, skip_boundary=True)}), state)
    check("(b) await-check: a docs/session write WITHOUT the boundary line REFUSED, naming (1) only",
          pre and rc == 2 and bool(missing_line(err, 1)) and not missing_line(err, 2) and not missing_line(err, 3), f"phase={pre} rc={rc} err={err[:300]}")
    pre = p2()
    rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": LANE, "content": block("2000-01-01T00:00:00Z", docs10)}), state)
    check("(b) await-check: a docs/session write with a WRONG boundary REFUSED, naming (1)", pre and rc == 2 and bool(missing_line(err, 1)), f"phase={pre} rc={rc}")
    
    for knob, label in (({"no_pointer": "CLEANUP"}, "an applied with no pointer"), ({"name_only": "CLEANUP"}, "a backticked document name alone"),
                        ({"drop": "CLEANUP"}, "no line at all")):
        pre = p2()
        rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": LANE, "content": block(b, docs10, **knob)}), state)
        seg = missing_line(err, 2)
        check(f"(c) await-check: CLEANUP with {label} REFUSED, (2) names CLEANUP and no other document",
              pre and rc == 2 and "CLEANUP" in seg and "WORKFLOW" not in seg and "RETRY-POLICY" not in seg
              and not missing_line(err, 1) and not missing_line(err, 3), f"phase={pre} rc={rc} seg={seg[:200]} err={err[:200]}")
    
    for knob, label in (({"work": False}, "missing"), ({"work_pointer": False}, "with no backticked pointer")):
        pre = p2()
        rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": LANE, "content": block(b, docs10, **knob)}), state)
        check(f"(d) await-check: WORK SINCE THE READ: {label} REFUSED, naming (3) only",
              pre and rc == 2 and bool(missing_line(err, 3)) and not missing_line(err, 1) and not missing_line(err, 2), f"phase={pre} rc={rc} err={err[:300]}")

    pre = p2()
    rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": LANE, "content": ctx + "\nWORK SINCE THE READ: `git log -1`\n"}), state)
    seg = missing_line(err, 2)
    check("await-check: pasting the gate's own clear text (plus a WORK line) is REFUSED — its lines satisfy no document",
          pre and rc == 2 and all(stem(d) in seg for d in docs10), f"phase={pre} rc={rc} seg={seg[:200]}")
    check("await-check: refused check writes do not release the marker", p2(), f"phase={phase_of(marker)}")
    
    pre = p2()
    rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": LANE, "content": block(b, docs10)}), state)
    check("(e) await-check: a conforming docs/session write is ALLOWED and releases the marker",
          pre and b is not None and rc == 0 and not os.path.exists(marker), f"phase-before={pre} boundary={b} rc={rc} marker={os.path.exists(marker)} err={err[:300]}")
    logtxt = open(os.path.join(state, "log.jsonl"), encoding="utf-8").read() if os.path.exists(os.path.join(state, "log.jsonl")) else ""
    check("(e) the release is logged check-written", '"event": "check-written"' in logtxt, logtxt[-300:])
    rc, out, err = run(base("PreToolUse", tool_name="Agent", tool_input={"prompt": "x"}), state)
    check("(e) after the check is written: Agent allowed", rc == 0, f"rc={rc} err={err[:120]}")
    if os.path.exists(marker):
        os.remove(marker)

    for cwd, armed in (
        (fixture_path('reference/unrelated-paper'), False),
        (fixture_path('reference/unrelated-area'), False),
        (fixture_path('source/SERVICE-APP'), True),
        (fixture_path('source/unrelated-app'), False),
        (fixture_path('source/desktop-app-feature/crates'), True),
        (fixture_path('source/cli-app'), True),
        (fixture_path('source/sample-app/docs'), True),
        (r"C:\work-feature-one", True),
        (r"C:\work-feature-two\src", True),
        (fixture_path('reference/project'), True),
        (fixture_path('reference/project/proposals'), True),
        (r"D:\somewhere\desktop-app", False),
    ):
        rc, out, err = run(base("SessionStart", source="compact", cwd=cwd), state)
        got = os.path.exists(marker)
        check(f"scope {cwd!r}: {'ARMS' if armed else 'never arms'}", rc == 0 and got == armed, f"rc={rc} marker={got}")
        if got:
            os.remove(marker)

    rc, out, err = run(base("SessionStart", source="compact", cwd=fixture_path('reference/operations/activity')), state)
    m = json.load(open(marker, encoding="utf-8"))
    check("operations: arms with the operations roster", m.get("roster") == "operations" and len(m["docs"]) == 4 and "roster: operations" in out, f"roster={m.get('roster')} docs={len(m['docs'])}")
    check("operations: the ledger is in the roster", any(k.lower().endswith("task-ledger.md") for k in m["docs"]))
    rc, out, err = run(base("PreToolUse", tool_name="mcp__project__read", tool_input={"filePath": TEN[2]}), state)
    check("operations: a read of REVIEW-CHECKLIST (not its canon) is refused", rc == 2, f"rc={rc}")
    rc, out, err = run(base("PreToolUse", tool_name="mcp__project__read_lines", tool_input={"filePath": fixture_path('reference/operations/activity/feed/ACTIVITY-LOG.md'), "startLine": 2700, "maxLines": 120}), state)
    check("operations: a partial read of the feed FEED is allowed", rc == 0, f"rc={rc} err={err[:120]}")
    rc, out, err = run(base("PreToolUse", tool_name="mcp__project__ls", tool_input={"path": fixture_path('reference/operations/activity/updates')}), state)
    check("operations: listing the updates directory is allowed", rc == 0, f"rc={rc}")
    rc, out, err = run(base("PreToolUse", tool_name="mcp__project__ls", tool_input={"path": fixture_path('reference/operations')}), state)
    check("operations: listing outside the list paths is refused", rc == 2, f"rc={rc}")
    for d in list(m["docs"]):
        rc, out, err = run(base("PostToolUse", tool_name="mcp__project__read", tool_input={"filePath": d}, tool_response={"content": [{"type": "text", "text": whole_read_response(d)}]}), state)

    check("operations: four whole reads RELEASE the marker with the operations next-duty (phase 2 is project-only)", not os.path.exists(marker) and "FEEDS" in out, f"phase={phase_of(marker)} out={out[:200]}")
    gctx = clear_context(out)
    check("operations: the clear carries the operations next duty and asks for no written table",
          "FEEDS" in gctx and "drift check" not in gctx and "applied" not in gctx, gctx[-300:])
    rc, out, err = run(base("PreToolUse", tool_name="Agent", tool_input={"prompt": "x"}), state)
    check("operations: after the release an Agent dispatch is allowed", rc == 0, f"rc={rc} err={err[:160]}")
    if os.path.exists(marker):
        os.remove(marker)

    rc, out, err = run(base("SessionStart", source="compact", cwd=fixture_path('reference/workshop/backlog/x')), state)
    m = json.load(open(marker, encoding="utf-8"))
    check("workshop: arms with five boot documents", m.get("roster") == "workshop" and len(m["docs"]) == 5, f"roster={m.get('roster')} docs={len(m['docs'])}")
    rc, out, err = run(base("PreToolUse", tool_name="mcp__project__ls", tool_input={"path": fixture_path('reference/workshop/backlog')}), state)
    check("workshop: listing backlog is allowed while armed", rc == 0, f"rc={rc}")
    rc, out, err = run(base("PreToolUse", tool_name="Glob", tool_input={"pattern": "*", "path": fixture_path('reference/workshop')}), state)
    check("workshop: a Glob outside backlog is refused", rc == 2, f"rc={rc}")
    rc, out, err = run(base("PreToolUse", tool_name="Read", tool_input={"file_path": fixture_path('reference/workshop/PROJECT-INDEX.md')}), state)
    check("workshop: a read of PROJECT INDEX is allowed", rc == 0, f"rc={rc}")
    os.remove(marker)

    CONN = "mcp__00000000-0000-4000-8000-000000000001__"
    rc, out, err = run(base("SessionStart", source="compact", cwd=fixture_path('reference/workshop')), state)
    m = json.load(open(marker, encoding="utf-8"))
    docs = list(m["docs"])
    card = next(k for k in docs if k.lower().endswith("task-card.md"))
    rules = next(k for k in docs if k.lower().endswith("workshop-rules.md"))

    def lines_of(p):
        data = open(p, "rb").read()
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)

    def ticked_now(p):
        if not os.path.exists(marker):
            return True
        return json.load(open(marker, encoding="utf-8"))["docs"].get(p) is not None

    rc, out, err = run(base("PreToolUse", tool_name=CONN + "read", tool_input={"filePath": card}), state)
    check("connector: a MCP read of a roster doc through the claude.ai connector is allowed", rc == 0, f"rc={rc} err={err[:160]}")
    rc, out, err = run(base("PreToolUse", tool_name=CONN + "read_lines", tool_input={"filePath": card, "startLine": 1}), state)
    check("connector: its read_lines of a roster doc is allowed", rc == 0, f"rc={rc}")
    rc, out, err = run(base("PreToolUse", tool_name=CONN + "ls", tool_input={"path": fixture_path('reference/workshop/backlog')}), state)
    check("connector: its listing of backlog is allowed", rc == 0, f"rc={rc}")
    rc, out, err = run(base("PreToolUse", tool_name=CONN + "shell", tool_input={"command": "ls"}), state)
    check("connector: its shell is still refused", rc == 2, f"rc={rc}")
    rc, out, err = run(base("PreToolUse", tool_name=CONN + "read", tool_input={"filePath": fixture_path('reference/workshop/extras/notes.md')}), state)
    check("connector: its read of a NON-roster file is still refused", rc == 2, f"rc={rc}")

    total = lines_of(card)
    half = total // 2
    page1 = {"type": "text", "file": {"filePath": card, "content": "x" * 3000, "numLines": half, "startLine": 1, "totalLines": total, "truncatedByTokenCap": True}}
    rc, out, err = run(base("PostToolUse", tool_name="Read", tool_input={"file_path": card}, tool_response=page1), state)
    check("a harness Read cut off by the token cap ticks NOTHING", not ticked_now(card), "ticked on a partial read")
    page2 = {"type": "text", "file": {"filePath": card, "content": "x" * 3000, "numLines": total - half, "startLine": half + 1, "totalLines": total}}
    rc, out, err = run(base("PostToolUse", tool_name="Read", tool_input={"file_path": card, "offset": half + 1, "limit": total - half}, tool_response=page2), state)
    check("the second page completes the coverage and ticks it", ticked_now(card), "not ticked after both pages")

    rtotal = lines_of(rules)
    short = {"content": [{"type": "text", "text": json.dumps({"ok": True, "startLine": 1, "endLine": 20, "lineCount": 20, "lines": ["x"] * 20})}]}
    rc, out, err = run(base("PostToolUse", tool_name="mcp__project__read_lines", tool_input={"filePath": rules, "startLine": 1, "maxLines": 2000}, tool_response=short), state)
    check("a read_lines answer that stops short of the last line ticks NOTHING", not ticked_now(rules), "ticked on a short answer")
    full = {"content": [{"type": "text", "text": json.dumps({"ok": True, "startLine": 1, "endLine": rtotal, "lineCount": rtotal, "lines": ["x"] * 3})}]}
    rc, out, err = run(base("PostToolUse", tool_name=CONN + "read_lines", tool_input={"filePath": rules, "startLine": 1, "maxLines": 2000}, tool_response=full), state)
    check("a whole read_lines through the connector ticks its doc", ticked_now(rules), "not ticked")
    for d in docs:
        if d in (card, rules):
            continue
        rc, out, err = run(base("PostToolUse", tool_name=CONN + "read", tool_input={"filePath": d}, tool_response={"content": [{"type": "text", "text": whole_read_response(d)}]}), state)
    check("connector: whole reads RELEASE the workshop gate with its next duty (phase 2 is project-only)", not os.path.exists(marker) and "backlog" in out, f"phase={phase_of(marker)} out={out[:200]}")
    actx = clear_context(out)
    check("workshop: the clear carries the workshop's next duty and asks for no written table",
          "backlog" in actx and "drift check" not in actx and "applied" not in actx, actx[-300:])
    if os.path.exists(marker):
        os.remove(marker)

    state11b = tempfile.mkdtemp(prefix="compaction-gate-selftest-lines-")
    marker11b = os.path.join(state11b, SESSION + ".json")
    rc, out, err = run(base("SessionStart", source="compact"), state11b, drop=nocheck)
    check("lines-shape: a compaction arms the project gate", rc == 0 and os.path.exists(marker11b), f"rc={rc} err={err[:160]}")

    def lines_resp(path, wrap=False, **over):

        n = lines_of(path)
        o = {"ok": True, "lines": ["x"] * min(n, 5), "path": path, "startLine": 1, "endLine": n, "lineCount": n, "lineEndings": "lf",
             "truncated": False, "omittedDueToOutputLimit": False, "totalLines": n, "totalBytes": os.path.getsize(path)}
        o.update(over)
        return {"content": [{"type": "text", "text": json.dumps(o)}]} if wrap else json.dumps(o)

    def ticked11b(p):
        mk = marker_now(marker11b)
        return mk is None or mk["docs"].get(p) is not None

    rc, out, err = run(base("PostToolUse", tool_name=CONN + "read", tool_input={"filePath": TEN[3]}, tool_response=lines_resp(TEN[3], truncated=True, endLine=20, lineCount=20)), state11b, drop=nocheck)
    check("lines-shape: a read the server marks truncated ticks NOTHING", not ticked11b(TEN[3]), "ticked on a truncated lines answer")
    rc, out, err = run(base("PostToolUse", tool_name=CONN + "read", tool_input={"filePath": TEN[5]}, tool_response=lines_resp(TEN[5], wrap=True, truncated=True, endLine=20, lineCount=20)), state11b, drop=nocheck)
    check("lines-shape: a truncated read in the wrapped form ticks NOTHING either (the wrapper's own `content` key is not the file)", not ticked11b(TEN[5]), "ticked on a wrapped truncated answer")
    rc, out, err = run(base("PostToolUse", tool_name=CONN + "read", tool_input={"filePath": TEN[6]}, tool_response=lines_resp(TEN[6], omittedDueToOutputLimit=True)), state11b, drop=nocheck)
    check("lines-shape: a read with lines omitted for the output limit ticks NOTHING", not ticked11b(TEN[6]), "ticked on an omitted lines answer")
    rc, out, err = run(base("PostToolUse", tool_name=CONN + "read", tool_input={"filePath": TEN[7]}, tool_response=lines_resp(TEN[7])), state11b, drop=nocheck)
    check("lines-shape: a whole `lines` read through the connector (the string form) TICKS its doc", ticked11b(TEN[7]), "not ticked: the lines shape is not read as whole")
    rc, out, err = run(base("PostToolUse", tool_name="mcp__project__read", tool_input={"filePath": TEN[4]}, tool_response=lines_resp(TEN[4], wrap=True)), state11b, drop=nocheck)
    check("lines-shape: a whole `lines` read through the native tool (the wrapped form) ticks its doc", ticked11b(TEN[4]), "not ticked")
    try:
        with open(os.path.join(state11b, "log.jsonl"), encoding="utf-8") as f:
            shapes = [json.loads(l) for l in f if '"read-shape"' in l]
    except Exception:
        shapes = []
    check("lines-shape: the read-shape log line carries the head of the response text, so the next odd shape is read, not guessed",
          bool(shapes) and all(isinstance(s.get("head"), str) and "lines" in s["head"] for s in shapes), json.dumps(shapes[-1:])[:300])

    def to_phase2():
        run(base("SessionStart", source="compact"), state)
        mk = marker_now(marker)
        ds = list(mk["docs"]) if mk else list(TEN)
        o = ""
        for d in ds:
            _, o, _ = run(base("PostToolUse", tool_name="mcp__project__read", tool_input={"filePath": d}, tool_response={"content": [{"type": "text", "text": whole_read_response(d)}]}), state)
        return ds, boundary_in(clear_context(o))

    ds, bb = to_phase2()
    pre = phase_of(marker) == "await-check"
    rc, out, err = run(base("PreToolUse", tool_name="mcp__project__edit", tool_input={"filePath": r"C:\work-feature-three\docs\session\lane.md", "expectedText": "TBD", "replacementText": block(bb, ds, work_style="1. **WORK SINCE THE READ**: ")}), state)
    check("12a. await-check: a project edit (replacementText, backslash docs\\session\\ path, numbered bold WORK line) ALLOWED and releases", pre and bb is not None and rc == 0 and not os.path.exists(marker), f"phase={pre} rc={rc} err={err[:300]}")
    ds, bb = to_phase2()
    pre = phase_of(marker) == "await-check"
    rc, out, err = run(base("PreToolUse", tool_name="Edit", tool_input={"file_path": fixture_path('source/sample-app/docs/session/task-note.md'), "old_string": "x", "new_string": block(bb, ds, work_style="work since the read: ")}), state)
    check("12b. await-check: a harness Edit (new_string, lowercase WORK line) ALLOWED and releases", pre and bb is not None and rc == 0 and not os.path.exists(marker), f"phase={pre} rc={rc} err={err[:300]}")
    ds, bb = to_phase2()
    pre = phase_of(marker) == "await-check"
    rc, out, err = run(base("PreToolUse", tool_name="mcp__claude_ai_project__append_to_file", tool_input={"filePath": LANE, "content": "\r\n" + block(bb, ds).replace("\n", "\r\n")}), state)
    check("12c. await-check: a claude.ai project append (content, CRLF) ALLOWED and releases", pre and bb is not None and rc == 0 and not os.path.exists(marker), f"phase={pre} rc={rc} err={err[:300]}")

    ds, bb = to_phase2()
    check("12f. a project compaction still enters await-check", phase_of(marker) == "await-check", f"phase={phase_of(marker)}")
    stems10 = [stem(d) for d in ds]
    one_line = f"COMPACTION-BOUNDARY: {bb}\n- " + " · ".join(stems10) + " — applied — `WORKFLOW.md:12`\n- WORK SINCE THE READ: `git log -1`\n"
    pre = phase_of(marker) == "await-check"
    rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": LANE, "content": one_line}), state)
    seg = missing_line(err, 2)
    named = [s for s in stems10 if s in seg]
    check("12f. ONE line naming all ten documents with one pointer is REFUSED, (2) naming nine documents",
          pre and rc == 2 and len(named) == 9 and not missing_line(err, 1) and not missing_line(err, 3) and os.path.exists(marker), f"phase={pre} rc={rc} named={len(named)} seg={seg[:200]}")
    two = block(bb, ds, drop="TOOL-GUIDE").replace("- WORKFLOW — applied", "- WORKFLOW and TOOL-GUIDE — applied", 1)
    pre = phase_of(marker) == "await-check"
    rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": LANE, "content": two}), state)
    seg = missing_line(err, 2)
    check("12f. a line naming TWO documents counts for its LEFTMOST document only: refused, (2) naming TOOL-GUIDE only",
          pre and rc == 2 and "TOOL-GUIDE" in seg and [s for s in stems10 if s in seg] == ["TOOL-GUIDE"], f"phase={pre} rc={rc} seg={seg[:200]}")
    pre = phase_of(marker) == "await-check"
    rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": LANE, "content": block(bb, ds)}), state)
    check("12f. the conforming ten-line block still releases", pre and rc == 0 and not os.path.exists(marker), f"phase={pre} rc={rc} err={err[:200]}")
    if os.path.exists(marker):
        os.remove(marker)

    ds, bb = to_phase2()
    stems10 = [stem(d) for d in ds]
    edge = [f"COMPACTION-BOUNDARY: {bb}",
            "- TOOL-GUIDE — applied — `TOOL-GUIDE.md:5` (as WORKFLOW says)",
            "- WORKFLOW — applied — `WORKFLOW.md:9`"]
    edge += [f"- {s} — applied — `{s}.md:3`" for s in stems10 if s not in ("WORKFLOW", "TOOL-GUIDE")]
    edge += ["WORK SINCE THE READ: `git log -1`"]
    pre = phase_of(marker) == "await-check"
    rc, out, err = run(base("PreToolUse", tool_name="Write", tool_input={"file_path": LANE, "content": "\n".join(edge)}), state)
    check("12g. a cross-reference above the referenced document's own line never claims it: every document on its own line ⇒ ALLOWED and releases",
          pre and rc == 0 and not os.path.exists(marker), f"phase={pre} rc={rc} err={[l for l in err.splitlines() if l.startswith('MISSING')]}")
    if os.path.exists(marker):
        os.remove(marker)
    ds, bb = to_phase2()
    pre = phase_of(marker) == "await-check"
    run(base("SessionStart", source="compact"), state)
    mk = marker_now(marker)
    rc, out, err = run(base("PreToolUse", tool_name="Bash", tool_input={"command": "git status"}), state)
    check("12d. a NEW compaction while in await-check re-arms the read phase (docs unread, Bash refused)", pre and bool(mk) and mk.get("phase") != "await-check" and all(v is None for v in mk["docs"].values()) and rc == 2, f"phase-before={pre} rc={rc}")
    if os.path.exists(marker):
        os.remove(marker)
    
    with open(marker, "w", encoding="utf-8") as f:
        json.dump({"created": int(time.time()) - 4 * 60 * 60, "session_id": SESSION, "roster": "project", "phase": "await-check", "boundary": "2000-01-02T00:00:00Z", "docs": {p: 1 for p in TEN}}, f)
    rc, out, err = run(base("PreToolUse", tool_name="Agent", tool_input={"prompt": "x"}), state)
    check("12e. an await-check marker older than 3 h releases: Agent allowed, marker deleted", rc == 0 and not os.path.exists(marker), f"rc={rc} marker={os.path.exists(marker)}")
    if os.path.exists(marker):
        os.remove(marker)

    S13 = "selftest-session-0013"
    m13 = os.path.join(state, S13 + ".json")
    audit_root = os.path.join(state, "audit")
    adir = os.path.join(audit_root, S13)
    os.makedirs(adir, exist_ok=True)
    made = []

    def b13(event, sess=S13, **kw):
        d = base(event, **kw)
        d["session_id"] = sess
        return d

    def p13(path=m13):
        return phase_of(path) == "await-check"

    def phase2_13(sess=S13, extra=None, drop=()):
        run(b13("SessionStart", sess=sess, source="compact"), state, extra, drop)
        mk = marker_now(os.path.join(state, (sess or "no-session") + ".json"))
        ds_ = list(mk["docs"]) if mk else list(TEN)
        ctx_ = ""
        for d in ds_:
            _, o, _ = run(b13("PostToolUse", sess=sess, tool_name="mcp__project__read", tool_input={"filePath": d},
                              tool_response={"content": [{"type": "text", "text": whole_read_response(d)}]}), state, extra, drop)
            ctx_ = clear_context(o) or ctx_
        return ds_, boundary_in(ctx_), ctx_

    def put(name, obj, d=None):
        path = os.path.join(d or adir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))
        made.append(path)
        return path

    def launch(age_s, d=None, sess=S13):
        t = time.time() - age_s
        ts_ = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))
        put(f"launch-{ts_}.json", {"session_id": sess, "launched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)), "window_from_line": 8, "cwd": CWD}, d)
        return ts_

    def clean():  
        while made:
            p_ = made.pop()
            if os.path.isfile(p_):
                os.remove(p_)
        if os.path.exists(m13):
            os.remove(m13)

    def write13(content, sess=S13, extra=None, drop=()):
        return run(b13("PreToolUse", sess=sess, tool_name="Write", tool_input={"file_path": LANE, "content": content}), state, extra, drop)

    def no_auditor(text) -> bool:

        return "auditor" not in (text or "").replace(state, "<state>").lower()

    def grades10(ds_):
        return [{"doc": stem(d), "verdict": "drifted" if i % 3 == 2 else "applied", "rule": f"a rule sentence of {stem(d)}, quoted verbatim", "line": 100 + i,
                 "byte": f"`transcript:{100 + i}` the driver wrote the lane record after the read"} for i, d in enumerate(ds_)]

    def log_lines():
        p_ = os.path.join(state, "log.jsonl")
        return open(p_, encoding="utf-8").read().splitlines() if os.path.exists(p_) else []

    clean()
    launch(60)
    ds, bb, ctxA = phase2_13()
    check("13a. RETIRED: with the retired auditor's PENDING launch on disk, the clear into await-check carries no auditor sentence (no 'auditor' word)",
          p13() and bb is not None and no_auditor(ctxA), ctxA[:700])
    pre = p13()
    rc, out, err = run(b13("PreToolUse", tool_name="Agent", tool_input={"prompt": "x"}), state)
    check("13a. ... an Agent refused in await-check: the refusal names the block (WORK SINCE THE READ:) and carries no auditor sentence",
          pre and rc == 2 and "WORK SINCE THE READ:" in err and no_auditor(err), err[:700])
    pre = p13()
    rc, out, err = write13(block(bb, ds))
    check("13a. ... the plain conforming drift-check write is ALLOWED at once and releases (no wait for a verdict)",
          pre and rc == 0 and not os.path.exists(m13), f"phase={pre} rc={rc} err={err[:400]}")

    clean()
    ds, bb, _ = phase2_13()
    launch(runner_T() + 180)
    pre = p13()
    rc, out, err = write13(block(bb, ds, work=False))
    check("13b. RETIRED: with a TIMED-OUT launch on disk, a block missing only its WORK line is refused naming (3) only — no MISSING (4), no auditor sentence",
          pre and rc == 2 and bool(missing_line(err, 3)) and not missing_line(err, 1) and not missing_line(err, 2) and not missing_line(err, 4) and no_auditor(err) and p13(),
          f"phase={pre} rc={rc} err={err[:500]}")
    pre = p13()
    rc, out, err = write13(block(bb, ds))
    check("13b. ... the plain conforming block (no AUDITOR-TIMED-OUT line) is ALLOWED and releases",
          pre and rc == 0 and not os.path.exists(m13), f"phase={pre} rc={rc} err={err[:400]}")
    
    clean()
    ds, bb, _ = phase2_13()
    tsB2 = launch(16 * 60)
    put(f"done-{tsB2}", {"timed_out": True, "rc": None})
    pre = p13()
    rc, out, err = write13(block(bb, ds))
    check("13b2. RETIRED: a done marker saying timed_out with no verdict: the plain conforming block is ALLOWED and releases",
          pre and rc == 0 and not os.path.exists(m13), f"phase={pre} rc={rc} err={err[:400]}")

    clean()
    ds, bb, _ = phase2_13()
    tsD = launch(300)
    put(f"done-{tsD}", {"rc": 0})
    put(f"verdict-{tsD}.json", {"rows": [], "grades": grades10(ds), "totals": {"candidates": 0}})
    pre = p13()
    rc, out, err = write13(block(bb, ds, drop="CLEANUP"))
    seg = missing_line(err, 2)
    check("13d. RETIRED: with a GRADED verdict on disk, a block missing CLEANUP's line is refused, (2) naming CLEANUP only, no auditor sentence",
          pre and rc == 2 and [stem(d) for d in ds if stem(d) in seg] == ["CLEANUP"] and no_auditor(err) and p13(), f"phase={pre} rc={rc} seg={seg[:300]} err={err[:400]}")
    pre = p13()
    n_log = len(log_lines())
    rc, out, err = write13(block(bb, ds))
    check("13d. ... the plain self-graded conforming block (no AUDITOR: / CONCUR / REFUTE lines) is ALLOWED and releases",
          pre and rc == 0 and not os.path.exists(m13), f"phase={pre} rc={rc} err={err[:400]}")
    rel = [ln for ln in log_lines()[n_log:] if '"event": "check-written"' in ln and S13 in ln]
    check("13d. ... and the release is logged check-written with no auditor field",
          len(rel) == 1 and no_auditor(rel[0]), str(rel)[:400])

    clean()
    nowhere = os.path.join(state, "no-such-audit-dir")
    ex = {"COMPACTION_AUDIT_STATE_DIR": nowhere}
    ds, bb, _ = phase2_13(extra=ex)
    pre = p13()
    rc, out, err = write13(block(bb, ds), extra=ex)
    check("13g. NO audit state dir (COMPACTION_AUDIT_STATE_DIR names a path that does not exist): the plain conforming block is ALLOWED and releases, and the path is not created (a guard)",
          pre and rc == 0 and not os.path.exists(m13) and not os.path.exists(nowhere), f"phase={pre} rc={rc} err={err[:400]}")
    clean()
    empty_tmp = os.path.join(state, "empty-temp")
    os.makedirs(empty_tmp, exist_ok=True)
    ex = {"TEMP": empty_tmp, "TMP": empty_tmp}
    ds, bb, _ = phase2_13(extra=ex, drop=("COMPACTION_AUDIT_STATE_DIR",))
    pre = p13()
    rc, out, err = write13(block(bb, ds), extra=ex, drop=("COMPACTION_AUDIT_STATE_DIR",))
    check("13g. ... nor with COMPACTION_AUDIT_STATE_DIR unset and %TEMP% holding no claude-compaction-audit folder (a guard)",
          pre and rc == 0 and not os.path.exists(m13) and not os.path.exists(os.path.join(empty_tmp, "claude-compaction-audit")), f"phase={pre} rc={rc} err={err[:400]}")
    clean()

    try:
        with open(GATE, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant) and isinstance(tree.body[0].value.value, str):
            tree.body = tree.body[1:]
        code = ast.unparse(tree)
    except Exception as e:
        code = f"<unparseable: {e!r}> COMPACTION_AUDIT_STATE_DIR"
    left = [s for s in ("COMPACTION_AUDIT_STATE_DIR", "claude-compaction-audit", "legacy-audit-runner.py", "AUDITOR-TIMED-OUT", "THE AUDITOR FIRST", "AUDITOR:", "CONCUR", "REFUTE") if s in code]
    check("13h. RETIRED: the gate's code carries no poll of the audit state and no auditor requirement (none of COMPACTION_AUDIT_STATE_DIR, claude-compaction-audit, legacy-audit-runner.py, AUDITOR-TIMED-OUT, THE AUDITOR FIRST, AUDITOR:, CONCUR, REFUTE)",
          not left, str(left))

    with open(marker, "w", encoding="utf-8") as f:
        f.write("{broken")
    rc, out, err = run(base("PreToolUse", tool_name="Bash", tool_input={"command": "git status"}), state)
    check("corrupt marker: Bash allowed (fail-open)", rc == 0, f"rc={rc}")
    check("corrupt marker: released (deleted by the gate)", not os.path.exists(marker))
    if os.path.exists(marker):
        os.remove(marker)

    print(f"\n{len(failures)} failed")
    return 1 if failures else 0

def _run() -> int:
    root = tempfile.mkdtemp(prefix="compaction-gate-selftest-root-")
    tempfile.tempdir = root
    try:
        return main()
    finally:
        tempfile.tempdir = None
        print("Scratch retained: " + root + " and " + FIXTURE_ROOT)

if __name__ == "__main__":
    sys.exit(_run())
