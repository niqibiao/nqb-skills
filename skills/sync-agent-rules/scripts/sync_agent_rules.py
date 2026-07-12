#!/usr/bin/env python3
"""
agents-sync: keep the user-level CLAUDE.md / AGENTS.md in sync with a remote git repo.

The repo holds ONE canonical instructions file. `pull` overwrites every local target
with it (backing up first); `push` sends one local source file up and refuses to
clobber remote changes silently.

State lives under ~/.claude/agents-sync/ (a config.json + a private git working copy),
so it survives skill updates and never touches the user's real files except the targets.

Exit codes: 0 ok | 1 error | 2 not-initialized | 3 conflict (caller must ask user) | 4 auth/network
"""
import argparse, json, os, shutil, subprocess, sys
from datetime import datetime

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".claude", "agents-sync")
CONFIG_PATH = os.path.join(BASE, "config.json")
REPO = os.path.join(BASE, "repo")

# Only AGENTS.md is synced with full content. CLAUDE.md is a one-line stub that
# imports it (see DEFAULT_STUB / ensure_stub), so the two never hold duplicate copies.
DEFAULT_TARGETS = [os.path.join(HOME, "AGENTS.md")]

# ~/.claude/CLAUDE.md is kept as `@~/AGENTS.md` — Claude Code resolves @-imports
# relative to the importing file, so the absolute ~/ form is required to reach ~/AGENTS.md.
DEFAULT_STUB = {"path": os.path.join(HOME, ".claude", "CLAUDE.md"),
                "import": "~/AGENTS.md"}


# ---------- helpers ----------

def die(msg, code=1):
    sys.stderr.write(msg.rstrip() + "\n")
    sys.exit(code)

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH) as f:
        return json.load(f)

