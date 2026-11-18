# MathArena 本地评测指南（中文）

MathArena（NeurIPS D&B '25）是一个评测大语言模型在最新数学竞赛与奥赛题上表现的平台。本仓库包含全部评测代码，本文档介绍如何在本地环境完成**环境准备、模型配置与评测执行**。

---

## 一、项目概览

| 目录 | 说明 |
| --- | --- |
| `configs/competitions/` | 竞赛配置（AIME、IMO、USAMO、Putnam、Kangaroo、ArXivLean 等） |
| `configs/models/` | 模型/Agent 配置（按厂商分目录：openai、anthropic、glm、deepseek、qwen 等） |
| `configs/judges/` | 评判（judge）模型配置，用于证明类竞赛的打分 |
| `configs/agent_scaffolds/` | Agent 脚手架配置 |
| `data/` | 本地数据集（`problems/*.tex`、`answers.csv`、`grading_scheme.json`） |
| `scripts/` | 评测入口：`run.py`、`judge/judge.py`、`regrade.py` 等 |
| `src/matharena/` | 核心库（runner、solver、parser、grader 等） |
| `harness_wrapper/` | Harness（Codex/Claude Code 等 CLI agent）运行封装 |
| `app/` | 本地结果查看 Web 应用（Flask） |
| `outputs/` | 评测结果输出目录 |
| `logs/` | 运行日志：`status`（进度）、`requests`（API 请求原文）、`broken_runs`（序列化失败的运行） |

评测的基本流程：

```
run.py（跑模型） → [可选] judge.py（LLM 评判打分） → app.py（本地网页查看结果）
```

---

## 二、环境准备

### 1. 前置要求

