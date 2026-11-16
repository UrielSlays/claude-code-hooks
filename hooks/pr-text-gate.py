#!/usr/bin/env python3
"""Check pull request text and witness reads of contribution rules.

PreToolUse checks visible titles and bodies. PostToolUse records read coverage.
Unseen text and unparseable input fail open."""

import os
import sys

def fast_path():
    if os.environ.get("HOOK_COMPACTION_AUDITOR") == "1" or os.environ.get("HOOK_PR_TEXT_SKIP") == "1":
        return None
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        return None
    low = raw.lower()
    return raw if ((b"gh" in raw and b"pr" in raw) or b"contributing.md" in low or b"pull_request_template" in low) else None

if __name__ == "__main__":
    _RAW = fast_path()
    if _RAW is None:
        sys.exit(0)

import hashlib  
import json  
import re  
import subprocess  
import tempfile  
import time  

RULE_FILES = ("CONTRIBUTING.md", os.path.join(".github", "pull_request_template.md"))
RULE_FILE_NAME = re.compile(r"(?:^|[\\/])(?:CONTRIBUTING\.md|pull_request_template\.md)$", re.I)
MCP_READER = re.compile(r"^mcp__.+__(read|read_lines)$")
MAX_RULE_BYTES = 8 * 1024 * 1024

STATE_DIR =os.environ.get("PR_TEXT_GATE_STATE_DIR") or os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir(), "claude-pr-text-gate")
MAX_SCAN = 65536

MODEL_SLUG = re.compile(
    r"\b(?:(?:gpt|claude|gemini|grok|llama|mistral|mixtral|qwen|deepseek|phi)(?:-(?:[a-z]+-)?)?[rv]?\d[a-z0-9]*(?:[.-][a-z0-9]+)*|o\d+(?:-[a-z0-9]+)*)\b",
    re.I | re.ASCII,
)
REVIEW_PROCESS = re.compile(
    r"\b(?:review-logs|review-notes|panel|(?:seat|round)[\s_-]*(?:#\s*)?\d+|APPROVE-WITH-CHANGES|REJECT|READY-TO-BUILD|codex\s+review)\b|@codex(?![\w-])|\b" + re.escape(os.environ.get("PR_TEXT_RECORDS_PREFIX", "review-records")) + "@",
    re.I | re.ASCII,
)
TITLE_REVIEW_MARKERS = [
    re.compile(p, re.ASCII)
    for p in (
        "\u2326",
        r"\bseat[- ]?[123]\b",
        r"\bround [0-9]",
        r"\b(gate|panel|bot|design|codex)[- ]r[0-9]",
        r"\bSELECTED\b",
        r"\bREJECTED\b",
        r"\bALTERNATE\b",
        r"\bEXECUTED\b",
        r"\bAPPROVE-WITH-CHANGES\b",
        r"\bREJECT\b",
        r"\bREADY-TO-BUILD\b",
        r"@codex\b",
    )
]
REFUSED = [MODEL_SLUG, REVIEW_PROCESS] + TITLE_REVIEW_MARKERS

GH_PR = re.compile(r"(?<![\w.-])gh(?:\.exe)?(?:\s+(?:-R|--repo)(?:\s+|=)[^\s;&|]+)?\s+pr\s+(create|edit)(?![\w-])")
CD = re.compile(r"""(?:^|[;&|(\n]\s*|&&\s*)(?:cd|pushd|Set-Location|Push-Location|sl)\s+(?:-(?:LiteralPath|Path)\s+)?("[^"]*"|'[^']*'|[^\s;&|]+)""", re.I)
HEREDOC = re.compile(r"""(['"])\$\(\s*cat\s*<<-?\s*(['"]?)(\w+)\2[^\n]*\n(.*?)\n[ \t]*\3[ \t]*\n\s*\)\1""", re.S)
HERE_STRING = re.compile(r"""@(['"])[ \t]*\r?\n(.*?)\r?\n\1@""", re.S)
FLAG_KIND = {"--title": "title", "-t": "title", "--body": "body", "-b": "body", "--body-file": "file", "-F": "file"}
VARIABLE_ONLY = re.compile(r"^\$[\w{}:.()\[\]'\"-]*$")

def log(event: str, **kw) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": int(time.time()), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **kw}, ensure_ascii=False) + "\n")
    except Exception:
        pass

