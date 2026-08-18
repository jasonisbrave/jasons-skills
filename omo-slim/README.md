# omo-slim（Kimi Code 复刻版）

复刻 [oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim) 核心思想的 Kimi Code 插件：**与其让一个模型做所有事，不如把工作的每个部分路由给最合适的智能体和模型档位**，在主 Agent 一个编排者下平衡质量、速度与成本。

## 包含内容

四个专家子智能体（提示词提取自原版源码并适配 Kimi Code 工具名）：

| 智能体 | 角色 | 建议模型档位 |
| --- | --- | --- |
| `explorer` | 只读代码侦察：找文件、找符号、回答「X 在哪」 | 快速便宜档 |
| `librarian` | 外部调研：官方文档、开源示例、库用法 | 快速便宜档 |
| `fixer` | 实现专家：接收明确规格，高效执行代码改动 | 均衡编码档 |
| `oracle` | 只读战略顾问：架构决策、疑难调试、代码审查、YAGNI 把关 | 最强推理档 |

外加一份注入主 Agent 系统提示的**委派规则表**（`SYSTEM.md`），告诉主 Agent 什么场景派给谁、配哪档模型。

## 安装

```bash
git clone https://github.com/jasonisbrave/jasons-skills.git
```

然后在 Kimi Code 中：

```
/plugins install <克隆路径>/omo-slim
/reload
```

## 配置模型路由（关键一步）

插件本身只提供智能体和委派规则。**按档位路由模型**依赖 Kimi Code 的实验特性 `[secondary_model]`，需要在用户级配置中手动开启（插件无权修改 `config.toml`）：

1. 设置环境变量（Windows 示例）：

   ```
   setx KIMI_CODE_EXPERIMENTAL_SECONDARY_MODEL 1
   ```

   设置后重启终端 / Kimi Code 进程。

2. 在 `~/.kimi-code/config.toml` 中配置模型池（别名需替换为你自己 `[models]` 里已注册的模型）：

   ```toml
   [secondary_model]
   default_model = "kimi-code/kimi-for-coding-highspeed"   # 子任务默认走便宜档

   [secondary_model.models]
   "kimi-code/k3" = "最强推理。用于 oracle（架构决策、疑难调试、代码审查）及其他复杂问题。"
   "kimi-code/kimi-for-coding" = "均衡编码主力。用于 fixer 和大多数功能实现、代码变更任务。"
   "kimi-code/kimi-for-coding-highspeed" = "快速便宜。用于 explorer、librarian，以及日常小改、代码解释、摘要、批量简单任务。"
   ```

   模型池不限于 Kimi 系——任何在 `[providers]` / `[models]` 注册过的模型（Claude、OpenAI、Gemini 等）都可以入池混用。

不配模型池也能用：四个智能体照常工作，只是子任务继承主 Agent 的模型，没有成本分层。

## 使用

无需特殊操作。正常描述任务，主 Agent 会按委派规则表自动派发：

- 「这个函数在哪定义的？」→ `explorer`（便宜档）
- 「帮我查一下某某库的官方用法」→ `librarian`（便宜档）
- 「按这个方案改掉」→ `fixer`（均衡档）
- 「这个架构有什么问题 / 这个 bug 查不出原因」→ `oracle`（最强档）

也可以显式指定：「用 oracle 审查一下这个模块」。

TUI 中用 `/secondary-model` 可随时切换子任务默认模型，即时生效。

## 与原版的差异

Kimi Code 没有 OpenCode 的 JS 插件运行时，以下原版能力**不在**本插件范围内：

- 每个智能体**硬绑定**模型（Kimi Code 中模型是派发时选择，由委派规则 + 模型池描述软引导；想强制所有子任务只用单一模型，可在 `[secondary_model]` 中加 `force = true`）
- `/preset` 预设热切换、Council 多模型会诊、Companion 桌面浮窗、tmux 多路复用集成、LSP 工具

## 注意事项

- 插件级 agents 优先级**低于**用户级（`~/.kimi-code/agents/`）和项目级同名文件。如果你已在用户级装过同名智能体，本地版本会覆盖插件版本。
- 插件改动需 `/reload` 或新开会话生效。
- 全局 `[thinking].effort` 对子智能体同样生效，会覆盖模型自带的默认思考档位。

## 致谢

- 原版插件：[alvinunreal/oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim)（MIT）

## License

MIT
