#!/usr/bin/env python3
"""Require configured reference documents to be read after a parent compaction.

SessionStart arms; PreToolUse gates; PostToolUse records read coverage.
Only configured workspaces are gated. Errors release the gate.
The optional written check validates structure, not the truth of the check."""

import json
import os
import re
import sys
import time
import tempfile
from contextlib import contextmanager

STATE_DIR = os.environ.get("COMPACTION_GATE_STATE_DIR") or os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir(), "claude-compaction-reread")
MAX_AGE_SECONDS = 3 * 60 * 60
ALWAYS_ALLOWED = {"ToolSearch", "ReadNotifications"}

MCP_READER = re.compile(r"^mcp__.+__(read|read_lines|ls)$")

def read_path(ti: dict, key: str) -> str:
    return str((ti or {}).get(key) or (ti or {}).get("path") or "")

def reader(tool: str):
    if tool == "Read":
        return ("Read", "file_path")
    if tool == "Glob":
        return ("ls", "path")
    mm = MCP_READER.match(tool or "")
    if mm:
        verb = mm.group(1)
        return (verb, "path" if verb == "ls" else "filePath")
    return None

def norm(p: str) -> str:
    return re.sub(r"/+", "/", str(p or "").replace("\\", "/")).lower().rstrip("/")

def rosters() -> dict:
    """Load a positive workspace list. Relative paths use the configuration folder."""
    path = os.path.expandvars(os.path.expanduser(os.environ.get(
        "COMPACTION_GATE_CONFIG", "~/.claude/compaction-rosters.json")))
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        config = json.load(f)
    result = {}
    for key, value in config["rosters"].items():
        r = dict(value)
        for field in ("docs", "feeds", "lists", "roots", "source_roots"):
            resolved = []
            for item in r.get(field, []):
                item = os.path.expandvars(os.path.expanduser(item))
                if not os.path.isabs(item):
                    item = os.path.join(os.path.dirname(os.path.abspath(path)), item)
                resolved.append(os.path.normpath(item))
            r[field] = resolved
        r.setdefault("label", "configured reference documents")
        r.setdefault("canon", "the configured workspace rules")
        r.setdefault("after", "check the work since the last read and fix whatever has drifted")
        result[key] = r
    return result

def roster_for(cwd: str):
    """First matching roster wins. Names also match a dash or underscore suffix."""
    c = norm(cwd)
    if not c:
        return None
    parts = c.split("/")
    for key, r in rosters().items():
        if under_any(c, r["roots"]):
            return key
        if len(parts) >= 2 and re.fullmatch(r"[a-z]:", parts[0]):
            if any(parts[1].startswith(norm(prefix)) for prefix in r.get("drive_prefixes", [])):
                return key
        candidates = [seg for i, seg in enumerate(parts)
                      if i and parts[i - 1] in [norm(x) for x in r.get("source_parents", [])]]
        for root in r["source_roots"]:
            n = norm(root)
            if c.startswith(n + "/"):
                candidates.append(c[len(n) + 1:].split("/")[0])
        for seg in candidates:
            for name in r.get("source_names", []):
                name = norm(name)
                if seg == name or seg.startswith(name + "-") or seg.startswith(name + "_"):
                    return key
    return None

def log(event: str, **kw) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": int(time.time()), "event": event, **kw}, ensure_ascii=False) + "\n")
    except Exception:
        pass

def marker_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "no-session")
    return os.path.join(STATE_DIR, safe + ".json")

