#!/usr/bin/env python3
"""AI-native SDLC guard v6 (session provenance + signed human approvals). Modes:
  pre | post | stop     Claude Code PreToolUse / PostToolUse / Stop hook (JSON on stdin)
  lock                  freeze tests/ at HEAD (agent or human)
  accept <file> [status]  HUMAN ONLY, own terminal: set intent/spec accepted, plan approved
  unlock                HUMAN ONLY, own terminal: release the test lock
  init-approver         HUMAN ONLY, once: create a passphrase-protected approver key and
                        .github/allowed_signers so CI can verify accept/unlock commits
  gate <stage> <slug>   stage = spec|plan|build: is the upstream artifact human-accepted?
  ci <base>             PR check (server side, judged on the PR's net effect)
  report [days]         guard blocks/restores per kind (default 7 days)
  status                every change (slug) with its stage, status and who acts next
  doctor                deterministic setup check (used by the audit skill); exit 1 on FAIL
  version               print the guard version
Principle: everything that changed since the agent session started is agent work, whatever
the commit says. Protected paths are held at the session baseline; violations are restored in
the working tree and the agent is told to commit the restore. Exit 2 = block (stderr to Claude)."""
import hashlib, json, os, re, shutil, subprocess, sys, time
VERSION = "6.0.0"

# ---- CONFIG ---------------------------------------------------------------
GEN   = []                                   # generated / frozen paths
GATES = [".claude/hooks/", ".claude/settings.json", ".claude/settings.local.json",
         "REVIEW.md", ".github/workflows/", ".github/allowed_signers"]
TESTS = ["tests/"]
ARTIFACTS = {"spec": ("intent", "accepted"), "plan": ("specs", "accepted"), "build": ("plans", "approved")}
ACCEPT = re.compile(r"(accepted|approved|signed[- ]?off)\b", re.I)
AGENT_TRAILER = re.compile(r"^(Co-Authored-By:\s*Claude|Claude-Session:)", re.I | re.M)
SIGNERS = ".github/allowed_signers"                 # present = signed-approval mode
APPROVER_KEY = os.path.expanduser(os.environ.get("SDLC_APPROVER_KEY", "~/.config/sdlc/approver_ed25519"))
# ---------------------------------------------------------------------------
ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
LOCK = ".claude/FIX_TASK"
PROTECTED = GATES + GEN
ART_DIRS = {d for d, _ in ARTIFACTS.values()}

def git(*a, check=False, raw=False):
    r = subprocess.run(["git", "-C", ROOT, *a], capture_output=True, text=True)
    if check and r.returncode: raise SystemExit(f"git {' '.join(a)} failed: {r.stderr}")
    return r.stdout if raw else r.stdout.strip()

def exists(rev, path):
    return subprocess.run(["git", "-C", ROOT, "cat-file", "-e", f"{rev}:{path}"], capture_output=True).returncode == 0

def show(rev, path): return git("show", f"{rev}:{path}", raw=True) if exists(rev, path) else None

def wt(path):
    p = os.path.join(ROOT, path)
    return open(p).read() if os.path.isfile(p) else None

def changed_since(rev, paths, upto=None):  # files differing from rev; [] when paths is empty
    if not paths: return []
    return git("diff", "--name-only", rev, *([upto] if upto else []), "--", *paths).splitlines()

def untracked(paths):
    return git("ls-files", "--others", "--exclude-standard", "--", *paths).splitlines() if paths else []

def under(path, prefixes): return any(path == x.rstrip("/") or path.startswith(x) for x in prefixes)

def rel(p):
    return os.path.relpath(os.path.realpath(os.path.join(ROOT, p)), os.path.realpath(ROOT)) if p else ""

def audit(kind, msg):
    try: open(os.path.join(state_dir(), "audit.log"), "a").write(f"{int(time.time())}\t{kind}\t{msg[:300]}\n")
    except Exception: pass

def quarantine(path, label="restored"):    # keep a copy of anything the guard overwrites or deletes
    src = os.path.join(ROOT, path)
    if not os.path.isfile(src): return
    dst = os.path.join(state_dir(), "quarantine", time.strftime("%Y%m%d-%H%M%S"), path)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as a, open(dst, "wb") as b: b.write(a.read())
    audit("quarantine", f"{label} {path} -> {dst}")

def block(msg, kind="block"): audit(kind, msg); print(f"SDLC guard: {msg}", file=sys.stderr); sys.exit(2)