def unq(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok

def read_value(s: str, i: int):
    m = HEREDOC.match(s, i)
    if m:
        return m.group(4), m.end()
    m = HERE_STRING.match(s, i)
    if m:
        return m.group(2), m.end()
    if s[i] == '"':
        j = i + 1
        out = []
        while j < len(s) and s[j] != '"':
            if s[j] in "\\`" and j + 1 < len(s):
                out.append(s[j + 1])
                j += 2
                continue
            out.append(s[j])
            j += 1
        text = "".join(out)
        return (None if VARIABLE_ONLY.match(text.strip()) else text), min(j + 1, len(s))
    if s[i] == "'":
        j = i + 1
        out = []
        while j < len(s):
            if s[j] == "'":
                if j + 1 < len(s) and s[j + 1] == "'":
                    out.append("'")
                    j += 2
                    continue
                break
            out.append(s[j])
            j += 1
        return "".join(out), min(j + 1, len(s))
    j = i
    while j < len(s) and s[j] not in " \t\r\n;&|":
        j += 1
    text = s[i:j]
    return (None if text.startswith("$") else text), j

def fields_of(cmd: str, start: int):
    found = []
    i = start
    n = len(cmd)
    while i < n:
        while i < n and cmd[i] in " \t":
            i += 1
        if i < n and cmd[i] in "\\`" and i + 1 < n and cmd[i + 1] in "\r\n":
            i += 2  
            while i < n and cmd[i] in "\r\n":
                i += 1
            continue
        if i >= n or cmd[i] in "\r\n;&|":
            break
        if cmd[i] in "\"'@":
            _, i = read_value(cmd, i)
            continue
        j = i
        while j < n and cmd[j] not in " \t\r\n;&|=\"'":
            j += 1
        word = cmd[i:j]
        kind = FLAG_KIND.get(word)
        if kind is None:
            if j < n and cmd[j] in "=\"'":
                if cmd[j] == "=":
                    j += 1
                if j < n and cmd[j] not in " \t\r\n;&|":
                    _, j = read_value(cmd, j)
            i = max(j, i + 1)
            continue
        if j < n and cmd[j] == "=":
            j += 1
        while j < n and cmd[j] in " \t":
            j += 1
        if j >= n or cmd[j] in "\r\n;&|":
            break
        value, j = read_value(cmd, j)
        found.append((kind, value))
        i = j
    return found

def base_dir(cmd: str, at: int, cwd: str) -> str:
    cds = list(CD.finditer(cmd[:at]))
    if cds:
        target = os.path.expanduser(unq(cds[-1].group(1)))
        if not os.path.isabs(target) and cwd:
            target = os.path.join(cwd, target)
        if os.path.isdir(target):
            return target
    return cwd

def refused_in(text: str):
    seen = set()
    hits = []
    for pattern in REFUSED:
        for m in pattern.finditer(text[:MAX_SCAN]):
            key = (m.start(), m.group(0))
            if key in seen:
                continue
            seen.add(key)
            hits.append(m.group(0))
    return hits

def refuse(msg: str) -> int:
    sys.stderr.buffer.write((msg + "\n").encode("utf-8"))
    sys.stderr.flush()
    return 2

def safe(name) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name or "")) or "none"

def reads_dir(p: dict) -> str:
    return os.path.join(STATE_DIR, "reads", safe(p.get("session_id") or "no-session"), safe(p.get("agent_id") or "driver"))

def bytes_id(path: str):
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_RULE_BYTES + 1)
    except Exception:
        return None
    if len(data) > MAX_RULE_BYTES:
        return None
    return hashlib.sha384(data.replace(b"\r\n", b"\n")).hexdigest()

def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)

def response_object(resp):
    try:
        o = resp
        if isinstance(o, str):
            o = json.loads(o)
        if isinstance(o, dict) and isinstance(o.get("content"), list):
            o = o["content"]
        if isinstance(o, list):
            parts = [c.get("text") for c in o if isinstance(c, dict) and isinstance(c.get("text"), str)]
            o = json.loads("".join(parts)) if parts else None
        return o if isinstance(o, dict) else None
    except Exception:
        return None

def page_of(tool, ti: dict, resp):
    if tool == "Read":
        f = resp.get("file") if isinstance(resp, dict) else None
        if not isinstance(f, dict) or not all(is_int(f.get(k)) for k in ("startLine", "numLines", "totalLines")):
            return None
        return f.get("filePath") or ti.get("file_path"), f["startLine"], f["startLine"] + f["numLines"] - 1, f["totalLines"], "harness"
    if isinstance(tool, str) and MCP_READER.match(tool):
        o = response_object(resp)
        if not isinstance(o, dict) or o.get("ok") is False or not isinstance(o.get("lines"), list):
            return None
        first, last = o.get("startLine"), o.get("endLine")
        if not (is_int(first) and is_int(last)):
            return None
        last = min(last, first + len(o["lines"]) - 1)  
        if o.get("partialLine") is True and is_int(o.get("partialLineNumber")):
            last = min(last, o["partialLineNumber"] - 1)  
        total = o["totalLines"] if (o.get("truncated") is False and is_int(o.get("totalLines"))) else None
        return o.get("path") or ti.get("filePath") or ti.get("path"), first, last, total, "project"
    return None

def merged(spans):
    out = []
    for s in sorted((int(x[0]), int(x[1])) for x in spans if isinstance(x, (list, tuple)) and len(x) == 2):
        if out and s[0] <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], s[1])
        else:
            out.append([s[0], s[1]])
    return out

