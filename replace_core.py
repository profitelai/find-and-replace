"""Profitell Find & Replace: engine and version history. Standard library only (Python 3.9+).

Safety model
- A scan never writes anything.
- Every replace first stores the exact before and after bytes of each changed file (content-addressed blobs), then writes atomically.
- If a file changed since you previewed it, it is skipped and reported, never overwritten blindly.
- History is linear: Back / Forward move between versions; a new replace after going back discards the forward branch (the blobs stay on disk).
- Going back or forward refuses to overwrite a file that was edited outside the tool, unless you force it.
"""
import bisect, difflib, fnmatch, hashlib, json, os, re, tempfile, time

HOME = os.environ.get("PFR_HOME") or os.path.join(os.path.expanduser("~"), ".profitell-replace")
DEFAULT_EXCLUDE_DIRS = [".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode", ".next", "dist", "build", ".cache"]
MAX_PREVIEW_MATCHES = 2000

class ReplaceError(Exception): pass

# ---------------------------------------------------------------- matching

def compile_pattern(find, regex=False, case=False, whole_word=False):
    if find == "": raise ReplaceError("Type what to find.")
    pat = find if regex else re.escape(find)
    if whole_word: pat = r"(?<!\w)(?:" + pat + r")(?!\w)"
    flags = re.MULTILINE | (0 if case else re.IGNORECASE)
    try: return re.compile(pat, flags)
    except re.error as e: raise ReplaceError(f"That pattern is not valid: {e}")

_DOLLAR = re.compile(r"\$(\$|\d+|\{(\w+)\})")

def _to_template(replace):
    """Regex-mode replacement: support $1, ${1}, ${name}, $$ as well as \\1 and \\g<name>."""
    def sub(m):
        if m.group(1) == "$": return "$"
        g = m.group(2) or m.group(1)
        return f"\\g<{g}>"
    return _DOLLAR.sub(sub, replace)

def make_replacer(pattern, replace, regex):
    if not regex: return lambda m: replace
    tpl = _to_template(replace)
    def f(m):
        try: return m.expand(tpl)
        except (re.error, IndexError) as e: raise ReplaceError(f"The replacement refers to something the pattern does not capture: {e}")
    return f

def validate(find, replace, regex, case, whole_word):
    p = compile_pattern(find, regex, case, whole_word)
    if regex:
        tpl = _to_template(replace)
        try: re.compile(p.pattern).groups  # noqa
        except re.error: pass
        for g in re.findall(r"\\g<(\w+)>", tpl):
            if g.isdigit() and int(g) > p.groups: raise ReplaceError(f"The replacement uses group {g}, but the pattern only has {p.groups} group(s).")
            if not g.isdigit() and g not in p.groupindex: raise ReplaceError(f"The replacement uses group name '{g}', which the pattern does not define.")
    return p

def parse_list(s):
    """One entry per line (blank lines ignored)."""
    return [x for x in (l.strip() for l in (s or "").splitlines()) if x]

def find_matches(text, pat, protect=(), skip_lines=(), case=False):
    """(kept, excluded_count). A match is excluded when it overlaps any occurrence of a `protect` text,
    or when the line(s) it sits on contain a `skip_lines` text. Used by both scan and apply so match numbers always agree."""
    ms = list(pat.finditer(text))
    if not ms or not (protect or skip_lines): return ms, 0
    fl = 0 if case else re.IGNORECASE
    prot = [(m.start(), m.end()) for t in protect for m in re.finditer(re.escape(t), text, fl)]
    skip = [re.compile(re.escape(t), fl) for t in skip_lines]; kept = []
    for m in ms:
        if any(a < m.end() and m.start() < b for a, b in prot): continue
        if skip:
            ls = text.rfind("\n", 0, m.start()) + 1; le = text.find("\n", max(m.end(), m.start())); le = len(text) if le < 0 else le
            line = text[ls:le]
            if any(r.search(line) for r in skip): continue
        kept.append(m)
    return kept, len(ms) - len(kept)

# ---------------------------------------------------------------- files

def sha(b): return hashlib.sha256(b).hexdigest()

def is_binary(b): return b"\x00" in b[:8192]

def split_globs(s):
    return [g.strip() for g in re.split(r"[,\n]", s or "") if g.strip()]

def iter_files(root, include="", exclude="", exclude_dirs=None, max_bytes=5_000_000):
    """Yield (relpath, abspath, skipped_reason_or_None). Hidden dirs listed in exclude_dirs are not entered."""
    inc, exc = split_globs(include), split_globs(exclude); xd = set(DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs)
    for dp, dn, fn in os.walk(root, followlinks=False):
        dn[:] = sorted(d for d in dn if d not in xd)
        for f in sorted(fn):
            ab = os.path.join(dp, f); rel = os.path.relpath(ab, root).replace(os.sep, "/")
            if os.path.islink(ab): continue
            if inc and not any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(rel, g) for g in inc): continue
            if exc and any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(rel, g) for g in exc): continue
            try: size = os.path.getsize(ab)
            except OSError: continue
            if size > max_bytes: yield rel, ab, f"larger than {max_bytes // 1_000_000} MB"; continue
            yield rel, ab, None

