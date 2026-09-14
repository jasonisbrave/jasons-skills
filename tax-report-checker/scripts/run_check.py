#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复核编排：parse_tax → check_formulas →（standard/deep: check_values, check_completeness）
→（提供 link-map 时: check_links）→ 汇总写出 <outdir>/results.json。

用法:
    python run_check.py <报告目录> -o <outdir> [--scope quick|standard|deep]
                        [--link-map path] [--skip-values]
                        [--use-ai|--no-ai] [--auto-link-map]
                        [--api-key KEY] [--model MODEL]
                        [--skip-text-ai] [--skip-ai-review]
                        [--max-workers N]

- quick:    公式健康 + 完整性
- standard/deep: 公式健康 + 数值校验 + 完整性 + AI 文本检查 + AI 预复核
  （提供/自动生成 link-map 时再加报告勾稽）
- 单项检查失败（如缺文件）不中断，记一条 warning "检查未执行" 并继续
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Windows 控制台输出防乱码（Python 3.7+）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import check_completeness  # noqa: E402
import check_formulas  # noqa: E402
import check_links  # noqa: E402
import check_values  # noqa: E402
import parse_tax  # noqa: E402
from _common import count_severities, dump_json, load_json, make_result, setup_console  # noqa: E402
from ai_worker import (  # noqa: E402
    DeepSeekClient,
    apply_verdicts,
    build_evidence_bundles,
    check_api_key,
    check_typos_ai,
    chunk_report_text,
    final_review,
    generate_link_map,
    review_results,
)


def _fail(msg: str):
    raise FileNotFoundError(msg)


def _guarded(check_type: str, name: str, fn) -> list[dict]:
    """执行单项检查；失败不中断，记 warning "检查未执行"。"""
    try:
        return fn()
    except Exception as e:
        print(f"  [WARN] {name} 未执行: {type(e).__name__}: {e}")
        return [make_result(
            check_type, "检查未执行", "warning", False,
            f"{name} 未执行: {type(e).__name__}: {e}",
        )]


def _build_ai_client(use_ai: bool, api_key: str | None, model: str | None):
    """根据 CLI 参数决定 AI 是否启用，并返回 (use_ai, client_or_None, config_or_None)。"""
    client = None
    config = None
    if use_ai:
        has_key, config = check_api_key(api_key)
        if not has_key or config is None:
            print("[INFO] 未配置 DeepSeek，降级纯代码模式", file=sys.stderr)
            use_ai = False
            config = None
        else:
            chosen_model = model or config.get("model") or "deepseek-chat"
            client = DeepSeekClient(
                api_key=config["api_key"],
                model=chosen_model,
                base_url=config.get("base_url", "https://api.deepseek.com"),
            )
    return use_ai, client, config


def _run_ai_text_check(text_md_path: Path, client: DeepSeekClient, max_workers: int) -> list[dict]:
    """AI 文本错别字/病句检查。"""
    results: list[dict] = []
    if not text_md_path.exists():
        return results
    try:
        chunks = chunk_report_text(text_md_path.read_text(encoding="utf-8"))
        if not chunks:
            return results
        ai_result = check_typos_ai(chunks, client, max_workers=max_workers)
        for typo in ai_result.get("typos", []):
            results.append(make_result(
                check_type="文本格式",
                rule_name="AI错别字检查",
                severity=typo.get("severity", "warning"),
                passed=False,
                description=f"疑似错别字：{typo.get('original', '')} → {typo.get('suggestion', '')}",
                context=typo.get("original", ""),
                evidence=typo.get("reason", ""),
                source_location="AI文本检查",
                confidence=typo.get("confidence"),
            ))
        for err in ai_result.get("grammar_errors", []):
            results.append(make_result(
                check_type="文本格式",
                rule_name="AI病句检查",
                severity=err.get("severity", "warning"),
                passed=False,
                description=f"疑似病句：{err.get('original', '')}",
                context=err.get("original", ""),
                evidence=err.get("reason", ""),
                source_location="AI文本检查",
                confidence=err.get("confidence"),
            ))
    except Exception as e:
        print(f"[WARN] AI 文本检查失败: {type(e).__name__}: {e}", file=sys.stderr)
    return results


