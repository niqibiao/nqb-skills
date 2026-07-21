#!/usr/bin/env python3
"""
Snapshot a zellij session's tabs + the Claude Code session running in each,
then regenerate a zellij layout that resumes every Claude session on restore.

Core idea: don't reinvent zellij's layout format. Take zellij's own
`dump-layout`, and for every `pane command="claude"` block, resolve which
Claude session is *currently* active in that pane's cwd and rewrite the pane's
args to `--resume <session-id>`. Restoring is then just `zellij --layout <file>`.

How the current session id is found: Claude Code writes each session to
~/.claude/projects/<cwd-with-slashes-as-dashes>/<session-uuid>.jsonl. The most
recently modified .jsonl in that project dir is the session currently in use.
(lsof can't be used — claude doesn't hold the jsonl handle open.)

Usage:
  snapshot.py save    [--session NAME]     # default: $ZELLIJ_SESSION_NAME
  snapshot.py restore [--session NAME] [--new-name NAME] [--print]
  snapshot.py show    [--session NAME]     # print the saved manifest
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
SNAP_ROOT = os.path.join(HOME, ".claude", "zellij-snapshots")
PROJECTS = os.path.join(HOME, ".claude", "projects")
IS_WIN = sys.platform == "win32"


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


# --- Windows helpers -------------------------------------------------------
# POSIX behaviour is unchanged: these are no-ops on non-Windows paths (which
# contain no backslashes and are already case-canonical).

def _deescape_kdl_path(p):
    r"""zellij dumps a cwd as a KDL string literal, so a Windows path comes out
    double-escaped (C:\Users -> C:\\Users). Undo that. No-op on POSIX."""
    return p.replace("\\\\", "\\") if p else p


def _kdl_escape(s):
    r"""Backslashes in a KDL string literal must be doubled or zellij's parser
    rejects the layout (\U in C:\Users is an invalid escape). No-op on POSIX."""
    return s.replace("\\", "\\\\") if IS_WIN else s


def _norm(p):
    """Canonical form for comparing two cwds. normcase folds Windows' case- and
    separator-insensitivity (and is the identity on POSIX), so the same dir seen
    from a pid file and from dump-layout compares equal."""
    return os.path.normcase(os.path.normpath(p)) if p else p


def _cim_claude_procs():
    """Windows: {pid: command line} for every live claude.exe, in one call.
    Replaces both `pgrep` (liveness) and per-pid `ps -o command=` (flags)."""
    script = ("@(Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
              "Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress")
    r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    out = (r.stdout or "").strip()
    if not out:
        return {}
    try:
        data = json.loads(out)
    except Exception:
        return {}
    if data is None:
        return {}
    if isinstance(data, dict):        # ConvertTo-Json emits a bare object for 1
        data = [data]
    procs = {}
    for d in data:
        pid = d.get("ProcessId")
        if pid is not None:
            procs[int(pid)] = d.get("CommandLine") or ""
    return procs


def _running_claude_sessions_windows():
    """Windows counterpart of running_claude_sessions(). Same shape, but liveness
    + cmdline come from one CIM call, and — unlike POSIX — non-interactive
    claude.exe (daemon / bg-pty-host / fork-session, which also write runtime
    files) are excluded via the runtime file's `kind` field, so they can't win a
    cwd match and resolve a tab to the wrong session."""
    procs = _cim_claude_procs()
    sessions = []
    for f in glob.glob(os.path.join(HOME, ".claude", "sessions", "*.json")):
        try:
            pid = int(os.path.splitext(os.path.basename(f))[0])
        except ValueError:
            continue
        if pid not in procs:
            continue
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if d.get("kind") != "interactive":
            continue
        if d.get("sessionId") and d.get("cwd"):
            toks = procs.get(pid, "").split()
            flags = []
            skip = False
            for tok in toks[1:]:
                if skip:
                    skip = False
                    continue
                if tok == "--resume":
                    skip = True
                    continue
                if tok in ("--continue", "-c"):
                    # Conflicts with the --resume <sid> we inject on restore:
                    # --continue reopens the *latest* conversation, not this one.
                    continue
                flags.append(tok)
            sessions.append({"pid": pid,
                             "sessionId": d["sessionId"],
                             "cwd": os.path.normpath(d["cwd"]),
                             "started": d.get("startedAt", 0),
                             "flags": flags})
    return sessions


def current_session():
    name = os.environ.get("ZELLIJ_SESSION_NAME")
    if not name:
        sys.exit("Not inside a zellij session (ZELLIJ_SESSION_NAME unset). "
                 "Run this from within the session you want to snapshot, "
                 "or pass --session NAME.")
    return name


def dump_layout(session=None):
    # Windows: an implicit `action dump-layout` returns only the plugin-pane
    # skeleton — no command=/cwd= on the content panes — so we MUST target the
    # session explicitly to get a runtime dump with cwds. POSIX is unchanged.
    if IS_WIN and session:
        cmd = ["zellij", "--session", session, "action", "dump-layout"]
    else:
        cmd = ["zellij", "action", "dump-layout"]
    r = run(cmd)
    if r.returncode != 0:
        if IS_WIN:
            return None   # unreachable session -> caller synthesizes from runtime files
        sys.exit(f"`zellij action dump-layout` failed:\n{r.stderr}")
    out = r.stdout
    # A KDL dump always begins at `layout {`; on Windows zellij may prepend
    # shell-banner noise. Trim it. No-op on a clean POSIX dump (idx == 0).
    idx = out.find("layout {")
    return out[idx:] if idx > 0 else out


def project_dir_for(cwd_abs):
    if IS_WIN:
        # Claude names project dirs by replacing every non-alnum char (drive
        # colon, backslash, dot, space) with '-': C:\Users\niqib -> C--Users-niqib.
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(cwd_abs))
    else:
        slug = cwd_abs.replace("/", "-")
    return os.path.join(PROJECTS, slug)


def latest_session_id(cwd_abs, exclude=frozenset()):
    """Most recently modified .jsonl in the project dir = active session.
    `exclude` skips ids already claimed by another pane, so two tabs sharing
    a cwd resolve to two different sessions instead of colliding on one."""
    files = glob.glob(os.path.join(project_dir_for(cwd_abs), "*.jsonl"))
    files = [f for f in files
             if os.path.splitext(os.path.basename(f))[0] not in exclude]
    if not files:
        return None, None
    latest = max(files, key=os.path.getmtime)
    sid = os.path.splitext(os.path.basename(latest))[0]
    return sid, os.path.getmtime(latest)


def running_claude_sessions():
    """Live claude processes with their CURRENT session, from the runtime files
    ~/.claude/sessions/<pid>.json. This is the source of truth: sessionId here
    reflects the session in use right now (survives --resume-less launches and
    /clear), unlike the layout's launch-time args. Stale files for dead pids
    are filtered out via pgrep."""
    if IS_WIN:
        return _running_claude_sessions_windows()
    r = run(["pgrep", "-x", "claude"])
    live = {int(x) for x in r.stdout.split()} if r.returncode == 0 else set()
    sessions = []
    for f in glob.glob(os.path.join(HOME, ".claude", "sessions", "*.json")):
        try:
            pid = int(os.path.splitext(os.path.basename(f))[0])
        except ValueError:
            continue
        if pid not in live:
            continue
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if d.get("sessionId") and d.get("cwd"):
            # Launch flags from the live process cmdline (minus argv0 and the
            # old --resume <id>), so restore preserves --dangerously-skip-
            # permissions / --model / etc even when the pane's command was
            # mis-discovered as an MCP child.
            cmd = run(["ps", "-o", "command=", "-p", str(pid)]).stdout.strip()
            toks = cmd.split()
            flags = []
            skip = False
            for tok in toks[1:]:
                if skip:
                    skip = False
                    continue
                if tok == "--resume":
                    skip = True
                    continue
                flags.append(tok)
            sessions.append({"pid": pid,
                             "sessionId": d["sessionId"],
                             "cwd": os.path.normpath(d["cwd"]),
                             "started": d.get("startedAt", 0),
                             "flags": flags})
    return sessions


def abs_cwd(top_cwd, pane_cwd):
    if not pane_cwd:
        return top_cwd
    if os.path.isabs(pane_cwd):
        return pane_cwd
    return os.path.normpath(os.path.join(top_cwd, pane_cwd))


def parse_tabs(dump):
    """From `dump-layout`, return [(tab_name, cwd_abs)] using each tab's content
    pane (the first `pane` with a `command=` that isn't a plugin bar). We take
    ONLY the tab name + cwd — never the command — because zellij's command
    discovery mis-reports claude panes as their MCP child (e.g. `npm exec
    @playwright/mcp`). The cwd, however, stays correct, and that's what we
    match on."""
    lines = dump.splitlines()
    top_cwd = HOME
    m = re.search(r'^\s*cwd\s+"([^"]+)"', "\n".join(lines[:5]), re.M)
    if m:
        top_cwd = _deescape_kdl_path(m.group(1))
    tabs = []
    have_content = False
    for line in lines:
        tm = re.search(r'tab\s+name="([^"]+)"', line)
        if tm:
            tabs.append([tm.group(1), None])
            have_content = False
            continue
        if tabs and not have_content and "command=" in line and "plugin" not in line:
            cm = re.search(r'cwd="([^"]+)"', line)
            pane_cwd = _deescape_kdl_path(cm.group(1)) if cm else None
            tabs[-1][1] = abs_cwd(top_cwd, pane_cwd)
            have_content = True
    return [(name, cwd or top_cwd) for name, cwd in tabs if name]


def build_manifest(dump):
    """Resolve each tab to its current claude session by matching the tab's cwd
    against live claude processes (from ~/.claude/sessions/<pid>.json). Returns
    manifest entries with tab, cwd, session_id, args, source."""
    tabs = parse_tabs(dump)
    live = running_claude_sessions()
    by_cwd = {}
    for s in sorted(live, key=lambda x: x["started"]):
        by_cwd.setdefault(_norm(s["cwd"]), []).append(s)
    used_pids = set()
    used_sids = set()
    manifest = []
    for tab, cwd_abs in tabs:
        sid = None
        source = None
        # Default to NO flags: never inject flags a tab didn't originally have.
        # A live match below replaces this with the process's real launch flags;
        # an inferred tab (dead process) restores with just --resume, so it does
        # NOT silently gain --dangerously-skip-permissions it never ran with.
        flags = []
        cand = [s for s in by_cwd.get(_norm(cwd_abs), []) if s["pid"] not in used_pids]
        if cand:
            s = cand[0]
            sid = s["sessionId"]
            used_pids.add(s["pid"])
            flags = s.get("flags") or flags
            source = "process"
        else:
            sid, _ = latest_session_id(cwd_abs, exclude=used_sids)
            source = "inferred" if sid else None
        if sid:
            used_sids.add(sid)
        args = (["--resume", sid] if sid else []) + flags
        manifest.append({"tab": tab, "cwd": cwd_abs, "session_id": sid,
                         "args": args, "source": source})
    return manifest


def _fallback_manifest():
    """Windows fallback for when the session can't be dumped (not discoverable by
    a new client — e.g. its temp registry file was cleaned up while the server
    keeps running). Synthesize one tab per LIVE interactive claude on the machine,
    straight from the runtime files. This is NOT the session's real tab layout —
    it's every live claude — but it preserves the conversations, which is the
    whole point of the snapshot."""
    manifest = []
    for x in running_claude_sessions():
        sid = x["sessionId"]
        name = os.path.basename(x["cwd"].rstrip("\\/")) or x["cwd"]
        manifest.append({"tab": name, "cwd": x["cwd"], "session_id": sid,
                         "args": ["--resume", sid] + list(x["flags"]),
                         "source": "runtime"})
    return manifest


def claude_pane_command():
    """(command, prefix_args) for launching claude in a zellij pane.

    On Windows, zellij command panes hand the child PIPE std handles even though
    a ConPTY console is attached (and is all the pane renders) — claude sees no
    TTY, drops into headless mode, and `--resume` exits immediately. Launch it
    through conwrap.ps1, which rebinds fd 0/1/2 to CONIN$/CONOUT$ (read-write,
    inheritable) before spawning claude, restoring interactive mode."""
    if not IS_WIN:
        return "claude", []
    pwsh = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    wrapper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conwrap.ps1")
    claude = shutil.which("claude") or "claude"
    return pwsh, ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", wrapper, claude]


def minimal_layout(manifest):
    """Build a clean, minimal layout: one tab per entry, each a single claude
    pane with cwd + resume args. No captured pane sizes — so zellij sizes
    everything to the CURRENT terminal (fixes wrong window sizes on restore).

    A default_tab_template with the stock tab-bar/status-bar plugins IS
    included: with --new-session-with-layout zellij uses THIS layout verbatim
    and ignores the user's default layout, so without these the restored
    session comes up with no tab bar — all tabs exist but are invisible and
    un-switchable. The plugins carry no geometry, so terminal-fit still holds."""
    lines = [
        "layout {",
        "    default_tab_template {",
        "        pane size=1 borderless=true {",
        '            plugin location="zellij:tab-bar"',
        "        }",
        "        children",
        "        pane size=2 borderless=true {",
        '            plugin location="zellij:status-bar"',
        "        }",
        "    }",
    ]
    command, prefix = claude_pane_command()
    for t in manifest:
        args = prefix + (t.get("args") or [])
        quoted = " ".join(f'"{_kdl_escape(a)}"' for a in args)
        cwd = f' cwd="{_kdl_escape(t["cwd"])}"' if t.get("cwd") else ""
        lines.append(f'    tab name="{t["tab"]}" {{')
        lines.append(f'        pane command="{_kdl_escape(command)}"{cwd} {{')
        if quoted:
            lines.append(f"            args {quoted}")
        lines.append(f"            start_suspended true")
        lines.append(f"        }}")
        lines.append(f"    }}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def config_layout_path(session):
    """Named-layout path in zellij's config dir. This is CONFIG, not cache:
    zellij only reads it, never overwrites it on serialization. Restore with
    `zellij --session <s> --layout <s>`."""
    if IS_WIN:
        base = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
        return os.path.join(base, "Zellij", "config", "layouts", f"{session}.kdl")
    return os.path.join(HOME, ".config", "zellij", "layouts", f"{session}.kdl")


def cmd_save(args):
    session = args.session or current_session()
    layout = dump_layout(session)
    if layout is None:
        # Windows: session not reachable by a new client. Fall back to a
        # conversation-level snapshot synthesized from runtime files.
        manifest = _fallback_manifest()
        fallback = True
    else:
        manifest = build_manifest(layout)
        fallback = False
    resumable = [m for m in manifest if m["session_id"]]

    # Safety guard: never overwrite good snapshots with an empty/degraded one.
    # A healthy session resolves a session id for (almost) every tab; 0 means
    # the dump had no resolvable claude tabs (e.g. saving from a broken rebuild).
    if not resumable:
        sys.exit(f"✋ Refusing to save: resolved 0 resumable claude tabs in "
                 f"'{session}'.\n   This session isn't healthy (no live claude "
                 f"matched any tab's cwd), and saving would wipe the existing "
                 f"good snapshot.\n   Existing snapshot left untouched.")

    new_layout = minimal_layout(manifest)

    sdir = os.path.join(SNAP_ROOT, session)
    os.makedirs(sdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    # Write the restorable layout (latest + timestamped history).
    with open(os.path.join(sdir, "restore-layout.kdl"), "w") as f:
        f.write(new_layout)
    with open(os.path.join(sdir, f"restore-layout-{stamp}.kdl"), "w") as f:
        f.write(new_layout)

    meta = {"session": session, "saved_at": stamp, "tabs": manifest}
    with open(os.path.join(sdir, "manifest.json"), "w") as f:
        json.dump(meta, f, indent=2)

    # Install as a named layout in zellij's CONFIG dir (never overwritten by
    # serialization), so restore is `zellij --session <s> --layout <s>`.
    clp = config_layout_path(session)
    try:
        os.makedirs(os.path.dirname(clp), exist_ok=True)
        with open(clp, "w") as f:
            f.write(new_layout)
        installed = (f"   ↻ Installed named layout — restore from a plain "
                     f"terminal with:\n       zellij --session {session} "
                     f"--layout {session}")
    except Exception as e:
        installed = f"   ! Could not install named layout: {e}"

    claude_tabs = [m for m in manifest if m["session_id"]]
    print(f"✅ Saved snapshot of session '{session}' ({stamp})")
    if fallback:
        print(f"   ⚠ '{session}' wasn't reachable via zellij — synthesized from live claude")
        print(f"     runtime files: tabs = every live interactive claude on this machine,")
        print(f"     not '{session}'s real tab layout.")
    print(f"   {len(manifest)} claude tab(s), {len(claude_tabs)} with a resumable session:\n")
    for m in manifest:
        sid = m["session_id"] or "(no session found — will start fresh)"
        tag = {"process": "live ✓", "layout-stale": "stale?",
               "inferred": "inferred", "runtime": "live (synth)"}.get(m["source"], "-")
        extra = [a for a in m.get("args", [])
                 if a != "--resume" and a != m["session_id"]]
        print(f"   • {m['tab']:<18} {m['cwd']}")
        print(f"       └─ {sid}  [{tag}]")
        print(f"          args: {' '.join(extra) if extra else '(none)'}")
    synced = installed

    print(f"\n   Layout : {os.path.join(sdir, 'restore-layout.kdl')}")
    print(f"   Manifest: {os.path.join(sdir, 'manifest.json')}")
    print(f"\n{synced}")
    print(f"\n   Or restore manually:  snapshot.py restore --session {session}")


def spawn_background(session):
    """Create <session> detached in the background from its named layout, so a
    plain `zellij attach <session>` reaches it. Non-tty safe — works from a cc
    pane, which cold-start execvp can't. Returns True on success."""
    clp = config_layout_path(session)
    snap = os.path.join(SNAP_ROOT, session, "restore-layout.kdl")
    if os.path.exists(clp):
        layout_ref = session          # named layout in ~/.config/zellij/layouts
    elif os.path.exists(snap):
        layout_ref = snap             # fall back to a file path
    else:
        sys.exit(f"No layout for '{session}'. Run `save` first.")
    # Clear any dead/overwritten same-name session so our layout is used, not a
    # stale resurrection. Skip if we're currently inside that session.
    if os.environ.get("ZELLIJ_SESSION_NAME") != session:
        run(["zellij", "delete-session", session, "--force"])
        if IS_WIN:
            # delete-session returns before the session is actually gone here,
            # so the create below would collide with the old same-name session.
            # Wait until list-sessions no longer shows it.
            for _ in range(20):
                if session not in run(["zellij", "list-sessions"]).stdout:
                    break
                time.sleep(0.25)
    create = ["zellij", "--new-session-with-layout", layout_ref,
              "attach", "--create-background", session]
    if IS_WIN:
        # This call detaches a background process that inherits our stdio; the
        # capturing run() would block forever on a pipe that never closes. Send
        # all three streams to DEVNULL so nothing is inherited to wait on.
        subprocess.run(create, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        run(create)
    return session in run(["zellij", "list-sessions"]).stdout


def cmd_restore(args):
    session = args.session or (os.environ.get("ZELLIJ_SESSION_NAME") or "")
    if not session:
        sys.exit("Pass --session NAME (no zellij session to infer it from).")
    sdir = os.path.join(SNAP_ROOT, session)
    layout = os.path.join(sdir, "restore-layout.kdl")
    manifest_path = os.path.join(sdir, "manifest.json")
    if not os.path.exists(layout):
        sys.exit(f"No snapshot for '{session}' at {layout}. Run `save` first.")

    in_zellij = os.environ.get("ZELLIJ_SESSION_NAME")

    # Inside a DIFFERENT live zellij session: inject the tabs into it via
    # `zellij action` (can't nest a new session). Rare — the normal reboot flow
    # is non-zellij. Run in a fresh empty session to avoid duplicate tabs.
    if in_zellij and in_zellij != session:
        with open(manifest_path) as f:
            meta = json.load(f)
        made = 0
        command, prefix = claude_pane_command()
        for t in meta["tabs"]:
            cmd = ["zellij", "action", "new-tab", "--name", t["tab"]]
            if t.get("cwd"):
                cmd += ["--cwd", t["cwd"]]
            cmd += ["--", command] + prefix + list(t.get("args", []))
            if run(cmd).returncode == 0:
                made += 1
        print(f"✅ Injected {made}/{len(meta['tabs'])} tab(s) into '{in_zellij}'.")
        return

    # Normal path — from a plain terminal OR a cc pane (not inside a zellij
    # session): create the session detached in the background, then attach.
    # Background create is what makes this work from a non-tty cc pane.
    if spawn_background(session):
        print(f"✅ Session '{session}' restored (running in the background).\n"
              f"   Connect with:  zellij attach {session}\n"
              f"   (tabs start suspended — press Enter in each to resume its claude)")
    else:
        sys.exit(f"! Could not restore '{session}'.")


def cmd_spawn(args):
    """Alias for the restore background path: create the session detached from
    its named layout so `zellij attach <session>` reaches it. Kept as a separate
    verb for discoverability; `restore` does the same from a non-zellij cc."""
    session = args.session or (os.environ.get("ZELLIJ_SESSION_NAME") or "")
    if not session:
        sys.exit("Pass --session NAME.")
    if spawn_background(session):
        print(f"✅ Background session '{session}' is ready.\n"
              f"   Attach with:  zellij attach {session}\n"
              f"   (tabs start suspended — press Enter in each to resume its claude)")
    else:
        sys.exit(f"! Could not confirm '{session}' was created.")


def cmd_show(args):
    session = args.session or current_session()
    mf = os.path.join(SNAP_ROOT, session, "manifest.json")
    if not os.path.exists(mf):
        sys.exit(f"No snapshot for '{session}'.")
    with open(mf) as f:
        print(f.read())


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("save", "restore", "show", "spawn"):
        sp = sub.add_parser(name)
        sp.add_argument("--session", help="session name (default: $ZELLIJ_SESSION_NAME)")
    args = p.parse_args()
    {"save": cmd_save, "restore": cmd_restore, "show": cmd_show,
     "spawn": cmd_spawn}[args.cmd](args)


if __name__ == "__main__":
    main()
