# 25 — 前端首访提速与图片显示加速方案

最后更新：2026-07-24  
范围：`web/` 静态导出站点（`gptimage.relai.asia`）  
测量基线：`web_dist` build @ 20260724-195225

---

## 1. 现状诊断

### 1.1 用户体感

| 现象 | 技术解释 |
|------|----------|
| Ctrl+F5 后首屏慢 | 需重新下载 `/_next/static/*`（~2.1 MB 共 43 文件），并并行打多个 API |
| 多页面访问后变快 | 浏览器 HTTP 缓存命中 + TCP/TLS 复用 + 部分接口数据已在内存 |
| 号池【刷新】曾不灵 | 已修：先 `reload-from-storage` 再拉列表（20260724 已部署） |

### 1.2 构建产物（实测）

```
static_total ≈ 2099 KB（43 文件）
最大单 chunk ≈ 273 KB
```

| 路由 | 页面源码行数 | HTML 引用 script 数 | 备注 |
|------|-------------|---------------------|------|
| `/accounts` | **3118** | 128 | 最重页面，首屏 +400ms 次要接口 |
| `/image` | 1981 | 110 | 生图表单 + 轮询 |
| `/ops` | 1276 | 104 | 运维大盘 |
| `/logs` | 513 | 110 | 含图片缩略图列表 |
| `/chat` | 33（入口） | 107 | 实际重逻辑在 `conversation-workbench` |
| `/login` | 101 | 101 | 相对轻 |

> HTML 里引用的 JS 总和会重复计算共享 chunk；**真实首访下载量 ≈ 全部 static 体积（~2.1 MB）**，不是单页 5 MB。

### 1.3 首屏 API 瀑布（号池页）

```
t=0ms     GET /api/accounts
t=50ms    settings / refresh-all / outlook-auto-recovery / panda-sync
t=400ms   activity/daily + ip-nurture
并行      usage/recent（不阻塞，但占带宽）
```

### 1.4 图片显示链路

- 组件：`web/src/components/image-thumbnail.tsx`
  - 优先 `/image-thumbnails/` 路径，失败回退原图
  - 已设 `loading="lazy"`、`decoding="async"`、`fetchPriority="low"`
- 后端：`services/image_storage_service` → `ensure_thumbnail` 生成缩略图
- 日志页 / 图片管理页均走该组件

---

## 2. 优化项收益评估

评分：**收益**（首访 TTI / 图片 LCP）× **成本**（改动量 + 风险）

| # | 方案 | 收益 | 成本 | 优先级 | 预期效果 |
|---|------|------|------|--------|----------|
| **P0-1** | **路由懒加载** `dynamic()`：`/chat`、`/debug`、`/ops`、`/image-manager` | **高** | 低 | ★★★★★ | 号池/生图/登录首包减少 **chat 专属** highlight.js + react-markdown + motion（估 **300–500 KB** 等价解析） |
| **P0-2** | **号池页拆子组件 + 按需加载**：活动日历、养号区块 `dynamic(..., { ssr:false })` | **高** | 中 | ★★★★☆ | 首屏 JS 执行时间 ↓；`activity/daily` 等接口推迟到区块展开或 idle |
| **P0-3** | **图片：列表虚拟化 + 首屏限流**（logs 一次只渲染可见行） | **高**（图片密集页） | 中 | ★★★★☆ | 日志页 200 条缩略图时 DOM/解码压力 ↓ **50–80%** |
| **P1-1** | **Bundle analyzer 审计 + 砍依赖** | **中** | 低 | ★★★★☆ | 确认 `motion` 是否仅在 chat 动画使用；`radix-ui` 全量 vs 按需 |
| **P1-2** | **缩略图 WebP + 固定尺寸**（后端生成 128/256 WebP） | **中高** | 中 | ★★★★☆ | 单张缩略图 **10–30 KB → 2–8 KB**；日志页 LCP 明显改善 |
| **P1-3** | **API 合并/缓存**：号池顶部统计一次接口返回 stats+refresh 状态 | **中** | 中 | ★★★☆☆ | 减少 4–6 个 RTT，弱网下 **200–500 ms** |
| **P2-1** | **静态资源 CDN**（`/_next/static` 长缓存 immutable） | **中**（远距离） | 低 | ★★★☆☆ | 你方用户若在国内、源站在 SG：**TTFB -50~150ms**；体积不变 |
| **P2-2** | **Service Worker 预缓存 shell** | **中** | 高 | ★★☆☆☆ | 二次访问接近瞬时；首访无收益；维护成本高 |
| **P2-3** | **全站 SSR/ISR 改架构** | 低（当前 export） | 极高 | ★☆☆☆☆ | 不推荐；与 `output: 'export'` 冲突 |

### 2.1 结论：值不值得做？