def load_marker(session_id: str):
    p = marker_path(session_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            m = json.load(f)
        if not isinstance(m, dict) or not isinstance(m.get("docs"), dict):
            raise ValueError("marker shape")
    except Exception as e:
        log("marker-corrupt-released", session_id=session_id, error=str(e)[:120])
        try:
            os.remove(p)
        except Exception:
            pass
        return None
    if int(time.time()) - int(m.get("created", 0)) > MAX_AGE_SECONDS:
        log("marker-aged-released", session_id=session_id, age=int(time.time()) - int(m.get("created", 0)))
        try:
            os.remove(p)
        except Exception:
            pass
        return None
    return m

def save_marker(session_id: str, m: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(marker_path(session_id), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)

def doc_of(m: dict, path: str):
    n = norm(path)
    for d in m["docs"]:
        if norm(d) == n:
            return d
    return None

def under_any(path: str, roots) -> bool:
    n = norm(path)
    return any(n == norm(r) or n.startswith(norm(r) + "/") for r in roots)

def unread(m: dict):
    return [p for p, v in m["docs"].items() if v is None]

def response_text(resp) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    try:
        return json.dumps(resp, ensure_ascii=False)
    except Exception:
        return str(resp)

def read_object(resp):
    try:
        o = resp
        if isinstance(o, str):
            o = json.loads(o)
        if isinstance(o, dict) and isinstance(o.get("content"), list):
            parts = [c.get("text") for c in o["content"] if isinstance(c, dict) and isinstance(c.get("text"), str)]
            if parts:
                o = json.loads("".join(parts))
        return o if isinstance(o, dict) else None
    except Exception:
        return None

def lines_whole(o: dict) -> bool:
    total = o.get("totalLines")
    return (o.get("truncated") is False and o.get("omittedDueToOutputLimit") is False
            and isinstance(total, int) and total > 0 and o.get("startLine") == 1 and o.get("endLine") == total
            and (o.get("lineCount") in (None, total)))

def line_count(path: str) -> int:
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
    except Exception:
        return 0

def first_int(key: str, text: str):
    mm = re.search(r'\\*"' + key + r'\\*"\s*:\s*(\d+)', text)
    return int(mm.group(1)) if mm else None

def cover(m: dict, doc: str, start: int, end: int, total: int) -> bool:
    allc = m.setdefault("cover", {})
    c = allc.get(doc)
    if not isinstance(c, dict) or c.get("total") != total:
        c = {"total": total, "spans": []}
    if end >= start:
        c["spans"].append([int(start), int(end)])
    allc[doc] = c
    reach = 0
    for s, e in sorted(c["spans"]):
        if s > reach + 1:
            break
        reach = max(reach, e)
    return total > 0 and reach >= total

PHASE_CHECK = "await-check"

CHECK_ROSTERS: set = {s.strip() for s in os.environ.get("HOOK_GATE_CHECK_ROSTERS", "").split(",") if s.strip()}
CHECK_REFUSED ={"Agent", "Workflow", "SendMessage", "Write", "Edit", "NotebookEdit"}
CHECK_WRITERS = {"Write", "Edit"}

MCP_MUTATOR = re.compile(r"^mcp__.+__(write|create_file|edit|append_to_file|browser_type|browser_click|delete_file|move)$")
MCP_WRITER_VERBS = {"write", "create_file", "edit", "append_to_file"}
CONTENT_KEYS = ("content", "new_string", "replacementText", "text")
CHECK_RECORD_SUBDIR = norm(os.environ.get("CHECK_RECORD_SUBDIR", "docs/session")).strip("/")
CHECK_LOCATION = re.compile(r"(?:^|/)" + re.escape(CHECK_RECORD_SUBDIR) + "/")  
POINTER = re.compile(r"`([^`\r\n]+)`")
VERDICT = re.compile(r"\b(?:applied|drifted)\b", re.I)
WORK_LINE = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s+)*(?:\*\*|__)?\s*work since the read\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?(?P<rest>.*)$", re.I)

def iso_utc(t) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(t)))

def boundary_of(m: dict) -> str:
    return str(m.get("boundary") or iso_utc(m.get("created", 0)))

def stem(p) -> str:
    return os.path.splitext(os.path.basename(str(p).replace("\\", "/")))[0]

def doc_names(m: dict) -> set:
    out = set()
    for d in m["docs"]:
        out |= {norm(stem(d)), norm(os.path.basename(str(d).replace("\\", "/"))), norm(d)}
    return out

def pointers(line: str, names: set) -> list:
    return [s for s in POINTER.findall(line) if s.strip() and norm(s.strip()) not in names]

def check_block(m: dict, text) -> dict:
    text = text if isinstance(text, str) else ""
    b = boundary_of(m)
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    names = doc_names(m)

    owned = set()
    for ln in lines:
        if not (VERDICT.search(ln) and pointers(ln, names)):
            continue
        low = ln.lower()
        best = None
        for d in m["docs"]:
            s = stem(d).lower()
            i = low.find(s)
            if i >= 0 and (best is None or i < best[0] or (i == best[0] and len(s) > best[1])):
                best = (i, len(s), d)
        if best is not None:
            owned.add(best[2])
    missing_docs = [stem(d) for d in m["docs"] if d not in owned]
    work = 0
    for ln in lines:
        mm = WORK_LINE.match(ln)
        if mm:
            work = max(work, len(pointers(mm.group("rest"), set())))
    return {"has_boundary": f"COMPACTION-BOUNDARY: {b}" in text, "missing_docs": missing_docs, "docs": len(m["docs"]), "work_pointers": work}