def status_of(text):                      # the first Status: field is the header
    m = re.search(r"Status:\s*([\w -]+)", text or "")
    return m.group(1).strip().lower() if m else None

def accepted(text): st = status_of(text); return bool(st and ACCEPT.match(st))

# ---- session provenance (state lives outside the repo, under ~/.claude/sdlc) ----
def state_dir():                          # SDLC_STATE_DIR is for tests; hooks get Claude Code's own env
    common = os.path.realpath(os.path.join(ROOT, git("rev-parse", "--git-common-dir") or ".git"))
    base = os.environ.get("SDLC_STATE_DIR") or os.path.expanduser("~/.claude/sdlc")
    d = os.path.join(base, hashlib.sha1(common.encode()).hexdigest()[:16])
    try: os.makedirs(os.path.join(d, "sessions"), exist_ok=True)
    except OSError: pass                   # read-only inside the sandbox: readers cope with missing files
    return d

def baseline(sid):
    d = os.path.join(state_dir(), "sessions")
    for old in os.listdir(d):                              # prune sessions older than 30 days
        if time.time() - os.path.getmtime(os.path.join(d, old)) > 30 * 86400: os.remove(os.path.join(d, old))
    f = os.path.join(d, re.sub(r"[^\w-]", "_", sid or "default"))
    if not os.path.exists(f): open(f, "w").write(git("rev-parse", "HEAD"))
    return open(f).read().strip()

def ledger(): return os.path.join(state_dir(), "agent-commits")

def status_cmd():
    rows, slugs = [], set()
    dirs = {"intent": "intent", "spec": "specs", "plan": "plans"}
    for d in dirs.values():
        for f in git("ls-files", "--cached", "--others", "--exclude-standard", "--", d + "/").splitlines():
            if f.endswith(".md"): slugs.add(os.path.basename(f)[:-3])
    locked = bool((wt(LOCK) or "").strip())
    for slug in sorted(slugs):
        st = {k: status_of(wt(f"{d}/{slug}.md")) if wt(f"{d}/{slug}.md") is not None else None for k, d in dirs.items()}
        def nxt():
            for stage, want, skill in (("intent", "accepted", "intent"), ("spec", "accepted", "spec"), ("plan", "approved", "plan")):
                s = st[stage]
                if s is None: return f"agent: write {stage} (skill: {skill})"
                if s == "rejected": return f"stopped: {stage} rejected"
                if not s.startswith(want):
                    return (f"HUMAN: review, then `sdlc_guard.py accept {dirs[stage]}/{slug}.md`" if s == "ready-for-review"
                            else f"agent: finish {stage} draft (skill: {skill})")
            if locked: return "agent: build in progress; HUMAN unlocks when green"
            acc = acceptance_commit(f"plans/{slug}.md")
            built = acc and any("chore(gate): unlock tests" in m for m in git("log", "--format=%s", f"{acc}..HEAD").splitlines())
            return "agent: review, then ship (skills: review, ship)" if built else "agent: build test-first (skill: build)"
        rows.append((slug, st["intent"] or "-", st["spec"] or "-", st["plan"] or "-", nxt()))
    print(f"guard {VERSION} | tests {'LOCKED' if locked else 'unlocked'} | signed mode {'on' if os.path.exists(os.path.join(ROOT, SIGNERS)) else 'off'}")
    if not rows: print("no changes yet (agent: start one with the intent skill)"); return
    w = max(len(r[0]) for r in rows)
    print(f"{'slug':{w}}  {'intent':16} {'spec':16} {'plan':16} next"); [print(f"{r[0]:{w}}  {r[1]:16} {r[2]:16} {r[3]:16} {r[4]}") for r in rows]

