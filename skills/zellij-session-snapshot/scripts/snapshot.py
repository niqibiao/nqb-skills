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
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
SNAP_ROOT = os.path.join(HOME, ".claude", "zellij-snapshots")
PROJECTS = os.path.join(HOME, ".claude", "projects")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def current_session():
    name = os.environ.get("ZELLIJ_SESSION_NAME")
    if not name:
        sys.exit("Not inside a zellij session (ZELLIJ_SESSION_NAME unset). "
                 "Run this from within the session you want to snapshot, "
                 "or pass --session NAME.")
    return name


def dump_layout():
    r = run(["zellij", "action", "dump-layout"])
    if r.returncode != 0:
        sys.exit(f"`zellij action dump-layout` failed:\n{r.stderr}")
    return r.stdout


def project_dir_for(cwd_abs):
    return os.path.join(PROJECTS, cwd_abs.replace("/", "-"))


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
        top_cwd = m.group(1)
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
            tabs[-1][1] = abs_cwd(top_cwd, cm.group(1) if cm else None)
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
        by_cwd.setdefault(s["cwd"], []).append(s)
    used_pids = set()
    used_sids = set()
    manifest = []
    for tab, cwd_abs in tabs:
        sid = None
        source = None
        flags = ["--dangerously-skip-permissions"]
        cand = [s for s in by_cwd.get(cwd_abs, []) if s["pid"] not in used_pids]
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
    for t in manifest:
        args = t.get("args") or []
        quoted = " ".join(f'"{a}"' for a in args)
        cwd = f' cwd="{t["cwd"]}"' if t.get("cwd") else ""
        lines.append(f'    tab name="{t["tab"]}" {{')
        lines.append(f'        pane command="claude"{cwd} {{')
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
    return os.path.join(HOME, ".config", "zellij", "layouts", f"{session}.kdl")


def cmd_save(args):
    session = args.session or current_session()
    layout = dump_layout()
    manifest = build_manifest(layout)
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
    print(f"   {len(manifest)} claude tab(s), {len(claude_tabs)} with a resumable session:\n")
    for m in manifest:
        sid = m["session_id"] or "(no session found — will start fresh)"
        tag = {"process": "live ✓", "layout-stale": "stale?",
               "inferred": "inferred"}.get(m["source"], "-")
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
    run(["zellij", "--new-session-with-layout", layout_ref,
         "attach", "--create-background", session])
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
        for t in meta["tabs"]:
            cmd = ["zellij", "action", "new-tab", "--name", t["tab"]]
            if t.get("cwd"):
                cmd += ["--cwd", t["cwd"]]
            cmd += ["--", "claude"] + list(t.get("args", []))
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