def _run_ai_review(all_results: list[dict], client: DeepSeekClient, max_workers: int) -> None:
    """AI 预复核：直接修改 all_results 中的条目。"""
    candidates = [r for r in all_results
                  if r.get("severity") in ("error", "warning") and not r.get("passed")]
    if not candidates:
        return
    try:
        reviews = review_results(candidates, client, max_workers=max_workers)
    except Exception as e:
        print(f"[WARN] AI 预复核失败: {type(e).__name__}: {e}", file=sys.stderr)
        return

    for review in reviews:
        idx = review.get("original_index")
        decision = review.get("decision")
        reason = review.get("reason", "")
        if idx is None or idx < 0 or idx >= len(candidates):
            continue
        result = candidates[idx]
        evidence = result.get("evidence", "")
        if decision == "confirm":
            result["evidence"] = ((evidence + "\n") if evidence else "") + f"Agent复核：确认 + {reason}"
        elif decision == "downgrade":
            result["severity"] = "info"
            result["evidence"] = ((evidence + "\n") if evidence else "") + f"Agent复核：口径/结构说明: {reason}"
        elif decision == "needs_review":
            result["evidence"] = ((evidence + "\n") if evidence else "") + f"Agent复核：存疑 + {reason}"


def _write_needs_human_review(
    needs_human: list[str],
    all_results: list[dict],
    bundles: list[dict],
    out_path: Path,
) -> None:
    """写出待人工复核.md，供 Agent/人工回原文定案。"""
    bundle_by_idx: dict[int, dict] = {}
    for b in bundles:
        for idx in b.get("original_indices", []):
            bundle_by_idx[idx] = b

    lines = ["# 待人工复核条目", ""]
    lines.append("以下条目经 API 证据包终审后仍证据不足或口径判断需要人工定案，请回原文核对。")
    lines.append("")

    for idx_str in needs_human:
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        if idx < 0 or idx >= len(all_results):
            continue
        r = all_results[idx]
        bundle = bundle_by_idx.get(idx)
        lines.append(f"## 条目索引 {idx}")
        lines.append(f"- 检查类型: {r.get('check_type', '')}")
        lines.append(f"- 规则: {r.get('rule_name', '')}")
        lines.append(f"- 描述: {r.get('description', '')}")
        lines.append(f"- 位置: {r.get('source_location', '')}")
        lines.append(f"- 应为值: {r.get('expected', '')}")
        lines.append(f"- 实际值: {r.get('actual', '')}")
        lines.append(f"- 差异: {r.get('difference', '')}")
        lines.append(f"- 已有证据: {r.get('evidence', '')}")
        if bundle:
            lines.append("- 证据包摘录:")
            lines.append("```")
            lines.append(bundle.get("bundle_text", "")[:1200])
            lines.append("```")
        lines.append("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(report_dir: str | Path, outdir: str | Path, scope: str = "standard",
        link_map: str | Path | None = None, skip_values: bool = False,
        use_ai: bool = True, auto_link_map: bool = False,
        api_key: str | None = None, model: str | None = None,
        skip_text_ai: bool = False, skip_ai_review: bool = False,
        use_final_review: bool = True, max_workers: int = 60) -> dict:
    """编排入口。返回最终 results.json 的内容 dict。"""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    all_results: list[dict] = []
    step = 0

    # AI 开关 + 客户端初始化
    use_ai, client, _ = _build_ai_client(use_ai, api_key, model)

    # 1) 解析
    step += 1
    print(f"[{step}] 解析文件（parse_tax）...")
    manifest = _guarded("完整性", "parse_tax 解析",
                        lambda: parse_tax.run(report_dir, out))
    n_files = len(manifest.get("files", [])) if isinstance(manifest, dict) else 0
    print(f"  解析对象 {n_files} 个")

    sheets_json_path = out / "sheets.json"
    text_md_path = out / "report_text.md"

    # 2) AI 文本检查（标准/深度且启用 AI）
    if use_ai and client is not None and not skip_text_ai and scope != "quick":
        step += 1
        print(f"[{step}] AI 文本检查（错别字/病句）...")
        ai_text_results = _run_ai_text_check(text_md_path, client, max_workers)
        all_results += ai_text_results
        print(f"  发现 {len(ai_text_results)} 项")

    # 3) 公式健康（quick/standard/deep 都跑）
    step += 1
    print(f"[{step}] 公式健康检查（check_formulas）...")
    if not sheets_json_path.exists():
        all_results += _guarded("公式健康", "check_formulas", lambda: _fail("缺少 sheets.json"))
    else:
        res = _guarded("公式健康", "check_formulas", lambda: check_formulas.run(sheets_json_path))
        all_results += res
        dump_json(out / "check_formulas.json", res)
        print(f"  发现 {len(res) - 1} 项")

    # 4) 数值校验（standard/deep 且未 --skip-values）
    if scope != "quick" and not skip_values:
        step += 1
        print(f"[{step}] 数值校验（check_values）...")
        if not sheets_json_path.exists():
            all_results += _guarded("数值校验", "check_values", lambda: _fail("缺少 sheets.json"))
        else:
            res = _guarded("数值校验", "check_values", lambda: check_values.run(sheets_json_path))
            all_results += res
            dump_json(out / "check_values.json", res)
            stat = next((r for r in res if r["rule_name"] == "数值校验统计"), None)
            print(f"  {stat['description'] if stat else '完成'}")

    # 5) 完整性（quick/standard/deep 都跑）
    step += 1
    print(f"[{step}] 完整性检查（check_completeness）...")
    if not sheets_json_path.exists():
        all_results += _guarded("完整性", "check_completeness", lambda: _fail("缺少 sheets.json"))
    else:
        res = _guarded("完整性", "check_completeness",
                       lambda: check_completeness.run(sheets_json_path, text_md_path if text_md_path.exists() else None))
        all_results += res
        dump_json(out / "check_completeness.json", res)
        print(f"  发现 {len(res)} 项")

    # 6) 自动生成 / 加载 link_map
    link_map_path = link_map
    if auto_link_map and link_map_path is None:
        if use_ai and client is not None and text_md_path.exists() and sheets_json_path.exists():
            step += 1
            print(f"[{step}] 自动生成 link_map...")
            try:
                sheets_data = load_json(sheets_json_path)
                report_text = text_md_path.read_text(encoding="utf-8")
                links, link_warnings = generate_link_map(report_text, sheets_data, client)
                link_map_path = out / "link_map.json"
                dump_json(link_map_path, {"links": links})
                print(f"  生成 {len(links)} 条有效链接，丢弃 {len(link_warnings)} 条")
                for w in link_warnings:
                    print(f"    [丢弃] {w['link'].get('id', '?')}: {'; '.join(w.get('reasons', []))}", file=sys.stderr)
            except Exception as e:
                print(f"[WARN] 自动生成 link_map 失败: {type(e).__name__}: {e}", file=sys.stderr)
                link_map_path = None
        else:
            print("[WARN] --auto-link-map 需要 AI、report_text.md 和 sheets.json，已跳过", file=sys.stderr)

    # 7) 报告勾稽（提供 link-map 时）
    if link_map_path is not None:
        step += 1
        print(f"[{step}] 报告勾稽（check_links）...")
        res = _guarded("报告勾稽", "check_links", lambda: check_links.run(out, link_map_path))
        all_results += res
        dump_json(out / "check_links.json", res)
        print(f"  发现 {len(res)} 项")

    # 8) AI 终审 / 预复核（标准/深度且启用 AI）
    final_review_stats = {"applied": False, "needs_human": 0, "confirmed": 0, "downgraded": 0, "deleted": 0, "unreviewed": 0}
    needs_human: list[str] = []
    if use_ai and client is not None and not skip_ai_review and scope != "quick":
        if use_final_review and sheets_json_path.exists() and text_md_path.exists():
            step += 1
            print(f"[{step}] API 证据包终审（final_review）...")
            try:
                sheets_data = load_json(sheets_json_path)
                report_text = text_md_path.read_text(encoding="utf-8")
                reviews = final_review(all_results, sheets_data, report_text, client, max_workers=max_workers)
                needs_human = apply_verdicts(all_results, reviews)
                # 缺审兜底：LLM 未返回判定的候选条目安全默认落 needs_human（不产出未经复核的静默结论）
                reviewed = {r.get("original_index") for r in reviews if r.get("original_index") is not None}
                unreviewed = [i for i, r in enumerate(all_results)
                              if not r.get("passed") and r.get("severity") in ("error", "warning")
                              and i not in reviewed]
                for i in unreviewed:
                    r = all_results[i]
                    ev = r.get("evidence", "")
                    r["evidence"] = ((ev + "\n") if ev else "") + "API终审：需人工复核 + LLM 未返回该条判定，安全默认落人工"
                    needs_human.append(str(i))
                final_review_stats = {
                    "applied": True,
                    "needs_human": len(needs_human),
                    "confirmed": sum(1 for r in reviews if r.get("verdict") == "confirm_true_error"),
                    "downgraded": sum(1 for r in reviews if r.get("verdict") == "downgrade_suspect"),
                    "deleted": sum(1 for r in reviews if r.get("verdict") == "delete_false_positive"),
                    "unreviewed": len(unreviewed),
                }
                if needs_human:
                    bundles = build_evidence_bundles(all_results, sheets_data, report_text)
                    _write_needs_human_review(needs_human, all_results, bundles, out / "待人工复核.md")
                    print(f"  终审 {len(reviews)} 项（LLM 缺审 {len(unreviewed)} 项落人工），needs_human 共 {len(needs_human)} 项 → 待人工复核.md")
                else:
                    print(f"  终审 {len(reviews)} 项完成")
            except Exception as e:
                print(f"[WARN] API 终审失败: {type(e).__name__}: {e}", file=sys.stderr)
        else:
            step += 1
            print(f"[{step}] AI 轻量预复核（终审前置条件不足或 --skip-final-review）...")
            before = len([r for r in all_results if r.get("severity") in ("error", "warning") and not r.get("passed")])
            _run_ai_review(all_results, client, max_workers)
            after = len([r for r in all_results if r.get("severity") in ("error", "warning") and not r.get("passed")])
            print(f"  复核 {before} 项，剩余 error/warning {after} 项")

    counts = count_severities(all_results)
    doc = {
        "meta": {
            "report_dir": str(report_dir),
            "scope": scope,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "counts": counts,
            "final_review": final_review_stats,
        },
        "results": all_results,
    }
    results_path = dump_json(out / "results.json", doc)

    print(f"\n[run_check] 完成 → {results_path}")
    print(f"  scope={scope} | error {counts['error']} / warning {counts['warning']} "
          f"/ info {counts['info']} / passed {counts['passed']}")
    return doc