def save_config(cfg):
    os.makedirs(BASE, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def git(args, cwd=REPO, check=True):
    """Run git non-interactively (no credential prompts that would hang)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git", *args], cwd=cwd, env=env,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        out = (r.stdout + r.stderr).lower()
        if any(w in out for w in ("authentication", "403", "401", "could not read",
                                  "terminal prompts disabled", "connection", "resolve host")):
            die("AUTH/NETWORK: git could not reach or authenticate to the repo.\n"
                + r.stdout + r.stderr +
                "\nHint: configure a git credential helper for this host, e.g.\n"
                "  macOS:   git config --global credential.helper osxkeychain\n"
                "  Windows: git config --global credential.helper manager\n"
                "  Linux:   git config --global credential.helper store\n"
                "then run any git clone/pull against the repo once to cache the token.", 4)
        die(f"git {' '.join(args)} failed:\n{r.stdout}{r.stderr}")
    return r

def current_branch():
    return git(["symbolic-ref", "--short", "HEAD"]).stdout.strip()

def has_commits():
    return git(["rev-parse", "--verify", "HEAD"], check=False).returncode == 0

def read(path):
    with open(path, "rb") as f:
        return f.read()

def show_remote_file(branch, fname):
    """Return bytes of fname on origin/branch, or None if absent."""
    r = git(["show", f"origin/{branch}:{fname}"], check=False)
    return r.stdout.encode() if r.returncode == 0 else None

def ts():
    return datetime.now().strftime("%Y%m%d-%H%M%S")

def ensure_stub(cfg):
    """Make ~/.claude/CLAUDE.md a one-line `@<import>` stub that pulls in AGENTS.md.
    Idempotent; backs up any pre-existing content before replacing it."""
    stub = cfg.get("stub")
    if not stub:
        return
    path = os.path.expanduser(stub["path"])
    line = "@" + stub["import"]
    if os.path.exists(path) and read(path).decode(errors="replace").strip() == line:
        return
    if os.path.exists(path):
        bak = f"{path}.bak.{ts()}"
        shutil.copy2(path, bak)
        print(f"  backed up {path} -> {bak}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(line + "\n")
    print(f"  wrote stub {path} -> {line}")


# ---------- commands ----------

def cmd_init(args):
    if load_config() and not args.force:
        die("Already initialized. Config at " + CONFIG_PATH +
            "\nUse --force to re-clone with a new URL.", 1)
    url = args.repo_url.strip()
    if not url:
        die("--repo-url is required for init.")
    if os.path.exists(REPO):
        shutil.rmtree(REPO)
    os.makedirs(BASE, exist_ok=True)
    print(f"Cloning {url} ...")
    git(["clone", url, REPO], cwd=BASE)

    # Pick the canonical filename in the repo.
    if args.repo_file:
        repo_file = args.repo_file
    elif os.path.exists(os.path.join(REPO, "AGENTS.md")):
        repo_file = "AGENTS.md"
    elif os.path.exists(os.path.join(REPO, "CLAUDE.md")):
        repo_file = "CLAUDE.md"
    else:
        repo_file = "AGENTS.md"  # empty repo: created on first push

    targets = args.targets or DEFAULT_TARGETS
    cfg = {
        "repo_url": url,
        "repo_file": repo_file,
        "targets": targets,
        "push_source": args.push_source or targets[-1],
        "stub": DEFAULT_STUB,
    }
    save_config(cfg)
    ensure_stub(cfg)
    exists = os.path.exists(os.path.join(REPO, repo_file))
    print("Initialized.")
    print(f"  repo_file   : {repo_file}" + ("" if exists else "  (not in repo yet — will be created on first push)"))
    print(f"  targets     : {', '.join(targets)}")
    print(f"  push_source : {cfg['push_source']}")
    print(f"  stub        : {cfg['stub']['path']} -> @{cfg['stub']['import']}")

def _require_cfg():
    cfg = load_config()
    if not cfg:
        die("Not initialized. Run: sync_agent_rules.py init --repo-url <URL>", 2)
    if not os.path.isdir(os.path.join(REPO, ".git")):
        die("Working copy missing. Re-run init --force.", 2)
    return cfg

def cmd_pull(args):
    cfg = _require_cfg()
    branch = current_branch()
    git(["fetch", "origin"])
    if git(["rev-parse", "--verify", f"origin/{branch}"], check=False).returncode != 0:
        die(f"Repo is empty (no '{branch}' branch on remote yet) — nothing to pull. Push first.", 1)
    git(["reset", "--hard", f"origin/{branch}"])
    git(["clean", "-fd"])

    src = os.path.join(REPO, cfg["repo_file"])
    if not os.path.exists(src):
        die(f"Repo has no '{cfg['repo_file']}' yet — nothing to pull. Push first.", 1)
    new = read(src)

    changed, unchanged = [], []
    for t in cfg["targets"]:
        t = os.path.expanduser(t)
        if os.path.exists(t) and read(t) == new:
            unchanged.append(t)
            continue
        if os.path.exists(t):
            bak = f"{t}.bak.{ts()}"
            shutil.copy2(t, bak)
            print(f"  backed up {t} -> {bak}")
        os.makedirs(os.path.dirname(t), exist_ok=True)
        with open(t, "wb") as f:
            f.write(new)
        changed.append(t)

    ensure_stub(cfg)

    if changed:
        print("Updated: " + ", ".join(changed))
    if unchanged:
        print("Already current: " + ", ".join(unchanged))
    if not changed:
        print("Everything already up to date.")

def cmd_push(args):
    cfg = _require_cfg()
    source = os.path.expanduser(args.source or cfg["push_source"])
    if not os.path.exists(source):
        die(f"Push source not found: {source}")
    new = read(source)

    branch = current_branch()
    git(["fetch", "origin"])

    remote_exists = git(["rev-parse", "--verify", f"origin/{branch}"], check=False).returncode == 0
    if remote_exists:
        # Is the remote ahead of our working copy (someone else pushed since last sync)?
        behind = int(git(["rev-list", "--count", f"HEAD..origin/{branch}"]).stdout.strip() or "0") \
                 if has_commits() else 1
        remote_bytes = show_remote_file(branch, cfg["repo_file"])
        if behind and remote_bytes is not None and remote_bytes != new and not args.overwrite_remote:
            print("CONFLICT: the remote has changes your local copy doesn't have.")
            print(f"  Remote '{cfg['repo_file']}' differs from what you're about to push.")
            print("  Options: pull first and merge by hand, or re-run push with --overwrite-remote")
            print("           to make your local version win.")
            print("--- git diff (remote -> your source) ---")
            # show a readable diff between remote and the source we'd push
            _print_diff(remote_bytes, new, "remote", "local")
            sys.exit(3)
        # Align working copy to latest remote before applying our change.
        git(["reset", "--hard", f"origin/{branch}"])
        git(["clean", "-fd"])

    dest = os.path.join(REPO, cfg["repo_file"])
    with open(dest, "wb") as f:
        f.write(new)

    if not git(["status", "--porcelain"]).stdout.strip():
        print("Nothing to push — remote already matches your source.")
        return

    git(["add", cfg["repo_file"]])
    git(["commit", "-m", f"sync {cfg['repo_file']} from {os.path.basename(source)} ({ts()})"])
    push = git(["push", "origin", f"HEAD:{branch}"], check=False)
    if push.returncode != 0:
        out = (push.stdout + push.stderr)
        if "non-fast-forward" in out or "rejected" in out:
            git(["reset", "--hard", "HEAD~1"], check=False)
            print("CONFLICT: remote moved during push. Re-run push (it will re-check).")
            sys.exit(3)
        die("push failed:\n" + out)
    print(f"Pushed {cfg['repo_file']} to {cfg['repo_url']} ({branch}).")

def _print_diff(a_bytes, b_bytes, a_name, b_name):
    import difflib
    a = a_bytes.decode(errors="replace").splitlines()
    b = b_bytes.decode(errors="replace").splitlines()
    diff = list(difflib.unified_diff(a, b, fromfile=a_name, tofile=b_name, lineterm=""))
    print("\n".join(diff[:200]) if diff else "(identical)")

def cmd_status(args):
    cfg = load_config()
    if not cfg:
        print("Not initialized. Run init --repo-url <URL> first.")
        return
    print(f"repo_url    : {cfg['repo_url']}")
    print(f"repo_file   : {cfg['repo_file']}")
    print(f"push_source : {cfg['push_source']}")
    branch = current_branch()
    fetch = git(["fetch", "origin"], check=False)
    if fetch.returncode != 0:
        print("remote      : UNREACHABLE (" + (fetch.stderr.strip().splitlines() or ["?"])[-1] + ")")
        return
    remote_bytes = show_remote_file(branch, cfg["repo_file"])
    print(f"\nLocal targets vs remote '{cfg['repo_file']}':")
    for t in cfg["targets"]:
        t = os.path.expanduser(t)
        if not os.path.exists(t):
            state = "MISSING locally"
        elif remote_bytes is None:
            state = "remote empty"
        elif read(t) == remote_bytes:
            state = "in sync"
        else:
            state = "DIFFERS from remote"
        print(f"  {t}: {state}")
    # local divergence between the two targets themselves
    existing = [os.path.expanduser(t) for t in cfg["targets"] if os.path.exists(os.path.expanduser(t))]
    if len(existing) >= 2 and len({read(t) for t in existing}) > 1:
        print("\nNote: your local targets differ from EACH OTHER — decide which to push as source.")

    stub = cfg.get("stub")
    if stub:
        path = os.path.expanduser(stub["path"])
        line = "@" + stub["import"]
        if not os.path.exists(path):
            state = "MISSING (run pull/init to create)"
        elif read(path).decode(errors="replace").strip() == line:
            state = f"stub -> {line}"
        else:
            state = "NOT a stub (holds other content)"
        print(f"\nCLAUDE.md stub:\n  {path}: {state}")


def main():
    p = argparse.ArgumentParser(description="Sync user-level CLAUDE.md/AGENTS.md with a git repo.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init")
    pi.add_argument("--repo-url", required=True)
    pi.add_argument("--repo-file", help="canonical filename in the repo (auto-detected if omitted)")
    pi.add_argument("--targets", nargs="+", help="local files to overwrite on pull")
    pi.add_argument("--push-source", help="local file to push (default: last target)")
    pi.add_argument("--force", action="store_true")
    pi.set_defaults(func=cmd_init)

    pp = sub.add_parser("pull"); pp.set_defaults(func=cmd_pull)

    ph = sub.add_parser("push")
    ph.add_argument("--source", help="override the local file to push")
    ph.add_argument("--overwrite-remote", action="store_true",
                    help="make local win even if remote has diverged")
    ph.set_defaults(func=cmd_push)

    ps = sub.add_parser("status"); ps.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
