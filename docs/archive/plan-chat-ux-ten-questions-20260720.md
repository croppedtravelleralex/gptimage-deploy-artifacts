# 对话 UX 十问收口

最后更新：2026-07-20  
状态：**已部署 Panda**（artifacts `8224c9a`，备份 `git-artifacts-deploy-20260720-162812`）

## Track Q2 — 截图十问

| ID | 项 | 状态 |
|---|---|---|
| 1 | 剥离泄漏 `search(...)` | done |
| 2 | 流式慢/整段感 | partial（阶段条+说明；短回复上游常单 delta） |
| 3 | 代码块灰底 | done |
| 4/6 | chat_requirements 403 可读 | done |
| 5 | 链接跳转 | done |
| 7 | deepseek_error 说明 | done |
| 8 | 完成后消息下总耗时 | done |
| 9 | 仅消息区滚动 | done |
| 10 | 上传附件 | done（图片/文本） |

### 部署证据

```
BACKUP=/root/gptimage/backups/git-artifacts-deploy-20260720-162812
ARTIFACT_SHA=8224c9a
healthy True / schedulable 6 / inflight 0 / total 8
startup_errors 0
utils_helper=PRESENT / strip_leaked_tool_calls PRESENT
public: 总耗时=1 准备中=1 上传图片或文本=2 对话鉴权=1
```
