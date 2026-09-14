---
name: tax-report-checker
description: 复核税务鉴证报告产出物（Word报告 + Excel申报表），发现勾稽错误、公式被改、文字与申报表数字不一致、填报遗漏、文本格式问题。覆盖三类报告：企业所得税汇算清缴报告、研发费用加计扣除报告、高新技术企业认定专项鉴证报告。用于"复核税务报告/所得税汇缴复核/加计扣除复核/高新报告复核/检查申报表/报告和申报表核对/汇缴报告检查"。即使用户只说"帮我看看这份所得税报告""核对下汇缴的数字""检查加计扣除报告有没有错"也应触发。支持 .doc/.xls 旧格式自动转换，输出 Excel + Markdown 复核报告。算术 100% 走代码，AI 负责语义定位、预复核与 API 证据包终审。
metadata:
  author: nigo
  version: "1.1.0"
---

# 税务报告核查

复核税务报告产出物（Word 报告正文 + Excel 申报表/附表），支持三类：企业所得税汇算清缴（核心）、研发费用加计扣除、高新认定专项鉴证。

与 audit-report-checker 的分工差异：税务报告的 Excel 申报表本身是公式联动的数据源，不需要"从文档里抠表格"。复核重心是四件事——**公式健康**（模板公式的命脉，被覆盖/报错/断链是常见事故）、**数值双重校验**（缓存值 vs 代码重算，防公式被改坏仍显示正常值）、**报告文字 ↔ 申报表数字勾稽**（人工复核最痛的部分）、**填报完整性 + 文本格式**。

## 最重要的一条原则：算术绝不手算

LLM 直接做数值加减会算错，且无法复现。所有数值计算，哪怕只是三个数相加，都调用 `scripts/calculator.py`；脚本内部的比较也已全部走 Decimal。AI 只决定"传哪些数""哪个等于哪个"，代码算出结果。

```bash
python scripts/calculator.py check "113,157.68" "113,157.68"   # 核对相等
python scripts/calculator.py sum "1,234.56 5,678.90 100.00"    # 求和
python scripts/calculator.py reconcile "100" "50" "30" "120"   # 期初+增-减=期末
```

calculator 支持千分位逗号、括号负数、横线（=0）、全角字符、万元单位，容差默认 0.01。

---

## 工作流

### Step 0 — 识别报告类型 + 选检查模式与深度

先扫用户给的目录/文件，判断报告类型（可多类混合）：

| 类型 | 识别特征 | 规则文件 |
|---|---|---|
| 所得税汇缴 | 文件名含"所得税"，xlsx 有 A100000 主表/A000000 基础信息表等官方表号 sheet | `references/cit_rules.md` |
| 加计扣除 | 文件名含"加计"，xlsx 有"数据中转/数据汇总/附件2优惠明细表" sheet | `references/rd_rules.md` |
| 高新认定 | 文件名含"高新"，含"研究开发费用结构明细表/高新技术产品(服务)收入明细表" | `references/hnte_rules.md` |

用 `question` 工具问用户两件事：

**检查模式**（单选）：
- **成品复核**（默认）：复核已填写完成的报告，查错漏
- **模板完好性**：侧重校验模板公式是否被改坏、表式是否与官方一致（适用于每年新规更新模板后自查）

**检查深度**（单选）：
- **快速**：公式健康 + 完整性（勾选/必填/财报差异）
- **标准**（默认）：快速 + 数值双重校验 + 报告↔申报表勾稽 + 文本格式
- **深度**：标准 + 全量勾稽覆盖 + 逐表人工结构复核

**旧格式提示**：若目录中有 .doc/.xls，告知用户将自动转换为 docx/xlsx 后解析（Word COM 或 LibreOffice，原文件不动）。

### Step 1 — 解析文档

```bash
python scripts/parse_tax.py "<报告目录或文件>" -o <输出目录>
```

- .doc/.xls 自动调 legacy_convert 转换（COM 优先，LibreOffice 回退；都没有会明确报错并退出，此时引导用户手动另存为新版格式）
- .docx → `report_text.md`：每段带 `<!-- SRC file="..." para=N -->` 定位标记，内嵌表格渲染为 markdown 表
- .xlsx → `sheets.json`：双遍读取（公式 + 缓存值），含 defined_names 和外部链接清单
- `files_manifest.json`：文件角色清单

