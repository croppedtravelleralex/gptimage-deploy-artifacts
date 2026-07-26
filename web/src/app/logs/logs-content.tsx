"use client";

import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, ImageIcon, LoaderCircle, RefreshCw, Search, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { DateRangeFilter } from "@/components/date-range-filter";
import { ImageLightbox } from "@/components/image-lightbox";
import { ImageThumbnail, getImageThumbnailUrl } from "@/components/image-thumbnail";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { deleteSystemLogs, fetchSystemLogs, type SystemLog } from "@/lib/api";
import {
  buildCallLogPhases,
  dedupeCallLogs,
  formatDurationMs,
  formatTokensPerSec,
  getInlinePhases,
  type PhaseTiming,
} from "@/lib/image-log-phases";

const LogType = {
  Call: "call",
  Account: "account",
  LlmOps: "llm_ops",
} as const;

const typeLabels: Record<string, string> = {
  [LogType.Call]: "调用日志",
  [LogType.Account]: "账号管理日志",
  [LogType.LlmOps]: "LLM 操作日志",
};

function getDetailText(item: SystemLog, key: string) {
  const value = item.detail?.[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : "-";
}

function PhaseChip({ phase }: { phase: PhaseTiming }) {
  return (
    <span
      className="inline-flex max-w-full flex-col rounded bg-stone-100 px-1.5 py-0.5 text-[10px] leading-tight text-stone-600"
      title={phase.hint ? `${phase.label}: ${phase.hint}` : phase.label}
    >
      <span>
        {phase.label} {(phase.ms / 1000).toFixed(1)}s
      </span>
    </span>
  );
}

function DurationCell({ item }: { item: SystemLog }) {
  const detail = item.detail as Record<string, unknown> | undefined;
  const phases = getInlinePhases(detail);
  const tokensPerSec = formatTokensPerSec(detail);
  return (
    <div className="min-w-[220px] space-y-1">
      <div className="font-medium text-stone-800">{formatDurationMs(detail)}</div>
      {phases.length ? (
        <div className="flex flex-wrap gap-1">
          {phases.map((phase) => (
            <PhaseChip key={phase.key} phase={phase} />
          ))}
        </div>
      ) : null}
      {tokensPerSec ? <div className="text-[10px] text-stone-400">{tokensPerSec}</div> : null}
    </div>
  );
}

function PhaseTimingsPanel({ phases }: { phases: PhaseTiming[] }) {
  if (!phases.length) return null;
  return (
    <div className="rounded-xl border border-stone-200 bg-white p-4">
      <div className="mb-1 text-sm font-medium text-stone-700">阶段耗时（端到端分解）</div>
      <p className="mb-3 text-xs leading-5 text-stone-500">
        各段为 pipeline 实测字段；「开票+SSE」含 requirements / ticket / 上游 image_gen 空窗，不等于 SSE 传输时长；「轮询收图」由 sS 槽占用 − 取号 − 开票+SSE 推导。
      </p>
      <div className="grid gap-2">
        {phases.map((item) => (
          <div key={item.key} className="flex flex-col gap-0.5 rounded-lg bg-stone-50 px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <span className="text-sm text-stone-700">
                {item.label}
                {item.derived ? <span className="ml-1 text-[10px] text-stone-400">（推导）</span> : null}
              </span>
              {item.hint ? <div className="text-[11px] leading-4 text-stone-400">{item.hint}</div> : null}
            </div>
            <span className="shrink-0 font-mono text-sm font-medium text-stone-800">{(item.ms / 1000).toFixed(2)} s</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function getUrls(item: SystemLog | null) {
  const urls = item?.detail?.urls;
  return Array.isArray(urls) ? urls.filter((url): url is string => typeof url === "string") : [];
}

function getStatus(item: SystemLog) {
  const status = item.detail?.status;
  if (status === "success") return "成功";
  if (status === "failed") return "失败";
  return "-";
}

export default function LogsContent() {
  const [items, setItems] = useState<SystemLog[]>([]);
  const [type, setType] = useState<string>(LogType.Call);
  const [source, setSource] = useState("");
  const [outcome, setOutcome] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [detailLog, setDetailLog] = useState<SystemLog | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [lightboxIndex, setLightboxIndex] = useState(0);
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [isLoading, setIsLoading] = useState(true);
  const [isDeleting, setIsDeleting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [deletingItems, setDeletingItems] = useState<SystemLog[]>([]);
  const detailUrls = getUrls(detailLog);
  const detailImages = detailUrls.map((url, index) => ({ id: `${index}`, src: url }));
  const detailPhaseTimings = buildCallLogPhases(detailLog?.detail as Record<string, unknown> | undefined);
  const detailUsage = detailLog?.detail as Record<string, unknown> | undefined;
  const isCallLog = type === LogType.Call;
  const isLlmOps = type === LogType.LlmOps;
  const visibleItems = useMemo(
    () => (isCallLog ? dedupeCallLogs(items) : items),
    [items, isCallLog],
  );
  const pageCount = Math.max(1, Math.ceil(visibleItems.length / pageSize));
  const safePage = Math.min(page, pageCount);
  const currentRows = visibleItems.slice((safePage - 1) * pageSize, safePage * pageSize);
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const currentPageSelected = currentRows.length > 0 && currentRows.every((item) => selectedSet.has(item.id));
  const allSelected = visibleItems.length > 0 && visibleItems.every((item) => selectedSet.has(item.id));
  const loadLogs = async () => {
    setIsLoading(true);
    try {
      const data = await fetchSystemLogs({
        type,
        start_date: startDate,
        end_date: endDate,
        source: isLlmOps ? source : undefined,
        outcome: isLlmOps ? outcome : undefined,
        limit: 2000,
      });
      setItems(data.items);
      setSelectedIds((current) => current.filter((id) => data.items.some((item) => item.id === id)));
      setPage(1);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载日志失败");
    } finally {
      setIsLoading(false);
    }
  };

  const clearFilters = () => {
    setStartDate("");
    setEndDate("");
    setSource("");
    setOutcome("");
  };

  const openDetail = (item: SystemLog) => {
    setDetailLog(item);
    setDetailOpen(true);
  };

  const openLogImage = (item: SystemLog, index: number) => {
    setDetailLog(item);
    setLightboxIndex(index);
    setLightboxOpen(true);
  };

  const toggleIds = (ids: string[], checked: boolean) => {
    setSelectedIds((current) => checked ? Array.from(new Set([...current, ...ids])) : current.filter((id) => !ids.includes(id)));
  };

  const confirmDelete = async () => {
    const ids = deletingItems.map((item) => item.id);
    if (ids.length === 0) return;
    setIsDeleting(true);
    try {
      const data = await deleteSystemLogs(ids);
      toast.success(`已删除 ${data.removed} 条日志`);
      setDeletingItems([]);
      setSelectedIds((current) => current.filter((id) => !ids.includes(id)));
      if (detailLog && ids.includes(detailLog.id)) {
        setDetailOpen(false);
        setDetailLog(null);
      }
      await loadLogs();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除日志失败");
    } finally {
      setIsDeleting(false);
    }
  };

  useEffect(() => {
    void loadLogs();
  }, [type, startDate, endDate, source, outcome]);

  return (
    <section className="space-y-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-1">
          <div className="text-xs font-semibold tracking-[0.18em] text-stone-500 uppercase">Logs</div>
          <h1 className="text-2xl font-semibold tracking-tight">日志管理</h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <Select value={type} onValueChange={setType}>
            <SelectTrigger className="h-10 w-[150px] rounded-xl border-stone-200 bg-white"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value={LogType.Call}>调用日志</SelectItem>
              <SelectItem value={LogType.Account}>账号管理日志</SelectItem>
              <SelectItem value={LogType.LlmOps}>LLM 操作日志</SelectItem>
            </SelectContent>
          </Select>
          {isLlmOps ? (
            <>
              <Select value={source || "__all__"} onValueChange={(value) => setSource(value === "__all__" ? "" : value)}>
                <SelectTrigger className="h-10 w-[130px] rounded-xl border-stone-200 bg-white"><SelectValue placeholder="source" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all__">全部 source</SelectItem>
                  <SelectItem value="L0">L0</SelectItem>
                  <SelectItem value="L1">L1</SelectItem>
                  <SelectItem value="L2">L2</SelectItem>
                  <SelectItem value="ai_review">ai_review</SelectItem>
                </SelectContent>
              </Select>
              <Select value={outcome || "__all__"} onValueChange={(value) => setOutcome(value === "__all__" ? "" : value)}>
                <SelectTrigger className="h-10 w-[130px] rounded-xl border-stone-200 bg-white"><SelectValue placeholder="outcome" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all__">全部 outcome</SelectItem>
                  <SelectItem value="ok">ok</SelectItem>
                  <SelectItem value="reject">reject</SelectItem>
                  <SelectItem value="error">error</SelectItem>
                </SelectContent>
              </Select>
            </>
          ) : null}
          <DateRangeFilter startDate={startDate} endDate={endDate} onChange={(start, end) => { setStartDate(start); setEndDate(end); }} />
          <Button variant="outline" onClick={clearFilters} className="h-10 rounded-xl border-stone-200 bg-white px-4 text-stone-700">
            清除筛选条件
          </Button>
          <Button onClick={() => void loadLogs()} disabled={isLoading} className="h-10 rounded-xl bg-stone-950 px-4 text-white hover:bg-stone-800">
            {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <Search className="size-4" />}
            查询
          </Button>
        </div>
      </div>

      <Card className="overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="p-0">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-stone-100 px-5 py-4">
            <div className="flex flex-wrap items-center gap-3 text-sm text-stone-600">
              <span>共 {visibleItems.length} 条{isCallLog && visibleItems.length !== items.length ? `（去重前 ${items.length}）` : ""}</span>
              <label className="flex items-center gap-2">
                <Checkbox checked={currentPageSelected} onCheckedChange={(checked) => toggleIds(currentRows.map((item) => item.id), Boolean(checked))} />
                本页全选
              </label>
              <label className="flex items-center gap-2">
                <Checkbox checked={allSelected} onCheckedChange={(checked) => toggleIds(visibleItems.map((item) => item.id), Boolean(checked))} />
                全选结果
              </label>
              {selectedIds.length > 0 ? <span>已选 {selectedIds.length} 条</span> : null}
              <Select
                value={String(pageSize)}
                onValueChange={(value) => {
                  setPageSize(Number(value));
                  setPage(1);
                }}
              >
                <SelectTrigger className="h-8 w-[110px] rounded-lg border-stone-200 bg-white text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="10">10 条/页</SelectItem>
                  <SelectItem value="50">50 条/页</SelectItem>
                  <SelectItem value="200">200 条/页</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center gap-2">
              <Button variant="ghost" className="h-8 rounded-lg px-3 text-stone-500" onClick={() => void loadLogs()} disabled={isLoading}>
                <RefreshCw className={`size-4 ${isLoading ? "animate-spin" : ""}`} />
                刷新
              </Button>
              <button type="button" className="text-sm text-stone-500 hover:text-stone-900 disabled:text-stone-300" onClick={() => setSelectedIds([])} disabled={selectedIds.length === 0 || isDeleting}>
                取消选择
              </button>
              <Button variant="outline" className="h-8 rounded-lg border-rose-200 bg-white px-3 text-rose-600 hover:bg-rose-50" onClick={() => setDeletingItems(visibleItems.filter((item) => selectedSet.has(item.id)))} disabled={selectedIds.length === 0 || isDeleting}>
                <Trash2 className="size-4" />
                删除所选
              </Button>
            </div>
          </div>
          <div className="overflow-x-auto">
            <Table className="min-w-[900px]">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-12"></TableHead>
                  <TableHead>时间</TableHead>
                  <TableHead>类型</TableHead>
                  {isCallLog ? <TableHead>令牌名称</TableHead> : null}
                  {isCallLog ? <TableHead>调用耗时</TableHead> : null}
                  {isCallLog ? <TableHead>状态</TableHead> : null}
                  {isCallLog ? <TableHead className="w-36">图片</TableHead> : null}
                  {isLlmOps ? <TableHead>source</TableHead> : null}
                  {isLlmOps ? <TableHead>kind</TableHead> : null}
                  {isLlmOps ? <TableHead>outcome</TableHead> : null}
                  {isLlmOps ? <TableHead>耗时</TableHead> : null}
                  {isLlmOps ? <TableHead>account_hash</TableHead> : null}
                  <TableHead>简述</TableHead>
                  <TableHead className="w-40">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {currentRows.map((item) => {
                  const urls = getUrls(item);
                  return (
                    <TableRow key={item.id} className="text-stone-600">
                      <TableCell>
                        <Checkbox checked={selectedSet.has(item.id)} onCheckedChange={(checked) => toggleIds([item.id], Boolean(checked))} />
                      </TableCell>
                      <TableCell className="whitespace-nowrap">{item.time}</TableCell>
                      <TableCell><Badge variant="secondary" className="rounded-md">{typeLabels[item.type] || item.type}</Badge></TableCell>
                      {isCallLog ? <TableCell>{getDetailText(item, "key_name")}</TableCell> : null}
                      {isCallLog ? <TableCell><DurationCell item={item} /></TableCell> : null}
                      {isCallLog ? (
                        <TableCell>
                          <Badge variant={item.detail?.status === "failed" ? "danger" : "success"} className="rounded-md">
                            {getStatus(item)}
                          </Badge>
                        </TableCell>
                      ) : null}
                      {isCallLog ? (
                        <TableCell>
                          {urls.length ? (
                            <div className="flex items-center gap-1.5">
                              {urls.slice(0, 3).map((url, imageIndex) => (
                                <button
                                  key={`${url}-${imageIndex}`}
                                  type="button"
                                  className="relative size-9 overflow-hidden rounded-lg border border-stone-200 bg-stone-100"
                                  onClick={() => openLogImage(item, imageIndex)}
                                  title="预览图片"
                                >
                                  <ImageThumbnail src={url} thumbnailSrc={getImageThumbnailUrl(url)} className="h-full w-full" />
                                </button>
                              ))}
                              {urls.length > 3 ? <span className="text-xs text-stone-400">+{urls.length - 3}</span> : null}
                            </div>
                          ) : (
                            <span className="inline-flex items-center gap-1 text-xs text-stone-400">
                              <ImageIcon className="size-3.5" />
                              -
                            </span>
                          )}
                        </TableCell>
                      ) : null}
                      {isLlmOps ? <TableCell>{getDetailText(item, "source")}</TableCell> : null}
                      {isLlmOps ? <TableCell>{getDetailText(item, "kind")}</TableCell> : null}
                      {isLlmOps ? (
                        <TableCell>
                          <Badge
                            variant={item.detail?.outcome === "error" || item.detail?.outcome === "reject" ? "danger" : "success"}
                            className="rounded-md"
                          >
                            {getDetailText(item, "outcome")}
                          </Badge>
                        </TableCell>
                      ) : null}
                      {isLlmOps ? (
                        <TableCell>
                          {typeof item.detail?.latency_ms === "number" ? `${item.detail.latency_ms} ms` : "-"}
                        </TableCell>
                      ) : null}
                      {isLlmOps ? <TableCell className="font-mono text-xs">{getDetailText(item, "account_hash")}</TableCell> : null}
                      <TableCell className="max-w-[420px] truncate text-stone-500">{item.summary || "-"}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          <Button variant="ghost" className="h-8 rounded-lg px-3 text-stone-600" onClick={() => openDetail(item)}>
                            查看详情
                          </Button>
                          <Button variant="ghost" className="h-8 rounded-lg px-3 text-rose-600 hover:bg-rose-50 hover:text-rose-700" onClick={() => setDeletingItems([item])}>
                            删除
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
          <div className="flex items-center justify-end gap-2 border-t border-stone-100 px-4 py-3 text-sm text-stone-500">
            <span>第 {safePage} / {pageCount} 页，共 {visibleItems.length} 条</span>
            <Button variant="outline" size="icon" className="size-9 rounded-lg border-stone-200 bg-white" disabled={safePage <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>
              <ChevronLeft className="size-4" />
            </Button>
            <Button variant="outline" size="icon" className="size-9 rounded-lg border-stone-200 bg-white" disabled={safePage >= pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))}>
              <ChevronRight className="size-4" />
            </Button>
          </div>
          {!isLoading && visibleItems.length === 0 ? <div className="px-6 py-14 text-center text-sm text-stone-500">没有找到日志</div> : null}
        </CardContent>
      </Card>
      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="flex h-[min(88vh,860px)] w-[min(92vw,920px)] flex-col overflow-hidden rounded-2xl p-0">
          <DialogHeader className="shrink-0 border-b border-stone-100 px-6 py-5">
            <DialogTitle>日志详情</DialogTitle>
          </DialogHeader>
          <div className="flex-1 overflow-y-auto px-6 py-5">
            <div className="space-y-4">
              <div className="grid gap-3 rounded-xl border border-stone-200 bg-white p-4 text-sm text-stone-600 md:grid-cols-2">
                {Object.entries(detailLog?.detail || {})
                  .filter(([key, value]) => key !== "urls" && key !== "phase_timings_ms" && typeof value !== "object")
                  .map(([key, value]) => (
                    <div key={key} className="flex items-start justify-between gap-4">
                      <span className="text-stone-400">{key}</span>
                      <span className="text-right font-medium break-all text-stone-700">{String(value)}</span>
                    </div>
                  ))}
              </div>
              {detailPhaseTimings.length ? <PhaseTimingsPanel phases={detailPhaseTimings} /> : null}
              {detailLog?.type === LogType.Call && detailUsage ? (
                <div className="rounded-xl border border-stone-200 bg-white p-4">
                  <div className="mb-3 text-sm font-medium text-stone-700">Token / 流量</div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {[
                      ["prompt_tokens", "输入 token"],
                      ["completion_tokens", "输出 token"],
                      ["tokens_per_sec", "吞吐"],
                      ["upload_bytes", "上行字节"],
                      ["download_bytes", "下行字节"],
                      ["traffic_bytes", "总流量"],
                    ].map(([key, label]) => {
                      const raw = detailUsage[key];
                      if (raw == null) return null;
                      let text = String(raw);
                      if (key === "tokens_per_sec" && typeof raw === "number") {
                        text = `${raw.toFixed(1)} t/s`;
                      } else if (key.endsWith("_bytes") && typeof raw === "number") {
                        text = raw >= 1024 * 1024
                          ? `${(raw / (1024 * 1024)).toFixed(2)} MiB`
                          : raw >= 1024
                            ? `${(raw / 1024).toFixed(1)} KiB`
                            : `${raw} B`;
                      }
                      return (
                        <div key={key} className="flex items-center justify-between rounded-lg bg-stone-50 px-3 py-2 text-sm">
                          <span className="text-stone-500">{label}</span>
                          <span className="font-medium text-stone-800">{text}</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : null}
              {detailUrls.length ? (
                <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-3">
                  {detailUrls.map((url, index) => (
                    <button
                      key={url}
                      type="button"
                      className="aspect-square overflow-hidden rounded-xl border border-stone-200 bg-stone-100"
                      onClick={() => {
                        setLightboxIndex(index);
                        setLightboxOpen(true);
                      }}
                    >
                      <ImageThumbnail src={url} thumbnailSrc={getImageThumbnailUrl(url)} className="h-full w-full" />
                    </button>
                  ))}
                </div>
              ) : null}
              <pre className="max-h-[72vh] overflow-auto rounded-xl border border-stone-200 bg-stone-50 p-4 text-xs leading-6 text-stone-700">
                {JSON.stringify(detailLog?.detail || {}, null, 2)}
              </pre>
            </div>
          </div>
        </DialogContent>
      </Dialog>
      <ImageLightbox
        images={detailImages}
        currentIndex={lightboxIndex}
        open={lightboxOpen}
        onOpenChange={setLightboxOpen}
        onIndexChange={setLightboxIndex}
      />
      <Dialog open={deletingItems.length > 0} onOpenChange={(open) => (!open ? setDeletingItems([]) : null)}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>{deletingItems.length === 1 ? "删除日志" : "删除所选日志"}</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              确认删除 {deletingItems.length} 条日志吗？删除后无法恢复。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" className="rounded-xl" onClick={() => setDeletingItems([])} disabled={isDeleting}>
              取消
            </Button>
            <Button className="rounded-xl bg-rose-600 text-white hover:bg-rose-700" onClick={() => void confirmDelete()} disabled={isDeleting || deletingItems.length === 0}>
              {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : null}
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
