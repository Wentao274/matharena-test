# MathArena Jenkins 触发评测使用手册

通过 Jenkins 参数化构建触发 MathArena 精度评测。评测机(远端)运行本仓库脚本,
被测模型部署在 GPU 机器上(vllm serve 等 OpenAI 兼容服务),评测机经 HTTP 访问。
**纯模型评测模式,不需要 Docker/沙箱**(Docker 仅 harness 类 agent 模型需要)。
**裁判仅支持本地部署的模型**(经 OpenAI 兼容 API 访问,`JUDGE_MODEL`/`JUDGE_BASE_URL` 必填),
不再提供 gemini 等远程裁判入口。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `jenkins/Jenkinsfile` | Jenkins pipeline:参数化构建、连通性预检、SSH 远程执行、拉取结果、发送邮件 |
| `jenkins/run_matharena_eval.py` | 评测机编排脚本:生成模型/裁判配置 → 预下载数据集 → run.py 评测 → judge.py 评判 → 提取评分 |
| `jenkins/extract_results.py` | 从 `outputs/` 逐题文件提取 pass@1,生成 `result_summary.json/csv`(已通过冒烟测试) |

每次触发会**删除并重建**以下运行时配置(固定文件名,全部字段由 Jenkins 参数传入),
不修改仓库任何原有文件:

| 文件 | 说明 |
| --- | --- |
| `configs/models/alaya_test_model.yaml` | 被测模型(每次触发) |
| `configs/models/alaya_local_judge.yaml` | 本地裁判模型(每次触发) |
| `configs/judges/local_judge.yaml` | 本地裁判配置(基于 `answer_judge.yaml` 原文逐字节复制,仅 `model_config` 指向生成的裁判模型) |

## 完整流程

```
Jenkins(slave-2 agent)
 ├─ 1. 打印测试参数                # 回显全部构建参数 + 裁判参数必填校验
 ├─ 2. API 连通性预检              # ssh 远端机 curl /v1/models + chat/completions
 │      # 同时预检本地裁判服务,裁判挂了提前失败(避免评测跑完才发现全"待评判")
 │      └ 失败 → 标记 UNSTABLE,跳过 3/4,邮件给出红色失败块
 ├─ 3. 环境检查                    # 清理残留评测进程 → 检查 uv → uv sync 同步依赖(每次触发,幂等) → 验证 venv 与关键依赖
 ├─ 4. 运行matharena评测           # ssh 远端机执行 run_matharena_eval.py:
 │        生成 alaya_test_model.yaml + alaya_local_judge.yaml + local_judge.yaml(存在先删除)
 │        → (代理)预下载 HuggingFace 数据集 → 评测/评判以 HF_HUB_OFFLINE 离线读缓存(不联网)
 │        → scripts/run.py 评测 (api: custom 纯模型,经 HTTP;生成配置含 harness: false 规避竞赛默认 codex harness)
 │        → scripts/judge/judge.py 评判 (ArXivMath 必需,裁判 = 本地部署模型)
 │        → jenkins/extract_results.py 提取 → result_summary.json/csv
 ├─ 5. 拉取测试结果                # scp jenkins_output/<tester>/<build>/<chip>/<model>/ 到 Jenkins
 └─ 6. 发送邮件                    # HTML 报告(概要表+各数据集 pass@1),附件日志+评分明细
post: 归档 artifacts + 清理 workspace
```

## 一、前置条件

### Jenkins 侧(管理员一次性配置)

| 项 | 值 |
| --- | --- |
| Agent label | `slave-2`(Linux,与 sgl-eval-test 相同的 agent) |
| SSH 凭据 ID | `HOST_SSH_KEY`(能免密登录远端评测机 `root@10.201.132.50`) |
| 必装插件 | Pipeline、SSH Agent、Email Extension、Pipeline Utility Steps(readJSON/fileExists) |

### 评测机侧(一次性准备)

```bash
cd /dingofs/data2/userdata/liwt/maas-image/matharena-test   # WORK_DIR
git pull                                                   # 拉到含 jenkins/ 目录的代码
# uv sync 可选:虚拟环境创建/依赖安装/验证由 pipeline 环境检查阶段每次构建自动执行
# 仅需确保评测机已安装 uv(未安装时环境检查阶段会报错并给出安装命令)
```

