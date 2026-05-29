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
- **Codex memory persists** across rounds via `--resume-last` (warm broker session), so it
  effectively stays alive for the whole discussion; only the *latest* turn is fed each round.
- The blocking `node … task` call **is** the turn synchronization — no file-watching needed.

Let `SK` = `<this skill dir>/scripts/discuss.py`, run with `python`.

## Roles — play to each model's strengths

Frame the prompts so each side does what it is best at:

- **Codex (GPT-5-codex) = execution-grounded adversary.** It can run commands, read the real
  repo, run tests. Demand that every objection be **grounded in evidence** — a command it ran,
  a `file:line`, a concrete repro/counter-example — never vibes. This is where Codex beats CC.
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

- File: `<cwd>/cc-codex-discussion-history/YYYYMMDD-HHMMSS-<slug>.md` (made by `new`).
- Each turn block: `## CC · Round N` / `## Codex · Round N`, body, then a marker line
  `<!-- DONE role=cc round=N -->`. `append` writes body + marker atomically.
- `delta <file>` → body of the last block; `delta <file> --role R --round N` → that exact block.
  The parser is **fail-closed**: malformed/duplicate/out-of-order blocks make it exit non-zero.
- `check <file>` validates the whole transcript (run it before writing the conclusion).
- Discussion **language matches the user's input**. Round cap: **6**.

## Workflow

### 1. Seed (CC, round 1)
Process the user's requirement; gather relevant context (Codex can't see this chat). Then:
```
F=$(python "<SK>" new "<slug>" --topic "<one-line topic>")
```
Write CC's opening — topic, the context Codex needs, CC's initial position + reasoning, and the
specific attack surface you want challenged — and append it (heredoc for multi-line):
```
python "<SK>" append "$F" --role cc --round 1 <<'EOF'
<opening text>
EOF
```

### 2. Codex turn (round N) — read-only, reply on stdout
Pick the read scope by topic type (decision B): **code/bug topics** → run at repo root so Codex can
inspect code; **pure design topics** → add `--cwd "<…/cc-codex-discussion-history>"` to limit its
read scope. Never pass `--write`. Add `--resume-last` for **round ≥ 2**. Embed CC's latest turn inline.
```
CC_TURN=$(python "<SK>" delta "$F")          # CC's just-written turn
REPLY=$(node "$CODEX" task [--resume-last] [--cwd "<scope>"] "$(cat <<EOF
You and Claude Code are running an adversarial stress-test of a proposal. Reply in <user's language>.
Your role: the evidence-grounded adversary — if you can run commands / read the real repo, do it; back every objection with a file:line, command output, or a reproducible counter-example, never vibes. Explicitly concede the points that are correct.
This is Claude Code's latest turn; respond to it:
---
${CC_TURN}
---
Output only your reply body (it will be recorded verbatim). If you believe there are no substantive objections left and the debate can converge, add a final line: ### AGREEMENT
EOF
)" 2>/dev/null)
```
Progress goes to stderr; `2>/dev/null` leaves a clean reply on stdout. Then **CC** records it:
```
printf '%s\n' "$REPLY" | python "<SK>" append "$F" --role codex --round N
```
**Failure handling (item 4):** if the `node` call exits non-zero, times out, or `$REPLY` is empty,
**do not** append and **do not** advance the round. Surface the stderr to the user and offer
retry / abort / continue-with-summary. (`append` refuses an empty block as a backstop.)

**Resume-drift guard (item 5):** if another Codex task may be running in this repo, or Codex's reply
doesn't engage CC's latest turn (off-context), drop `--resume-last` and instead prepend a compact
running summary of the debate to the prompt — deterministic mode over warm-context efficiency.

### 3. CC rebuttal + ledger (round N+1)
Update the ledger: mark each objection accepted/rejected/deferred **with evidence**, verifying
Codex's findings against the project spec/decisions before accepting. Engage the *strongest* open
objection honestly (no performative agreement). Append CC's turn (`--role cc --round N+1`), then loop
to step 2 — unless converged or the cap is hit.

Convergence = both sides' latest turns carry `### AGREEMENT` **and** the ledger has zero open rows.

### 4. Conclude + sign-off
Run `python "<SK>" check "$F"` (must pass). Append `## Conclusion`: the agreed answer (or, per
the gate, the unresolved positions + crux + recommended decision), the resolved ledger, then
`Confidence: …` + residual risks + "what would change this." Present it to the **user for sign-off** —
never close silently.

## Notes
- `python "<SK>" last "$F"` → `{role, round, blocks}` if you lose track.
- `--resume-last` resumes the repo's latest Codex task thread; run rounds back-to-back so an
  unrelated job isn't picked up (see the drift guard).
- Codex read-only can still execute read commands, so repo grounding works without `--write`.