### Step 2 — 语义理解 + 生成 link_map.json（Agent 必做）

读 `report_text.md` 和 sheets.json 摘要，理解：
- 纳税人名称、所属年度、报告文号、出具机构
- 报告类型组合（所得税/加计/高新 是否混合）
- Word 报告正文/说明中的关键金额 → 对应 Excel 哪个 sheet 哪个单元格

**生成 `link_map.json`**（写入输出目录，报告↔表格勾稽的定位基石）：

```json
{"links": [
  {"id": "利润总额", "text_file": "00企业所得税报告（含说明）.docx",
   "text_hint": "利润总额", "cell_file": "2025年度企业所得税申报表（模版）.xlsx",
   "sheet": "A100000企业所得税年度纳税申报主表", "coord": "E21",
   "tolerance": 0.01, "note": "报告利润总额=主表利润总额"}
]}
```

- `text_hint`：报告文字中该金额所在段落的关键词（如"利润总额为""加计扣除总额"）
- 各报告类型的标准勾稽点清单在对应 references 文件里（cit_rules.md 等），按清单逐项建立 link，不要凭印象造
- 模板每年更新导致单元格坐标变化时，以 references 里的"按行次/项目名定位"说明为准重新确认坐标——坐标是当年模板的，跨年要核对
- 也可在 `run_check.py` 中加 `--auto-link-map` 让 AI 自动生成初稿（会写入 `<outdir>/link_map.json`），但生成后 Agent 仍需按标准清单抽查校验，不可直接采信

### Step 3 — 代码侧检查

```bash
python scripts/run_check.py "<报告目录>" -o <输出目录> \
  --scope <quick|standard|deep> --link-map <输出目录>/link_map.json

# 自动生成 link_map（标准/深度下可用，生成后仍需 Agent 抽查校验）
python scripts/run_check.py "<报告目录>" -o <输出目录> \
  --scope standard --auto-link-map

# 禁用 AI，只跑纯代码检查
python scripts/run_check.py "<报告目录>" -o <输出目录> --no-ai

# 跳过 AI 文本检查或 AI 终审
python scripts/run_check.py "<报告目录>" -o <输出目录> \
  --skip-text-ai --skip-ai-review

# 跳过 API 终审，回退到轻量预复核
python scripts/run_check.py "<报告目录>" -o <输出目录> --skip-final-review
```

run_check 编排以下检查（单步失败不中断，记 warning 继续）：

