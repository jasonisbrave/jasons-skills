#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_worker.py — DeepSeek AI 层（tax-report-checker）

职责：
- 封装 DeepSeek API 调用（OpenAI 兼容接口）
- 并发执行税务报告复核的 AI 任务：
  1. 自动生成报告↔申报表勾稽 link_map
  2. 文本错别字/病句检查
  3. warning/error 预复核
- 无 key / key 失效时优雅降级

设计原则：
- DeepSeek 做可并发的批量语义工作
- Agent 做全局语义终审
- 所有算术仍由 calculator.py / check_*.py 完成

安全：
- API key 不写进 skill 目录
- 检查顺序：--api-key > DEEPSEEK_API_KEY 环境变量 > ~/.deepseek/config.json
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# 把脚本目录加入 sys.path，保证直接 import 时能加载 _common
try:
    _HERE = Path(__file__).resolve().parent
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    from _common import group_rows, match_file_key, parse_report_text, split_coord, to_decimal
except Exception as _e:  # pragma: no cover - 兜底，避免独立 import 时崩溃
    raise RuntimeError(f"ai_worker 依赖 _common 加载失败: {_e}")

try:
    import openai
except ImportError:
    raise RuntimeError("请先安装 openai 库: pip install openai")

try:
    import json_repair
except ImportError:  # noqa: F841
    json_repair = None


DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"


# ──────────────────────────────────────────────────────────────────
# DeepSeek Client 封装
# ──────────────────────────────────────────────────────────────────
class DeepSeekClient:
    """DeepSeek API 客户端，OpenAI 兼容接口。"""

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.deepseek.com"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

        # 初始化 OpenAI 客户端
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=120.0,
        )

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.0, max_retries: int = 3) -> str:
        """发送聊天请求，返回文本内容。带重试 + 指数退避。

        重试策略：
        - 可重试：网络/限流/服务端 5xx / 空返回
        - 不可重试（直接抛出）：401/403/400 等客户端错误
        """
        import time as _time
        from openai import (
            APIConnectionError,
            APITimeoutError,
            RateLimitError,
            AuthenticationError,
            PermissionDeniedError,
            BadRequestError,
        )

        last_reason = ""
        last_exc = None
        for attempt in range(max_retries):
            try:
                kwargs = dict(model=self.model, messages=messages, temperature=temperature)
                response = self.client.chat.completions.create(**kwargs)
                choice = response.choices[0]
                content = choice.message.content or ""
                last_reason = getattr(choice, "finish_reason", "") or ""
                if content.strip():
                    return content
                print(f"[WARN] 空返回(finish_reason={last_reason})，重试 {attempt+1}/{max_retries}", file=sys.stderr)
            except (APIConnectionError, APITimeoutError, RateLimitError) as e:
                last_exc = e
                print(f"[WARN] API可重试异常({type(e).__name__}: {e})，重试 {attempt+1}/{max_retries}", file=sys.stderr)
            except (AuthenticationError, PermissionDeniedError, BadRequestError) as e:
                raise RuntimeError(
                    f"API 请求被拒绝({type(e).__name__}: {e})，请检查 API key、模型名称和请求参数"
                ) from e
            except Exception as e:
                last_exc = e
                print(f"[WARN] API异常({type(e).__name__}: {e})，重试 {attempt+1}/{max_retries}", file=sys.stderr)
            if attempt < max_retries - 1:
                _time.sleep(2 ** attempt)
        raise RuntimeError(f"API 请求多次失败（重试 {max_retries} 次）: {last_exc}")

    def chat_json(self, messages: List[Dict[str, str]], temperature: float = 0.0) -> Dict[str, Any]:
        """发送聊天请求，返回解析后的 JSON。"""
        content = self.chat(messages, temperature)

        # JSON 解析容错：剥离 ```json 包裹
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            if json_repair is not None:
                try:
                    content = json_repair.repair_json(content)
                    return json.loads(content)
                except Exception as e2:
                    raise ValueError(f"JSON 修复失败: {e2}\n原始内容: {content[:500]}...")
            raise ValueError(f"JSON 解析失败: {e}\n原始内容: {content[:500]}...")

    def list_models(self, retries: int = 3) -> List[str]:
        """列出可用模型（flash 优先排序）。"""
        import time as _time
        last_err = None
        for attempt in range(retries):
            try:
                models = self.client.models.list()
                model_names = [m.id for m in models.data if m.id]
                flash_models = sorted([m for m in model_names if "flash" in m.lower()])
                other_models = sorted([m for m in model_names if "flash" not in m.lower()])
                return flash_models + other_models
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    _time.sleep(2 ** attempt)
        raise RuntimeError(f"查询模型列表失败（重试 {retries} 次）: {last_err}")