def main() -> None:
    setup_console()
    parser = argparse.ArgumentParser(description="税务报告复核编排")
    parser.add_argument("report_dir", help="报告目录（或单文件）")
    parser.add_argument("-o", "--outdir", required=True, help="输出目录")
    parser.add_argument("--scope", choices=["quick", "standard", "deep"], default="standard",
                        help="检查范围：quick=公式健康+完整性；standard/deep=全量（deep 预留）")
    parser.add_argument("--link-map", default=None, help="勾稽链接定义 link_map.json（可选）")
    parser.add_argument("--skip-values", action="store_true", help="跳过数值校验")
    parser.add_argument("--use-ai", dest="use_ai", action="store_true", default=True,
                        help="启用 AI 层（默认启用）")
    parser.add_argument("--no-ai", dest="use_ai", action="store_false",
                        help="禁用 AI 层，只跑纯代码检查")
    parser.add_argument("--auto-link-map", action="store_true",
                        help="自动生成 link_map.json（未提供 --link-map 时生效，需 AI）")
    parser.add_argument("--api-key", default=None, help="DeepSeek API key")
    parser.add_argument("--model", default=None, help="DeepSeek 模型名称")
    parser.add_argument("--skip-text-ai", action="store_true", help="跳过 AI 文本错别字/病句检查")
    parser.add_argument("--skip-ai-review", action="store_true", help="跳过 AI 预复核/终审")
    parser.add_argument("--final-review", dest="use_final_review", action="store_true", default=True,
                        help="启用 API 证据包终审（默认启用）")
    parser.add_argument("--skip-final-review", dest="use_final_review", action="store_false",
                        help="跳过 API 终审，回退到轻量预复核")
    parser.add_argument("--max-workers", type=int, default=60, help="AI 并发任务数（默认 60）")
    args = parser.parse_args()
    try:
        run(
            args.report_dir, args.outdir, args.scope,
            args.link_map, args.skip_values,
            use_ai=args.use_ai,
            auto_link_map=args.auto_link_map,
            api_key=args.api_key,
            model=args.model,
            skip_text_ai=args.skip_text_ai,
            skip_ai_review=args.skip_ai_review,
            use_final_review=args.use_final_review,
            max_workers=args.max_workers,
        )
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 复核编排失败: {type(e).__name__}: {e}", file=sys.stderr)
        print("提示: 请确认报告目录存在且包含 .docx/.doc/.xlsx/.xls 文件，"
              "且输出目录可写。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