def doctor_cmd():
    res = []
    def chk(ok, level, msg): res.append(("PASS" if ok else level, msg))
    try: s = json.load(open(os.path.join(ROOT, ".claude/settings.json")))
    except Exception: s = {}
    hooks = s.get("hooks", {})
    wired = lambda ev, mode: any("sdlc_guard.py" in h.get("command", "") and h.get("command", "").rstrip().endswith(mode)
                                 for g in hooks.get(ev, []) for h in g.get("hooks", []))
    chk(os.path.exists(os.path.join(ROOT, ".claude/hooks/sdlc_guard.py")), "FAIL", "guard installed at .claude/hooks/sdlc_guard.py")
    chk(wired("PreToolUse", "pre") and wired("PostToolUse", "post") and wired("Stop", "stop"), "FAIL", "hooks wired: PreToolUse pre, PostToolUse post, Stop stop")
    chk(exists("HEAD", ".claude/hooks/sdlc_guard.py") and exists("HEAD", ".claude/settings.json"), "FAIL", "guard and settings committed")
    sb = s.get("sandbox", {})
    chk(sb.get("enabled") and sb.get("allowUnsandboxedCommands") is False, "FAIL", "sandbox enabled, no unsandboxed retry")
    chk(any(".env" in x for x in sb.get("filesystem", {}).get("denyRead", [])), "FAIL", "sandbox denies reading .env")
    chk(sb.get("failIfUnavailable") is True, "WARN", "sandbox failIfUnavailable (refuse to run unsandboxed)")
    if sys.platform.startswith("linux"):
        chk(bool(shutil.which("bwrap") and shutil.which("socat")), "FAIL", "sandbox prerequisites bubblewrap + socat installed")
    chk(os.path.exists(os.path.join(ROOT, ".github/workflows/sdlc-gates.yml")), "WARN", "CI gate workflow present")
    chk(os.path.exists(os.path.join(ROOT, SIGNERS)), "WARN", "signed mode (.github/allowed_signers)")
    chk("verifying your work" in (wt("CLAUDE.md") or "").lower(), "FAIL", "CLAUDE.md has a 'Verifying your work' block")
    chk(wt("REVIEW.md") is not None, "WARN", "REVIEW.md present")
    chk(all(os.path.isdir(os.path.join(ROOT, d)) for d in ("intent", "specs", "plans")), "WARN", "intent/ specs/ plans/ exist")
    ev = [f for f in os.listdir(os.path.join(ROOT, "evals"))] if os.path.isdir(os.path.join(ROOT, "evals")) else []
    chk(any(f.endswith(".json") for f in ev), "WARN", f"evals present ({sum(f.endswith('.json') for f in ev)} found)")
    chk(os.path.exists(os.path.join(ROOT, "monitoring/bands.yaml")), "INFO", "control bands configured (needed only once deployed)")
    for lvl, msg in res: print(f"{lvl:4}  {msg}")
    fails = sum(l == "FAIL" for l, _ in res); print(f"\n{fails} FAIL, {sum(l == 'WARN' for l, _ in res)} WARN  (guard {VERSION})")
    sys.exit(1 if fails else 0)

def installing(base):                     # session started before the guard existed = the install
    return not exists(base, ".claude/hooks/sdlc_guard.py")   # session; gates protect from the next one

def record(base):
    seen = set(open(ledger()).read().split()) if os.path.exists(ledger()) else set()
    new = [c for c in git("rev-list", f"{base}..HEAD").splitlines() if c not in seen]
    if new: open(ledger(), "a").write("".join(c + "\n" for c in new))

def agent_commit(c):
    if c in {a[0] for a in approvals()}: return False
    known = os.path.exists(ledger()) and c in open(ledger()).read().split()
    return known or bool(AGENT_TRAILER.search(git("show", "-s", "--format=%B", c)))

_APPROVALS = None
def approvals():                         # human decisions: "<commit> <path> <status>" per line (cached per run)
    global _APPROVALS
    if _APPROVALS is None:
        f = os.path.join(state_dir(), "approvals")
        _APPROVALS = [l.split(" ", 2) for l in open(f).read().splitlines() if l.count(" ") >= 2] if os.path.exists(f) else []
    return _APPROVALS

def approve(commit, path, st):
    global _APPROVALS; _APPROVALS = None
    open(os.path.join(state_dir(), "approvals"), "a").write(f"{commit} {path} {st}\n")

def allowed_status(path, base):          # last human decision, else the status at session start
    st = [a[2] for a in approvals() if a[1] == path]
    return st[-1] if st else status_of(show(base, path))

def lock_sha(base):                       # ratchet: once locked it stays locked until a human unlocks
    released = {a[2].split(":", 1)[1] for a in approvals() if a[1] == LOCK and a[2].startswith("unlocked:")}
    for v in (wt(LOCK), show("HEAD", LOCK), show(base, LOCK)):
        if v and v.strip() and v.strip() not in released: return v.strip()
    return None

def signed_mode(rev="HEAD"): return exists(rev, SIGNERS)