# ──────────────────────────────────────────────────────────────────
# API Key 检查
# ──────────────────────────────────────────────────────────────────
def check_api_key(api_key: Optional[str] = None) -> Tuple[bool, Optional[Dict[str, str]]]:
    """检查 API key 是否存在且有效。

    检查顺序：
    1. api_key 参数
    2. DEEPSEEK_API_KEY 环境变量
    3. ~/.deepseek/config.json

    返回：
        (has_key, config_dict)
    """
    if api_key:
        config = {"api_key": api_key, "model": DEFAULT_MODEL, "base_url": DEFAULT_BASE_URL}
    elif "DEEPSEEK_API_KEY" in os.environ:
        config = {
            "api_key": os.environ["DEEPSEEK_API_KEY"],
            "model": DEFAULT_MODEL,
            "base_url": DEFAULT_BASE_URL,
        }
    else:
        config_path = Path.home() / ".deepseek" / "config.json"
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if "api_key" not in config:
                    return False, None
                config.setdefault("model", DEFAULT_MODEL)
                config.setdefault("base_url", DEFAULT_BASE_URL)
            except Exception:
                return False, None
        else:
            return False, None

    try:
        client = DeepSeekClient(
            api_key=config["api_key"],
            model=config["model"],
            base_url=config["base_url"],
        )
        client.list_models()
        return True, config
    except Exception as e:
        msg = str(e).lower()
        auth_fail = any(k in msg for k in ("401", "403", "authentication", "unauthorized", "invalid api key"))
        if auth_fail:
            return False, None
        print(f"[WARN] key 验证时服务暂时不可用({e})，假定 key 有效继续：{config.get('base_url')}", file=sys.stderr)
        return True, config


