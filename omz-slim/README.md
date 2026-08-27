# omz-slim (oh my ZCode — slim)

A cost-aware orchestration plugin for ZCode, inspired by
[oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim).

Core idea: the main conversation acts as an Orchestrator that plans, approves,
and assembles; background subagents do the token-heavy reading and mechanical
editing, so the expensive main context stays small.

## What's inside

| Component | Role |
|---|---|
| `skills/omz-slim` | Orchestration rules loaded into context (when to delegate, parallelism, budget discipline) |
| `agents/omz-explorer` | Read-only codebase scout (cheap model — set `model:` in its frontmatter) |
| `agents/omz-fixer` | Fast mechanical implementation executor (cheap model) |
| `agents/omz-oracle` | Hard-problem advisor — highest-cost path, manual only |
| `/delegate <task>` | Send a task to a background subagent |
| `/oracle <question>` | Escalate a hard problem |
| `/preset [strict\|balanced\|off]` | Adjust routing discipline for the session |

## Install (local marketplace)

1. Clone this repo (or just the `omz-slim/` directory).
2. In ZCode: **Settings → Plugin Management → Discover → +**
3. Add a marketplace from the local directory containing `omz-slim/`
   (or add this GitHub repo directly as a marketplace).
4. Install and enable **omz-slim**.

## Cost tuning

The agents default to `model: inherit`. To route them to a cheaper model,
edit the `model:` field in `agents/*.md` frontmatter to the model ID you
want (the same knob oh-my-opencode-slim uses for its Explorer/Librarian).
