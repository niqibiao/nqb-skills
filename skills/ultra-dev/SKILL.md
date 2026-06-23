---
name: ultra-dev
description: Project-agnostic controller that drives one feature slice end-to-end — spec →
  review → plan → review → implement → review → docs → report → merge — as a main-loop
  orchestrator (not a Workflow), wiring together the brainstorming / writing-plans /
  subagent-driven-development skills with risk-tiered adversarial review gates and minimal
  human stops. Use when the user wants the whole pipeline run on a change (e.g. "take this
  end-to-end", "run the full pipeline on X", "spin up a slice and push it to a PR",
  "ultra-dev this"). Classes each slice light/medium/heavy and inserts only the review weight
  that tier warrants; stops to ask only at a real decision threshold or the final merge;
  discovers and delegates to a project's own PR / decision-log / verification / doc-sync /
  domain-review skills, with generic fallbacks. Not tied to any specific project.
---

# ultra-dev — project-agnostic end-to-end controller

Drive a requirement from spec all the way to "you confirm the merge", inserting review gates
sized to the slice's risk. **Mid-run, the only proactive stops are a hardline (a major decision /
public contract / core invariant) and the final merge — the spec and plan gates do not pause by
default.** (Kickoff tier confirmation is the upfront handshake, not a mid-run stop.)

This skill is a **main-loop orchestrator**: the main loop (me) drives it, calling sub-skills,
commands, and subagents stage by stage. It is **not** a Workflow script — a Workflow runs
autonomously to the end and cannot stop to ask you mid-run. Only the post-impl gate may *call* a
Workflow on demand, for parallel review.