GPU 机器上按本仓库 `README_LOCAL.md` 的方式拉起模型服务,例如:

```bash
vllm serve /path/to/model --served-model-name glm-5.2 --port 8000
```

## 二、创建 Jenkins Job

1. Jenkins 首页 → **New Item** → 输入名称(如 `matharena-eval`)→ 选择 **Pipeline** → OK
2. 勾选 **This project is parameterized**(Pipeline Job 参数实际由 Jenkinsfile `parameters` 块自动生成,无需手工添加)
3. **Pipeline** 区域:
   - Definition: **Pipeline script from SCM**
   - SCM: Git → Repository URL: 本仓库地址 → Credentials: 具有读权限的凭据
   - Branch: 默认分支
   - Script Path: `jenkins/Jenkinsfile`
4. 保存。首次构建仅生成参数表(选择 *Build with Parameters* 前需要先空跑一次;
   Jenkins 的已知行为:第一次构建后参数表才会出现)

## 三、触发一次评测

1. 打开 Job → **Build with Parameters**
2. 填写参数(见下表),典型最小配置:

```
TESTER     = liwt                    # 你的名字,用于输出目录与邮件
CHIP       = nvidia-h100             # 芯片平台
MODEL      = glm-5.3                 # 与 vllm --served-model-name 一致
BASE_URL   = https://codingplan-staging.alayanew.com:26443   # 不要带 /v1(自动拼接)
API_KEY    = (空)                    # 服务有鉴权才填
TASK_ARXIVMATH = (勾选)              # ArXivMath 09/2026,53 题
JUDGE_MODEL    = qwen3.5-32b         # 必填!本地裁判模型名(裁判服务 --served-model-name)
JUDGE_BASE_URL = http://10.201.149.35:8000   # 必填!裁判服务地址,不带 /v1
JUDGE_API_KEY  = (空)                # 裁判服务有鉴权才填
RECIPIENTS     = liwt@zetyun.com     # 报告接收人,逗号分隔
```

裁判参数未填时构建会在"打印测试参数"阶段直接失败,不浪费算力。

3. 点击 **Build**,在 Console Output 观察进度
4. 结束后收取报告邮件;`jenkins_output/` 结果也会归档到该构建的 Artifacts

## 四、参数说明

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `TESTER` | 是 | 测试人员名称(组织输出目录、邮件展示) |
| `CHIP` | 是 | 芯片平台名称 |
| `MODEL` | 是 | 模型服务名,须与 `vllm serve --served-model-name` 一致;含 `/` 时取最后一段作为目录名 |
| `BASE_URL` | 是 | OpenAI 兼容端点根 URL,**不要带 `/v1`**;流水线幂等拼接(已带也不影响) |
| `API_KEY` | 否 | 模型服务鉴权 Key,无鉴权留空;写入生成配置的 `api_key_env` |
| `TASK_ARXIVMATH` | 是(勾选) | ArXivMath 09/2026(`arxiv/september`,研究级数学题,pass@1,需 LLM 评判) |
| `N` | 否 | 每题运行次数,默认 1(ArXivMath 官方口径;增大计算量线性增长) |
| `PROBLEMS` | 否 | 只跑指定题目:1 起始题号,空格分隔(如 `1 2 3 4 5`),**冒烟测试用**;空 = 全量。评判只处理已生成的运行文件,未跑题目在汇总中计为"缺失"(不算失败) |
| `MAX_TOKENS` | 否 | 生成最大 token 数,默认 131072;**不要超过模型上下文长度**,否则请求报错;留空 = 不写入配置 |
| `TEMPERATURE` / `TOP_P` | 否 | 采样参数,默认 1.0 / 0.95;留空 = 不写入配置 |
| `CONCURRENT_REQUESTS` | 否 | 并发请求数,默认 1;建议不超过模型服务的 `--max-num-seqs`,否则大量 429/超时 |
| `TIMEOUT` | 否 | 单请求超时,**单位秒**,默认空 = 框架默认 30000 秒 |
| `JUDGE_MODEL` | **是** | 本地裁判模型名,须与裁判服务 `--served-model-name` 一致;自动生成 `alaya_local_judge.yaml` + `local_judge.yaml`(固定名,每次触发删除重建)。建议与被测模型用不同家族的强模型,避免自评偏差 |
| `JUDGE_BASE_URL` | **是** | 本地裁判服务根 URL,不带 `/v1`(流水线自动拼接,幂等) |
| `JUDGE_API_KEY` | 否 | 本地裁判服务鉴权 Key,无鉴权留空;裁判固定 `temperature: 0` 保证评判可复现 |
| `PROXY_URL` | 否 | 代理,仅 **uv sync 依赖安装**与**数据集预下载**使用;**连通性检查、评测、评判一律不走代理**(各阶段显式清空代理变量,防全局环境泄漏)。默认占位符 `http://<proxy-host>:<proxy-port>`,填实际地址后生效(占位符=不用代理) |
| `DESCRIPTION` | 否 | 模型服务描述(仅邮件展示) |
| `RECIPIENTS` | 是 | 报告邮件接收者,逗号分隔 |
| `WORK_DIR` | 是 | 评测机上的仓库目录,默认 `/dingofs/data2/userdata/liwt/maas-image/matharena-test`,**请勿改动** |

