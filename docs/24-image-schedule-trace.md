# 生图调度追踪（image_schedule_trace）

状态：**v0.1 已接线**（2026-07-24）  
关联：`21-image-scheduling-and-pipeline.md`、`PROD-latency-phase-breakdown-20260724.md`

## 目标

为每个生图任务记录 **队列进/出、上槽/占槽/下槽** 等检测点时间戳，用前后时间差构建：

- `phases_ms`（与现有 `phase_timings_ms` 对齐）
- `explanations`（为何慢：池子 active/queued、并发闸门等）
- `checkpoints`（各事件首次 mono_ns）

**所有串行/并发验收**应走同一套 `schedule_trace` 模块；call log `detail.schedule_trace` 落盘。

## 架构

```text
ImageTaskService.enqueue  → task_queued
worker start              → task_worker_start
PipelineRun.begin         → pipeline_admit
mark_account_*            → account_wait_start / account_acquired
acquire_ss                → ready_buffer_* / ss_queue_enter / ss_slot_acquired (+ pool aux)
mark_sse_stream_end       → sse_stream_end
poll 完成                 → poll_resolve_end
download                  → download_start / download_end
finish                    → pipeline_finish → task_terminal
```

| 组件 | 路径 |
|------|------|
| Rust 核心（cdylib，热路径） | `crates/image_schedule_trace/` |
| Python 包装 + fallback | `services/image_pipeline/schedule_trace.py` |
| 阶段模型（Python 镜像） | `services/image_pipeline/schedule_trace_model.py` |
| 构建 | 本机 `build_schedule_trace_linux.py`（Docker Linux `.so`）+ `build_schedule_optimization_artifact.py`；**禁 Panda 编译/scp** |
| 验收 | `python scripts/_tmp_verify_schedule_trace.py [--panda]` |

## 性能

- 热路径：`emit()` 为 **append 一条 (u8,u64,u32)**，Rust 侧无字符串分配
- `aux` 打包池快照：`high16=active, low16=queued|slot`
- 每任务预分配 ~32 事件；finish 时一次性 JSON 序列化
- 无 Rust 库时自动 **Python fallback**（功能一致，略慢）

配置：`image_pipeline.schedule_trace_enabled`（默认 `true`）

## 检测点清单

| kind | 含义 |
|------|------|
| `task_queued` | 任务入队 |
| `task_worker_start` | worker 开跑 |
| `pipeline_admit` | pipeline admit |
| `account_wait_start` / `account_acquired` | 取号 |
| `ready_buffer_wait_*` | ready_buffer 背压 |
| `ss_queue_enter` | 进入 sS SlotPool 等待（含 active/queued） |
| `ss_slot_acquired` / `ss_slot_released` | 上槽/下槽 |
| `sse_stream_end` | SSE 段结束 |
| `poll_resolve_end` | 轮询拿到 URL |
| `download_*` | 下载 |
| `pipeline_finish` / `task_terminal` | 结束 |

可扩展：`global_concurrency_wait_*`、`ps_*`（已预留 kind id）

## 验收

```bash
# 本地 smoke（13 events + phases_ms）
python scripts/_tmp_verify_schedule_trace.py

# Panda 单次生图 + call log 校验（需先 artifacts 部署）
python scripts/build_schedule_trace_linux.py --target linux
python scripts/build_schedule_optimization_artifact.py
# push .artifact-schedule-deploy → gptimage-deploy-artifacts，Panda 上 deploy_panda_schedule_core.sh
python scripts/_tmp_verify_schedule_trace.py --panda --email qaflowakjewai6ps@proton.me
```

产物：`docs/captures/spa/schedule-trace-verify-*.json`

## 与优化闭环

1. conc10/serial 跑完 → 从 call log 拉 `schedule_trace`
2. 看 `ss_queue_enter.pool_active/queued` 与 `explanations`
3. 区分：**本批占槽** vs **外部流量** vs **槽未释放**
4. 调度调参：`submit_workers` / `sse_slots` / early `release_ss`