- **Python >= 3.12**（仓库自带 `.python-version`）
- **[UV](https://github.com/astral-sh/uv)**：推荐的依赖管理工具
- **Docker**（可选）：仅运行 harness 类模型（codex/claude/kimi 等 CLI agent）或容器化评判时需要
- Windows 环境下，仓库中的 `*.sh` 脚本需在 Git Bash 或 WSL 中执行

### 2. 安装 UV

- **macOS / Linux：**
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **Windows (PowerShell)：**
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```

### 3. 安装依赖

使用 UV 时，在仓库根目录直接运行（UV 会自动创建虚拟环境并同步依赖）：

```bash
uv sync
```

> **本地部署模型说明**：本手册推荐的评测方式（`api: custom`，见第五节）通过 HTTP 调用 GPU 机器上的推理服务，**评测机无需 GPU、也无需安装 vLLM**。`pyproject.toml` 中的 vllm 依赖仅在评测机本机直接加载模型（`api: vllm`）时才会用到。

### 4. 替代安装方式（conda）

```bash
conda create -n matharena python=3.12
conda activate matharena
python -m pip install -e .
```

> 若使用 conda 方式，下文所有 `uv run python` 请替换为 `python`。

### 5. 配置 API Key

评测脚本会通过 `load_dotenv()` 读取仓库根目录的 `.env` 文件，也可以直接设置系统环境变量。各 API 对应的密钥如下：

| API 提供商 (`api` 字段) | 环境变量 |
| --- | --- |
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `google` | `GOOGLE_API_KEY` |
| `glm` | `GLM_API_KEY` |
| `bigmodel` | `BIGMODEL_API_KEY` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `xai` | `XAI_API_KEY` |
| `together` | `TOGETHER_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `meta` | `META_API_KEY` |
| `custom` | 由模型配置中 `api_key_env` 指定（如 `VLLM_API_KEY`）；未设置时使用占位符 `EMPTY`。用于评测本地部署的模型（OpenAI 兼容端点），见第五节 |
| `vllm` | 无需密钥（在评测机本机直接加载模型，需评测机具备 GPU） |

建议在仓库根目录创建 `.env` 文件（已被 `.gitignore` 忽略，不会误提交）：

```
GLM_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here
```

### 6. （可选）准备 Harness 运行环境

若要评测 harness 类模型（如 `gpt-61-sol` 使用的 codex），需先构建 Docker 镜像并准备缓存（一次性，在仓库根目录执行）：

```bash
bash scripts/build_harness_arxivlean_image.sh
```

支持 Podman 时：

```bash
bash scripts/build_harness_arxivlean_image.sh --container-runtime podman
```

---

## 三、模型配置

### 1. 配置文件位置

模型配置是 YAML 文件，位于 `configs/models/<厂商>/<模型名>.yaml`。运行时通过相对路径（不含 `.yaml` 后缀）引用，例如 `glm/glm-5`、`openai/gpt-4o`。

### 2. 必填字段

| 字段 | 说明 |
| --- | --- |
| `model` | 模型名。`api: custom` 时必须与推理服务的 `--served-model-name` 完全一致。OpenAI 模型的推理强度可附加在名称后，如 `o3-mini--high` |
| `api` | API 提供商，见上表（`openai`/`anthropic`/`glm`/`custom`/`vllm` 等） |
| `human_readable_id` | 唯一的可读标识，用于展示 |

### 3. 常用可选字段

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `temperature` / `top_p` / `top_k` | 采样参数 | — |
| `max_tokens` | 最大生成 token 数 | — |
| `concurrent_requests` | 并发请求数 | 30 |
| `timeout` | 请求超时（秒） | 30000 |
| `max_retries` | API 重试次数 | 50 |
| `base_url` | API 端点地址。`api: custom` 时必填，如 `http://192.168.1.100:8000/v1` | — |
| `api_key_env` | API 密钥的环境变量名。`api: custom` 时使用；未设置则使用占位符 `EMPTY` | `EMPTY` |
| `extra_body` | 额外请求体参数，用于传递 OpenAI 官方 API 之外的参数（如 vLLM 的 `top_k`、`min_p`、`repetition_penalty`） | — |
| `read_cost` / `write_cost` | 每百万 token 输入/输出成本（美元），用于费用统计；本地部署模型可设为 0 | 1 / 1 |
| `cache_read_cost` | 每百万缓存 token 读取成本 | 同 `read_cost` |
| `date` | 模型发布日期（`yyyy-mm-dd`） | — |
| `batch_processing` | 使用批处理 API（仅 OpenAI / Anthropic / OpenRouter） | false |
| `use_openai_responses_api` | 使用 OpenAI Responses API | false |
| `harness` | 通过 CLI agent（`codex`/`claude`/`kimi`/`qwen` 等）运行，需竞赛配置 `allow_harness: true` | — |
| `harness_version` | 指定 CLI 精确版本 | 最新 release |
| `harness_config` | Docker 镜像、资源限制、认证等 harness 设置 | — |

### 4. 配置示例

以 `configs/models/glm/glm-5.yaml` 为例：

```yaml
model: glm-5
api: glm
max_tokens: 131072
concurrent_requests: 32
temperature: 1
top_p: 0.95
read_cost: 1
write_cost: 3.2
human_readable_id: GLM 5
date: "2026-02-11"
other_params:
  open: true
  creator: Z.ai
  huggingface_id: zai-org/GLM-5
```

新建自定义模型时，可复制一份相近的现有配置，修改 `model`、`api`、`human_readable_id` 及采样/成本参数即可。

**评测本地部署模型的配置示例**（`api: custom`，完整步骤见第五节）：

```yaml
# configs/models/local/my_model.yaml
model: my-local-model              # 必须与 vllm serve 的 --served-model-name 完全一致
api: custom
base_url: http://192.168.1.100:8000/v1   # GPU 机器的 IP:端口，注意结尾带 /v1
api_key_env: VLLM_API_KEY          # 可选：仅当 vllm serve 设置了 --api-key 时才需要，否则可省略此行
max_tokens: 32768
temperature: 1.0
top_p: 0.95
extra_body:                        # vLLM 特有采样参数经 extra_body 传递
  top_k: 20
concurrent_requests: 64            # 建议不超过 vLLM 服务端的 --max-num-seqs
read_cost: 0
write_cost: 0                      # 本地部署无 API 费用
human_readable_id: My Local Model
date: "2026-11-18"
```

仓库中现成的同类配置可参考 `configs/models/qwen/qwen3.5_27b.yaml`、`configs/models/glm/ocr.yaml`。

### 5. Agent 配置

Agent（如 `openai/gpt-5-agent.yaml`）由两部分组成：

- **基础模型配置**：指向一个纯模型 YAML，定义底层 LLM API；
- **脚手架（scaffold）配置**：定义 Agent 的工作流（工具调用、自检等）。

新增自定义脚手架可参考 `src/matharena/solvers/selfcheck_agent.py`（基于 `base_agent.py` 的工具函数）。

---

## 四、竞赛配置（简要）

竞赛配置位于 `configs/competitions/<组名>/<竞赛名>.yaml`，核心字段：

- `instruction`：发给模型的指令，*必须*要求最终答案写入 `\boxed{}`；
- `strict_parsing`：是否严格解析格式；
- `n_problems`：题目总数；
- `dataset_path`：HuggingFace 数据集（如 `MathArena/aime_2026`）或 `data/` 下的本地路径；
- `final_answer`：`false` 表示需要 LLM 评判打分（如 IMO、USAMO 证明题）；
- `judge_configs`：评判配置列表（`scripts/judge/judge.py` 使用）。

当前主要竞赛（节选）：

| 类型 | 竞赛 | 配置路径 | 是否需要评判 |
| --- | --- | --- | --- |
| ArXiv 系列 | ArXivMath 09/2026（53 题，本手册示例） | `arxiv/september` | 是 |
| ArXiv 系列 | ArXivMath 08/2026（57 题） | `arxiv/august` | 是 |
| ArXiv 系列 | BrokenArXiv 08-09/2026 | `arxiv_false/august`、`arxiv_false/september` | 是 |
| ArXiv 系列 | ArXivLean 09/2026 | `arxivlean/september` | 否（Lean 比对） |
| 最终答案类 | AIME 2026 | `aime/aime_2026` | 否 |
| 最终答案类 | HMMT Feb 2026 | `hmmt/hmmt_feb_2026` | 否 |
| 视觉数学 | Kangaroo 2025 各级别 | `kangaroo/kangaroo_2025_*` | 否 |
| 证明类 | USAMO 2026 | `usamo/usamo_2026` | 是 |
| 证明类 | IMO 2025 | `imo/imo_2025` | 是 |
| 证明类 | Putnam 2025 | `putnam/putnam_2025` | 是 |

> 注意：IMO 2025、USAMO 2025 等历史竞赛原为人工阅卷，无法自动评分；若需运行，请参照 `usamo/usamo_2026.yaml` 的方式调整配置并添加 `judge_configs`。

---

## 五、执行步骤：用本地模型评测 ArXivMath（完整示例）

本章以 **ArXivMath**（MathArena 当前评测任务，从 arXiv 论文中提取的开放数学研究问题；最新一期 09/2026 共 53 题，HuggingFace 数据集 `MathArena/arxivmath-0926`，配置 `arxiv/september`）为贯穿示例，完整演示"模型部署在 GPU 机器、评测脚本在另一台机器"的操作流程。评测其他任务只需替换 `--comp` 参数，可选任务见第四节的任务表。

> **ArXivMath 与 AIME 等最终答案类任务的两个关键区别**：
> 1. **必须执行 LLM 评判**（步骤 5）才能得到成绩——其答案多为开放数学表达式，无法直接字符串比对；
> 2. 每题为研究级难题，推理链很长，官方评测通常每题只跑 1 次（`--n 1`）以控制计算量。

整体架构与两台机器的分工：

```
┌───────────────────────────┐    HTTP（OpenAI 兼容 API）    ┌───────────────────────────┐
│  GPU 机器（模型机）         │  http://<模型机IP>:8000/v1   │  评测机（运行本仓库）        │
│  vllm serve <模型> ...     │ ◄──────────────────────────► │  scripts/run.py            │
│  只需 GPU + Python + vLLM   │                              │  无需 GPU、无需安装 vLLM     │
└───────────────────────────┘                              └───────────────────────────┘
```

- **模型机**：负责加载模型并对外提供 OpenAI 兼容 API 服务，**不需要**克隆本仓库；
- **评测机**：克隆本仓库、执行评测脚本，通过 HTTP 调用模型机的 API，**不需要 GPU**；需能访问 HuggingFace（评测任务的数据集如 `MathArena/arxivmath-0926` 会在首次运行时自动下载）。另外，ArXivMath 的评判步骤需要一个裁判模型 API（默认为 Gemini，需 `GOOGLE_API_KEY`；也可换成指向本地模型的评判配置，见步骤 5）。

> 若模型与评测脚本在同一台机器且该机器有 GPU，也可以使用 `api: vllm` 让框架直接在本机加载模型（如 `configs/models/qwen/qwen3_4b_2507.yaml`），无需启动服务；跨机器评测请按本节步骤进行。

### 步骤 1：启动推理服务

**操作机器：GPU 机器（模型机）**

以 vLLM 为例，安装后启动服务：

```bash
pip install vllm

vllm serve /models/Qwen3-32B \
  --served-model-name my-local-model \
  --host 0.0.0.0 \
  --port 8000 \
  --tensor-parallel-size 1 \
  --max-model-len 65536 \
  --max-num-seqs 64 \
  --gpu-memory-utilization 0.90 \
  --trust-remote-code
```

关键参数说明：

| 参数 | 说明 |
| --- | --- |
| `--served-model-name` | 服务对外的模型名，**必须与模型配置 YAML 的 `model` 字段完全一致**，否则请求会返回 404 |
| `--host 0.0.0.0` | 允许其他机器访问（默认 `127.0.0.1` 只能本机访问） |
| `--port` | 服务端口，默认 8000 |
| `--tensor-parallel-size` | 张量并行卡数，多卡机器按 GPU 数量设置 |
| `--max-model-len` | 最大上下文长度，建议不小于"系统指令 + 题目 + `max_tokens`"之和 |
| `--max-num-seqs` | vLLM 可同时处理的最大请求数，决定吞吐上限 |
| `--gpu-memory-utilization` | 显存占用比例 |
| `--api-key <key>` | （可选）设置鉴权；设置后需在评测机 `.env` 中配置同名变量并在 YAML 中填写 `api_key_env` |

**如何确认成功**：在模型机上执行：

```bash
curl http://127.0.0.1:8000/v1/models
```

返回的 JSON 中 `data[0].id` 应为 `my-local-model`，即服务已就绪。

### 步骤 2：创建模型配置

**操作机器：评测机**

假设模型机 IP 为 `192.168.1.100`。新建 `configs/models/local/my_model.yaml`：

```yaml
model: my-local-model              # 与 --served-model-name 完全一致
api: custom
base_url: http://192.168.1.100:8000/v1   # 模型机 IP:端口，结尾带 /v1
max_tokens: 32768
temperature: 1.0
top_p: 0.95
concurrent_requests: 64            # 建议不超过服务端 --max-num-seqs
read_cost: 0
write_cost: 0                      # 本地部署无 API 费用
human_readable_id: My Local Model
date: "2026-11-18"
```

要点：

- `model` 必须与 `--served-model-name` 完全一致，否则请求返回 404；
- `base_url` 为 `http://<模型机IP>:<端口>/v1`，注意结尾的 `/v1`；
- 若服务设置了 `--api-key`，在 `.env` 中写入 `VLLM_API_KEY=<key>` 并在 YAML 中加 `api_key_env: VLLM_API_KEY`；未设置鉴权则可省略；
- vLLM 特有采样参数（`top_k`、`min_p`、`repetition_penalty` 等）通过 `extra_body` 传递，完整示例见第三节。

### 步骤 3：验证连通性

**操作机器：评测机**

先确认能拉到模型列表：

```bash
curl http://192.168.1.100:8000/v1/models
```

再发一次真实对话请求，确认模型可正常推理：

```bash
curl http://192.168.1.100:8000/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\": \"my-local-model\", \"messages\": [{\"role\": \"user\", \"content\": \"1+1=?\"}], \"max_tokens\": 32}"
```

（Linux / Git Bash 下把续行符 `^` 换成 `\`）

若无法访问，依次检查：`--host` 是否为 `0.0.0.0`、两机网络是否互通、防火墙是否放行端口。

### 步骤 4：运行 ArXivMath 评测

**操作机器：评测机**

用本地模型跑 ArXivMath 09/2026（53 题，每题 1 次尝试）：

```bash
uv run python scripts/run.py --comp arxiv/september --models local/my_model --n 1
```

`--comp` / `--models` 均为相对路径（不含 `.yaml` 后缀），分别相对 `configs/competitions/` 与 `configs/models/`。

**执行行为说明：**

- 首次运行会自动从 HuggingFace 下载 `MathArena/arxivmath-0926` 数据集（评测机需联网），之后使用本地缓存；
- 官方评测对 ArXivMath 使用每题 1 次尝试（`--n 1`，见 `scripts/run_all_fa.sh` 中的覆盖设置）；如需更稳定的统计可增大 `--n`，但计算量线性增长；
- ArXivMath 每题为长推理任务，建议在模型配置中给足 `max_tokens`（并相应调大 vLLM 的 `--max-model-len`）；
- 终端实时显示进度，进度信息同步写入 `logs/status/`；
- 结果按题写入 `outputs/arxiv/september/` 下的运行文件；**中断后（断网、服务重启等）重跑同一命令即可断点续跑**，已完成的题目自动跳过；
- 默认目录 `outputs/` 下，同 comp+模型的多次执行会**混合在同一目录**（断点续跑、不覆盖但也不隔离批次）；**如需每次执行互相隔离、后者不覆盖前者，用带时间戳的 `--output-dir`**（放在 `outputs/` 之下，已被 git 忽略）：

```bash
uv run python scripts/run.py --comp arxiv/september --models local/my_model --n 1 \
    --output-dir outputs/manual-$(date +%Y%m%d-%H%M%S)
```

使用自定义 `--output-dir` 时，步骤 5 的评判命令必须传同一目录；
- 跑完后结果处于待评判状态（界面显示 "TODO Grading"），需继续步骤 5 才能得到成绩。

一次评测多期任务（最新两期为例）：

```bash
uv run python scripts/run.py --comp arxiv/september arxiv/august --models local/my_model --n 1
```

**常用参数：**

| 参数 | 说明 |
| --- | --- |
| `--n` | 每题运行次数（默认 4，正式评测常用 4 的倍数以计算 pass@1） |
| `--comp-n` | 按竞赛覆盖运行次数，如 `apex/apex_2025=16` |
| `--problems` | 只跑指定题目（1 起始的题号，如 `7 32 16`） |
| `--redo-all` | 忽略已有结果，全部重跑（默认跳过 `outputs/` 中已完成的运行，可断点续跑） |
| `--workers` | 覆盖本次评测的并发请求数 |
| `--shuffle-seed` | 以固定随机种子打乱题目顺序（保持题目 ID 不变） |
| `--run-root` | 将配置与数据冻结到一个全新目录并在其中保存输出，便于复现 |
| `--prepare-only` | 只准备 `--run-root`，不实际调用模型 |
| `--container-runtime` | harness 运行与评判的容器后端：`docker`（默认）或 `podman` |

**并发建议**：本地 vLLM 吞吐高、无外部限流，可适当调大并发——通过模型配置中的 `concurrent_requests` 或运行时的 `--workers` 控制。若 vLLM 端报错或超时增多，应降低 `--workers`，或在服务端调大 `--max-num-seqs` / `--gpu-memory-utilization`。

### 步骤 5：运行 LLM 评判（ArXivMath 必选）

**操作机器：评测机**

ArXivMath 的答案多为开放数学表达式，无法直接比对，竞赛配置中设置了 `grading: answer_judge`——跑完模型后**必须**执行 LLM 评判才能得到成绩：

```bash
uv run python scripts/judge/judge.py --comp arxiv/september --models local/my_model
```

若步骤 4 使用了带时间戳的自定义 `--output-dir`，此处追加同一参数（如 `--output-dir outputs/manual-20261118-1530`）。

评判机制说明：

- 默认评判配置由竞赛配置的 `judge_configs` 指定，ArXivMath 使用 `judges/answer_judge`：由 `gemini/gemini-38-flash` 担任裁判，对"模型最终答案与标准答案是否数学等价"打 0/1 分（接受等价分数、展开/因式分解、变量重命名等无害格式差异）；
- 因此默认情况下**评测机需要配置 `GOOGLE_API_KEY`**（写入 `.env` 即可，见第二节）；
- 想让裁判也用本地模型：复制 `configs/judges/answer_judge.yaml` 为新文件（如 `configs/judges/my_local_judge.yaml`），将其中 `model_config` 改为指向一个 `api: custom` 的模型配置（可复用步骤 2 的方式另建一个裁判专用的模型 YAML，指向本地或 GPU 机器上的模型），再用 `--judge-configs` 覆盖：

```bash
uv run python scripts/judge/judge.py --comp arxiv/september --models local/my_model --judge-configs judges/my_local_judge
```

- 其他参数：`--problem-ids 1 2` 只评判特定题目，`--redo` 强制重判；
- 评判结果会写回 `outputs/` 中的运行文件，并自动重算 `correct` 与 `pass_at_1`；评判按 token 用量与模型配置中的价格统计费用。

### 步骤 6：查看结果

**操作机器：评测机**

启动本地检查网页：

```bash
uv run python app/app.py --comp arxiv/september
```

浏览器访问 [http://localhost:5001/](http://localhost:5001/)，可查看最终答案、完整交互轨迹、评分详情。警告图标含义：

- 💀：解析器报错或遇到异常内容
- ⚠️：正确答案可能存在于模型回答中但未被提取
- ❕：模型可能触及最大 token 限制

**ArXivMath 特别提示**：其答案多为自由格式数学表达式，解析提取失败的情况比 AIME 类整数答案常见得多。跑完（评判前后均可）建议逐题浏览、人工核验可疑运行；发现问题可删除对应输出文件、用 `scripts/nuke_single_run.py` 删除单次运行，或修补解析器后运行 `scripts/regrade.py` 重新解析评分（详见 `readmes/README_arxivmath.md` 的输出检查流程）。

### 步骤 7：断点续跑与重新评分（按需）

- 评测中途失败（断网、限流、服务重启等）后，**直接重跑同一命令**即可从已有结果断点续跑，已完成的题目自动跳过；
- 如需完全重跑，加 `--redo-all`；
- 解析器或价格更新后，对已有运行重新解析/评分/计算成本：

```bash
uv run python scripts/regrade.py --comps arxiv/september
```

### 输出产物

- `outputs/<竞赛>/<模型>/<题号>.json.zst`：规范化运行记录（消息、成本、答案、评分）；
- `logs/status/`：实时运行进度；
- `logs/requests/`：API 请求原文（调试 provider 问题用）；
- `logs/broken_runs/`：序列化失败的运行。

---

## 六、常见问题

| 问题 | 处理方式 |
| --- | --- |
| `uv run` 报找不到命令 | 先安装 UV（见环境准备），Windows 下重开终端使 PATH 生效 |
| Windows 下无法执行 `*.sh` 脚本 | 使用 Git Bash 或 WSL；纯 `run.py`/`judge.py` 用 PowerShell 直接运行即可 |
| API 限流/超时频繁 | 降低 `--workers` 或模型配置中的 `concurrent_requests`；框架自带最多 50 次重试 |
| 想跑本地开源模型 | 分两种情况：**模型部署在另一台 GPU 机器上**（本手册场景）→ 在模型机用 `vllm serve` 起服务、评测机用 `api: custom` 配置，见第五节；**模型与评测脚本在同一台机器且有 GPU** → 使用 `api: vllm` 配置让框架直接加载模型 |
| 请求返回 404 model not found | 模型配置 YAML 的 `model` 字段与 vLLM 服务端的 `--served-model-name` 不一致，改成完全相同即可 |
| 连不上模型机服务（连接被拒绝/超时） | 检查 vLLM 是否以 `--host 0.0.0.0` 启动、两机网络是否互通、防火墙是否放行端口、`base_url` 是否带 `/v1` 后缀 |
| vLLM 高并发下报错或超时增多 | 降低评测端 `--workers`，或在服务端调大 `--max-num-seqs`、`--gpu-memory-utilization`，或减小 `--max-model-len` |
| ArXivMath 跑完显示 "TODO Grading" | 正常现象：ArXivMath 必须执行评判步骤才有成绩，运行 `uv run python scripts/judge/judge.py --comp arxiv/september`（第五节步骤 5） |
| 评判时报 API key not found | ArXivMath 默认裁判是 `gemini/gemini-38-flash`，需在 `.env` 配置 `GOOGLE_API_KEY`；或用 `--judge-configs` 指向自定义（本地）评判配置 |
| Project Euler 无法自动判分 | 官方不公布答案，需自行在 `data/euler/euler/answers.csv` 中补充正确答案 |

---

## 七、参考链接

- 主 README：[README.md](README.md)
- 开发者指南（新增竞赛/模型）：[readmes/README_developer.md](readmes/README_developer.md)
- Harness 模型运行：[readmes/README_harness.md](readmes/README_harness.md)
- ArXivLean 评测：[readmes/README_arxivlean_september_evaluation.md](readmes/README_arxivlean_september_evaluation.md)
- LeanLean 评测：[readmes/README_leanlean.md](readmes/README_leanlean.md)
- 官网：[matharena.ai](https://matharena.ai/)，评测数据集与日志：[HuggingFace MathArena](https://huggingface.co/MathArena)
