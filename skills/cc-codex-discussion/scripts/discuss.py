#!/usr/bin/env python3
"""Transcript helper for the cc-codex-discussion skill.

CC is the SOLE writer of the shared .md file. Codex runs read-only and returns its
reply on stdout; CC wraps that reply into a block and appends it here. Each turn is a
block:

    ## CC · Round 1
    <body>

    <!-- DONE role=cc round=1 -->

The parser is structural and fail-closed: it pairs each `## {CC|Codex} · Round N`
heading with the matching `<!-- DONE role=.. round=N -->` marker and validates the
sequence (alternating cc/codex, rounds increasing, no duplicates). Non-turn sections
(`# Topic…`, `## Conclusion…`, `## Ledger…`) are ignored by the parser.

Subcommands:
  new <slug> [--topic T] [--scratch]  Create the discussion file; print absolute path.
                                      --scratch: create under the system temp dir (round-1 isolation).
  append <file> --role R --round N    Append stdin as one atomic block (body + marker). Fail-closed
                                      on order: rejects a block that breaks cc/codex alternation.
  delta <file> [--role R --round N]   Print one block's body. Default: the last block.
                                      With --role/--round: that exact block (fail closed).
  check <file>                        Validate the whole transcript; exit 1 + diagnostics
                                      if the block sequence is malformed.
  last <file>                         Print JSON {role,round} of the last block.
  codex-bin                           Print the resolved codex-companion.mjs path (or "").
"""
import argparse
import datetime as _dt
import json
import re
import sys
import tempfile
from pathlib import Path

HEAD_RE = re.compile(r"^## (CC|Codex) · Round (\d+)\s*$")
MARK_RE = re.compile(r"^<!-- DONE role=(cc|codex) round=(\d+) -->\s*$")
DIR_NAME = "cc-codex-discussion-history"


def _marker(role: str, rnd: int) -> str:
    return f"<!-- DONE role={role} round={rnd} -->"


def _slugify(text: str) -> str:
    text = re.sub(r"[^\w-]+", "-", text.strip().lower())
    return text.strip("-")[:40] or "discussion"


def _fail(msg: str) -> None:
    sys.stderr.write(f"discuss.py: {msg}\n")
    sys.exit(1)


def parse_blocks(text: str):
    """Return [{role, round, body}] or raise ValueError (fail closed)."""
    lines = text.splitlines()
    blocks, i, n = [], 0, len(lines)
    while i < n:
        h = HEAD_RE.match(lines[i])
        if not h:
            i += 1
            continue
        role = h.group(1).lower()
        rnd = int(h.group(2))
        # find the matching marker before the next heading
        j = i + 1
        body_lines = []
        marker = None
        while j < n:
            if HEAD_RE.match(lines[j]):
                break
            m = MARK_RE.match(lines[j])
            if m:
                marker = m
                break
            body_lines.append(lines[j])
            j += 1
        if marker is None:
            raise ValueError(f"block '## {h.group(1)} · Round {rnd}' has no DONE marker")
        if (marker.group(1), int(marker.group(2))) != (role, rnd):
            raise ValueError(
                f"heading ({role} r{rnd}) and marker ({marker.group(1)} r{marker.group(2)}) disagree"
            )
        blocks.append({"role": role, "round": rnd, "body": "\n".join(body_lines).strip()})
        i = j + 1
    return blocks


def validate(blocks):
    """Raise ValueError if the sequence violates protocol."""
    expect = [("cc", k) for k in range(1, 999) for _ in (0,)]  # not used; explicit below
    seen = set()
    for idx, b in enumerate(blocks):
        key = (b["role"], b["round"])
        if key in seen:
            raise ValueError(f"duplicate block {b['role']} round {b['round']}")
        seen.add(key)
        # expected: cc r1, codex r1, cc r2, codex r2, ...
        exp_role = "cc" if idx % 2 == 0 else "codex"
        exp_round = idx // 2 + 1
        if (b["role"], b["round"]) != (exp_role, exp_round):
            raise ValueError(
                f"out-of-order block #{idx}: got {b['role']} r{b['round']}, "
                f"expected {exp_role} r{exp_round} (need alternating cc/codex, rounds increasing)"
            )


def cmd_new(args) -> None:
    root = Path(tempfile.gettempdir()) if getattr(args, "scratch", False) else Path.cwd()
    base = root / DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = base / f"{stamp}-{_slugify(args.slug)}.md"
    header = f"# Topic: {args.topic or args.slug}\n\n_started {stamp}_\n\n"
    path.write_text(header, encoding="utf-8")
    print(path.resolve())