def shape(m: dict, session_id: str) -> str:
    b = boundary_of(m)
    stems = [stem(d) for d in m["docs"]]
    message = (
        f"COMPACTION-BOUNDARY: {b}\n"
        "Write the per-document drift check into the lane record or the board — a Write, an Edit, or a MCP write / create_file / edit / append_to_file whose path is under docs/session/ — carrying ALL THREE:\n"
        "(1) the COMPACTION-BOUNDARY line above, verbatim (the gate's arm time, UTC; print the transcript's compact_boundary timestamp beside it);\n"
        f"(2) one line per document — {' · '.join(stems)} — each document on its own line; a line counts for the document named first on it; naming it, with the word applied or drifted and at least one backticked pointer to the byte (a file:line, a §heading, a sha, in backticks) on the same line; a backticked document name alone is not a pointer;\n"
        "(3) a line beginning WORK SINCE THE READ: followed by at least one backticked pointer on that line — the actions taken since the last full read, named by byte.\n"
        "Shape of a (2) line, <DOC> standing for the document's name: - <DOC> — applied — `<DOC>.md:42` §the rule it applied\n"
        "Until it is written: Agent, Workflow, SendMessage, Write, Edit, NotebookEdit and every MCP write, create_file, edit, append_to_file, delete_file, move, browser_type and browser_click are REFUSED (a write anywhere but docs/session/ included); reads, greps, shells, ToolSearch and browser connect / evaluate / wait stay open. "
        f"The conforming write releases the gate. Marker: {marker_path(session_id)} (the owner releases a stuck gate by deleting it; it expires {MAX_AGE_SECONDS // 3600} h after the arm).\n"
    )
    return message.replace("docs/session/", CHECK_RECORD_SUBDIR + "/")

def await_check(payload: dict, session_id: str, m: dict) -> None:
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    mm = MCP_MUTATOR.match(tool or "")
    if tool not in CHECK_REFUSED and not mm:
        return
    writer = tool in CHECK_WRITERS or bool(mm and mm.group(1) in MCP_WRITER_VERBS)
    path = str(ti.get("file_path") or ti.get("filePath") or "")
    if writer and CHECK_LOCATION.search(norm(path)):
        text = next((ti[k] for k in CONTENT_KEYS if isinstance(ti.get(k), str)), "")
        c = check_block(m, text)
        if c["has_boundary"] and not c["missing_docs"] and c["work_pointers"] > 0:
            try:
                os.remove(marker_path(session_id))
            except Exception:
                pass
            log("check-written", session_id=session_id, roster=m.get("roster"), tool=tool, path=path[:200], boundary=boundary_of(m),
                docs=c["docs"], doc_lines=c["docs"] - len(c["missing_docs"]), work_pointers=c["work_pointers"])
            return
        miss = []
        if not c["has_boundary"]:
            miss.append(f"MISSING (1): the line COMPACTION-BOUNDARY: {boundary_of(m)} — verbatim, the timestamp the gate printed at the clear")
        if c["missing_docs"]:
            miss.append("MISSING (2): no line of its own naming " + " · ".join(c["missing_docs"]) + " together with the word applied or drifted and a backticked pointer to the byte on that same line (each document on its own line; a line counts for the document named first on it; a backticked document name alone is not a pointer)")
        if c["work_pointers"] == 0:
            miss.append("MISSING (3): a line beginning WORK SINCE THE READ: with at least one backticked pointer on it — the actions taken since the last full read, named by byte")
        log("check-incomplete", session_id=session_id, tool=tool, path=path[:200], boundary=c["has_boundary"], missing_docs=c["missing_docs"], work_pointers=c["work_pointers"])
        sys.stderr.write(f"COMPACTION GATE (phase {PHASE_CHECK}): refused — this {tool} to {path} is the drift-check write, and it is incomplete:\n" + "\n".join(miss) + "\n" + shape(m, session_id))
        sys.exit(2)
    log("refuse-await-check", session_id=session_id, tool=tool, roster=m.get("roster"))
    sys.stderr.write(
        f"COMPACTION GATE (phase {PHASE_CHECK}): refused — all {len(m['docs'])} documents of the {m.get('roster')} roster were read whole in this window, but the per-document drift check is not written yet; until it is, {tool} is refused.\n"
        + shape(m, session_id))
    sys.exit(2)