| 类别 | 建议 |
|------|------|
| **值得立刻做** | P0-1 路由懒加载、P0-2 号池按需加载、P1-1 analyzer |
| **值得排期** | P0-3 列表虚拟化、P1-2 WebP 缩略图 |
| **可选** | CDN（若源站 RTT >100ms）、API 合并 |
| **收益偏低/暂不做** | Service Worker、SSR 改造 |

**整体判断**：当前 **~2.1 MB static 不算巨型**，慢的主因是 **(1) 全路由共享大包 (2) 号池页巨型单文件 (3) 首屏 API 多 (4) 日志缩略图数量**。路由懒加载 + 号池拆分 **投入产出比最高**。

---

## 3. 分阶段实施计划

### Sprint FE-1（已部署 2026-07-24）

- [x] 路由 `dynamic()`：`/chat`、`/ops`、`/image-manager`
- [x] 号池：活动图表拆至 `accounts-activity-panels.tsx` 懒加载；养号数据仅「按 IP 分组」时拉取
- [x] 日志页虚拟滚动（>20 条启用 `@tanstack/react-virtual`）+ 阶段耗时全量分解 UI
- [x] WebP 缩略图（`ensure_thumbnail` → `.webp`，前端 webp→png→原图回退）
- [x] Bundle analyzer：`cd web && set ANALYZE=true&& npm run build`（`@next/bundle-analyzer`）

**目标**：非核心路由不进首包。

```tsx
// web/src/app/chat/page.tsx 示例
import dynamic from "next/dynamic";
const ConversationWorkbench = dynamic(
  () => import("./conversation-workbench").then((m) => m.ConversationWorkbench),
  { loading: () => <PageSkeleton /> },
);
```

| 文件 | 动作 |
|------|------|
| `chat/page.tsx` | dynamic 加载 workbench |
| `debug/page.tsx` | dynamic 加载 search-panel |
| `ops/page.tsx` | dynamic 加载主体 |
| `image-manager/page.tsx` | dynamic 加载 |

**验收**：
- `npm run build` 后 `/accounts` 引用 chunk 数减少或 accounts 专属 chunk 体积下降
- Lighthouse FCP on `/accounts`（慢 3G）改善 **≥15%**

### Sprint FE-2（2–3 天，号池瘦身）

1. 拆出 `accounts/activity-panel.tsx`、`accounts/nurture-panel.tsx`
2. 默认折叠养号区；展开时再 `fetchIpNurture*`
3. `requestIdleCallback` 或 `setTimeout(800)` 加载 `usage/recent`
4. 顶部【刷新】已含 reload + 全量刷新（已部署）

**验收**：号池首屏 Network 请求 ≤3 个（accounts + stats 可合并则 1–2）

### Sprint FE-3（2 天，图片加速）

| 层 | 改动 |
|----|------|
| 后端 | `ensure_thumbnail` 增 WebP 128/256；响应头 `Cache-Control: public, max-age=86400` |
| 前端 | `ImageThumbnail` 支持 `srcSet`；日志表 `react-window` 虚拟滚动 |
| 可选 | 图片 CDN 子域 `img.relai.asia` |

**验收**：日志页 100 条滚动 FPS ≥55；缩略图平均体积 ↓60%

### Sprint FE-4（0.5 天，审计）

```bash
cd web
npm install -D @next/bundle-analyzer
# next.config 开启 analyzer，build 后看 treemap
```

重点查：`motion`、`highlight.js` 语言包、`radix-ui` 重复、未用 `date-fns` locale。

---

## 4. CDN 与缓存策略（P2）

当前 Next export 已对 `/_next/static/chunks/*` 带 content hash，适合：

```
Cache-Control: public, max-age=31536000, immutable
```

Nginx 示例（Panda `web_dist`）：

```nginx
location /_next/static/ {
    alias /app/web_dist/_next/static/;
    expires 1y;
    add_header Cache-Control "public, immutable";
}
```

`/api/*` **不可**长缓存。`index.html` 建议 `max-age=0, must-revalidate` 以便发版即时生效。

---

## 5. 不做的项

- 不改 `output: 'export'` 为 Node SSR（运维复杂度 ↑）
- 不为首访上 Service Worker（维护/缓存失效风险）
- 不压缩业务功能（号池信息密度保留，只延迟非关键块）

---

## 6. 度量与回归

| 指标 | 工具 | 目标 |
|------|------|------|
| 首访 FCP/LCP | Lighthouse（Mobile Slow 4G） | FCP <2.5s，LCP <4s |
| JS 体积 | `_tmp_web_dist_size_report.py` | static_total <1.6 MB（-25%） |
| 号池 API 数 | DevTools Network | 首屏 ≤3 |
| 缩略图体积 | 抽样 20 张 | p50 <8 KB |

---

## 7. 关联

- 部署：号池刷新修复 `scripts/_tmp_deploy_refresh_logs.py`
- 日志时区：`services/log_service.py` → `Asia/Shanghai`（已部署）
- 生产延迟基线：`scripts/_tmp_run_prod_latency_suite.py` → `docs/captures/spa/PROD-latency-*`