def cmd_append(args) -> None:
    body = sys.stdin.read().strip()
    if not body:
        _fail("refusing to append an empty block (read-only Codex failure? do not advance round)")
    path = Path(args.file)
    text = path.read_text(encoding="utf-8")
    # Fail closed on order: the new block must continue the cc/codex alternation with
    # increasing rounds. Catches a skipped turn (e.g. forgetting to append Codex's reply
    # and writing the next CC turn) at write time, not later at `check`.
    try:
        blocks = parse_blocks(text)
        validate(blocks)
    except ValueError as e:
        _fail(f"transcript already malformed; rebuild it before appending ({e})")
    n = len(blocks)
    exp_role, exp_round = ("cc" if n % 2 == 0 else "codex"), n // 2 + 1
    if (args.role, args.round) != (exp_role, exp_round):
        _fail(
            f"out-of-order append: got {args.role} r{args.round}, expected {exp_role} r{exp_round} "
            f"({n} block(s) so far; turns must alternate cc/codex with increasing rounds). "
            f"Did you skip appending the other side's turn?"
        )
    if text and not text.endswith("\n"):
        text += "\n"
    heading = "CC" if args.role == "cc" else "Codex"
    block = f"\n## {heading} · Round {args.round}\n\n{body}\n\n{_marker(args.role, args.round)}\n"
    path.write_text(text + block, encoding="utf-8")


def cmd_delta(args) -> None:
    text = Path(args.file).read_text(encoding="utf-8")
    try:
        blocks = parse_blocks(text)
    except ValueError as e:
        _fail(f"parse failed: {e}")
    if not blocks:
        _fail("no turn blocks found")
    if args.role or args.round:
        if not (args.role and args.round):
            _fail("pass both --role and --round, or neither")
        hits = [b for b in blocks if b["role"] == args.role and b["round"] == args.round]
        if len(hits) != 1:
            _fail(f"expected exactly 1 block for {args.role} r{args.round}, found {len(hits)}")
        sys.stdout.write(hits[0]["body"] + "\n")
    else:
        sys.stdout.write(blocks[-1]["body"] + "\n")


def cmd_check(args) -> None:
    text = Path(args.file).read_text(encoding="utf-8")
    try:
        blocks = parse_blocks(text)
        validate(blocks)
    except ValueError as e:
        _fail(f"invalid transcript: {e}")
    print(json.dumps({"ok": True, "blocks": len(blocks)}))


def cmd_last(args) -> None:
    text = Path(args.file).read_text(encoding="utf-8")
    try:
        blocks = parse_blocks(text)
    except ValueError as e:
        _fail(f"parse failed: {e}")
    last = blocks[-1] if blocks else None
    print(json.dumps({"role": last["role"] if last else None,
                      "round": last["round"] if last else 0,
                      "blocks": len(blocks)}))


def cmd_codex_bin(_args) -> None:
    home = Path.home() / ".claude" / "plugins"
    market = sorted(home.glob("marketplaces/*/plugins/codex/scripts/codex-companion.mjs"))
    if market:
        print(market[0].resolve())
        return
    cache = sorted(home.glob("cache/*/codex/*/scripts/codex-companion.mjs"))
    print(cache[-1].resolve() if cache else "")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("new"); sp.add_argument("slug"); sp.add_argument("--topic", default=""); sp.add_argument("--scratch", action="store_true", help="create the history dir under the system temp dir (tempfile.gettempdir()) instead of cwd, for round-1 isolation"); sp.set_defaults(fn=cmd_new)
    sp = sub.add_parser("append"); sp.add_argument("file"); sp.add_argument("--role", required=True, choices=["cc", "codex"]); sp.add_argument("--round", type=int, required=True); sp.set_defaults(fn=cmd_append)
    sp = sub.add_parser("delta"); sp.add_argument("file"); sp.add_argument("--role", choices=["cc", "codex"]); sp.add_argument("--round", type=int); sp.set_defaults(fn=cmd_delta)
    sp = sub.add_parser("check"); sp.add_argument("file"); sp.set_defaults(fn=cmd_check)
    sp = sub.add_parser("last"); sp.add_argument("file"); sp.set_defaults(fn=cmd_last)
    sp = sub.add_parser("codex-bin"); sp.set_defaults(fn=cmd_codex_bin)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
