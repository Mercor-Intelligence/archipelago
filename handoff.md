# Archipelago Eval Pipeline — Handoff

## 目标

在 Archipelago 官方 benchmark（`mercor/apex-agents`）上跑 **5 task × 3 attempts** 的评测（pass@3 + pass@1 + mean score），用 **SoTALab 内部 New API** 作为 LLM provider（避免直连 Vertex AI / Anthropic）。

模型：**`doubao-seed-2-0-pro-260215`**（New API staging）。
Grader：**`doubao-seed-2-0-pro-260215`**（LLM-as-judge 走 New API proxy）。

目标 GCP 项目：**`sotalab-prod`**。

---

## 架构

```
Mac 本地调度器
   └─► GCS bucket: sotalab-archipelago-eval/eval-projects/<project>/
         ├─ state/        manifest.json, summary.json
         ├─ results/<task_id>/attempt<N>/    trajectory.json, grades.json, run.log, status.tsv
         ├─ logs/         worker stdout
         └─ setup/        archipelago.tgz, run_static.sh, apex-test-environment.tar

4 × GCE worker VMs (sotalab-prod, asia-east1-b, e2-standard-4)
   └─ 每个跑一个 static worker（脚本 worker_static.py）：
      - 拿固定 task_id 跑 k 个 attempts
      - 用预构建 docker image `apex-test-environment:latest` 起 environment
      - 调 New API 跑 agent + grading
```

**没有 Pub/Sub 路径**——之前 5 台 worker 用 `eval_worker.py` + Pub/Sub 自动分配，但**调试太痛**，换成了**静态分配**：每个 VM 跑一个固定 task。

---

## 进展（transcript 证据）

### ✅ 已完成
- **代码改造**（Mac 本地）：
  - `environment/docker-compose.yml` — 去掉 `container_name: archipelago-environment`（多 VM 并发不冲突）
  - `environment/docker-compose.yml` — `image: apex-test-environment:latest` + `pull_policy: never`
  - `examples/hugging_face_task/main.py` — `start_environment()` 加 fast path（如果 `apex-test-environment:latest` 存在，`docker run` 跳过 build）；失败回退 docker compose build
  - `examples/hugging_face_task/main.py` — grading 子进程 `grading_env` 把 `agents/.env`（LITELLM_PROXY）传过去
  - `examples/hugging_face_task/main.py` — 启动 environment 前 stop 所有占 port 8080 的容器
  - `examples/hugging_face_task/agent_config.json` — `max_steps: 50 → 200`
  - `examples/hugging_face_task/orchestrator_config.json` — agent model 用 `gemini-3.1-pro-preview`（裸名，New API 支持，无 `vertex_ai/` 前缀）
  - `examples/hugging_face_task/grading_settings.json` — grader model `doubao-seed-2-0-pro-260215`
  - `scripts/setup_worker.sh` — 从 GCS 拉代码、Secret Manager 拉 token、写 `agents/.env`、从 Artifact Registry 拉预构建 image、注册 systemd 服务
  - `scripts/run_single_task.py` — 拉 HF token 注入 env，转发 `LITELLM_PROXY_*` 给 agent subprocess
  - `scripts/worker_static.py` — 新写，静态分配 worker（一个 VM 一个 task）
  - `scripts/aggregate_results.py` — pass@1 / pass@k / mean score
  - `scripts/probe_new_api.sh` — New API 探活
  - `scripts/smoke_test.sh` — Mac 端 4 项 smoke test（HF token / New API / LiteLLM）
  - `scripts/monitor_gcs.py` — GCS 监控
  - `Makefile` — `make provision / publish / status / aggregate / teardown / probe / build-image / push-image`
  - `scripts/README.md` — 完整文档