def check_root(root):
    root = os.path.abspath(os.path.expanduser(root or ""))
    if not os.path.isdir(root): raise ReplaceError("That folder does not exist.")
    home = os.path.expanduser("~")
    if root in ("/", home, os.path.dirname(home)) or len(root.strip(os.sep).split(os.sep)) < 2:
        raise ReplaceError("Pick a project folder, not your whole computer or home folder.")
    return root

def read_text(ab):
    with open(ab, "rb") as fh: b = fh.read()
    if is_binary(b): return b, None, "binary file"
    try: return b, b.decode("utf-8"), None
    except UnicodeDecodeError: return b, None, "not UTF-8 text"

# ---------------------------------------------------------------- scan

def _snippets(text, starts, m, rep):
    ms, me = m.start(), m.end()
    li = bisect.bisect_right(starts, ms) - 1; line = li + 1
    ls = starts[li]; le = text.find("\n", me if me > ms else ms); le = len(text) if le < 0 else le
    if text[le - 1:le] == "\r": le -= 1
    pre = text[ls:ms]; mt = text[ms:me]; post = text[me:le]
    cut = lambda s, n, left: (("…" + s[-n:]) if left else (s[:n] + "…")) if len(s) > n else s
    pre, post, mt_s, rp = cut(pre, 80, True), cut(post, 80, False), cut(mt, 200, False), cut(rep, 200, False)
    return {"line": line, "col": ms - ls + 1, "before": [pre, mt_s, post], "after": [pre, rp, post]}

def scan(root, find, replace, regex=False, case=False, whole_word=False, include="", exclude="", exclude_dirs=None, max_bytes=5_000_000, protect="", skip_lines=""):
    root = check_root(root); pat = validate(find, replace, regex, case, whole_word); rp = make_replacer(pat, replace, regex)
    files, skipped, total, shown, scanned, excluded = [], [], 0, 0, 0, 0; prot, skl = parse_list(protect), parse_list(skip_lines)
    for rel, ab, why in iter_files(root, include, exclude, exclude_dirs, max_bytes):
        if why: skipped.append({"path": rel, "why": why}); continue
        raw, text, why = read_text(ab)
        if text is None:
            if why != "binary file": skipped.append({"path": rel, "why": why})
            continue
        scanned += 1; ms, ex = find_matches(text, pat, prot, skl, case); excluded += ex
        if not ms: continue
        starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
        matches = []
        for i, m in enumerate(ms):
            if shown < MAX_PREVIEW_MATCHES:
                d = _snippets(text, starts, m, rp(m)); d["i"] = i; matches.append(d); shown += 1
        total += len(ms); files.append({"path": rel, "count": len(ms), "hash": sha(raw), "matches": matches, "truncated": len(matches) < len(ms)})
    return {"root": root, "files": files, "total": total, "scanned": scanned, "excluded": excluded, "skipped": skipped, "preview_capped": shown >= MAX_PREVIEW_MATCHES}

# ---------------------------------------------------------------- history store

def _pdir(root):
    d = os.path.join(HOME, "projects", hashlib.sha1(root.encode()).hexdigest()[:12]); os.makedirs(d, exist_ok=True); return d

def _blob_path(h): return os.path.join(HOME, "blobs", h[:2], h)

def put_blob(b):
    h = sha(b); p = _blob_path(h)
    if not os.path.exists(p): os.makedirs(os.path.dirname(p), exist_ok=True); _atomic(p, b)
    return h

def get_blob(h):
    with open(_blob_path(h), "rb") as fh: return fh.read()

