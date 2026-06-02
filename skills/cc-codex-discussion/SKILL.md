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
`--scratch` puts the transcript under `tempfile.gettempdir()`, i.e. **outside** Codex's actual
workspace (`resolveWorkspaceRoot(--cwd)`, which is the git root for code topics). **This is what
makes round 1 independent — physically, not by prompt discipline.**

Form **CC's own independent analysis** from the raw materials (don't pre-guess what Codex will
say, don't pick an attack surface) and append it as `cc·round 1`:
```
python "<SK>" append "$F" --role cc --round 1 <<'EOF'
<CC's independent analysis of the raw materials>
EOF
```
> Defense-in-depth (optional): if worried Codex might read absolute paths outside its workspace,
> delay this append until after Codex's round-1 reply returns. Not required — temp isolation suffices.

**Read scope (both templates):** point Codex's `--cwd` at the git root that holds the raw
materials (code topics → repo root; pure-design → the repo holding the spec/plan). The read
scope must **never be narrower than the source materials** — what's forbidden is *unrelated*
repo exploration, not reading the materials CC pointed at. Never pass `--write`.

### 2a. Codex round 1 — independent investigation (read-only, reply on stdout)
**No `--resume-last`. The prompt MUST NOT contain `CC_TURN` / `delta` / "respond to it" /
`### AGREEMENT`** — round 1 is not adversarial yet.
```
LOG=$(mktemp)
REPLY=$(node "$CODEX" task [--cwd "<materials-repo-root>"] "$(cat <<EOF
You and Claude Code are independently reviewing a proposal/change. Reply in <user's language>.
Raw materials (open and read them yourself — do not assume CC relayed everything):
- <pointer: branch X vs base / PR #N / spec path / plan.md path>
- baseline anchor: <base commit / CL number>
Your role: an evidence-driven independent investigator — if you can run read commands / read the
real repo, do it. Prefer verification that does NOT write the workspace; if a test fails because
of read-only write blocks, record it as an environment limitation, not as a fact about the code.

[VCS intake — REQUIRED, output this section first] List the commands you actually ran and key
output: git status --short, baseline, git diff --stat / diff, and how you handled untracked (??)
files. If status shows ??, state that you read those untracked files OR ask CC for a snapshot. If
git is unavailable (safe.directory / permissions / non-git VCS), say so and ask for a snapshot.

Then give your OWN independent deep analysis: key risks, overlooked points, feasibility. Back
every conclusion with file:line / command output / a reproducible counter-example.
EOF
)" 2>"$LOG")
```

### 2b. Codex round N≥2 — adversarial (inject CC's latest turn)
**Round 2 = symmetric cross-review (mandatory).** Round 2 is the first time the two independent
round-1 takes meet. Codex already holds its own `codex r1` in warm context (`--resume-last`) but
has **never seen `cc r1`** — so on round 2 inject `cc r1` explicitly and make Codex cross-review
it. This closes the asymmetry where CC reviews Codex's r1 but Codex only ever sees CC's rebuttal.
For round ≥ 3 CC's positions already flow through the turns — inject only the latest turn.
```
CC_TURN=$(python "<SK>" delta "$F")              # CC's latest turn (= cc r2 on round 2)
CC_R1=$(python "<SK>" delta "$F" --role cc --round 1)   # round 2 ONLY — CC's independent take
LOG=$(mktemp)
REPLY=$(node "$CODEX" task --resume-last [--cwd "<materials-repo-root>"] "$(cat <<EOF
Continue the adversarial review. Reply in <user's language>. Speak with execution evidence; back
every objection with file:line / output / repro. Concede the points that are correct.

[Round 2 only] Below is CC's round-1 INDEPENDENT analysis — you wrote your own r1 without seeing
it. Cross-review it against your own r1: which of CC's points do you confirm, which do you refute
(with evidence), and — most important — what did CC flag that you missed?
---
${CC_R1}
---

This is Claude Code's latest turn; respond to it:
---
${CC_TURN}
---
Output only your reply body (recorded verbatim). If no substantive objections remain and the
debate can converge, add a final line: ### AGREEMENT
EOF
)" 2>"$LOG")
```
Drop the `CC_R1` block and its `[Round 2 only]` paragraph for round ≥ 3.
stderr goes to `$LOG` — **do not** use `2>/dev/null`, or there is nothing to surface on failure.

**Turn validity — pass all three before appending:**
- **Transport** — `node` exits zero, doesn't time out, `$REPLY` is non-empty (`append` refuses an empty block as a backstop).
- **Environment** — the reply shows no block (`Permission denied`, `command not found`, "I don't have access…"); a blocked environment makes the turn untrustworthy.
- **VCS intake (round 1, mandatory)** — `$REPLY` contains the `VCS intake` section and shows it actually obtained the diff (incl. untracked handling). Missing section / failed commands / unhandled untracked → **invalid turn**.

Any gate fails → **don't append, don't advance the round.** Surface `$LOG` (the stderr) plus the
failing command/path to the user; let them decide (retry, switch to snapshot, change `--cwd`,
continue without Codex, or abort). **If git self-fetch failed or the VCS is Perforce → fall back
to a snapshot:** CC supplies the `git diff`/`git status` content, or for Perforce the
`p4 describe -du/-ds` diff content / shelved diff (a CL number + file list is *not* enough).

Once all gates pass, **CC's first action is to append the reply verbatim**, before any analysis:
```
printf '%s\n' "$REPLY" | python "<SK>" append "$F" --role codex --round N
```

**Resume-drift guard (round ≥ 2):** if another Codex task may be running, or Codex's reply is
off-context, drop `--resume-last` and instead feed the **exact blocks** in the prompt:
`delta "$F" --role cc --round 1`, `delta "$F" --role codex --round 1`, the current ledger / open
crux, and CC's latest turn. Only if tokens are tight, fall back to a CC-written summary labelled
as such — never make "CC's summary of Codex r1" the default; it re-compresses Codex's independent
stance.

### 3. CC rebuttal + ledger (round N+1)
Update the ledger: mark each objection accepted/rejected/deferred **with evidence**, verifying
Codex's findings against the project spec/decisions before accepting. Engage the *strongest* open
objection honestly (no performative agreement). Append CC's turn (`--role cc --round N+1`), then loop
to **§2b** — unless converged or the cap is hit.

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
- Codex read-only can still execute read commands, so repo grounding works without `--write`.