def human_commit(*args):                  # commits a human decision; signed with the approver key in signed mode
    if signed_mode() or os.path.exists(os.path.join(ROOT, SIGNERS)):
        if not os.path.exists(APPROVER_KEY): raise SystemExit(f"approver key {APPROVER_KEY} missing (run init-approver)")
        git("-c", "gpg.format=ssh", "-c", "gpg.ssh.program=ssh-keygen", "-c", f"user.signingkey={APPROVER_KEY}",
            "commit", "-S", *args, check=True)
    else: git("-c", "commit.gpgsign=false", "commit", *args, check=True)

def verified(c, signers_rev):              # is commit c signed by a key listed in SIGNERS at signers_rev?
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False) as t: t.write(show(signers_rev, SIGNERS) or "")
    r = subprocess.run(["git", "-C", ROOT, "-c", "gpg.format=ssh", "-c", "gpg.ssh.program=ssh-keygen",
                        "-c", f"gpg.ssh.allowedSignersFile={t.name}", "verify-commit", c], capture_output=True)
    os.unlink(t.name); return r.returncode == 0

def require_human():
    if os.environ.get("CLAUDECODE"): raise SystemExit("this command is for a human, outside Claude Code")

# ---------------- pre ----------------
def pre(ev):
    base = baseline(ev.get("session_id")); tool, ti = ev.get("tool_name", ""), ev.get("tool_input", {})
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        p = rel(ti.get("file_path") or ti.get("notebook_path", ""))
        if p.startswith(".."): return
        if p.startswith(".git/"): block("the .git directory is off limits.")
        if under(p, GEN): block(f"{p} is generated. Change the generator instead.")
        if (under(p, GATES) and not installing(base)) or p == LOCK: block(f"{p} is a gate. Gates change only outside agent sessions, via a human-approved PR.")
        if lock_sha(base) and under(p, TESTS): block("tests are locked for this fix. Make them pass without editing them.")
        if p.split("/")[0] in ART_DIRS:
            before = wt(p) or ""
            if tool == "Write": after = ti.get("content", "")
            elif tool == "Edit": after = before.replace(ti.get("old_string", ""), ti.get("new_string", ""), 1)
            else: after = before + "".join(e.get("new_string", "") for e in ti.get("edits", []))
            if accepted(after) and status_of(after) != allowed_status(p, base):
                block("only a human sets accepted/approved/signed-off. Set 'Status: ready-for-review' and stop at the gate.")
        return
    if tool != "Bash": return
    cmd = ti.get("command", "")
    body = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n\1\b", "", cmd, flags=re.S)   # drop heredoc bodies
    if re.search(r"(^|[\s/'\"=<])\.env\b", body): block("shell access to .env. Secrets come from the environment.")
    if re.search(r"\.claude/sdlc|\.git/(hooks|config)\b", body): block("the guard's session state and git internals are off limits.")
    if re.search(r"deploy", body) and re.search(r"(?<![\w-])(prod|production|live)(?![\w-])", body) \
            and not os.environ.get("RELEASE_APPROVAL"):
        block("production deploy needs a named release authorization (RELEASE_APPROVAL set by a human).")
    if re.search(r"git\s+(reset\b[^|;&]*--hard|push\b[^|;&]*(\s-f\b|--force)|commit\b[^|;&]*--amend|rebase|filter-branch|update-ref|replace)|--no-verify", body):
        block("history-rewriting git commands are not allowed for the agent.")
    if re.search(r"sdlc_guard\.py\s+(unlock|accept)", body): block("accept and unlock are human decisions (run them in your own terminal).")

