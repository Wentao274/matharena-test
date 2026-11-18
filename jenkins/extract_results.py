#!/usr/bin/env python3
"""MathArena 评测结果提取脚本(由 jenkins/run_matharena_eval.py 编排调用,也可单独使用)。

MathArena 没有标准的汇总报告文件(官方 app.py 是交互式网页检查工具),
因此参考 sgl-eval 的 metrics.json 模式,从 outputs/ 的逐题运行文件中
提取 pass@1 等评分数据,生成:
  - result_summary.json : 供 Jenkins 邮件阶段读取(Groovy readJSON)
  - result_summary.csv  : 逐题明细(邮件附件)

读取对象: <output-dir>/<comp>/<model>/<problem_idx>.json.zst
其中 pass_at_1 字段的取值:
  - float      : 该题已评判,值为该题 pass@1(0~1)
  - "TODO Grading" : 该题尚未评判(judge.py 未跑或未覆盖)
  - None       : 无有效运行
"""

import argparse
import csv
import json
import os
import sys

import yaml

from matharena.json_zst import load_json_zst

# 数据集显示名映射(未命中时原样展示配置路径)
DISPLAY_NAMES = {
    "arxiv/september": "ArXivMath 09/2026",
    "arxiv/august": "ArXivMath 08/2026",
    "arxiv_false/september": "BrokenArXiv 09/2026",
    "arxiv_false/august": "BrokenArXiv 08/2026",
    "aime/aime_2026": "AIME 2026",
    "aime/aime_2025": "AIME 2025",
    "hmmt/hmmt_feb_2026": "HMMT Feb 2026",
    "usamo/usamo_2026": "USAMO 2026",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Extract MathArena scores into summary files")
    parser.add_argument("--output-dir", required=True, help="MathArena outputs 根目录(run.py --output-dir)")
    parser.add_argument("--model", required=True, help="模型配置名(solver 目录名,如 alaya_test_model)")
    parser.add_argument("--comps", required=True, nargs="+", help="竞赛配置路径列表,如 arxiv/september")
    parser.add_argument("--comp-configs-dir", default="configs/competitions", help="竞赛配置目录(读取期望题数)")
    parser.add_argument("--result-dir", required=True, help="汇总文件输出目录")
    return parser.parse_args()


def _load_run_file(path):
    """读取单个运行文件,失败返回 None(计入 broken)."""
    try:
        return load_json_zst(path)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: 无法读取运行文件 {path}: {exc}")
        return None


def _sum_or_none(values):
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def extract_competition(output_dir, comp, model, expected_problems):
    solver_dir = os.path.join(output_dir, comp, model)
    problems = []
    n_broken = 0
    if os.path.isdir(solver_dir):
        for fname in sorted(os.listdir(solver_dir)):
            if not fname.endswith(".json.zst"):
                continue
            run = _load_run_file(os.path.join(solver_dir, fname))
            if run is None:
                n_broken += 1
                continue
            correct = run.get("correct", [])
            n_runs = len(correct)
            n_correct = sum(1 for c in correct if c is True)
            n_unjudged = sum(1 for c in correct if isinstance(c, str))
            pass_at_1 = run.get("pass_at_1")
            warnings = run.get("warnings", [])
            n_warned = sum(1 for w in warnings if w)
            detailed_costs = run.get("detailed_costs", [])
            problems.append(
                {
                    "problem_idx": run.get("idx"),
                    "n_runs": n_runs,
                    "n_correct_runs": n_correct,
                    "n_unjudged_runs": n_unjudged,
                    "pass_at_1": pass_at_1,
                    "pass_at_1_display": (
                        f"{pass_at_1 * 100:.2f}%"
                        if isinstance(pass_at_1, (int, float))
                        else ("TODO" if pass_at_1 == "TODO Grading" else "N/A")
                    ),
                    "n_warned_runs": n_warned,
                    "input_tokens": _sum_or_none([dc.get("input_tokens") for dc in detailed_costs]),
                    "output_tokens": _sum_or_none([dc.get("output_tokens") for dc in detailed_costs]),
                    "time_seconds": _sum_or_none([dc.get("time") for dc in detailed_costs]),
                }
            )

    graded_values = [p["pass_at_1"] for p in problems if isinstance(p["pass_at_1"], (int, float))]
    n_graded = len(graded_values)
    n_todo = sum(1 for p in problems if p["pass_at_1"] == "TODO Grading")
    n_run = len(problems)
    summary = {
        "name": comp,
        "display_name": DISPLAY_NAMES.get(comp, comp),
        "expected_problems": expected_problems,
        "n_problems_run": n_run,
        "n_graded": n_graded,
        "n_todo_grading": n_todo,
        "n_missing": max(expected_problems - n_run, 0),
        "n_broken_files": n_broken,
        "pass_at_1": (sum(graded_values) / n_graded) if n_graded else None,
        "pass_at_1_display": (f"{sum(graded_values) / n_graded * 100:.2f}%" if n_graded else "N/A"),
        "n_correct_runs": sum(p["n_correct_runs"] for p in problems),
        "n_total_runs": sum(p["n_runs"] for p in problems),
        "n_warned_runs": sum(p["n_warned_runs"] for p in problems),
        "input_tokens": _sum_or_none([p["input_tokens"] for p in problems]),
        "output_tokens": _sum_or_none([p["output_tokens"] for p in problems]),
        "time_seconds": _sum_or_none([p["time_seconds"] for p in problems]),
    }
    return summary, problems


def read_expected_problems(comp_configs_dir, comp):
    """从竞赛配置读取 n_problems,失败时返回 None."""
    config_path = os.path.join(comp_configs_dir, f"{comp}.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return int(config.get("n_problems"))
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: 无法读取竞赛配置 {config_path}: {exc}")
        return None


def main():
    args = parse_args()
    os.makedirs(args.result_dir, exist_ok=True)

    competitions = []
    problem_rows = []
    for comp in args.comps:
        expected = read_expected_problems(args.comp_configs_dir, comp)
        if expected is None:
            print(f"ERROR: 竞赛配置不存在或缺少 n_problems: {comp}")
            sys.exit(1)
        summary, problems = extract_competition(args.output_dir, comp, args.model, expected)
        competitions.append(summary)
        for p in problems:
            problem_rows.append({"competition": comp, **p})

    overall_pass = [c["pass_at_1"] for c in competitions if c["pass_at_1"] is not None]
    result = {
        "model": args.model,
        "overall": {
            "n_competitions": len(competitions),
            "n_graded_competitions": len(overall_pass),
            "pass_at_1": (sum(overall_pass) / len(overall_pass)) if overall_pass else None,
            "pass_at_1_display": (
                f"{sum(overall_pass) / len(overall_pass) * 100:.2f}%" if overall_pass else "N/A"
            ),
            "n_problems": sum(c["n_problems_run"] for c in competitions),
            "n_graded": sum(c["n_graded"] for c in competitions),
            "n_todo_grading": sum(c["n_todo_grading"] for c in competitions),
            "n_missing": sum(c["n_missing"] for c in competitions),
        },
        "competitions": competitions,
    }

    json_path = os.path.join(args.result_dir, "result_summary.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    csv_path = os.path.join(args.result_dir, "result_summary.csv")
    fieldnames = [
        "competition",
        "problem_idx",
        "n_runs",
        "n_correct_runs",
        "n_unjudged_runs",
        "pass_at_1",
        "pass_at_1_display",
        "n_warned_runs",
        "input_tokens",
        "output_tokens",
        "time_seconds",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(problem_rows)

    print("=== 评分汇总 ===")
    for c in competitions:
        print(
            f"{c['display_name']}: pass@1 = {c['pass_at_1_display']} "
            f"(已评判 {c['n_graded']}/{c['n_problems_run']} 题, "
            f"待评判 {c['n_todo_grading']}, 缺失 {c['n_missing']})"
        )
    print(f"总体 pass@1: {result['overall']['pass_at_1_display']}")
    print(f"汇总已写入: {json_path}")
    print(f"明细已写入: {csv_path}")

    # 有未评判/缺失/损坏文件时以非零码提醒(Jenkins 邮件会体现具体状态)
    if result["overall"]["n_todo_grading"] or result["overall"]["n_missing"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
