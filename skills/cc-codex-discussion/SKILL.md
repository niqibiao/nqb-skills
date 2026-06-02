---
name: cc-codex-discussion
description: Run a turn-by-turn adversarial discussion between Claude Code and the Codex CLI through a shared markdown file. Claude Code processes the user's requirement + context, seeds a discussion file, then drives multiple rounds where it and Codex stress-test each other's positions until they converge on a high-confidence conclusion for the user to sign off. Use when the user wants Claude Code and Codex to debate/co-review/stress-test a design, requirement, plan, bug, or decision together (e.g. "discuss this with codex", "have codex debate this plan", "pull codex in to co-review this PR", "get a second opinion from codex by debating it", "cross-model design review").
---

# CC ↔ Codex Discussion

Claude Code (CC) and the Codex CLI debate a topic through one shared markdown file
until they reach a **high-confidence, evidence-backed conclusion**.

**Architecture (decided, do not re-derive):**
- **CC is the sole writer + loop driver.** CC writes every block. Codex runs **read-only**,
  returns its reply on **stdout**, and CC appends that reply. This removes write races and
  shrinks Codex's blast radius to zero.
- **Round 1 is independent.** Both sides analyze the raw materials on their own — Codex does
  *not* see CC's round-1 position (enforced physically, see Workflow §1). From round 2 they go
  adversarial. This kills the old anchoring where CC framed the attack surface and Codex reacted.
- **Codex memory persists** across rounds via `--resume-last` (warm broker session); from round 2
  only the *latest* turn is fed each round.
- The blocking `node … task` call **is** the turn synchronization — no file-watching needed.

Let `SK` = `<this skill dir>/scripts/discuss.py`, run with `python`.

## Roles — play to each model's strengths

Frame the prompts so each side does what it is best at:

- **Codex (GPT-5-codex) = execution-grounded.** It can run commands, read the real repo, run
  tests. Demand that every claim be **grounded in evidence** — a command it ran, a `file:line`,
  a concrete repro/counter-example — never vibes. This is where Codex beats CC. **Round 1 it is
  an independent investigator** (no CC position, explores the raw materials itself); **round 2+
  it is the adversary**.
- **CC (Claude/Opus) = architect, synthesizer, arbiter.** Holistic design judgment, weighing
  trade-offs, alignment with the project's spec/decision-log, user intent, and the final write-up.
  Before accepting any Codex finding, CC **verifies it against the project spec/decisions**
  (grep `docs/` `spec/` `ADR/` decision tables) — reviewers often misapply mode-A rules to mode-B.

## High-confidence conclusion protocol

A conclusion is only as trustworthy as its weakest unchallenged claim. Enforce:

1. **Open-objections ledger.** Maintain a `## Ledger` section CC rewrites each round:
   one row per objection — `id | raised-by | claim | status(open/accepted/rejected/deferred) | evidence/reason`.
2. **Evidence tag per claim.** Codex claims cite *what it ran / observed*; CC claims cite *spec ref or reasoning*.
3. **Adversarial verification before commit.** A claim may enter the conclusion as *confirmed*
   only after the other side explicitly challenged it and it survived. A load-bearing claim that
   was never challenged gets one explicit verification pass first.
4. **Conclusion gate.** CC may write an **agreed** conclusion only when the ledger has **zero open**
   rows. Any open row → the conclusion must be "**unresolved**: positions + crux + recommended decision for the user."
5. **Stated confidence.** Every conclusion ends with `Confidence: high|medium|low`, the residual
   risks, and "what would change this answer." No bare "looks good."

## Prerequisites

Codex CLI installed + authenticated (`/codex:setup`). Resolve the runtime:
```
CODEX=$(python "<SK>" codex-bin)   # prints codex-companion.mjs path, or empty
```
Empty → stop; tell the user to `npm i -g @openai/codex` and run `/codex:setup`.

## Protocol mechanics

- File: created in the **system temp dir** during the run (`new --scratch`, so it sits outside
  Codex's workspace and stays invisible during round-1 independent analysis); copied back to
  `<repo-root>/cc-codex-discussion-history/` after sign-off for audit. Path: `<temp>/cc-codex-discussion-history/YYYYMMDD-HHMMSS-<slug>.md`.
- Each turn block: `## CC · Round N` / `## Codex · Round N`, body, then a marker line
  `<!-- DONE role=cc round=N -->`. `append` writes body + marker atomically.
- `delta <file>` → body of the last block; `delta <file> --role R --round N` → that exact block.
  The parser is **fail-closed**: malformed/duplicate/out-of-order blocks make it exit non-zero.
- `check <file>` validates the whole transcript (run it before writing the conclusion).
- Discussion **language matches the user's input**. Round cap: **6**.

## Workflow

### 1. Seed (CC, round 1) — independent, mutually invisible
Process the user's requirement. Create the transcript in the **system temp dir** with a
**neutral slug/topic** (the topic is written to disk — an anchoring title leaks your framing):
```
F=$(python "<SK>" new "<neutral-slug>" --scratch --topic "<neutral one-line topic>")
```
`--scratch` puts the transcript outside Codex's actual workspace (`resolveWorkspaceRoot(--cwd)` =
the git root for code topics), which is what makes round 1 independent — physically, not by prompt.

Form **CC's own independent analysis** from the raw materials (don't pre-guess what Codex will
say, don't pick an attack surface) and append it as `cc·round 1`:
```
python "<SK>" append "$F" --role cc --round 1 <<'EOF'
<CC's independent analysis of the raw materials>
EOF
```