| 检查 | check_type | 内容 |
|---|---|---|
| 公式健康 | 公式健康 | 错误值(#REF!等)、跨表断链、外部链接、公式区硬编码覆盖 |
| 数值双重校验 | 数值校验 | 可安全求值公式（SUM/加减/ROUND/单引用）用缓存值重算 vs 缓存值，容差 0.01 |
| 完整性 | 完整性 | 填报表单勾选↔附表实填一致性、财报差异列≠0、基本信息必填项空白、公司名跨文件一致性 |
| 报告勾稽 | 报告勾稽 | 按 link_map：报告文字金额 vs 单元格缓存值（calculator 比较） |
| AI 文本检查 | 文本格式 | DeepSeek 并发检查错别字/病句 |
| API 证据包终审 | （叠加到各 check_type） | 对未通过的 error/warning 按证据包做四值判定，输出 `待人工复核.md` |

可选：`--skip-values`（跳过双重校验提速）、`--scope quick`（只跑公式健康+完整性，不走 AI）。

输出 `results.json`（meta.counts + results 数组），标准/深度且启用 AI 时额外输出 `<outdir>/待人工复核.md`。每项含 check_type/rule_name/severity/passed/description/expected/actual/difference/source_location/context/evidence。

### Step 4 — Agent 终审（必须执行，核心价值）

代码侧只是确定性粗筛。所有 error/warning 必须经整体语义复核才能输出，**终审必须回原文核对**：

- **API 证据包终审已自动执行**：标准/深度模式下，`run_check.py` 会把未通过的 error/warning 打包成"证据包"（finding 本体 + sheet 上下文 + 报告原文摘录 + 误报模式库），调用 DeepSeek 做四值判定（confirm_true_error / downgrade_suspect / delete_false_positive / needs_human），并做代码侧校验（verdict 合法、evidence_quote 必须是证据包原文子串、confidence≥0.6、confirm 时复验 difference 一致性）。判定结果以 `API终审：` 前缀写入 `evidence`。
- **Agent 终审职责聚焦**：
  1. 复核全部 `needs_human` 条目：读 `<outdir>/待人工复核.md`，回原文逐条定案。
  2. 抽查 `confirm_true_error` 和 `delete_false_positive` 高影响条目，防止机器误判。
  3. 同意机器判定时保留 `API终审：` 标记；改写/覆盖机器判定时用 `Agent复核：` 标记，便于区分责任。
- **公式区硬编码 warning**：打开 sheets.json 看该行上下文——是不是本来就是手工录入区（如"基本信息"表本来就是填数的）？是 → 删除；公式区中间夹死数 → 确认 error。
- **缓存值与重算不符 warning**：看公式是否含易失函数或跨表空值陷阱；确认公式被改坏 → error。
- **报告勾稽 error**：回 report_text.md 读该段原文——是不是"万元"口径被脚本漏放大、是不是取了段落里另一个数（如核减额 vs 审核额）？口径问题 → 存疑并注明；确实对不上 → 确认 error。
- **完整性 warning**：勾选未填报可能是该表本就不适用（模板公式自动勾选基于有数）——结合 A000000 基础信息判断企业类型后定夺。
- **文本格式**：通读 report_text.md，找错别字、病句、文号/日期/公司名前后不一致。脚本词库只是粗筛，这部分靠你。

复核规则：
- **确认真错**（回原文确认）→ 保留 error，evidence 加"Agent复核：确认 + 依据"
- **口径差异/特殊结构** → 降为存疑，evidence 注明口径
- **误报**（脚本取错数、模板未填写导致的正常空白）→ 删除不显示
- 每条语义补充结果追加进 results.json（字段同上）

存疑是唯一允许骑墙的结论，且仅当回原文后仍属口径判断（需税务师定夺）。看不懂不是存疑理由，要继续读到懂。

### Step 5 — 输出报告

```bash
python scripts/export_report.py <输出目录>/results.json -o <输出目录>
```

产出：
- **复核结果.xlsx**（7 sheets）：摘要 / 报告勾稽 / 公式健康 / 数值校验 / 完整性 / 文本格式 / 检查项（全量含通过）
- **复核报告.md**：按问题→存疑分组，含位置与原文摘录

输出只读不改源文件（audit-only）。

### Step 6 — 解读结果

直接告知报告保存路径 + 核心统计（几个问题/几个存疑/主要问题各是什么），不逐项展开，除非用户要求。按严重程度：
- **error（问题）**：确认错误需修改——给出应为值、实际值、位置、修复建议
- **warning（存疑）**：可疑需人工核实——说明口径疑点
- **info**：通过项和统计

---

## 关键指引

**三类报告的勾稽规则**在 references 里，做 Step 2 的 link_map 时必读对应文件：

| 文件 | 何时读 |
|---|---|
| `references/cit_rules.md` | 所得税：报告↔主表↔附表标准勾稽点、行次定位法、完整性检查点 |
| `references/rd_rules.md` | 加计扣除：报告文字金额↔附件2/归集表勾稽点、两版附件样式差异 |
| `references/hnte_rules.md` | 高新：三份明细表结构、外部链接注意事项、旧格式转换 |
| `references/user_rules.md` | 每次检查加载用户自定义规则；用户提新需求时写入 |

**模板年度更新**：所得税模板每年随新规更新，单元格坐标可能变。references 中所有坐标标注了对应年度模板；跨年时按"行次/项目名定位法"重新确认坐标后更新 references，不要改代码。

**大目录分批**：多类报告混合或大附件时，按"先 Word 报告（段少）→ 再 Excel 主表 → 后明细表"的顺序读，不全量加载 sheets.json（只读需要的 sheet 块）。

**用户自定义规则**：用户在对话中描述的特殊检查需求（如"我们要求核对系统导出的申报表与底稿一致性"），整理成"自然语言描述 + 适用场景 + 期望结果"写入 user_rules.md 的"## 我的规则"章节，检查时一并执行。

## 依赖

```bash
pip install openpyxl python-docx pywin32 openai json_repair   # pywin32 仅旧格式转换需要
```

旧格式转换备选：LibreOffice（`soffice --headless`）。脚本纯 Python 跨平台，中文路径、含空格路径均支持。所有文件读写 UTF-8。
