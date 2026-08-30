---
description: Dispatch a task to Kimi Code CLI non-interactively (kimi -p) and report only the summary
---

Dispatch the following task to Kimi Code via the Bash tool, from the current
workspace directory:

    kimi -p "<task>" --output-format text

Rules:

- Quote the task exactly; if it contains double quotes, pass it safely (single
  quotes or a heredoc), never by hand-editing its meaning.
- `kimi -p` runs with auto permissions and no per-step approval — only
  dispatch read-only, search, or review tasks this way. If the task writes
  files, confirm with the user first or tell them to run `kimi` interactively.
- If `kimi` is not on PATH, use `~/.kimi-code/bin/kimi`.
- When it finishes, verify the key claims cheaply (e.g. the diff actually
  exists), then report ONLY a condensed summary: decisions, files touched,
  test results. Never paste the full stdout into the conversation.

Task: $ARGUMENTS