## 五、查看结果

### 邮件报告
- 正文:测试概要表(参数/状态/耗时)、各数据集 pass@1 汇总表(已评判/已运行题数、
  待评判、缺失)、token 用量、连通性失败时显示红色失败块与原因
- 附件:`matharena_run.log`(评测日志)、`matharena_judge.log`(评判日志)、
  `result_summary.json`(汇总)、`result_summary.csv`(**逐题明细**)、
  连通性日志(如有)

### 输出目录结构
```
jenkins_output/<tester>/<build_number>/<chip>/<model>/
    matharena_run.log        # 评测日志(run.py 输出)
    matharena_judge.log      # 评判日志(judge.py 输出,多数据集追加)
    matharena_extract.log    # 提取日志
    result_summary.json      # 各数据集 pass@1 汇总(邮件数据源)
    result_summary.csv       # 逐题评分明细
    outputs/                 # MathArena 原生运行文件(run.py --output-dir 隔离)
        arxiv/september/alaya_test_model/0.json.zst ...
```

### 状态语义
| 构建状态 | 含义 |
| --- | --- |
| SUCCESS | 评测/评判/提取全部成功 |
| UNSTABLE | 某环节失败但报告仍发出:连通性失败 / 评测失败 / 评判失败 / 提取失败,详见邮件与控制台 |
| FAILURE | 无数据集被勾选 / 流水线级错误(如 SSH 失败) |

编排脚本退出码:0=全部成功;1=评测失败;2=数据集预下载失败;3=评判失败;4=提取失败
(存在未评判/缺失题时提取以退出码 2 提醒,不算失败,邮件体现"部分完成")。

## 六、断点续跑与重试