# ---------------- post / stop ----------------
def enforce(ev, final=False):
    base = baseline(ev.get("session_id")); fixed = []
    prot = GEN if installing(base) else PROTECTED
    # 1. protected paths: working tree and index are held at the session baseline
    changed = changed_since(base, prot) + untracked(prot)
    for path in dict.fromkeys(changed):
        quarantine(path)
        if exists(base, path): git("checkout", base, "--", path)
        else:
            git("rm", "-q", "--cached", "--ignore-unmatch", "--", path)
            if os.path.exists(os.path.join(ROOT, path)): os.remove(os.path.join(ROOT, path))
        fixed.append(path)
    # 2. the test lock is a ratchet: it can be added, never removed by the agent
    sha = lock_sha(base)
    if sha and (wt(LOCK) or "").strip() != sha:
        open(os.path.join(ROOT, LOCK), "w").write(sha + "\n"); git("add", LOCK); fixed.append(LOCK)
    # 3. locked tests are held at the lock commit
    if sha:
        changed = changed_since(sha, TESTS) + untracked(TESTS)
        for path in dict.fromkeys(changed):
            quarantine(path)
            if exists(sha, path): git("checkout", sha, "--", path)
            else:
                git("rm", "-q", "--cached", "--ignore-unmatch", "--", path)
                if os.path.exists(os.path.join(ROOT, path)): os.remove(os.path.join(ROOT, path))
            fixed.append(path)
    # 4. acceptance can only come from outside the session (only files changed since the baseline can have escalated)
    art = [d + "/" for d in ART_DIRS]
    touched = changed_since(base, art) + untracked(art)
    touched += [a[1] for a in approvals() if under(a[1], art)]          # files a human decided on mid-session
    for d in [None]:
        for path in dict.fromkeys(touched):
            text = wt(path)
            if text and accepted(text) and status_of(text) != allowed_status(path, base):
                back = allowed_status(path, base) or "ready-for-review"; quarantine(path, "status-reset")
                open(os.path.join(ROOT, path), "w").write(re.sub(r"(Status:\s*)[\w -]+", r"\g<1>" + back, text, count=1))
                fixed.append(f"{path} (status reset to '{back}')")
    record(base)
    # 5. anything wrong in committed history since the baseline?
    bad = changed_since(base, prot, "HEAD")
    if sha and not exists("HEAD", LOCK): bad.append(LOCK)
    if sha: bad += changed_since(sha, TESTS, "HEAD")
    for d in [None]:
        for path in changed_since(base, art, "HEAD"):
            now = show("HEAD", path)
            if accepted(now) and status_of(now) != allowed_status(path, base): bad.append(path)
    msg = []
    if fixed: msg.append(f"protected changes were undone in the working tree: {sorted(set(fixed))} "
                         f"(copies kept under {os.path.join(state_dir(), 'quarantine')}).")
    if bad:
        paths = " ".join(sorted(set(p.split(" (")[0] for p in bad)))
        msg.append(f"commits made in this session change protected state [{paths}]. Commit the restored files now, "
                   f"staging them by name: `git add -A -- {paths} && git commit -m 'restore gates'`.")
    if msg:
        if final and ev.get("stop_hook_active"):         # avoid a stop loop; the ledger and CI still flag it
            print("SDLC guard: " + " ".join(msg), file=sys.stderr); sys.exit(0)
        block(" ".join(msg) + " Ask the human if a gate genuinely must change.", kind="restore")

# ---------------- gate / ci ----------------
def acceptance_commit(path):
    for c in git("log", "--format=%H", "--", path).splitlines():
        if status_of(show(c, path)) != status_of(show(c + "^", path)): return c
    return None

def gate(stage, slug):
    if stage not in ARTIFACTS: raise SystemExit(f"stage must be one of {list(ARTIFACTS)}")
    d, want = ARTIFACTS[stage]; path = f"{d}/{slug}.md"
    if wt(path) is None: print(f"FAIL {path} missing"); sys.exit(1)
    st = status_of(wt(path)) or ""
    if not st.startswith(want): print(f"FAIL {path} is '{st}', needs '{want}' from a human"); sys.exit(1)
    if git("status", "--porcelain", "--", path): print(f"FAIL {path} has uncommitted changes"); sys.exit(1)
    c = acceptance_commit(path)
    if not c or agent_commit(c): print(f"FAIL {path} was set to '{st}' by an agent commit {c and c[:7]}"); sys.exit(1)
    if signed_mode() and not verified(c, "HEAD"):
        print(f"FAIL {path}: acceptance commit {c[:7]} is not signed by an approver key in {SIGNERS}"); sys.exit(1)
    print(f"OK {path} {st} by human commit {c[:7]}")