def arm_parent(payload: dict, parent_boundary: str) -> None:
    key = roster_for(payload.get("cwd", ""))
    if key is None:
        log("compact-out-of-scope", cwd=payload.get("cwd", ""))
        return
    r = rosters()[key]
    present = [d for d in r["docs"] if os.path.isfile(d)]
    missing = [d for d in r["docs"] if d not in present]
    if missing:
        log("roster-docs-missing-dropped", roster=key, missing=missing)
    if not present:
        log("roster-empty-not-armed", roster=key)
        return
    session_id = payload.get("session_id", "")
    now = int(time.time())
    m = {"created": now, "boundary": iso_utc(now), "parent_boundary": parent_boundary, "session_id": session_id, "roster": key, "cwd": payload.get("cwd", ""), "transcript_path": payload.get("transcript_path", ""), "feeds": r["feeds"], "lists": r["lists"], "after": r["after"], "docs": {p: None for p in present}}
    save_marker(session_id, m)
    log("armed", session_id=session_id, roster=key, docs=len(present), cwd=payload.get("cwd", ""))
    sys.stdout.write(
        f"COMPACTION GATE (roster: {key}): until all {len(present)} of {r['label']} are read whole, every other tool call is refused "
        "(allowed: a whole read of one of them through an MCP read / read_lines or the harness Read with no offset/limit, a read of this roster's feed paths, a listing of its list paths, a tool-schema load). "
        f"Then {r['after']}. The documents: " + " · ".join(present) + f". Marker: {marker_path(session_id)}.\n"
    )

