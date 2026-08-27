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

## Updating an installed copy

Installed plugins load from a per-version cache, not from this directory —
repo edits stay invisible until the client installs a newer version:

1. Commit your change and **bump `version` together in all three manifests**:
   `omz-slim/.zcode-plugin/plugin.json`, `omz-slim/marketplace.json`, and the
   root `marketplace.json`. Identical old versions are treated as up to date,
   even if content changed.
2. For a GitHub-marketplace install, push; for a local-directory install,
   make sure the directory you registered contains the new commit (`git pull`).
3. In ZCode: **Settings → Plugin Management** — compare the Installed tab's
   version against the new manifest version, then update the plugin from its
   Discover entry (or Uninstall → Get).
4. Verify: **Settings → Subagents** and **Settings → Skills** should reflect
   the new components.

## Cost tuning

The agents are routed to BigModel coding-plan models (fully qualified with the
provider prefix so they never fall through to a pay-per-token API key):

| Agent | Model | Why |
|---|---|---|
| `omz-explorer` | `builtin:bigmodel-coding-plan/GLM-5.3-Flash` | bulk reading; cheapest fast tier |
| `omz-fixer` | `builtin:bigmodel-coding-plan/GLM-5.3-Flash` | mechanical edits + test loops |
| `omz-oracle` | `builtin:bigmodel-coding-plan/GLM-5.3` | flagship quality, escalation only |

To change routing, edit the `model:` field in `agents/*.md` frontmatter to
any model ID you want (`inherit` follows the main-thread model). If an agent
fails to start over an unresolvable ID, use the bare name (e.g.
`GLM-5.3-Flash`) or revert to `inherit`.
