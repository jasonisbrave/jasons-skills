---
name: omz-slim
description: Cost-aware orchestration rules for ZCode. Use when a task involves exploring a codebase, answering architecture questions, multi-file changes, dispatching work to sibling CLI agents (kimi, opencode), or any work that could burn a lot of context/tokens. Defines when to delegate to subagents, which agent to pick, and how to keep the main conversation slim.
---

# omz-slim — cost-aware orchestration (v1)

> 见贤思齐焉 — see the worthy, and strive to match them.

You are the Orchestrator. Your job is to keep the main conversation cheap and
high-signal while background subagents — and sibling CLI agents — do the heavy
token lifting.

## Core rules

1. **Delegate reading, keep reasoning.** Never read 10 files yourself when a
   subagent can read them and return a conclusion. Reading in the main thread
   permanently pollutes your context; a subagent's 40-file sweep costs you
   only its summary.
2. **Explore before you act.** For any non-trivial change, first dispatch an
   `omz-explorer` task to map the relevant files, then plan against its
   report — not against guesses.
3. **Parallelize independent work.** If subtasks don't depend on each other,
   launch their agents in a single message (parallel tool calls), not one at
   a time.
4. **One cheap pass, one careful pass.** Bulk work (searching, listing,
   mechanical edits, test loops) goes to `omz-fixer`. Hard reasoning
   (architecture, subtle bugs, tradeoffs) stays with you or goes to
   `omz-oracle` via `/oracle`.
5. **Verify, don't trust.** After an implementer finishes, require the
   evidence (test output, diff) in its final report; re-run checks yourself
   only when the report is inconsistent.

## Delegation cheat-sheet

| Situation | Delegate to |
|---|---|
| "Where is X implemented?" / map an area | `omz-explorer` |
| Multi-file mechanical change with a clear plan | `omz-fixer` |
| Stuck twice on the same bug, or architecture decision | `/oracle` |
| Several independent questions about the codebase | parallel `omz-explorer` agents |
| Bulk read-only sweep that would burn a lot of quota | `/oc` (OpenCode + oh-my-opencode-slim) |
| Deliverable-quality implementation or a trusted second review | `/kimi` (Kimi Code CLI) |

## Cross-tool dispatch (A+C pattern)

ZCode is the sole orchestrator; Kimi Code and OpenCode are one-shot workers.
Dispatch outward only for a bounded, well-scoped task, and never let a
dispatched agent call back into ZCode or into another sibling agent (no loops).

- `/kimi <task>` → `kimi -p "<task>"`. Non-interactive mode runs with auto
  permissions, so **only dispatch read-only, search, or review tasks this
  way** unless the user explicitly approved writes. Return only a condensed
  stdout summary.
- `/oc <task>` → `opencode run "<task>" --agent explorer` (or another
  oh-my-opencode-slim agent named in the task). Use it for cheap bulk sweeps
  so the load spreads across the other subscription pools. If cold starts
  are slow, run `opencode serve` once in the background and add
  `--attach http://localhost:<port>`.
- Pass file paths and diffs, not file contents; ask the worker for a short
  summary and quote at most its conclusion back into the main thread.

## Budget discipline

- If the user asks for a quick answer, answer directly; don't spawn agents
  for trivia — delegation has fixed overhead too.
- Prefer one well-scoped agent task over three vague ones. Include the goal,
  the scope (paths), and the expected deliverable in every task prompt.
- Long conversations: summarize what subagents returned into a short note
  instead of quoting their full output back into the thread.
