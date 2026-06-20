# Archipelago Batch Eval — Local Scheduler + GCP Worker Pool

Run the official [mercor/apex-agents](https://huggingface.co/datasets/mercor/apex-agents)
benchmark (480 tasks) at scale, with progress tracking, idempotency, and
deterministic re-runs.

## Architecture

```
┌─────────────────┐   prepare state   ┌──────────────────────┐
│ Mac Local       │ ────────────────▶ │ GCS bucket           │
│ Scheduler       │                  │ state/task_ids.json  │
│ (编排/监控)      │                  │ state/jobs.json      │
└─────────────────┘                  └──────────┬───────────┘
        │                                      │ claim jobs
        │ poll GCS for status                  ▼
        │                          ┌──────────────────────┐
        ▼                          │ GCE Worker VMs      │
┌────────────────────┐             │ (pre-bootstrapped)  │
│ Mac local mirror   │             │ - GCS queue worker  │
│ ~/.archipelago-    │             │ - docker compose    │
│   eval/<project>/  │             │ - run agent         │
└────────────────────┘             │ - run grading       │
                                   │ - upload to GCS     │
                                   └──────────────────────┘
                                              │
                                              ▼
                                   ┌──────────────────────┐
                                   │ GCS bucket           │
                                   │ sotalab-archipelago- │
                                   │ eval                 │
                                   │  └─ eval-projects/   │
                                   │      └─ <project>/   │
                                   │         ├─ state/    │
                                   │         ├─ results/  │
                                   │         └─ logs/     │
                                   └──────────────────────┘
```

## Components

| Path | What |
|---|---|
| `scripts/local_scheduler.py` | Mac 端：sample tasks / 写 GCS 队列状态 / status / aggregate |
| `scripts/worker_queue.py` | VM 端：GCS claim 队列 worker，跑一个 job 后写 done/failure |
| `scripts/eval_worker.py` | 旧 Pub/Sub subscriber，仅保留作手动恢复入口 |
| `scripts/run_single_task.py` | 单任务 runner（起 environment + agent + grading + 上传 GCS） |
| `scripts/setup_worker.sh` | VM startup-script：装 docker/uv/git/gsutil, clone 仓库, 拉 New API token, 注册 systemd |
| `scripts/aggregate_results.py` | 拉所有 grades.json，算 pass@1/pass@k/mean score |
| `scripts/probe_new_api.sh` | 探活 New API staging 模型 |
| `scripts/teardown.sh` | 一键关 VM + bucket |
| `Makefile` | 顶层入口：`make provision / publish / status / aggregate / teardown` |

## Quick Start

```bash
cd /Users/lumin/sotalab/archipelago

# 一次性：建 GCS bucket + eval project prefixes
# 注：需要 IAM 角色 compute.admin + storage.admin + iam.serviceAccountUser
make provision

# 准备 15 个 job（5 tasks × 1 model × 3 attempts）的 GCS 队列状态
make publish

# 启动/扩容 GCS queue worker 池
make dynamic-workers TARGET_RUNNING=5 MAX_RUNNING=16

# 进度监控（每跑完一个 job，结果写到 GCS results/，status 立刻能查到）
make status

# 跑完后聚合：pass@1 / pass@3 / mean score
make aggregate

# 一键清理
make teardown
```

## Tunables

```bash
# 跑 qwen 评测
make publish EVAL_PROJECT=qwen-pass3 MODEL=qwen3.7-max

# 跑 10 题 × 5 attempts
make publish N_TASKS=10 K=5

# 只开 2 台 VM（debug 时）
make vm WORKER_COUNT=2
```

## Worker Idempotency & Re-run

`scripts/worker_queue.py` 在起跑前会检查：

```
gs://<bucket>/<project_dir>/results/<task_id>/attempt<N>/grades.json
```

- **存在 + `run_status=completed`** → 跳过整个 job（防重入）
- **不存在** → 正常跑
- **并发防重**：worker 通过 `state/claims/<queue>/<job_id>.json` 的 GCS generation precondition 原子 claim

```bash
# 重跑某个 task 的所有 attempts
python scripts/local_scheduler.py publish \
  --n-tasks 1 --k 3 \
  --force-rerun  # 注意：会重发整个 batch
```

更精细的重跑（单 attempt）需要扩展 `local_scheduler.py`，目前支持 `--force-rerun` 整体重发。

## Configuration Knobs (VM startup-script)

`scripts/setup_worker.sh` 顶部可改：

| Env var | 默认 |
|---|---|
| `ARCHIPELAGO_REPO_URL` | `https://github.com/SoTALab-ai/archipelago.git` |
| `ARCHIPELAGO_REVISION` | `main` |
| `EVAL_PROJECT_DIR` | `eval-projects/seed-pro-pass3-20260616` |
| `EVAL_BUCKET` | `sotalab-archipelago-eval` |
| `RUN_DYNAMIC_QUEUE` | `0`（`make dynamic-workers` 创建的 VM 会设为 `1`） |
| `QUEUE_NAME` | `pass5` |
| `TASK_IDS_GCS_URI` | `gs://<bucket>/<project_dir>/state/task_ids.json` |
| `NEW_API_SECRET_NAME` | `NEW_API_TOKEN-staging` |
| `NEW_API_BASE` | `https://new-api-staging.sotalab.ai/v1` |

## Why New API (not direct Gemini/OpenAI)

- 统一入口（OpenAI 兼容）
- 自带 cache / spend tracking
- 所有模型（`doubao-seed-2-0-pro-260215` / `qwen3.7-max` 等）走一个 token

`agents/.env` 写入：

```bash
LITELLM_PROXY_API_BASE=https://new-api-staging.sotalab.ai/v1
LITELLM_PROXY_API_KEY=<NEW_API_TOKEN-staging from Secret Manager>
```

注意：`/v1` 结尾很重要，否则 LiteLLM 拼 URL 拼错。

## Required IAM

`vincenthhcui@gmail.com` (你) 在 `sotalab-prod` 项目上需要：

- `roles/compute.admin`
- `roles/storage.admin`
- `roles/iam.serviceAccountUser`
- `roles/secretmanager.secretAccessor` (only on `sotalab-staging`, for the New API token)

## Project Directory Layout (GCS)

```
gs://sotalab-archipelago-eval/
└── eval-projects/
    ├── seed-pro-pass3-20260616/        # 本次评测
    │   ├── state/
    │   │   ├── manifest.json           # publish 时写
    │   │   ├── jobs.json               # publish 时写
    │   │   ├── task_ids.json           # queue worker 输入
    │   │   ├── claims/<queue>/         # worker 原子 claim
    │   │   ├── done/<queue>/           # worker 完成标记
    │   │   ├── failures/<queue>/       # worker 失败标记
    │   │   └── summary.json            # aggregate 时写
    │   ├── results/
    │   │   └── <task_id>/attempt<N>/
    │   │       ├── trajectory.json
    │   │       ├── grades.json
    │   │       ├── final_snapshot.tar.gz
    │   │       ├── run.log
    │   │       └── status.tsv          # 含 run_status / agent_exit / worker_id
    │   └── logs/
    │       └── worker-<hostname>.log
    └── qwen-pass3-20260715/            # 下次评测（隔离）
        └── ...
```

## End-to-End Smoke Test (manual)

If you just want to verify the pipeline works:

```bash
# 1. Pick a short task
TASK=task_7c394865481b40cdbdd577a039825679

# 2. Write a one-task jobs file and run one static attempt
printf '[{"task_id":"%s","model":"doubao-seed-2-0-pro-260215"}]\n' "$TASK" \
  > /tmp/archipelago-smoke-task_ids.json
gsutil cp /tmp/archipelago-smoke-task_ids.json \
  gs://sotalab-archipelago-eval/eval-projects/seed-pro-pass3-20260616/state/task_ids.json

python scripts/run_single_task.py \
  --task-id "$TASK" --attempt 1 \
  --model doubao-seed-2-0-pro-260215 \
  --eval-project eval-projects/seed-pro-pass3-20260616 \
  --bucket sotalab-archipelago-eval \
  --project-dir eval-projects/seed-pro-pass3-20260616

# 3. Watch the worker
make status

# 4. Confirm grades.json landed
gsutil ls gs://sotalab-archipelago-eval/eval-projects/seed-pro-pass3-20260616/results/$TASK/attempt1/

# 5. Aggregate (or just cat summary.json when all done)
make aggregate
```
