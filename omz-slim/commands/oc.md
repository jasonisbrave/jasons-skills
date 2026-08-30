---
description: Dispatch a cheap bulk task to OpenCode (oh-my-opencode-slim agent) and report only the summary
---

Dispatch the following task to OpenCode via the Bash tool, from the current
workspace directory:

    opencode run "<task>" --agent explorer

Rules:

- Default to the `explorer` agent (read-only sweep). If the task text names
  another oh-my-opencode-slim agent (librarian, oracle, council, ...), pass
  `--agent <name>` instead.
- Use this for cheap bulk work (searching, listing, cross-referencing,
  mechanical passes) so the load spreads across the other subscription pools
  — not for deliverable-quality implementation.
- If cold starts are slow, start `opencode serve` in the background once and
  add `--attach http://localhost:<port>` to subsequent `opencode run` calls.
- When it finishes, report ONLY a condensed summary of its findings. Never
  paste the full output into the conversation.

Task: $ARGUMENTS