def witness(p: dict) -> None:
    ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
    page = page_of(p.get("tool_name"), ti, p.get("tool_response"))
    if page is None:
        return
    path, first, last, total, reader = page
    if not isinstance(path, str) or not RULE_FILE_NAME.search(path):
        return
    full = os.path.abspath(os.path.expanduser(path))
    current = bytes_id(full)
    if current is None:
        return
    folder = reads_dir(p)
    os.makedirs(folder, exist_ok=True)
    key = hashlib.sha384((os.path.normcase(os.path.realpath(full)) + "|" + reader).encode("utf-8")).hexdigest()[:32]
    state_path = os.path.join(folder, key + ".json")
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    if not isinstance(st, dict) or st.get("bytes") != current or (total is not None and st.get("total") not in (None, total)):
        st = {"bytes": current, "total": None, "spans": [], "whole": False}  
    if total is not None:
        st["total"] = total
    spans = list(st.get("spans") or [])
    if last >= first:
        spans.append([first, last])
    st["spans"] = merged(spans)
    t = st.get("total")
    if is_int(t) and (t == 0 or (st["spans"] and st["spans"][0][0] <= 1 and st["spans"][0][1] >= t)):
        st["whole"] = True
    st.update({"path": full, "reader": reader, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    tmp = state_path + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, state_path)

def read_whole(p: dict):
    out = set()
    folder = reads_dir(p)
    try:
        names = os.listdir(folder)
    except Exception:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            continue
        if isinstance(st, dict) and st.get("whole") is True and isinstance(st.get("bytes"), str):
            out.add(st["bytes"])
    return out

def repo_root(where: str):
    try:
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        r = subprocess.run(["git", "-C", where, "rev-parse", "--show-toplevel"], capture_output=True, timeout=10, env=env, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return None
        top = r.stdout.decode("utf-8", "replace").strip()
        return os.path.normpath(top) if top else None
    except Exception:
        return None

def unread_rules(p: dict, dirs):
    seen = read_whole(p)
    missing = []
    for where in dirs:
        top = repo_root(where)
        if top is None:
            continue
        for rel in RULE_FILES:
            full = os.path.join(top, rel)
            if not os.path.isfile(full):
                continue
            current = bytes_id(full)
            if current is not None and current not in seen and full not in missing:
                missing.append(full)
    return missing

def gate(p: dict) -> int:
    ti = p.get("tool_input")
    cmd = ti.get("command") if isinstance(ti, dict) else None
    if not isinstance(cmd, str):
        return 0
    matches = list(GH_PR.finditer(cmd))
    if not matches:
        return 0
    cwd = (ti.get("cwd") if isinstance(ti.get("cwd"), str) else None) or p.get("cwd") or os.getcwd()
    problems = []
    writes_text_in = []  
    for m in matches:
        where = base_dir(cmd, m.start(), cwd)
        fields = fields_of(cmd, m.end())
        if m.group(1) == "create" or fields:
            writes_text_in.append(where)
        for kind, value in fields:
            if value is None:
                log("fail-open-unseen", kind=kind, cmd=cmd[:200])
                continue
            label = kind
            text = value
            if kind == "file":
                label = "body"
                path = os.path.expanduser(value.replace("`", ""))
                if path == "-":
                    log("fail-open-stdin-body", cmd=cmd[:200])
                    continue
                if not os.path.isabs(path):
                    path = os.path.join(where, path)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except Exception as e:
                    log("fail-open-body-file", path=path, error=repr(e)[:200])
                    continue
            for hit in refused_in(text):
                problems.append((label, hit))
    if not problems:
        missing = unread_rules(p, writes_text_in)
        if not missing:
            log("allow", cmd=cmd[:120])
            return 0
        log("refused-unread", missing=missing, cmd=cmd[:200])
        return refuse(
            "pull request text: refused. This command writes a pull request's title or body, and the repository's own rules "
            "for that text were not read whole in this session at their current bytes: " + "; ".join(missing) + ". "
            "Read each one from line 1 to the end (MCP read, or the harness Read with no offset or limit), write the "
            "title and body to what they say, and run the command again. A file edited after it was read is read again. "
            "This refusal is the driver's to clear: never hand it to the owner."
        )
    log("refused", problems=problems[:20], cmd=cmd[:200])
    shown = "; ".join(f"{label}: {json.dumps(hit, ensure_ascii=False)}" for label, hit in problems[:12])
    more = f" (and {len(problems) - 12} more)" if len(problems) > 12 else ""
    return refuse(
        f"pull request text: refused. The title or body carries a model identifier or a review-process word — {shown}{more}. "
        "A pull request says what is wrong, what changed, why this way and how to test it; which model wrote or reviewed it, "
        "and the review's rounds, seats, verdicts and log locations, are kept in the records repository under the pull "
        "request's number and are not pointed at from the pull request. Rewrite the text and run the command again. "
        "This refusal is the driver's to clear: never hand it to the owner."
    )

def main(raw: bytes) -> int:
    try:
        p = json.loads(raw.decode("utf-8", "replace").lstrip("﻿"))
        if not isinstance(p, dict):
            return 0
        event = p.get("hook_event_name")
        if event == "PostToolUse":
            witness(p)
            return 0
        if event == "PreToolUse":
            return gate(p)
        return 0
    except Exception as e:  
        log("fail-open-error", error=repr(e)[:300])
        return 0

if __name__ == "__main__":
    sys.exit(main(_RAW))