### ✅ GCP 资源
- GCS-backed queue state under `state/task_ids.json`, `state/jobs.json`, `state/claims/`, `state/done/`, `state/failures/`
- GCS bucket `sotalab-archipelago-eval` + 项目目录 `eval-projects/seed-pro-pass3-20260616/`
- Secret Manager secrets（`sotalab-prod`）:
  - `NEW_API_TOKEN-staging`（从 staging 推过来）
  - `HF_TOKEN`
  - `GITHUB_TOKEN`（gh auth token，仅 setup_worker.sh 参考）

### ✅ 4 × GCE worker VMs
- `archipelago-eval-worker-1` 到 `archipelago-eval-worker-4`（`sotalab-prod / asia-east1-b / e2-standard-4`）
- Worker-5 没创建（quota 限制）
- 全部 setup 完（docker / gsutil / uv / agents + grading 依赖）

### ✅ Mac 端 smoke test 全过
```
[1/4] fetch New API token       OK (len=48)
[2/4] New API responds         OK (53 models)
[3/4] HF token                 OK (gated=auto, grants access)
[4/4] litellm proxy            OK doubao-seed + qwen3.6-27b both return "ok"
SMOKE TEST PASSED
```

### ✅ New API 模型验证（probe）
- `doubao-seed-2-0-pro-260215` — 在线
- `qwen3.6-27b` — 在线
- `gemini-3.1-pro-preview`（裸名）— 在线
- **`vertex_ai/gemini-3.1-pro-preview`**（带前缀）— **不在 New API 列表**，所以 agent_config 用裸名

### ✅ Agent 真实跑通（worker-1，task_498）
- Step 1-50 全跑完
- Tool 调用真实工作：`filesystem_server_search_files`, `pdf_server_pdf`, `code_execution_server_code_exec`
- 模型用 `doubao-seed-2-0-pro-260215` 通过 New API 响应（之前 verify 过直 curl）
- **rc=0**，但 grading 跑了没生成 `grades.json`

### ❌ 没跑通
- **grades.json 没生成**：第一次跑 `agent_config.json` 里 `max_steps=50` 太小，agent 跑到 50 步没出 `final_answer`，框架 `agent status: failed` → "Skipping grading"
- **修了 max_steps=200**，但发现 orchestrator_config.json 还是 `vertex_ai/gemini-3.1-pro-preview`（本地 Edit 没真保存，或 tgz 没传最新）→ 改了两次了（先 doubao-seed-2-0-pro-260215 后改 gemini-3.1-pro-preview），需要重 deploy + 重 run

### ❌ 端到端跑通（run_grader）
- agent 跑完 + grading 跑完 + grades.json 落 GCS —— **还没真验证**
- 多次 retry / fail 都因为 env 不对 / 模型名错 / max_steps 太小 / uv 权限 / venv python symlink 等
- 最近的修法（`gemini-3.1-pro-preview` 裸名 + `max_steps=200`）**还没实测**

---

## 尝试过的坑（避坑参考）