@contextmanager
def boundary_lock(path: str):
    with open(path, "a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 1
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

def sync_parent_compaction(payload: dict) -> None:
    transcript = payload.get("transcript_path") or ""
    session_id = payload.get("session_id") or ""
    if (payload.get("agent_id") or "/subagents/" in norm(transcript)
            or not os.path.isfile(transcript)
            or roster_for(payload.get("cwd", "")) is None):
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    receipt = marker_path(session_id)[:-5] + ".boundary.json"
    with boundary_lock(receipt + ".lock"):
        try:
            with open(receipt, encoding="utf-8") as f:
                prior = json.load(f)
        except (FileNotFoundError, ValueError):
            prior = {}
        offset = prior.get("offset", 0) if prior.get("transcript") == norm(transcript) else 0
        if not isinstance(offset, int) or offset < 0 or offset > os.path.getsize(transcript):
            offset = 0
        latest = prior.get("boundary_id")
        with open(transcript, "rb") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    break  
                offset = f.tell()
                if b"compact_boundary" not in line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if (not isinstance(entry, dict) or entry.get("type") != "system"
                        or entry.get("subtype") != "compact_boundary"
                        or entry.get("isSidechain") or entry.get("agentId")
                        or entry.get("sessionId", session_id) != session_id):
                    continue
                identity = entry.get("uuid") or entry.get("timestamp")
                if isinstance(identity, str) and identity:
                    latest = identity
        if latest and latest != prior.get("boundary_id"):
            arm_parent(payload, latest)
        current = {"transcript": norm(transcript), "offset": offset, "boundary_id": latest}
        if current != prior:
            fd, temporary = tempfile.mkstemp(prefix="boundary-", suffix=".json", dir=STATE_DIR)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(current, f)
                os.replace(temporary, receipt)
            finally:
                if os.path.exists(temporary):
                    os.remove(temporary)

def session_start(payload: dict) -> None:
    if payload.get("source") == "compact":
        sync_parent_compaction(payload)

def pre_tool_use(payload: dict) -> None:
    if payload.get("agent_id"):
        return  
    sync_parent_compaction(payload)
    session_id = payload.get("session_id", "")
    m = load_marker(session_id)
    if m is None:
        return
    if m.get("phase") == PHASE_CHECK:
        await_check(payload, session_id, m)
        return
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    if tool in ALWAYS_ALLOWED:
        log("allow", session_id=session_id, tool=tool)
        return
    r = reader(tool)
    if r and r[0] != "ls":
        path = read_path(ti, r[1])
        if doc_of(m, path):
            log("allow-roster-read", session_id=session_id, tool=tool, path=path[:200])
            return
        if under_any(path, m.get("feeds") or []):
            log("allow-feed-read", session_id=session_id, tool=tool, path=path[:200])
            return
    if r and r[0] == "ls" and under_any(read_path(ti, r[1]), m.get("lists") or []):
        log("allow-list", session_id=session_id, tool=tool)
        return
    left = unread(m)
    roster_key = m.get("roster")
    canon = rosters().get(roster_key, {}).get("canon", "the roster's own boot text")
    log("refuse", session_id=session_id, tool=tool, unread=len(left), roster=roster_key)
    sys.stderr.write(
        f"COMPACTION GATE: refused — the window opened on a compaction summary and {len(left)} of the {len(m['docs'])} canonical documents of the {roster_key} roster are still unread in it. "
        "Until all are read WHOLE (MCP read / read_lines from line 1 to the end, or the harness Read with no offset/limit), the only calls allowed are those reads, this roster's feed reads and list calls, and a tool-schema load — "
        f"not {tool}. Still unread: " + " · ".join(left) + ". "
        f"Canon: {canon}. Marker: {marker_path(session_id)} (the owner clears a stuck gate by deleting it).\n"
    )
    sys.exit(2)

def post_tool_use(payload: dict) -> None:
    if payload.get("agent_id"):
        return
    session_id = payload.get("session_id", "")
    m = load_marker(session_id)
    if m is None:
        return
    if m.get("phase") == PHASE_CHECK:
        return  
    tool = payload.get("tool_name", "")
    r = reader(tool)
    if r is None or r[0] == "ls":
        return
    kind = r[0]
    ti = payload.get("tool_input") or {}
    doc = doc_of(m, read_path(ti, r[1]))
    if doc is None:
        return
    resp = payload.get("tool_response")
    text = response_text(resp)
    f = resp.get("file") if isinstance(resp, dict) else None
    log("read-shape", session_id=session_id, tool=tool, resp=type(resp).__name__, keys=sorted(resp)[:12] if isinstance(resp, dict) else None,
        file_keys=sorted(k for k in f if k != "content")[:12] if isinstance(f, dict) else None,
        head=text[:200])  
    whole = False
    if kind == "Read":

        if isinstance(f, dict) and all(isinstance(f.get(k), int) for k in ("startLine", "numLines", "totalLines")):
            whole = cover(m, doc, f["startLine"], f["startLine"] + f["numLines"] - 1, f["totalLines"])
        else:  
            whole = ("offset" not in ti and "limit" not in ti and len(text) >= 500 and "not been modified" not in text and "<tool_use_error>" not in text
                     and "truncatedByTokenCap" not in text and "PARTIAL view" not in text)
    elif kind == "read":
        o = read_object(resp)
        if isinstance(o, dict) and isinstance(o.get("lines"), list):
            whole = lines_whole(o)  
        else:
            whole = ('"content"' in text) or ("sizeBytes" in text)
    else:  
        total = line_count(doc)
        start = first_int("startLine", text) or int(ti.get("startLine") or 1)
        end = first_int("endLine", text)
        if end is None and "lines" in text:
            end = int(ti.get("endLine") or total)
            if ti.get("maxLines"):
                end = min(end, start + int(ti["maxLines"]) - 1)
        whole = end is not None and cover(m, doc, start, end, total)
    if not whole:
        save_marker(session_id, m)  
        log("read-not-whole", session_id=session_id, tool=tool, path=doc)
        return
    m["docs"][doc] = int(time.time())
    left = unread(m)
    if left:
        save_marker(session_id, m)
        log("ticked", session_id=session_id, path=doc, unread=len(left))
        return
    if m.get("roster") not in CHECK_ROSTERS:
        
        try:
            os.remove(marker_path(session_id))
        except Exception:
            pass
        log("cleared", session_id=session_id, roster=m.get("roster"))
        after = m.get("after") or "the roster's own next duty"
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
            f"COMPACTION GATE CLEARED: all {len(m['docs'])} canonical artifacts were read whole. Now {after}."}}) + "\n")
        return

    m["phase"] = PHASE_CHECK
    m["boundary"] = boundary_of(m)
    m["reads_done"] = int(time.time())
    save_marker(session_id, m)
    log("cleared", session_id=session_id, roster=m.get("roster"), phase=PHASE_CHECK, boundary=m["boundary"])
    after = m.get("after") or "the roster's own next duty"
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
        f"COMPACTION GATE CLEARED: all {len(m['docs'])} documents of the {m.get('roster')} roster were read whole in this window. Now, before any other action: {after}.\n"
        f"The gate now holds in phase {PHASE_CHECK} until that drift check is written:\n" + shape(m, session_id)}}) + "\n")

def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return
        event = payload.get("hook_event_name", "")
        if event == "SessionStart":
            session_start(payload)
        elif event == "PreToolUse":
            pre_tool_use(payload)
        elif event == "PostToolUse":
            post_tool_use(payload)
    except SystemExit:
        raise
    except Exception as e:  
        log("hook-error", error=str(e)[:200])
        return

if __name__ == "__main__":
    main()
