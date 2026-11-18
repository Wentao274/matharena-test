#!/usr/bin/env python3
"""MathArena 评测编排脚本(由 Jenkinsfile 远程调用,在评测机仓库根目录下运行)。

职责:
1. 按 Jenkins 传入的参数生成模型配置 configs/models/alaya_test_model.yaml
   (固定文件名;若已存在则先删除,再按参数新建——保证每次触发的配置一致)
2. 按参数生成本地裁判配置 configs/models/alaya_local_judge.yaml
   + configs/judges/local_judge.yaml(内容与 judges/answer_judge.yaml 逐字一致,
   仅 model_config 指向生成的裁判模型配置;同样删除重建)。
   裁判仅支持本地部署模型(api: custom),--judge-model/--judge-base-url 必填
3. (可选,走代理)预下载评测数据集(HuggingFace MathArena/*)
4. 调用 scripts/run.py 运行评测(纯模型模式,经 OpenAI 兼容 API 访问远端
   GPU 机器上的模型服务;不需要 Docker)
5. 调用 scripts/judge/judge.py 做 LLM 评判(ArXivMath 必需,裁判 = 本地部署模型)
6. 调用 jenkins/extract_results.py 生成评分汇总(result_summary.json/csv)

输出目录结构(对齐 sgl-eval 的 jenkins_output 组织):
    jenkins_output/<tester>/<build_number>/<chip>/<model_dir>/
        matharena_run.log      # 评测日志(邮件附件)
        matharena_judge.log    # 评判日志(邮件附件)
        outputs/               # MathArena 原生逐题运行文件(--output-dir 隔离)
        result_summary.json    # 评分汇总(Jenkins 邮件数据源)
        result_summary.csv     # 逐题评分明细(邮件附件)

注意:同一 build_number 下重跑(如 Jenkins stage 重试)会复用同一输出目录,
run.py 对已完成的题目自动跳过,天然支持断点续跑。

手动执行可省略 --build-number,脚本自动生成 manual-<YYYYMMDD-HHMMSS>,
每次执行获得独立目录,不会覆盖此前的结果;需断点续跑时显式传回同一编号。

退出码: 0 = 全部成功; 1 = 评测运行失败; 3 = 评测成功但评判失败;
        4 = 评分提取失败
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import date, datetime

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_CONFIG_NAME = "alaya_test_model"
MODEL_CONFIG_PATH = os.path.join(REPO_ROOT, "configs", "models", f"{MODEL_CONFIG_NAME}.yaml")
API_KEY_ENV_NAME = "ALAYA_TEST_API_KEY"
# 本地部署裁判(运行时按参数生成,固定文件名,与 alaya_test_model 同机制)
# 裁判仅支持本地部署模型,不再提供 gemini 等远程裁判入口
JUDGE_MODEL_CONFIG_NAME = "alaya_local_judge"
JUDGE_MODEL_CONFIG_PATH = os.path.join(REPO_ROOT, "configs", "models", f"{JUDGE_MODEL_CONFIG_NAME}.yaml")
LOCAL_JUDGE_CONFIG_NAME = "local_judge"
LOCAL_JUDGE_CONFIG_PATH = os.path.join(REPO_ROOT, "configs", "judges", f"{LOCAL_JUDGE_CONFIG_NAME}.yaml")
LOCAL_JUDGE_API_KEY_ENV_NAME = "ALAYA_JUDGE_API_KEY"
# 基准裁判配置(逐字节复制生成 local_judge,仅替换 model_config 首行)
ORIGINAL_ANSWER_JUDGE_PATH = os.path.join(REPO_ROOT, "configs", "judges", "answer_judge.yaml")


def parse_args():
    parser = argparse.ArgumentParser(description="Orchestrate a MathArena evaluation run")
    parser.add_argument("--tester", required=True, help="测试人员名称")
    parser.add_argument(
        "--build-number",
        default="",
        help="构建编号(Jenkins 传 BUILD_NUMBER;手动执行省略则自动生成 manual-<时间戳>)",
    )
    parser.add_argument("--chip", required=True, help="芯片平台名称(仅用于输出目录/邮件展示)")
    parser.add_argument("--model", required=True, help="模型服务名称(必须与 --served-model-name 一致)")
    parser.add_argument("--base-url", required=True, help="OpenAI 兼容端点 URL(带 /v1 后缀)")
    parser.add_argument("--api-key", default="", help="模型服务 API Key(无需认证时留空)")
    parser.add_argument(
        "--comps", required=True, nargs="+", help="竞赛配置路径列表,如 arxiv/september"
    )
    parser.add_argument("--n", type=int, default=1, help="每题运行次数(ArXivMath 官方口径为 1)")
    parser.add_argument("--max-tokens", default="", help="最大生成 token 数(空 = 不写入配置)")
    parser.add_argument("--temperature", default="", help="采样温度(空 = 不写入配置)")
    parser.add_argument("--top-p", default="", help="top_p(空 = 不写入配置)")
    parser.add_argument("--concurrent-requests", default="30", help="并发请求数(默认 30)")
    parser.add_argument("--timeout", default="", help="请求超时秒数(空 = 框架默认)")
    parser.add_argument(
        "--judge-model", required=True,
        help="本地裁判模型名(须与裁判服务 --served-model-name 一致;裁判仅支持本地部署)",
    )
    parser.add_argument(
        "--judge-base-url", required=True,
        help="本地裁判服务根 URL(不带 /v1,脚本自动拼接)",
    )
    parser.add_argument(
        "--judge-api-key", default="",
        help="本地裁判服务 API Key(无需认证时留空)",
    )
    parser.add_argument(
        "--proxy", default="", help="数据集下载代理(仅预下载阶段使用;评测与评判不走代理)"
    )
    return parser.parse_args()


def generate_model_config(args):
    """生成(或删除重建)configs/models/alaya_test_model.yaml。"""
    if os.path.exists(MODEL_CONFIG_PATH):
        print(f"=== 检测到已有配置 {MODEL_CONFIG_PATH},按要求删除后重建 ===")
        os.remove(MODEL_CONFIG_PATH)

    lines = [
        f"# 由 Jenkins 构建 #{args.build_number} 自动生成,请勿手工编辑",
        f"model: {args.model}",
        "api: custom",
        f"base_url: {args.base_url}",
    ]
    if args.api_key:
        # api: custom 时 api_key_env 指定密钥环境变量(由编排脚本注入子进程)
        lines.append(f"api_key_env: {API_KEY_ENV_NAME}")
    if args.max_tokens:
        lines.append(f"max_tokens: {args.max_tokens}")
    if args.temperature:
        lines.append(f"temperature: {args.temperature}")
    if args.top_p:
        lines.append(f"top_p: {args.top_p}")
    lines.append(f"concurrent_requests: {args.concurrent_requests}")
    if args.timeout:
        lines.append(f"timeout: {args.timeout}")
    lines += [
        "read_cost: 0",
        "write_cost: 0",
        "human_readable_id: Alaya Test Model",
        f'date: "{date.today().isoformat()}"',
    ]
    content = "\n".join(lines) + "\n"
    with open(MODEL_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    # 校验生成结果可被正常解析
    with open(MODEL_CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    required_keys = ["model", "api", "base_url", "human_readable_id"]
    missing = [k for k in required_keys if k not in config]
    if missing:
        raise RuntimeError(f"生成的模型配置缺少必填字段: {missing}")
    print(f"=== 模型配置已生成: {MODEL_CONFIG_PATH} ===")
    print(content)


def normalize_base_url(url):
    """幂等规范化 OpenAI 兼容端点:剥尾斜杠与结尾 /v1,统一补 /v1。"""
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url + "/v1"


def generate_local_judge_config(args):
    """生成(或删除重建)本地裁判的模型配置与裁判配置。

    裁判配置基于 configs/judges/answer_judge.yaml **原文逐字节复制**,仅把首行
    model_config 指向运行时生成的 alaya_local_judge——judge_prompt 等全部字段
    与官方裁判配置严格一致,且对上游 prompt 改动免疫。
    """
    if not os.path.exists(ORIGINAL_ANSWER_JUDGE_PATH):
        raise FileNotFoundError(f"未找到基准裁判配置 {ORIGINAL_ANSWER_JUDGE_PATH}")
    for path in (JUDGE_MODEL_CONFIG_PATH, LOCAL_JUDGE_CONFIG_PATH):
        if os.path.exists(path):
            print(f"=== 检测到已有配置 {path},按要求删除后重建 ===")
            os.remove(path)

    base_url = normalize_base_url(args.judge_base_url)
    lines = [
        f"# 由 Jenkins 构建 #{args.build_number} 自动生成的本地裁判模型配置,请勿手工编辑",
        f"model: {args.judge_model}",
        "api: custom",
        f"base_url: {base_url}",
    ]
    if args.judge_api_key:
        lines.append(f"api_key_env: {LOCAL_JUDGE_API_KEY_ENV_NAME}")
    lines += [
        "temperature: 0",  # 固定 0 保证评判可复现
        "concurrent_requests: 16",
        "read_cost: 0",
        "write_cost: 0",
        "human_readable_id: Alaya Local Judge",
        f'date: "{date.today().isoformat()}"',
    ]
    model_content = "\n".join(lines) + "\n"
    with open(JUDGE_MODEL_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(model_content)

    with open(ORIGINAL_ANSWER_JUDGE_PATH, "r", encoding="utf-8") as f:
        original_text = f.read()
    judge_text = re.sub(
        r"(?m)^model_config:.*$",
        f"model_config: {JUDGE_MODEL_CONFIG_NAME}",
        original_text,
        count=1,
    )
    header = (
        "# 本地部署裁判配置(由编排脚本按 Jenkins 参数生成,内容与 judges/answer_judge.yaml\n"
        "# 逐字一致,仅 model_config 指向生成的 alaya_local_judge)\n"
        "# 请勿手工编辑\n"
    )
    with open(LOCAL_JUDGE_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(header + judge_text)

    # 校验两份生成文件可解析且关键字段正确
    with open(JUDGE_MODEL_CONFIG_PATH, "r", encoding="utf-8") as f:
        model_cfg = yaml.safe_load(f)
    for key in ["model", "api", "base_url"]:
        if key not in model_cfg:
            raise RuntimeError(f"生成的裁判模型配置缺少必填字段: {key}")
    with open(LOCAL_JUDGE_CONFIG_PATH, "r", encoding="utf-8") as f:
        judge_cfg = yaml.safe_load(f)
    if (
        judge_cfg.get("model_config") != JUDGE_MODEL_CONFIG_NAME
        or judge_cfg.get("judge_points_max") != 1
        or judge_cfg.get("scaffold_config") != "judges/simple_judge"
        or not judge_cfg.get("override", {}).get("judge_prompt")
    ):
        raise RuntimeError("生成的裁判配置校验失败(字段与 answer_judge 口径不一致)")
    print(f"=== 本地裁判模型配置已生成: {JUDGE_MODEL_CONFIG_PATH} ===")
    print(model_content)
    print(f"=== 本地裁判配置已生成: {LOCAL_JUDGE_CONFIG_PATH} (model_config → {JUDGE_MODEL_CONFIG_NAME}) ===")


def download_datasets(args):
    """预下载各竞赛的 HuggingFace 数据集(仅预下载阶段走代理)。"""
    hf_datasets = []
    for comp in args.comps:
        config_path = os.path.join(REPO_ROOT, "configs", "competitions", f"{comp}.yaml")
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        dataset_path = config.get("dataset_path")
        if dataset_path and "/" in dataset_path and not dataset_path.startswith("data/"):
            hf_datasets.append(dataset_path)
    if not hf_datasets:
        print("=== 无 HuggingFace 数据集需要预下载 ===")
        return

    print(f"=== 预下载数据集: {hf_datasets} ===")
    proxy_env = dict(os.environ)
    if args.proxy:
        proxy_env["https_proxy"] = args.proxy
        proxy_env["http_proxy"] = args.proxy
        print(f"(使用代理 {args.proxy})")
    # datasets 库在仓库依赖中;预下载后 run.py 直接命中本地缓存
    subprocess.run(
        [sys.executable, "-c",
         "import sys, datasets\n"
         "for name in sys.argv[1:]:\n"
         "    print(f'downloading {name} ...')\n"
         "    datasets.load_dataset(name)\n"
         "    print(f'done {name}')\n",
         *hf_datasets],
        check=False,
        env=proxy_env,
        cwd=REPO_ROOT,
    )


def run_command(cmd, log_path, env, append=False):
    """执行命令并 tee 到日志文件,返回退出码。append=True 时追加(多竞赛评判共用一份日志)。"""
    print(f"=== 执行: {' '.join(cmd)} ===")
    tee_flag = "-a" if append else ""
    tee_cmd = (
        f"set -o pipefail; {' '.join(str(c) for c in cmd)} 2>&1 "
        f"| tee {tee_flag} '{log_path}'"
    )
    return subprocess.call(tee_cmd, shell=True, executable="/bin/bash", cwd=REPO_ROOT, env=env)


def _clean_placeholder_url(url):
    """Jenkins 参数默认占位符(如 http://<proxy-host>:<proxy-port>)视为未填写。"""
    if url and ("<" in url or ">" in url):
        print(f"WARN: 地址参数仍为占位符({url}),按未填写处理")
        return ""
    return url


def main():
    args = parse_args()
    args.proxy = _clean_placeholder_url(args.proxy)
    # 本地裁判参数为空串时(Jenkins 传参)直接报错,避免带空配置跑完整评测
    if not args.judge_model.strip() or not args.judge_base_url.strip():
        print("ERROR: 裁判仅支持本地部署模型,--judge-model/--judge-base-url 必填")
        sys.exit(1)

    # 手动执行未传构建编号:自动生成时间戳编号,每次执行独立目录,不覆盖此前结果
    if not args.build_number.strip():
        args.build_number = datetime.now().strftime("manual-%Y%m%d-%H%M%S")
        print(f"=== 未指定 --build-number,自动生成: {args.build_number} ===")
        print("    (如需断点续跑,请重新执行并显式传回同一编号)")

    output_dir = os.path.abspath(
        os.path.join(
            REPO_ROOT,
            "jenkins_output",
            args.tester,
            args.build_number,
            args.chip,
            args.model.split("/")[-1],
        )
    )
    os.makedirs(output_dir, exist_ok=True)
    print(f"=== 输出目录: {output_dir} ===")

    generate_model_config(args)

    # 本地裁判:生成配置(固定文件名,存在则删除重建)
    generate_local_judge_config(args)

    env = dict(os.environ)
    if args.api_key:
        env[API_KEY_ENV_NAME] = args.api_key
    if args.judge_api_key:
        env[LOCAL_JUDGE_API_KEY_ENV_NAME] = args.judge_api_key

    download_datasets(args)

    run_log = os.path.join(output_dir, "matharena_run.log")
    run_cmd = [
        sys.executable,
        "scripts/run.py",
        "--comp",
        *args.comps,
        "--models",
        MODEL_CONFIG_NAME,
        "--n",
        str(args.n),
        "--output-dir",
        os.path.join(output_dir, "outputs"),
    ]
    run_rc = run_command(run_cmd, run_log, env)
    if run_rc != 0:
        print(f"ERROR: 评测运行失败(退出码 {run_rc}),跳过评判与提取之外的步骤")
        sys.exit(1)

    # 评判阶段:ArXivMath(answer_judge 判分)必需;失败不中断提取
    # 裁判 = 本地部署模型(运行时生成的 judges/local_judge)
    judge_log = os.path.join(output_dir, "matharena_judge.log")
    judge_failures = []
    for comp in args.comps:
        judge_cmd = [
            sys.executable,
            "scripts/judge/judge.py",
            "--comp",
            comp,
            "--models",
            MODEL_CONFIG_NAME,
            "--judge-configs",
            LOCAL_JUDGE_CONFIG_NAME,
            "--output-dir",
            os.path.join(output_dir, "outputs"),
        ]
        rc = run_command(judge_cmd, judge_log, env, append=(comp != args.comps[0]))
        if rc != 0:
            judge_failures.append(comp)
    judge_failed = bool(judge_failures)
    if judge_failed:
        print(f"WARN: 以下竞赛评判失败: {judge_failures}")

    extract_log = os.path.join(output_dir, "matharena_extract.log")
    extract_cmd = [
        sys.executable,
        "jenkins/extract_results.py",
        "--output-dir",
        os.path.join(output_dir, "outputs"),
        "--model",
        MODEL_CONFIG_NAME,
        "--comps",
        *args.comps,
        "--result-dir",
        output_dir,
    ]
    extract_rc = run_command(extract_cmd, extract_log, env)
    # extract 退出码 2 表示存在未评判/缺失题,不算提取失败
    if extract_rc not in (0, 2):
        print(f"ERROR: 评分提取失败(退出码 {extract_rc})")
        sys.exit(4)

    if judge_failed:
        sys.exit(3)
    print("=== MathArena 评测流程完成 ===")


if __name__ == "__main__":
    main()