### 2. Codex turn (every round N) — read-only, reply on stdout
**Call shape (identical every round).** `--cwd` at the git root holding the raw materials (code
topics → repo root; pure-design → the spec/plan's repo); read scope must **never be narrower than
the source materials** (forbidden is *unrelated* exploration, not reading what CC pointed at).
Never `--write`. `--resume-last` only from round 2. stderr → `$LOG` (**never `2>/dev/null`**, or
there's nothing to surface on failure):
```
LOG=$(mktemp)
REPLY=$(node "$CODEX" task [--resume-last] [--cwd "<materials-repo-root>"] "<prompt body>" 2>"$LOG")
```
The prompt body differs by round:

**Round 1 — independent investigation.** No `--resume-last`. MUST NOT contain `CC_TURN` /
"respond to it" / `### AGREEMENT` (not adversarial yet):
```
You and Claude Code are independently reviewing a proposal/change. Reply in <user's language>.
Raw materials (open and read them yourself — don't assume CC relayed everything):
- <pointer: branch X vs base / PR #N / spec path / plan.md path>; baseline: <base commit / CL number>
Role: evidence-driven independent investigator — run read commands / read the real repo. Prefer
checks that don't write the workspace; a test failing on a read-only write block is an environment
limitation, not a fact about the code.
[VCS intake — REQUIRED, output first] commands you ran + key output: git status --short, baseline,
git diff --stat / diff, and how you handled untracked (??) files. If status shows ??, state you
read them OR ask for a snapshot. If git is unavailable (safe.directory / perms / non-git VCS), say
so and ask for a snapshot.
Then your OWN deep analysis: risks, overlooked points, feasibility. Cite file:line / output / repro.
```

**Round ≥ 2 — adversarial.** The invariant: **inject every CC turn Codex hasn't seen yet** (via
`delta`). On round 2 that's *two* turns — `cc r1` (Codex wrote its own r1 blind to it) and the
latest `cc r2` — so round 2 is a symmetric cross-review; from round 3 on it's just the latest:
```
CC_TURN=$(python "<SK>" delta "$F")                    # latest CC turn
CC_R1=$(python "<SK>" delta "$F" --role cc --round 1)  # round 2 only
```
```
Continue the adversarial review. Reply in <user's language>. Cite file:line / output / repro;
concede what's correct.
[round 2 only — cross-review the take you never saw] CC's round-1 independent analysis:
---
${CC_R1}
---
Which of CC's points do you confirm, which do you refute (evidence), and what did CC catch that you missed?
This is CC's latest turn; respond to it:
---
${CC_TURN}
---
Output only your reply body (recorded verbatim). If no substantive objections remain and the
debate can converge, end with: ### AGREEMENT
```

**Turn validity — gate before appending, every round:**
- **Transport** — `node` exits zero, doesn't time out, `$REPLY` non-empty (`append` refuses an empty *or* out-of-order block as a backstop — a skipped turn is blocked at write time, not at `check`).
- **Environment** — no block sign (`Permission denied`, `command not found`, "I don't have access…").
- **VCS intake** — round 1 only (the one turn where Codex must prove it obtained the materials itself): `$REPLY` has the `VCS intake` section and shows it got the diff (incl. untracked). Missing / failed / unhandled untracked → **invalid turn**.

Any gate fails → **don't append, don't advance.** Surface `$LOG` + the failing command/path; let
the user decide (retry / snapshot / change `--cwd` / continue without Codex / abort). **If git
self-fetch failed or the VCS is Perforce → snapshot fallback:** CC supplies the `git diff`/`git
status` content, or for Perforce `p4 describe -du/-ds` / shelved diff (a CL number + file list is
*not* enough).

Once gates pass, **CC's first action is to append the reply verbatim** (every round, N = the
current round), before any analysis:
```
printf '%s\n' "$REPLY" | python "<SK>" append "$F" --role codex --round N
```

**Resume-drift guard (round ≥ 2):** if another Codex task may be running, or the reply is
off-context, drop `--resume-last` and feed the exact blocks in the prompt instead (`delta` for
`cc r1`, `codex r1`, the ledger / open crux, and CC's latest turn) — deterministic over warm
context. Fall back to a *labelled* CC summary only if tokens are tight; never default to "CC's
summary of Codex r1" (it re-compresses Codex's independent stance).

### 3. CC rebuttal + ledger (round N+1)
Update the ledger: mark each objection accepted/rejected/deferred **with evidence**, verifying
Codex's findings against the project spec/decisions before accepting. Engage the *strongest* open
objection honestly (no performative agreement). Append CC's turn (`--role cc --round N+1`), then loop
to **§2** (round ≥ 2 body) — unless converged or the cap is hit.

Convergence = both sides' latest turns carry `### AGREEMENT` **and** the ledger has zero open rows.

### 4. Conclude + sign-off
Run `python "<SK>" check "$F"` (must pass). Append `## Conclusion`: the agreed answer (or, per
the gate, the unresolved positions + crux + recommended decision), the resolved ledger, then
`Confidence: …` + residual risks + "what would change this." Present it to the **user for sign-off** —
never close silently.

**Audit retention (after sign-off):** the transcript is in temp; independence no longer matters.
Copy the final file back into the repo's audit dir and tell the user the retained path:
```
mkdir -p "<repo-root>/cc-codex-discussion-history"
cp "$F" "<repo-root>/cc-codex-discussion-history/"
```

## Notes
- `python "<SK>" last "$F"` → `{role, round, blocks}` if you lose track.
- `--resume-last` resumes the repo's latest Codex task thread; run rounds back-to-back so an
  unrelated job isn't picked up (see the drift guard).