def ci(base):
    errs, label = [], os.environ.get("GATES_APPROVED")
    net_gates = changed_since(base, GATES, "HEAD")                                     # judged on net effect
    if net_gates and not label: errs.append(f"PR changes gate files {net_gates} (needs a human 'gates-approved' label)")
    signed = signed_mode(base)                     # signers are read from the BASE branch, never from the PR
    for c in git("rev-list", f"{base}..HEAD").splitlines():
        files = git("show", "--name-only", "--format=", c).splitlines()
        if signed and LOCK in files and exists(c + "^", LOCK) and not exists(c, LOCK) and not verified(c, base):
            errs.append(f"{c[:7]} removes the test lock without an approver signature")
        if exists(c + "^", LOCK) and any(under(f, TESTS) for f in files):
            errs.append(f"{c[:7]} changes tests while they were locked")
        for f in files:
            if f.split("/")[0] in ART_DIRS and f.endswith(".md"):
                now = show(c, f)
                if accepted(now) and status_of(now) != status_of(show(c + "^", f)):
                    if signed and not verified(c, base):
                        errs.append(f"{c[:7]} sets {f} to '{status_of(now)}' without an approver signature")
                    elif not signed and agent_commit(c):
                        errs.append(f"{c[:7]} sets {f} to '{status_of(now)}' but is an agent commit")
    if exists(base, LOCK) and not exists("HEAD", LOCK) and not label and not signed:
        errs.append("PR removes a test lock that existed before it (needs a human 'gates-approved' label)")
    print("\n".join(errs) or "OK gates"); sys.exit(1 if errs else 0)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode in ("pre", "post", "stop"):
        ev = json.loads(sys.stdin.read() or "{}")
        pre(ev) if mode == "pre" else enforce(ev, final=(mode == "stop"))
    elif mode == "lock":
        if TESTS and git("status", "--porcelain", "--", *TESTS): raise SystemExit("commit or discard test changes first: lock freezes tests at HEAD and would revert them")
        open(os.path.join(ROOT, LOCK), "w").write(git("rev-parse", "HEAD") + "\n")
        git("add", "--", LOCK); git("-c", "commit.gpgsign=false", "commit", "-qm", "chore(gate): lock tests", "--", LOCK, check=True); print("tests locked")
    elif mode == "accept":
        require_human(); path = rel(sys.argv[2]); text = wt(path) or ""
        st = sys.argv[3] if len(sys.argv) > 3 else ("approved" if path.startswith("plans/") else "accepted")
        if not status_of(text): raise SystemExit(f"{path} has no Status: field")
        if git("status", "--porcelain", "--", path):
            raise SystemExit(f"{path} has uncommitted changes. Review and commit (or discard) them first, so you accept exactly what you read.")
        print(f"accepting {path} as last committed: {git('log', '-1', '--format=%h %s', '--', path)}")
        open(os.path.join(ROOT, path), "w").write(re.sub(r"(Status:\s*)[\w -]+", r"\g<1>" + st, text, count=1))
        git("add", "--", path); human_commit("-qm", f"gate: {st} {path}", "--", path)
        approve(git("rev-parse", "HEAD"), path, st); print(f"{path} -> {st} (recorded as a human decision)")
    elif mode == "unlock":
        require_human(); sha = (wt(LOCK) or "").strip()
        git("rm", "-q", "--", LOCK); human_commit("-qm", "chore(gate): unlock tests", "--", LOCK)
        approve(git("rev-parse", "HEAD"), LOCK, f"unlocked:{sha}"); print("tests unlocked")
    elif mode == "init-approver":
        require_human(); os.makedirs(os.path.dirname(APPROVER_KEY), exist_ok=True)
        if not os.path.exists(APPROVER_KEY):
            print("Creating the approver key. Choose a passphrase: it is what proves a human approved.")
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-C", "sdlc-approver", "-f", APPROVER_KEY], check=True)
        os.makedirs(os.path.join(ROOT, ".github"), exist_ok=True)
        open(os.path.join(ROOT, SIGNERS), "a").write('* namespaces="git" ' + open(APPROVER_KEY + ".pub").read().strip() + "\n")
        print(f"wrote {SIGNERS}. Commit it yourself in a PR labelled 'gates-approved'; from then on CI requires signed approvals.")
    elif mode == "gate": gate(sys.argv[2], sys.argv[3])
    elif mode == "report":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7; since = time.time() - days * 86400; counts = {}
        f = os.path.join(state_dir(), "audit.log")
        for line in (open(f).read().splitlines() if os.path.exists(f) else []):
            ts, kind, msg = line.split("\t", 2)
            if int(ts) >= since: key = f"{kind}: {re.sub(r'[\[(].*', '', msg).strip()[:70]}"; counts[key] = counts.get(key, 0) + 1
        print(f"guard events, last {days} days:"); [print(f"  {n:4}  {k}") for k, n in sorted(counts.items(), key=lambda x: -x[1])] or print("  none")
    elif mode == "ci": ci(sys.argv[2])
    elif mode == "status": status_cmd()
    elif mode == "doctor": doctor_cmd()
    elif mode == "version": print(VERSION)
    else: print(__doc__); sys.exit(1)
