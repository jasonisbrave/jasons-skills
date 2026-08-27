---
description: Show or adjust the omz-slim cost preset (routing discipline level)
---

Manage the omz-slim cost preset. Argument: `status` (default), `strict`,
`balanced`, or `off`.

- **strict** — every non-trivial task must go through subagents; main thread
  answers only plan/approve/assemble. Maximum context savings.
- **balanced** (default) — delegate exploration and bulk edits; answer
  simple questions directly.
- **off** — no routing discipline; behave as plain ZCode.

Apply the requested preset to the rest of this session and acknowledge in one
line what gets delegated and what stays in the main thread.

Argument: $ARGUMENTS