# ──────────────────────────────────────────────────────────────────
# 并发执行器
# ──────────────────────────────────────────────────────────────────
def run_concurrent(
    tasks: List[Tuple[Callable, tuple, dict]],
    max_workers: int = 40,
    timeout: int = 120,
) -> List[Any]:
    """并发执行任务，每个任务 try/except，失败返回 None 不阻塞。"""
    results = [None] * len(tasks)
    total = len(tasks)
    completed = 0
    succeeded = 0
    failed = 0
    import time as _time
    start_time = _time.time()

    def run_task(index: int, func: Callable, args: tuple, kwargs: dict) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            print(f"[WARN] 任务 {index} 失败: {e}", file=sys.stderr)
            return None

    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {
        executor.submit(run_task, idx, func, args, kwargs): idx
        for idx, (func, args, kwargs) in enumerate(tasks)
    }
    try:
        for future in as_completed(futures):
            idx = futures[future]
            ok = False
            try:
                result = future.result(timeout=timeout)
                results[idx] = result
                if result is not None:
                    ok = True
            except TimeoutError:
                print(f"[WARN] 任务 {idx} 超时", file=sys.stderr)
                results[idx] = None
            except Exception as e:
                print(f"[WARN] 任务 {idx} 异常: {e}", file=sys.stderr)
                results[idx] = None

            completed += 1
            if ok:
                succeeded += 1
            else:
                failed += 1
            elapsed = round(_time.time() - start_time, 1)
            if total <= 30 or completed % max(1, total // 20) == 0 or completed == total:
                rate = round(completed / elapsed, 1) if elapsed > 0 else 0
                eta = round((total - completed) / rate, 1) if rate > 0 else 0
                print(f"[进度] {completed}/{total} 完成（成功{succeeded} 失败{failed}）| 已用{elapsed}s 速率{rate}/s 预计剩{eta}s",
                      file=sys.stderr, flush=True)
    finally:
        for f in futures:
            f.cancel()
        executor.shutdown(wait=False)

    return results


# ──────────────────────────────────────────────────────────────────
# 文本错别字 / 病句检查
# ──────────────────────────────────────────────────────────────────
# 税务报告常见术语白名单（避免专业术语被误判为错别字）
TAX_TERM_WHITELIST = [
    "应纳税所得额", "应纳所得税额", "应补（退）所得税额", "利润总额", "净利润",
    "营业收入", "营业成本", "期间费用", "税金及附加", "销售费用", "管理费用",
    "研发费用", "财务费用", "资产减值损失", "信用减值损失", "投资收益",
    "营业外收入", "营业外支出", "所得税费用", "研发费用加计扣除", "加计扣除",
    "高新技术企业", "高新收入", "高新技术产品", "研究开发费用", "研发项目",
    "纳税调整", "调增金额", "调减金额", "账载金额", "税收金额", "减免所得税额",
    "弥补亏损", "以前年度亏损", "实际利润额", "预缴税款", "汇算清缴",
    "主管税务机关", "纳税人识别号", "统一社会信用代码", "法定代表人",
    "资产负债表", "利润表", "现金流量表", "所有者权益变动表",
    "企业会计准则", "小企业会计准则", "企业会计制度", "民间非营利组织会计制度",
    "账面余额", "账面价值", "坏账准备", "减值准备", "累计折旧", "累计摊销",
    "公允价值", "摊余成本", "预期信用损失", "先进先出", "月末一次加权平均",
    "个别计价法", "移动加权平均法",
]


def _preprocess_pdf_spaces(text: str) -> str:
    """合并中文间的空格，避免 PDF 提取断词被当错别字。"""
    pattern = r"([\u4e00-\u9fa5])\s+([\u4e00-\u9fa5])"
    while re.search(pattern, text):
        text = re.sub(pattern, r"\1\2", text)
    return text


def chunk_report_text(md_text: str) -> List[str]:
    """把 report_text.md 按 SRC 注释分段成文本块列表。

    同一段落/表格的内容合并为一个 chunk，保留定位信息在段首。
    """
    entries = parse_report_text(md_text)
    groups: Dict[Tuple[str, str, int], List[str]] = {}
    for e in entries:
        key = (e.get("file", ""), e.get("kind", ""), e.get("idx", 0))
        groups.setdefault(key, []).append(e.get("text", ""))
    chunks = []
    for (file, kind, idx), lines in groups.items():
        header = f"<!-- SRC file=\"{file}\" {kind}={idx} -->"
        chunks.append(header + "\n" + "\n".join(lines))
    return chunks


_TYPO_AND_GRAMMAR_CHECK_PROMPT = """你是税务师，审阅税务报告文本，找出错别字和病句。

## 错别字定义：
- 明显的拼写错误（如"应纳税所额"→"应纳税所得额"）
- 只报告**原文中确实出现的词**，不要联想/推测/编造原文没有的词
- 不是专业术语（如"加计扣除""应纳税所得额"都是正确的专业术语，不报告）
- 不是换行导致的拆词（如"利\n润总额"不视为错别字）

过滤规则：
- 错别字长度 ≥ 2
- 必须从输入文本中逐字引用原文存在的词
- 不报告专业术语变体
- 不报告因换行导致的拆词
- 原文中同时包含正确的完整词时，不报告

## 病句定义：
- 语序不当、搭配不当、成分残缺、句式杂糅、重复冗余、不合逻辑

病句排除规则（严格执行，宁可漏报不可误报）：
- 法规/准则名称全文、表格数据行、含大量数字的技术性语句不视为病句
- 只报告明确无疑的语法错误
- 不确定时 confidence 必须 < 0.5

confidence 字段：0-1 的置信度。
- >= 0.85：高置信度
- 0.5 <= confidence < 0.85：中等置信度
- < 0.5：低置信度，不要报告

输入文本：
{text}

返回 JSON 格式：
{
  "typos": [
    {"original": "错误词", "suggestion": "正确词", "reason": "原因", "confidence": 0.0-1.0}
  ],
  "grammar_errors": [
    {"original": "原句", "suggestion": "修改建议", "reason": "原因", "confidence": 0.0-1.0}
  ]
}

无则返回空数组。直接返回 JSON，不要加 markdown 代码块标记。
"""


def _filter_tax_terms(typos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤税务/会计术语白名单。"""
    filtered = []
    for typo in typos:
        original = _preprocess_pdf_spaces(typo.get("original", ""))
        suggestion = _preprocess_pdf_spaces(typo.get("suggestion", ""))
        if original in TAX_TERM_WHITELIST or suggestion in TAX_TERM_WHITELIST:
            continue
        filtered.append(typo)
    return filtered


def check_typos_ai(report_chunks: List[str], client: DeepSeekClient, max_workers: int = 8) -> Dict[str, List[Dict[str, Any]]]:
    """并发检查文本错别字和病句。

    返回：
        {"typos": [...], "grammar_errors": [...]}
        每项包含 original/suggestion/reason/confidence/severity
    """
    if not report_chunks:
        return {"typos": [], "grammar_errors": []}

    BATCH_SIZE = 5
    preprocessed_chunks = [_preprocess_pdf_spaces(chunk) for chunk in report_chunks]

    def check_batch(chunks: List[str]) -> Dict[str, Any]:
        separator = "\n\n=== 分隔符 ===\n\n"
        combined_text = separator.join(chunks)
        prompt = _TYPO_AND_GRAMMAR_CHECK_PROMPT.replace("{text}", combined_text)
        try:
            return client.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
            )
        except Exception as e:
            print(f"[WARN] 文本错别字/病句检查失败: {e}", file=sys.stderr)
            return {"typos": [], "grammar_errors": []}

    batches = [preprocessed_chunks[i:i + BATCH_SIZE] for i in range(0, len(preprocessed_chunks), BATCH_SIZE)]
    tasks = [(check_batch, (batch,), {}) for batch in batches]
    batch_results_list = run_concurrent(tasks, max_workers=max_workers)

    all_typos = []
    all_grammar_errors = []
    for batch_result in batch_results_list:
        if batch_result:
            all_typos.extend(batch_result.get("typos", []))
            all_grammar_errors.extend(batch_result.get("grammar_errors", []))

    filtered_typos = _filter_tax_terms(all_typos)

    final_typos = []
    for typo in filtered_typos:
        confidence = typo.get("confidence", 0.0)
        if confidence < 0.5:
            continue
        typo["severity"] = "error" if confidence >= 0.85 else "warning"
        final_typos.append(typo)

    final_grammar_errors = []
    for err in all_grammar_errors:
        confidence = err.get("confidence", 0.0)
        if confidence < 0.85:
            continue
        err["severity"] = "warning"
        final_grammar_errors.append(err)

    def _dedup(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for it in items:
            key = (it.get("original", ""), it.get("suggestion", ""))
            if key not in deduped:
                d = it.copy()
                d["count"] = 1
                deduped[key] = d
            else:
                deduped[key]["count"] = deduped[key].get("count", 1) + 1
        return list(deduped.values())

    return {
        "typos": _dedup(final_typos),
        "grammar_errors": _dedup(final_grammar_errors),
    }


# ──────────────────────────────────────────────────────────────────
# warning/error 预复核
# ──────────────────────────────────────────────────────────────────
_REVIEW_PROMPT = """你是税务师，复核税务报告检查结果。

检查类型：{check_type}
检查规则：{rule_name}
规则描述：{description}

应为值：{expected}
实际值：{actual}
差异：{difference}

原文摘录：
{context}

依据/证据：
{evidence}

判断此差异是以下哪种情况，返回 JSON：
- confirm: 确认真错（计算错误、加总不平、明显不一致、文字金额与表格金额确实不符）
- downgrade: 特殊结构或口径差异（百分比列、万元换算、不同口径、模板未填、合法调整），降为 info
- needs_review: 存疑（需要人工终审判断），保持 warning

返回格式：
{"decision": "confirm|downgrade|needs_review", "reason": "原因说明"}

直接返回 JSON，不要加 markdown 代码块标记。
"""


def review_results(warnings: List[Dict[str, Any]], client: DeepSeekClient, max_workers: int = 8) -> List[Dict[str, Any]]:
    """并发复核 warning/error 条目，返回三值决策。

    返回：
        复核结果列表，每项包含 original_index, decision, reason
    """
    if not warnings:
        return []

    BATCH_SIZE = 10

    def review_batch(items: List[Tuple[Dict[str, Any], int]]) -> List[Dict[str, Any]]:
        separator = "\n\n=== 项目分隔符 ===\n\n"
        item_texts = []
        for warning, idx in items:
            item_text = f"""项目索引: {idx}
检查类型: {warning.get("check_type", "")}
检查规则: {warning.get("rule_name", "")}
规则描述: {warning.get("description", "")}
应为值: {warning.get("expected", "")}
实际值: {warning.get("actual", "")}
差异: {warning.get("difference", "")}
原文摘录:
{warning.get("context", "")}
依据/证据:
{warning.get("evidence", "")}
"""
            item_texts.append(item_text)

        combined_text = separator.join(item_texts)
        batch_prompt = f"""你是税务师，复核税务报告检查结果。

**重要**：当前输入包含多个检查项（用 "=== 项目分隔符 ===" 分隔），请为每项返回独立的复核结果。

判断规则：
- confirm: 确认真错（计算错误、加总不平、明显不一致、文字金额与表格金额确实不符）
- downgrade: 特殊结构或口径差异（百分比列、万元换算、不同口径、模板未填、合法调整），降为 info
- needs_review: 存疑（需要人工终审判断），保持 warning

返回格式：
{{
  "reviews": [
    {{"index": 0, "decision": "confirm|downgrade|needs_review", "reason": "原因说明"}},
    ...
  ]
}}

检查项：
{combined_text}
"""
        try:
            result = client.chat_json(
                [{"role": "user", "content": batch_prompt}],
                temperature=0.0,
            )
            return result.get("reviews", [])
        except Exception as e:
            print(f"[WARN] 批量复核失败: {e}", file=sys.stderr)
            return []

    batches = []
    for i in range(0, len(warnings), BATCH_SIZE):
        batch = [(warnings[idx], idx) for idx in range(i, min(i + BATCH_SIZE, len(warnings)))]
        batches.append(batch)

    tasks = [(review_batch, (batch,), {}) for batch in batches]
    batch_results_list = run_concurrent(tasks, max_workers=max_workers)

    all_reviews = []
    for batch_results in batch_results_list:
        if batch_results:
            all_reviews.extend(batch_results)

    results = []
    for review in all_reviews:
        idx = review.get("index")
        decision = review.get("decision")
        reason = review.get("reason")
        if idx is None or decision not in ["confirm", "downgrade", "needs_review"]:
            continue
        results.append({
            "original_index": idx,
            "decision": decision,
            "reason": reason,
        })
    return results


# ──────────────────────────────────────────────────────────────────
# 自动生成 link_map
# ──────────────────────────────────────────────────────────────────
_LINK_MAP_PROMPT = """你是税务报告复核专家。请根据下面的 Word 报告全文和 Excel 申报表摘要，生成 report_text.md 段落金额与申报表单元格的勾稽链接 link_map。

标准勾稽点（按报告类型识别，优先覆盖）：
- 所得税汇算清缴：利润总额、应纳税所得额、应纳所得税额、应补（退）所得税额、纳税调增合计、纳税调减合计
- 研发费用加计扣除：账面归集研发费、审核可加计研发费、加计扣除总额、核减合计、委托研发、其他相关费用限额、项目数量、资本化金额
- 高新认定：三年研发费总额、各年研发费、高新收入总额、研发费占比

返回 JSON：
{{"links": [
  {{"id": "利润总额", "text_file": "报告.docx", "text_hint": "利润总额",
    "cell_file": "申报表.xlsx", "sheet": "A100000企业所得税年度纳税申报主表", "coord": "E21",
    "tolerance": 0.01, "note": "报告利润总额=主表利润总额"}}
]}}

规则：
1. text_hint 必须是 report_text.md 中真实存在的连续文字片段
2. sheet 必须是申报表摘要中真实存在的 sheet 名
3. coord 必须是对应 sheet 中真实存在的单元格坐标
4. text_file / cell_file 尽量使用真实文件名；如不确定可填空字符串
5. 只返回有把握的标准勾稽点，宁缺毋滥
6. tolerance 默认 0.01；涉及万元口径时可填 0.01（代码侧会自动处理万元放大）

Word 报告全文：
{report_text}

Excel 申报表摘要：
{sheets_summary}

直接返回 JSON，不要加 markdown 代码块标记。
"""


def _norm_coord(coord: str) -> str:
    """统一坐标格式：去掉 $，大写。"""
    return str(coord).replace("$", "").upper().strip()


def _build_sheets_summary(sheets_json: Dict[str, Any]) -> str:
    """把 sheets.json 构建成适合放进 prompt 的摘要字符串。"""
    lines: List[str] = []
    total_cells = 0
    MAX_TOTAL = 1200
    MAX_PER_SHEET = 150

    for fname, fdata in sheets_json.items():
        if not isinstance(fdata, dict):
            continue
        sheets = fdata.get("sheets", {})
        if not sheets:
            continue
        lines.append(f"文件: {fname}")
        for sname, sdata in sheets.items():
            dims = sdata.get("dims", "") if isinstance(sdata, dict) else ""
            cells = sdata.get("cells", {}) if isinstance(sdata, dict) else {}
            lines.append(f"  Sheet: {sname} ({dims})")
            cell_items = list(cells.items())
            if len(cell_items) > MAX_PER_SHEET:
                # 优先保留含标准关键词的单元格，再加前若干条保证上下文
                keywords = ["利润", "所得", "研发", "加计", "高新", "收入", "合计", "总计", "合计"]
                prioritized = [
                    (c, e) for c, e in cell_items
                    if any(kw in str(c) or kw in str(e.get("v", "")) for kw in keywords)
                ]
                rest = [x for x in cell_items if x not in prioritized]
                cell_items = prioritized[:MAX_PER_SHEET // 2] + rest[:MAX_PER_SHEET - MAX_PER_SHEET // 2]
            for coord, entry in cell_items:
                v = entry.get("v") if isinstance(entry, dict) else entry
                f = entry.get("f") if isinstance(entry, dict) else None
                val_str = "" if v is None else str(v)
                if f:
                    lines.append(f"    {coord}: {val_str}  [公式: {f}]")
                else:
                    lines.append(f"    {coord}: {val_str}")
                total_cells += 1
                if total_cells >= MAX_TOTAL:
                    lines.append("  ...（后续单元格省略以控制长度）")
                    return "\n".join(lines)
    return "\n".join(lines)


def _is_numeric_cell(v: Any) -> bool:
    """判断单元格缓存值是否为数值（代码侧校验用）。"""
    if v is None or v == "":
        return False
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    try:
        to_decimal(v)
        return True
    except Exception:
        return False


def generate_link_map(
    report_text_md: str,
    sheets_json: Dict[str, Any],
    client: DeepSeekClient,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """自动生成 report_text ↔ 申报表的勾稽链接。

    Args:
        report_text_md: report_text.md 全文
        sheets_json: sheets.json 字典
        client: DeepSeek 客户端

    Returns:
        (links, warnings)
        - links: 通过代码侧校验的 link 列表
        - warnings: 被丢弃的无效 link 及原因（可作为 results 条目使用）
    """
    if not report_text_md or not sheets_json:
        return [], []

    text_entries = parse_report_text(report_text_md)
    sheets_summary = _build_sheets_summary(sheets_json)

    prompt = _LINK_MAP_PROMPT.format(
        report_text=report_text_md[:20000],  # 控制长度，避免撞 token 上限
        sheets_summary=sheets_summary,
    )

    try:
        result = client.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        raw_links = result.get("links", []) if isinstance(result, dict) else []
    except Exception as e:
        print(f"[WARN] 自动生成 link_map 失败: {e}", file=sys.stderr)
        return [], []

    links: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    for raw in raw_links:
        if not isinstance(raw, dict):
            continue
        link = {
            "id": str(raw.get("id", "") or "").strip(),
            "text_file": str(raw.get("text_file", "") or "").strip(),
            "text_hint": str(raw.get("text_hint", "") or "").strip(),
            "cell_file": str(raw.get("cell_file", "") or "").strip(),
            "sheet": str(raw.get("sheet", "") or "").strip(),
            "coord": _norm_coord(raw.get("coord", "")),
            "tolerance": float(raw.get("tolerance", 0.01)),
            "note": str(raw.get("note", "") or "").strip(),
        }

        reasons: List[str] = []

        # 校验 1：coord 在 sheets.json 对应 sheet 里存在且值可解析为数字
        cell_matched_file = match_file_key(sheets_json.keys(), link["cell_file"])
        if cell_matched_file is None:
            reasons.append(f"未找到 cell_file '{link['cell_file']}'")
        else:
            fdata = sheets_json[cell_matched_file]
            sheets = fdata.get("sheets", {}) if isinstance(fdata, dict) else {}
            sdata = sheets.get(link["sheet"])
            if sdata is None:
                reasons.append(f"sheet '{link['sheet']}' 不存在")
            else:
                cells = sdata.get("cells", {}) if isinstance(sdata, dict) else {}
                centry = cells.get(link["coord"])
                if centry is None:
                    reasons.append(f"coord '{link['coord']}' 不存在")
                else:
                    v = centry.get("v") if isinstance(centry, dict) else centry
                    if not _is_numeric_cell(v):
                        reasons.append(f"coord '{link['coord']}' 值非数值: {v!r}")

        # 校验 2：text_hint 能在 report_text.md 中找到
        text_matched_file = None
        if link["text_file"]:
            text_matched_file = match_file_key({e.get("file", "") for e in text_entries}, link["text_file"])
        hint_found = any(
            (text_matched_file is None or e.get("file") == text_matched_file)
            and link["text_hint"] in e.get("text", "")
            for e in text_entries
        )
        if not hint_found:
            reasons.append(f"text_hint '{link['text_hint']}' 在 report_text.md 中未找到")

        if reasons:
            warnings.append({
                "link": link,
                "reasons": reasons,
            })
            continue

        # 使用匹配到的真实文件名（如果 AI 给的文件名只是近似）
        if cell_matched_file:
            link["cell_file"] = cell_matched_file
        if text_matched_file:
            link["text_file"] = text_matched_file
        links.append(link)

    return links, warnings


# ──────────────────────────────────────────────────────────────────
# 证据包（Evidence Bundle）终审引擎
# ──────────────────────────────────────────────────────────────────
# 常见税务报告误报模式库（注入每个证据包）
TAX_MISCLASSIFICATION_PATTERNS = """常见误报模式（终审时必须对照）：
1. 本来就是手工录入区：如 A000000 基础信息表、封面、填报表单勾选列，这些区域本就应该填数，不应报"公式区硬编码"。
2. 易失函数与跨表空值：公式含 TODAY/RAND/INDIRECT 或引用未打开工作簿，缓存值与重算值差异属于正常，不是错误。
3. 万元口径漏放大：报告文字用"万元"而申报表用"元"，或反之；数值差恰为 10^4 倍是口径问题，不是计算错误。
4. 取错段落数字：同一段落有"核减额""审核额""账面数"等多个数字，脚本可能取了非对应数字。
5. 模板未填的正常空白：成品未填写或模板示例值为 0/空，导致勾稽/完整性 warning，属于正常状态。
6. 合法纳税调整：A105000 等调整表账载≠税收是调整表本身的意义，不是错误。
7. 二选一段落残留：模板提示语"（以下没有核减的条目删除）"等在成品中应已删除，残留才报错。
"""


def _parse_source_location(source_location: str) -> Dict[str, str]:
    """从 source_location 字符串提取报告文件、Excel 文件、sheet、coord。"""
    info = {"report_file": "", "cell_file": "", "sheet": "", "coord": ""}
    parts = [p.strip() for p in str(source_location).split("|")]
    for part in parts:
        # Excel 部分：文件名.xlsx#sheet!coord 或 文件名.xlsx#sheet
        m = re.match(r"(.+\.(?:xlsx|xls))#([^!]+)(?:!([A-Za-z]+\d+))?", part)
        if m:
            info["cell_file"] = m.group(1).strip()
            info["sheet"] = m.group(2).strip()
            info["coord"] = m.group(3).strip().upper() if m.group(3) else ""
            continue
        # 报告部分：文件名.docx#... / 文件名.doc#...
        m = re.match(r"(.+\.(?:docx|doc))#.*", part)
        if m:
            info["report_file"] = m.group(1).strip()
    return info


def _involved_rows_from_findings(findings: List[Dict[str, Any]]) -> Set[int]:
    """从 findings 的 source_location 中提取所有涉及行号。"""
    rows: Set[int] = set()
    for f in findings:
        loc = _parse_source_location(f.get("source_location", ""))
        if loc["coord"]:
            _, row = split_coord(loc["coord"])
            if row is not None:
                rows.add(row)
    return rows


def _build_sheet_context(
    sheets_data: Dict[str, Any],
    cell_file: str,
    sheet: str,
    involved_rows: Set[int],
) -> str:
    """构建 sheet 上下文：≤40 行给全部，否则给表头行 + 涉及行±5 行。"""
    if not cell_file or not sheet or not sheets_data:
        return ""
    matched_file = match_file_key(sheets_data.keys(), cell_file)
    if matched_file is None:
        return f"[未找到文件 {cell_file}]"
    fdata = sheets_data[matched_file]
    if not isinstance(fdata, dict):
        return ""
    sdata = fdata.get("sheets", {}).get(sheet)
    if sdata is None:
        return f"[未找到 sheet {sheet}]"
    cells = sdata.get("cells", {}) if isinstance(sdata, dict) else {}
    rows = group_rows(cells)
    if not rows:
        return ""

    all_row_nums = sorted(rows.keys())
    if len(all_row_nums) <= 40:
        selected_rows = all_row_nums
    else:
        selected_set: Set[int] = set(r for r in all_row_nums if r <= 2)  # 表头行
        for r in involved_rows:
            for rr in range(r - 5, r + 6):
                if rr in rows:
                    selected_set.add(rr)
        selected_rows = sorted(selected_set)

    lines = [f"Sheet 上下文: {matched_file}#{sheet}"]
    for r in selected_rows:
        items = rows[r]
        row_text = " | ".join(f"{coord}={entry.get('v', '')}" for _, coord, entry in items)
        lines.append(f"  行{r}: {row_text}")
    return "\n".join(lines)


def _find_report_excerpt(
    report_text_md: str,
    report_file: str,
    finding: Dict[str, Any],
    context_radius: int = 150,
) -> str:
    """在 report_text.md 中定位 finding 相关的原文摘录。"""
    if not report_text_md:
        return ""
    entries = parse_report_text(report_text_md)
    if not entries:
        return ""

    matched_file = None
    if report_file:
        matched_file = match_file_key({e.get("file", "") for e in entries}, report_file)

    # 候选关键词：context > description > rule_name
    candidates: List[str] = []
    for key in ("context", "description", "rule_name"):
        val = finding.get(key)
        if val and len(str(val).strip()) >= 2:
            candidates.append(str(val).strip())

    # 额外加入常见 tax 关键词（从 description/context 中匹配）
    tax_kws = ["利润总额", "应纳税所得额", "应纳所得税额", "应补", "纳税调整", "加计扣除",
               "研发费用", "高新收入", "研发费", "账面归集", "审核后"]
    for c in candidates[:]:
        for kw in tax_kws:
            if kw in c:
                candidates.append(kw)

    for kw in candidates:
        if len(kw) < 2:
            continue
        for e in entries:
            if matched_file is not None and e.get("file") != matched_file:
                continue
            text = e.get("text", "")
            idx = text.find(kw)
            if idx >= 0:
                start = max(0, idx - context_radius)
                end = min(len(text), idx + len(kw) + context_radius)
                row_suffix = f"!row={e.get('row')}" if e.get('row') else ""
                return (f"报告原文摘录: {e.get('file')}#{e.get('kind')}={e.get('idx')}{row_suffix}\n"
                        f"{text[start:end]}")
    return ""


def _findings_to_text(findings: List[Dict[str, Any]], original_indices: List[int]) -> str:
    """把 findings 格式化为纯文本。"""
    lines = ["检查发现问题:"]
    for i, (idx, f) in enumerate(zip(original_indices, findings)):
        lines.append(
            f"  [索引 {idx}] 类型={f.get('check_type', '')} 规则={f.get('rule_name', '')}\n"
            f"    描述={f.get('description', '')}\n"
            f"    应为值={f.get('expected', '')} 实际值={f.get('actual', '')} 差异={f.get('difference', '')}\n"
            f"    位置={f.get('source_location', '')}\n"
            f"    原文摘录={f.get('context', '')}\n"
            f"    已有证据={f.get('evidence', '')}"
        )
    return "\n".join(lines)


def build_evidence_bundles(
    results: List[Dict[str, Any]],
    sheets_data: Dict[str, Any],
    report_text_md: str,
) -> List[Dict[str, Any]]:
    """把未通过的 error/warning 结果分组打包成证据包。

    返回：
        证据包列表，每项包含 findings / original_indices / sheet_context /
        report_excerpt / misclassification_patterns / bundle_text
    """
    # 只处理未通过的 error/warning
    candidates: List[Tuple[int, Dict[str, Any]]] = [
        (idx, r) for idx, r in enumerate(results)
        if not r.get("passed") and r.get("severity") in ("error", "warning")
    ]
    if not candidates:
        return []

    # 按 (check_type, 文件/sheet) 分组
    groups: Dict[Tuple[str, str, str], List[Tuple[int, Dict[str, Any]]]] = {}
    for idx, r in candidates:
        ctype = r.get("check_type", "")
        loc = _parse_source_location(r.get("source_location", ""))
        if loc["cell_file"] and loc["sheet"]:
            key = (ctype, loc["cell_file"], loc["sheet"])
        elif loc["report_file"]:
            key = (ctype, "report", loc["report_file"])
        else:
            key = (ctype, "other", "")
        groups.setdefault(key, []).append((idx, r))

    MAX_PER_BUNDLE = 8
    bundles: List[Dict[str, Any]] = []

    for (ctype, kind_key, name_key), items in groups.items():
        # 每组按原始索引排序，再切分 ≤8 条
        items = sorted(items, key=lambda x: x[0])
        for i in range(0, len(items), MAX_PER_BUNDLE):
            chunk = items[i:i + MAX_PER_BUNDLE]
            original_indices = [idx for idx, _ in chunk]
            findings = [r for _, r in chunk]

            # 取第一个有 Excel 位置的信息用于 sheet 上下文
            cell_file = sheet = ""
            for _, r in chunk:
                loc = _parse_source_location(r.get("source_location", ""))
                if loc["cell_file"] and loc["sheet"]:
                    cell_file, sheet = loc["cell_file"], loc["sheet"]
                    break
            involved_rows = _involved_rows_from_findings(findings)
            sheet_context = _build_sheet_context(sheets_data, cell_file, sheet, involved_rows)

            # 报告原文摘录：优先取 link 类或带报告文件位置的 finding
            report_excerpt = ""
            for idx, r in chunk:
                loc = _parse_source_location(r.get("source_location", ""))
                if loc["report_file"] or r.get("check_type") == "报告勾稽":
                    report_excerpt = _find_report_excerpt(report_text_md, loc["report_file"], r)
                    if report_excerpt:
                        break
            if not report_excerpt:
                # 兜底：用任意 finding 试试
                for idx, r in chunk:
                    loc = _parse_source_location(r.get("source_location", ""))
                    excerpt = _find_report_excerpt(report_text_md, loc["report_file"], r)
                    if excerpt:
                        report_excerpt = excerpt
                        break

            bundle_text = "\n\n".join([
                _findings_to_text(findings, original_indices),
                sheet_context or "[无 sheet 上下文]",
                report_excerpt or "[无报告原文摘录]",
                "=== 误报模式库 ===",
                TAX_MISCLASSIFICATION_PATTERNS,
            ])

            bundles.append({
                "check_type": ctype,
                "kind": kind_key,
                "name": name_key,
                "findings": findings,
                "original_indices": original_indices,
                "sheet_context": sheet_context,
                "report_excerpt": report_excerpt,
                "misclassification_patterns": TAX_MISCLASSIFICATION_PATTERNS,
                "bundle_text": bundle_text,
            })

    return bundles


# 终审 verdict 四值
VERDICTS = {"confirm_true_error", "downgrade_suspect", "delete_false_positive", "needs_human"}

_FINAL_REVIEW_SYSTEM_PROMPT = """你是税务报告终审专家。基于给定的证据包，对每条检查发现做四值判定。

四值说明：
- confirm_true_error：确认真错（计算错误、勾稽不符、明显不一致、模板占位符未替换等）。
- downgrade_suspect：口径/结构差异，降为 info（万元换算、手工录入区、易失函数、模板未填、合法调整等）。
- delete_false_positive：误报，标记为通过（如专业术语被当错别字、取错段落数字、空白区正常等）。
- needs_human：证据不足或口径判断需要税务师/人工回原文定案，禁止猜测。

强制规则：
1. 判定必须引用证据包中的具体行/列/单元格/原文作为依据。
2. evidence_quote 必须是证据包原文的精确子串（去首尾空白后）。
3. 无法从给定证据得出结论时，必须返回 needs_human，禁止编造。
4. confidence 0-1；低于 0.6 的判定无效，视为 needs_human。

返回 JSON：
{"reviews": [{"index": 0, "verdict": "confirm_true_error|downgrade_suspect|delete_false_positive|needs_human", "reason": "...", "evidence_quote": "...", "confidence": 0.92}]}
"""


_FINAL_REVIEW_USER_PROMPT = """请对以下证据包中的每条检查发现做终审判定。

{evidence_bundle}

请严格按 system prompt 要求返回 JSON，不要加 markdown 代码块。
"""


def _validate_review(review: Dict[str, Any], bundle: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """代码侧校验单个 review。返回 (是否通过, 失败原因)。"""
    idx = review.get("index")
    if idx is None or not (0 <= idx < len(bundle["findings"])):
        return False, "index 越界"

    verdict = review.get("verdict")
    if verdict not in VERDICTS:
        return False, f"verdict 非法: {verdict}"

    confidence = float(review.get("confidence", 0.0))
    if confidence < 0.6:
        return False, "confidence < 0.6"

    quote = str(review.get("evidence_quote", "")).strip()
    if quote and quote not in bundle["bundle_text"]:
        return False, "evidence_quote 不是证据包原文子串"
    # 非 needs_human 判定必须有证据锚点：空引文 = 无锚点，强制落人工
    if not quote and verdict != "needs_human":
        return False, "缺少证据锚点(evidence_quote 为空)"

    # confirm 且 finding 有可解析 expected/actual 时，复验 difference 一致性
    if verdict == "confirm_true_error":
        finding = bundle["findings"][idx]
        try:
            expected = to_decimal(finding.get("expected", ""))
            actual = to_decimal(finding.get("actual", ""))
            diff = abs(expected - actual)
            reported_diff = finding.get("difference", "")
            if reported_diff:
                reported = to_decimal(reported_diff)
                if abs(diff - reported) > Decimal("0.02"):
                    return False, f"difference 不一致: {diff} vs {reported_diff}"
        except Exception:
            pass  # 解析不了就跳过此项校验

    return True, None


def final_review(
    results: List[Dict[str, Any]],
    sheets_data: Dict[str, Any],
    report_text_md: str,
    client: DeepSeekClient,
    max_workers: int = 8,
) -> List[Dict[str, Any]]:
    """基于证据包对每个未通过 error/warning 做 API 终审。

    返回：
        review 列表，每项包含 original_index / verdict / reason / evidence_quote / confidence
    """
    bundles = build_evidence_bundles(results, sheets_data, report_text_md)
    if not bundles:
        return []

    def review_bundle(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
        n = len(bundle["findings"])
        prompt = _FINAL_REVIEW_USER_PROMPT.format(evidence_bundle=bundle["bundle_text"])
        # 显式枚举索引，强制 LLM 逐条返回（防止"对每条检查发现做判定"被部分执行）
        prompt += f"\n本证据包共 {n} 条发现（索引 0 到 {n - 1}），必须逐条返回判定，不得遗漏任何索引。"
        try:
            response = client.chat_json(
                [
                    {"role": "system", "content": _FINAL_REVIEW_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )
            raw_reviews = response.get("reviews", []) if isinstance(response, dict) else []
        except Exception as e:
            print(f"[WARN] 证据包终审请求失败: {e}", file=sys.stderr)
            return []

        validated: List[Dict[str, Any]] = []
        for raw in raw_reviews:
            if not isinstance(raw, dict):
                continue
            review = {
                "index": raw.get("index"),
                "verdict": raw.get("verdict"),
                "reason": str(raw.get("reason", "")),
                "evidence_quote": str(raw.get("evidence_quote", "")),
                "confidence": float(raw.get("confidence", 0.0)),
            }
            ok, fail_reason = _validate_review(review, bundle)
            # index 守卫：非法/越界 index 无法定位原条目，丢弃该条 review（其余条目不受影响）
            idx_val = review.get("index")
            if not isinstance(idx_val, int) or not (0 <= idx_val < len(bundle["original_indices"])):
                continue
            original_index = bundle["original_indices"][idx_val]
            if not ok:
                review["verdict"] = "needs_human"
                review["reason"] = f"{review.get('reason', '')}（校验未通过：{fail_reason}）".strip()
            validated.append({
                "original_index": original_index,
                "verdict": review["verdict"],
                "reason": review["reason"],
                "evidence_quote": review["evidence_quote"],
                "confidence": review["confidence"],
            })
        return validated

    tasks = [(review_bundle, (b,), {}) for b in bundles]
    all_reviews_nested = run_concurrent(tasks, max_workers=max_workers)

    all_reviews: List[Dict[str, Any]] = []
    for bundle_reviews in all_reviews_nested:
        if bundle_reviews:
            all_reviews.extend(bundle_reviews)
    return all_reviews


def apply_verdicts(results: List[Dict[str, Any]], reviews: List[Dict[str, Any]]) -> List[str]:
    """把终审 verdict 应用到 results。

    直接修改 results 条目，保留审计痕迹（不物理删除）。
    返回 needs_human 条目的原始索引字符串清单。
    """
    needs_human: List[str] = []
    for review in reviews:
        idx = review.get("original_index")
        if idx is None or idx < 0 or idx >= len(results):
            continue
        result = results[idx]
        verdict = review.get("verdict")
        reason = review.get("reason", "")
        evidence = result.get("evidence", "")
        prefix = ""

        if verdict == "confirm_true_error":
            prefix = "API终审：确认真错 + "
        elif verdict == "downgrade_suspect":
            result["severity"] = "info"
            prefix = "API终审：口径/结构说明 + "
        elif verdict == "delete_false_positive":
            result["passed"] = True
            result["severity"] = "info"
            prefix = "API终审：误报删除 + "
        elif verdict == "needs_human":
            prefix = "API终审：需人工复核 + "
            needs_human.append(str(idx))
        else:
            continue

        result["evidence"] = ((evidence + "\n") if evidence else "") + prefix + str(reason)
    return needs_human