- 同一构建号重跑(Jenkins **Rebuild**/**Retry**):输出目录不变,
  `alaya_test_model.yaml` 按相同参数重建,`run.py` 自动跳过 `outputs/` 中
  已完成的题目,从断点继续;`judge.py` 同理跳过已评判题目
- 换新参数(如换 BASE_URL)**必须触发新构建**(新 build 号新目录),不要复用旧构建,
  否则新旧题目结果混在同一目录
- 评测机如有残留进程,下一次构建"环境检查"阶段会自动清理(SIGTERM → SIGKILL)

## 七、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 连通性预检失败,邮件红色块 | GPU 机器服务未启动 / BASE_URL 错 / 模型名不匹配。检查 `vllm serve` 是否存活,`curl http://<host>:8000/v1/models` 验证 |
| 邮件显示"待评判"大量题数 | 本地裁判侧问题:裁判服务未启动 / `JUDGE_MODEL` 与 `--served-model-name` 不匹配 / `JUDGE_BASE_URL` 错误,或裁判模型指令跟随太弱——裁判必须输出恰好一个 `<points>0</points>` 或 `<points>1</points>` 标签,格式不符自动重试 3 次后该题保持"待评判"。建议换更强的裁判模型 |
| 大量 429/超时警告 | CONCURRENT_REQUESTS 超过模型服务承受能力,调小(如 16/8) |
| 请求 400 报 context 超限 | MAX_TOKENS 超过模型上下文长度,按模型实际能力调小 |
| 数据集下载失败 | PROXY_URL 未填或代理不可用;编排脚本预下载失败会立即报错(退出码 2),已下载过的数据集走本地缓存 |
 | codex CLI 下载处 BrokenPipe | arxiv/september `allow_harness: true`,模型配置未显式 `harness: false` 时 runner 默认路由进 codex harness(需联网下载 CLI);生成配置已固定 `harness: false` 走纯模型 API |
| judge.py 报 `FileNotFoundError: configs/local_judge.yaml` | judge.py 按 `configs/{ref}.yaml` 拼接,ref 必须含子目录(同上游 `judges/answer_judge` 写法);编排脚本已改为传 `--judge-configs judges/local_judge` |
| 评测日志出现 `thinking_token_budget_exceeded (400)` 并重试 | 被测服务端思考预算上限(本次 65536)截断生成;api_client 自动重试,非流水线问题;频繁出现时需服务端调参或调小 `MAX_TOKENS` |
| 评测卡在 huggingface 重试 | 已修复:评测/评判以 HF_HUB_OFFLINE 离线模式读预下载缓存,不再向 HuggingFace 发联网检查;数据集拉取(含代理)只发生在预下载阶段 |
| 环境检查阶段报"未安装 uv" | 在评测机安装:`curl -LsSf https://astral.sh/uv/install.sh \| sh`(需出网或代理);依赖安装/venv 验证由每次构建自动执行,无需手工干预 |
| 冒烟测试只跑几道题 | `PROBLEMS` 填题号(如 `1 2 3 4 5`);评判自动只判已跑题目,邮件"缺失题数"较大属正常,pass@1 仅统计已评判题 |
| 连通性预检 401 | 服务要求 API Key:填 `API_KEY`(裁判服务填 `JUDGE_API_KEY`),预检会自动携带所填 Key 的 `Authorization: Bearer` 头;填了仍 401 说明 Key 无效或无权限 |
| API_KEY 填了但服务无鉴权 | 不影响(服务忽略鉴权头);反之服务有鉴权而未填,会在评测阶段大量 401 |
| 想加新数据集 | Jenkinsfile `parameters` 加 booleanParam → `运行matharena评测` stage 的 compList 加映射 → `extract_results.py` 的 `DISPLAY_NAMES` 加显示名 |

## 八、手工运行(不经 Jenkins,调试用)

在评测机仓库根目录直接执行编排脚本,参数与 Jenkins 一致。
**`--build-number` 可省略**:省略时自动生成 `manual-<YYYYMMDD-HHMMSS>`,
每次执行获得独立输出目录,不会覆盖此前的结果:

```bash
uv run python jenkins/run_matharena_eval.py \
    --tester liwt --chip nvidia-h100 \
    --model glm-5.3 --base-url https://codingplan-staging.alayanew.com:26443/v1 \
    --comps arxiv/september --n 1 \
    --judge-model qwen3.5-32b --judge-base-url http://10.201.149.35:8000
# → jenkins_output/liwt/manual-20261118-1530/nvidia-h100/glm-5.3/
```

| 场景 | 做法 |
| --- | --- |
| 常规手动执行 | 省略 `--build-number`,每次自动新目录(时间戳),前后互不覆盖 |
| 冒烟测试(只跑部分题) | 追加 `--problems '1 2 3 4 5'`(1 起始题号,空格分隔;空 = 全量) |
| 中断后续跑 | 重新执行并显式传回脚本启动时打印的编号,如 `--build-number manual-20261118-153045` |
| 对齐 Jenkins 结构 | 传任意编号即可,如 `--build-number manual-01` |

输出目录与 Jenkins 相同结构,适合在接入流水线前先验证环境与连通性。
另见 README_LOCAL.md 的"原生命令"方式(带时间戳 `--output-dir` 隔离同理)。