| 问题 | 修法 |
|---|---|
| `docker compose -p <project>` 多 VM 撞 container_name | 删 `container_name`，compose project name 隔离 |
| Docker daemon 拉不到镜像（防火墙） | VM 在 GCP 内网，没问题；Mac 本地用 Colima 会撞 |
| `mercor/apex-agents` gated repo 401 | Secret Manager 存 HF_TOKEN，setup_worker.sh 写到 `/root/.cache/huggingface/token`；run_single_task.py 也读 `/home/lumin/.cache/huggingface/token` |
| `mercor-mcp-shared` 私有 GitHub dep 拉不到 | 用 `apex-local-samples` 分支（替换为本地 `mcp-schema` path）；删 `--mount=secret,id=github_token` |
| `vertex_ai/gemini-3.1-pro-preview` 报 ServiceUnavailable | 改成裸名 `gemini-3.1-pro-preview`（New API 不识别 `vertex_ai/` 前缀）|
| `LITELLM_PROXY_API_BASE` 没传给 agent subprocess | run_single_task.py 显式 `env["LITELLM_PROXY_API_BASE"] = os.environ["..."]` |
| agents/.env 没被 litellm 加载 | env 文件要在 agents 目录；pydantic_settings 自动读 |
| Pub/Sub Worker 收不到消息（debug 困难） | 改静态 worker / GCS queue 方案，不再把默认评测链路接到 Pub/Sub |
| VM 没预构建 image（fallback 到 build，缺 github_token） | 把 image tar 推到 GCS，setup_worker.sh 从 Artifact Registry 拉（`ENV_IMAGE=asia-east1-docker.pkg.dev/sotalab-prod/docker-repo/archipelago-environment`） |
| VM venv 是 root 装的，lumin 读不了 | `sudo cp -r /root/.local/share/uv/python /opt/uv-python/`，venv symlink 改成 `/opt/uv-python/python/...` |
| `uv: Permission denied` | `sudo cp /root/.local/bin/uv /home/lumin/.local/bin/uv && chmod +x` |
| 端口 8080 占用（stuck container） | main.py 启动前 `docker ps --format ... \| awk '/0\.0\.0\.0:8080->/ {print $1}' \| xargs -r docker stop` |
| gcloud SSH SSL EOF 反复 | wait + retry；keep 命令短 |
| 旧 `eval_worker.py` Pub/Sub 配置容易漂移 | 默认不再启动 Pub/Sub subscriber；只在 `RUN_LEGACY_PUBSUB=1` 时手动启用 |
| gcloud `add-metadata --metadata-from-file` 不真更新 | 用 `--metadata="startup-script=<inline content>"` 强制 inline |
| gcs `gsutil -h "Cache-Control: no-cache" cp` 报 help | 用普通 `gsutil cp`，GCS 没有 server-side cache |
| agent 50 步没 final_answer | max_steps=200；改 grading_settings 用 New API judge |

---

## 当前需要做的（另一位 Agent 接手）

### 立刻执行（5 分钟）

1. **重打包 tgz**（已包含 max_steps=200 + orchestrator=gemini-3.1-pro-preview）：
   ```bash
   cd /Users/lumin/sotalab/archipelago
   tar --exclude='.apex_local' --exclude='apex-samples.zip' --exclude='.git' \
       --exclude='**/__pycache__' --exclude='**/.venv' --exclude='**/output' \
       --exclude='*.tar.gz' --exclude='*.zip' \
       -czf /tmp/archipelago.tgz archipelago
   gsutil cp /tmp/archipelago.tgz gs://sotalab-archipelago-eval/eval-projects/seed-pro-pass3-20260616/setup/archipelago.tgz
   ```

2. **在 4 台 VM 上重 deploy**（每台都做）：
   ```bash
   for w in 1 2 3 4; do
     VM="archipelago-eval-worker-$w"
     gcloud compute ssh lumin@$VM --project=sotalab-prod --zone=asia-east1-b --command="pkill -9 -f worker_static.py; pkill -9 -f main.py; pkill -9 -f run_single_task.py; sleep 2; sudo rm -rf /opt/archipelago/examples /opt/archipelago/scripts; sudo gsutil cp gs://sotalab-archipelago-eval/eval-projects/seed-pro-pass3-20260616/setup/archipelago.tgz /tmp/archipelago.tgz; cd /opt; sudo tar xzf /tmp/archipelago.tgz; sudo chown -R lumin:lumin /opt/archipelago; cat /opt/archipelago/examples/hugging_face_task/orchestrator_config.json; docker ps -a --format '{{.ID}}' | xargs -r docker rm -f"
   done
   ```
   确认每台 `orchestrator_config.json` 都是 `gemini-3.1-pro-preview`。

3. **启动 worker_static**（每台跑不同 task）：
   ```bash
   gcloud compute ssh lumin@archipelago-eval-worker-1 --project=sotalab-prod --zone=asia-east1-b --command="nohup setsid /home/lumin/.local/bin/run_static.sh --task-id task_7c394865481b40cdbdd577a039825679 --k 3 --model gemini-3.1-pro-preview --force > /tmp/static_worker.log 2>&1 < /dev/null & sleep 5; tail -5 /tmp/static_worker
