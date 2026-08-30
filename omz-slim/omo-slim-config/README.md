# omo-slim-config（OpenCode 可选配置）

[oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim)（omo-slim）的推荐配置，面向**双订阅用户**：把 OpenCode 各 agent 角色按场景路由到 Kimi 订阅（`kimi-for-coding`）与 GLM 订阅（`zhipuai-coding-plan`）的不同档位——重的给旗舰，廉价的给 highspeed / flash，订阅额度利用率最大化。

见贤思齐：[omz-slim](../README.md)（ZCode）与 [omk-slim](../../omk-slim/)（Kimi Code）是同一编排思想在另外两个工具里的实现。

## 文件

| 文件 | 复制到 | 作用 |
| --- | --- | --- |
| `opencode.jsonc` | `~/.config/opencode/opencode.jsonc` | 启用 omo-slim 插件；禁用内置 `explore` / `general`，交给 omo-slim 的同名角色；开启 LSP |
| `oh-my-opencode-slim.json` | `~/.config/opencode/oh-my-opencode-slim.json` | `kimi-glm` 预设：8 个角色的模型路由 + council 会诊配置 |

## kimi-glm 预设路由

| 角色 | 主模型 | 备选 | 说明 |
| --- | --- | --- | --- |
| `orchestrator` | `kimi-for-coding/k3` | `zhipuai glm-5.3` | 全部 skills、除 context7 外全部 MCP |
| `oracle` | `zhipuai glm-5.3` | `kimi k3` | 架构/疑难，最强推理档 |
| `council` | `zhipuai glm-5.3` | — | 多模型会诊主持人 |
| `librarian` | `zhipuai glm-5.3-highspeed` | `kimi highspeed` | 外部调研，挂 `context7` + `gh_grep` |
| `explorer` | `kimi highspeed` | `zhipuai glm-5.3-flash` | 只读代码侦察 |
| `designer` | `kimi-for-coding` | `zhipuai glm-5.2` | 设计类任务 |
| `fixer` | `zhipuai glm-5.3-flash` | `kimi highspeed` | 机械实现 |
| `observer` | `zhipuai glm-5v-turbo` | `glm-4.6v` | 视觉/看图任务 |

council 会诊池（`balanced` 预设）：`kimi k3-256k` / `kimi-for-coding` / `glm-5.2` 三模型互审。

## 前提

- OpenCode 中已登录 `kimi-for-coding` 与 `zhipuai-coding-plan` 两个 provider（`opencode auth login`）。
- 已安装 oh-my-opencode-slim 插件（`npm i -g oh-my-opencode-slim`，或按其官方 README）。
- 预设 `mcps` 引用的 `context7`、`gh_grep` 需在 OpenCode 的 MCP 配置中存在；没有就删掉对应项，不影响其余路由。

## 安装

```bash
# 若已有同名配置，先备份
cp opencode.jsonc ~/.config/opencode/opencode.jsonc
cp oh-my-opencode-slim.json ~/.config/opencode/
```

重启 OpenCode 生效。单订阅用户把每处的另一家模型从备选里删掉即可；模型别名请换成你 `[providers]` / `[models]` 里实际注册的 ID。