def _atomic(path, data, mode=None):
    d = os.path.dirname(path) or "."; fd, tmp = tempfile.mkstemp(dir=d, prefix=".pfr-tmp-")
    try:
        with os.fdopen(fd, "wb") as fh: fh.write(data); fh.flush(); os.fsync(fh.fileno())
        if mode is not None: os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def load_history(root):
    p = os.path.join(_pdir(root), "history.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh: return json.load(fh)
    return {"root": root, "versions": [], "head": 0}

def save_history(root, h): _atomic(os.path.join(_pdir(root), "history.json"), json.dumps(h, indent=1).encode("utf-8"))

# ---------------------------------------------------------------- apply

def apply(root, find, replace, selection, regex=False, case=False, whole_word=False, note="", protect="", skip_lines=""):
    """selection: {relpath: {"hash": sha_from_scan, "indices": [match indexes] or None for all}}."""
    root = check_root(root); pat = validate(find, replace, regex, case, whole_word); rp = make_replacer(pat, replace, regex)
    if not selection: raise ReplaceError("Nothing is selected.")
    plan, conflicts = [], []; prot, skl = parse_list(protect), parse_list(skip_lines)
    for rel, sel in selection.items():
        ab = os.path.normpath(os.path.join(root, rel))
        if not ab.startswith(root + os.sep): raise ReplaceError("A selected path is outside the folder.")
        try: raw, text, why = read_text(ab)
        except OSError: conflicts.append({"path": rel, "why": "file is gone"}); continue
        if text is None: conflicts.append({"path": rel, "why": why}); continue
        if sel.get("hash") and sel["hash"] != sha(raw): conflicts.append({"path": rel, "why": "file changed since you searched; search again"}); continue
        want = None if sel.get("indices") is None else set(sel["indices"]); out, last, n = [], 0, 0
        for i, m in enumerate(find_matches(text, pat, prot, skl, case)[0]):
            if want is not None and i not in want: continue
            out.append(text[last:m.start()]); out.append(rp(m)); last = m.end(); n += 1
        if n == 0: continue
        out.append(text[last:]); new = "".join(out).encode("utf-8")
        plan.append((rel, ab, raw, new, n, os.stat(ab).st_mode & 0o7777))
    if not plan: return {"changed": 0, "replacements": 0, "conflicts": conflicts, "version": None}
    h = load_history(root); entries = []
    for rel, ab, raw, new, n, mode in plan: entries.append({"path": rel, "before": put_blob(raw), "after": put_blob(new), "count": n})
    done = []
    try:
        for (rel, ab, raw, new, n, mode) in plan: _atomic(ab, new, mode); done.append((ab, raw, mode))
    except BaseException:
        for ab, raw, mode in done: _atomic(ab, raw, mode)
        raise
    h["versions"] = h["versions"][:h["head"]]; num = h["head"] + 1
    v = {"n": num, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "find": find, "replace": replace, "regex": regex, "case": case, "whole_word": whole_word, "note": note, "protect": prot, "skip_lines": skl, "files": entries,
         "replacements": sum(e["count"] for e in entries)}
    h["versions"].append(v); h["head"] = num; save_history(root, h)
    return {"changed": len(entries), "replacements": v["replacements"], "conflicts": conflicts, "version": num}

# ---------------------------------------------------------------- navigate

def history(root):
    root = check_root(root); h = load_history(root)
    return {"root": root, "head": h["head"], "versions": [{k: v[k] for k in ("n", "time", "find", "replace", "regex", "note", "replacements")} | {"files": len(v["files"]), "protect": v.get("protect", []), "skip_lines": v.get("skip_lines", [])} for v in h["versions"]]}

def _disk_sha(root, rel):
    try:
        with open(os.path.join(root, rel), "rb") as fh: return sha(fh.read())
    except OSError: return None

def goto(root, target, force=False):
    root = check_root(root); h = load_history(root); cur = h["head"]; vs = h["versions"]
    if not 0 <= target <= len(vs): raise ReplaceError("There is no such version.")
    if target == cur: return {"head": cur, "changed": 0, "conflicts": []}
    steps = []   # (path, expected_current_sha, new_sha)
    if target < cur:
        for n in range(cur, target, -1):
            for e in vs[n - 1]["files"]: steps.append((e["path"], e["after"], e["before"]))
    else:
        for n in range(cur + 1, target + 1):
            for e in vs[n - 1]["files"]: steps.append((e["path"], e["before"], e["after"]))
    sim, conflicts = {}, []
    for rel, exp, new in steps:
        if rel not in sim: sim[rel] = _disk_sha(root, rel)
        if sim[rel] != exp: conflicts.append({"path": rel, "why": "file was edited outside this tool since that version"})
        sim[rel] = new
    if conflicts and not force:
        seen, uniq = set(), []
        for c in conflicts:
            if c["path"] not in seen: seen.add(c["path"]); uniq.append(c)
        return {"head": cur, "changed": 0, "conflicts": uniq, "needs_force": True}
    final = {rel: new for rel, _, new in steps}; changed = 0
    for rel, new in final.items():
        ab = os.path.join(root, rel); mode = (os.stat(ab).st_mode & 0o7777) if os.path.exists(ab) else None
        if new is None: continue
        os.makedirs(os.path.dirname(ab), exist_ok=True); _atomic(ab, get_blob(new), mode); changed += 1
    h["head"] = target; save_history(root, h)
    return {"head": target, "changed": changed, "conflicts": [] if not force else conflicts}

def diff(root, n, max_lines=1500):
    root = check_root(root); h = load_history(root)
    if not 1 <= n <= len(h["versions"]): raise ReplaceError("There is no such version.")
    out = []
    for e in h["versions"][n - 1]["files"]:
        a = get_blob(e["before"]).decode("utf-8", "replace").splitlines(); b = get_blob(e["after"]).decode("utf-8", "replace").splitlines()
        lines = list(difflib.unified_diff(a, b, "before", "after", lineterm="", n=2))
        out.append({"path": e["path"], "count": e["count"], "lines": lines[:max_lines], "truncated": len(lines) > max_lines})
    return {"n": n, "files": out}