**Generic.** No single-project concept is baked in. Every project-specific action (VCS/PR flow,
decision log, verification suite, doc-sync, domain-review persona) is **discovered at runtime —
delegate to the project's own skill** — with a generic fallback when the project ships none. See
[Project bindings](#project-bindings-discovered-at-runtime).

**Announce at kickoff:** say you're using ultra-dev to run the end-to-end pipeline, and post this
slice's risk-tier call for the user to confirm.

## Map

```
brainstorm → spec ──[spec gate]┈┈ fold findings, continue (stop only on a §2 hardline)
   → writing-plans → plan ──[plan gate]┈┈ fold findings, continue (stop only on a §2 hardline)
      → SDD execution (per-task implementer + reviewer; final-round code-review)
         → [post-impl gate] → fixes (small inline / big → stop & ask) → trigger doc-sync → write report
            → (pause · you confirm) → squash-merge + delete branch per the project's VCS flow
```

## Dependencies (host environment)

This skill orchestrates; it does not reimplement. The host must already have:
- **superpowers** — `brainstorming`, `writing-plans`, `subagent-driven-development`,
  `finishing-a-development-branch` (generic VCS fallback).
- **codex** — `codex:adversarial-review`, `codex:review`.
- *(optional)* `cc-codex-discussion` (sibling skill in this marketplace) — the deep post-impl
  gate on medium/heavy slices **and** the heavy spec gate; **if absent, the post-impl gate falls
  back to `codex:review` and the heavy spec gate to `codex:adversarial-review`** (a spec has no
  code, so `codex:review` doesn't apply there).

Project-specific actions (PR / decision / verify / docs / domain review) are always discovered
and delegated at runtime — see [Project bindings](#project-bindings-discovered-at-runtime).

## 0. Kickoff: risk tiering (I judge → you confirm in one line)

**Triggers** (decide the tier; generic wording):
- **Heavy** — touches a project core invariant (the project's own red line / invariant) ∨ needs
  a decision recorded (if the project keeps a decision log) ∨ changes a **public contract**
  (public API / data format / schema / output contract / persistence layout).
- **Medium** — new feature or new behavior, but none of the above.
- **Light** — mechanical / single-file / pure-doc / reversible, no new behavior.

At kickoff give the call plus a one-line rationale; wait for the user to confirm or re-tier, then
run.

## 1. Gates per tier (the core table)

| Stage | Light | Medium | Heavy |
|---|---|---|---|
| spec gate | none | `codex:adversarial-review` | `cc-codex-discussion` |
| plan gate | none | `codex:adversarial-review` | `codex:adversarial-review` |
| SDD reviewer persona | generic | generic | **project domain/invariant review persona** (if the project has one, when its domain is touched) |
| post-impl gate | single `codex:review` | SDD final review → `cc-codex-discussion` | SDD final review → `cc-codex-discussion` |

Why these gates (don't swap them casually):
- **spec/plan use adversarial-review** — it challenges design / assumptions / decomposition /
  ordering; a plan has no code, so `codex:review` doesn't apply.
- **heavy spec escalates to cc-codex** — only a real design fork or a core-invariant change is
  worth debating to consensus over multiple rounds.
- **post-impl uses cc-codex (medium/heavy)** — its round-1 is Codex independently reading the
  diff for defects, so **don't also run a standalone `codex:review`** (it overlaps cc-codex
  round-1). By default only the light tier uses a single `codex:review` as a backstop; if
  `cc-codex-discussion` is absent, medium/heavy degrade to it too (see Dependencies).
- **domain review is a persona, not an extra gate** — for a slice touching the project's domain,
  swap the SDD task-reviewer / final-review `agentType` to the project's domain-review subagent
  (if any); no extra rounds.

### post-impl parallel review (medium/heavy may call a Workflow)

When findings span a wide surface, the post-impl gate may *call* a Workflow script for
multi-angle parallel review + adversarial verify (correctness / domain-invariants / perf /
spec-conformance, one lane each, pipeline mode). This skill stays the controller; the Workflow
only carries that step's throughput. Pull one in only when the user has ultracode on or
explicitly asks for a Workflow.

## 2. Stop-and-ask hardline (not by feel)

**Stop and ask the user iff:** touches a project core invariant ∨ needs a decision recorded or
conflicts with an existing decision ∨ changes a public contract ∨ a review gate raises a
**spec-unfounded design fork**.
**Otherwise: implement the recommended option and log it in the live ledger** (don't interrupt
the user for small points).
For an external review finding, first check it against the project's spec / decision record: if
an established decision overrides the default intuition, **reject the finding and cite the
decision**; only an unfounded fork warrants stopping.

## 3. Human stops (mid-run: only these 2 are proactive)

Kickoff tier confirmation (§0) is the upfront handshake, not a mid-run stop.

1. **Stop-and-ask threshold** — stop the moment a §2 hardline is hit. This is also where a
   spec/plan gate halts if it raises a spec-unfounded design fork, or touches a core invariant /
   public contract / a decision that must be recorded.
2. **Final merge** — after the report is written, the user confirms before merging and deleting
   the branch per the project's VCS flow.

**The spec and plan gates do not pause by default** — once findings are folded in, post the
spec/plan plus the gate verdict and **keep going** (a non-blocking announce; the user can
interrupt anytime), unless a hardline above is hit. Treat the built-in handoffs of `brainstorming`
/ `writing-plans` the same way — an announcement, not a mandatory gate (unless they surface a
§2-class issue).
Everything else (SDD implement → final review → cc-codex → fixes → docs → report) runs straight
through.

## 4. Live ledger + report

- **Live ledger (compaction-proof):** `.superpowers/sdd/disputes.md`, appended **in real time**
  as the run proceeds — every "implement the recommended option for a small point" decision, every
  gate finding's disposition (accepted / rejected / folded into a commit), every stop-and-ask
  conclusion. It is scratch — **make sure `.superpowers/` is in the target project's `.gitignore`**
  (SDD usually ignores it); don't let it land in git.
- **Report (the deliverable):** at close, assemble the ledger + `git log` + each gate's verdict
  into `docs/superpowers/reports/YYYY-MM-DD-<slice>.md` (or the project's report dir), covering:
  change summary / per-gate verdict / folded-in findings / **disputes (where I implemented the
  recommended option, awaiting your ruling)** / decisions recorded / test results (layer by layer
  over the change surface) / leftovers. The report **is committed** (into git), unlike the scratch
  ledger.

## 5. Model choice

When dispatching a subagent, pass `model` explicitly: complex (architectural judgment / deep
review / multi-file integration) → a stronger tier; mechanical (full code transcription /
single-file edit / pure restatement) → a cheaper tier. **Honor any model policy the project or
user has set** (if there's a floor, respect it); with no set policy, decide by the complexity
above.

## 6. Step sequence (the call list)

1. **Kickoff** — tier it → report the call, wait for confirm.
2. **spec** — `brainstorming` produces the spec → spec gate (per tier) → fold in → **announce and
   continue** (stop only on a §2 hardline).
3. **plan** — `writing-plans` produces the plan → plan gate (medium/heavy adversarial) → fold in
   → **announce and continue** (stop only on a §2 hardline).
4. **execute** — `subagent-driven-development`; for a slice touching the project's domain, use the
   project's domain-review persona (if any). Update the ledger after each task. If a change trips
   a project "must-do-after" rule (e.g. editing some file requires re-running the full baseline),
   do it.
5. **post-impl gate** — light = `codex:review`; medium/heavy = SDD final review →
   `cc-codex-discussion` (call a Workflow for parallel review if needed).
6. **fixes** — do small points inline and log them; stop at a threshold point. After fixing, run
   the project's verification suite over the change surface.
7. **record decisions** — for any change that needs a decision recorded, trigger the project's
   decision-log mechanism (if any).
8. **docs (mandatory at implement close)** — once implementation/fixes land, **trigger the
   project's doc-sync mechanism** (don't name a specific skill; trigger it if present) so related
   docs track the change. **Not skippable on medium/heavy;** a pure-doc light slice whose change
   is the doc itself may skip.
9. **report** — assemble the report and commit it.
10. **merge** — after the user confirms, squash-merge + delete the local/remote branch per the
    project's VCS/PR flow.

## Project bindings (discovered at runtime)

On entering a project, first see which specialized skills/agents it ships and **delegate** the
generic steps to them; fall back to generic where missing:

| Generic step | Prefer the project's own | Generic fallback |
|---|---|---|
| PR / merge / delete branch | the project's VCS/PR skill | `superpowers:finishing-a-development-branch` |
| decision log | the project's decision-record skill | skip if none (or note it in the report) |
| verification suite | the project's verify/test skill | the test command in the project README/CI |
| doc-sync | the project's doc-sync skill (just trigger it, don't name it) | manually check related docs if none |
| domain-review persona | the project's domain-review subagent | generic reviewer |

## Red lines (of this process itself)

- Don't start implementation directly on the main branch; a feature branch goes first.
- Gate selection and placement follow the §1 table — **don't swap them casually**.
- Merge + branch deletion is always the owner's call; this skill never auto-merges an unconfirmed
  PR.
