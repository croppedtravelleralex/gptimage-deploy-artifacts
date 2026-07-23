"use client";

import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, CircleAlert, LoaderCircle, ListTodo, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { ImageConversation, StoredImage } from "@/store/image-conversations";

export type ImageTaskPanelItem = {
  conversationId: string;
  conversationTitle: string;
  turnId: string;
  imageId: string;
  taskId: string;
  prompt: string;
  slotLabel: string;
  status: "queued" | "running" | "success" | "error";
  startTime: number;
  endTime?: number;
  error?: string;
};

function wallElapsedSecs(item: ImageTaskPanelItem, now: number): number {
  const end = item.endTime && item.status !== "queued" && item.status !== "running" ? item.endTime : now;
  return Math.max(0, (end - item.startTime) / 1000);
}

function collectPanelItems(conversations: ImageConversation[]): ImageTaskPanelItem[] {
  const items: ImageTaskPanelItem[] = [];
  for (const conversation of conversations) {
    for (const turn of conversation.turns) {
      if (turn.resultsDeleted) continue;
      turn.images.forEach((image: StoredImage, index) => {
        if (image.hiddenFromTaskPanel) return;
        const taskId = image.taskId || image.id;
        if (!taskId) return;
        const startTime = image.startTime || new Date(turn.createdAt).getTime() || Date.now();
        const slotLabel = `${index + 1}/${turn.images.length}`;
        const prompt = (turn.prompt || "").slice(0, 40) || `图 ${slotLabel}`;
        if (image.status === "loading") {
          items.push({
            conversationId: conversation.id,
            conversationTitle: conversation.title || "未命名",
            turnId: turn.id,
            imageId: image.id,
            taskId,
            prompt,
            slotLabel,
            status: image.taskStatus === "queued" ? "queued" : "running",
            startTime,
          });
          return;
        }
        if (image.status === "success" || image.status === "error") {
          const endTime =
            typeof image.durationMs === "number" && image.durationMs > 0
              ? startTime + image.durationMs
              : image.elapsedUpdatedAt || Date.now();
          items.push({
            conversationId: conversation.id,
            conversationTitle: conversation.title || "未命名",
            turnId: turn.id,
            imageId: image.id,
            taskId,
            prompt,
            slotLabel,
            status: image.status,
            startTime,
            endTime,
            error: image.error,
          });
        }
      });
    }
  }
  return items;
}

function formatSecs(seconds: number) {
  return `${seconds.toFixed(1)}s`;
}

function Row({
  item,
  now,
  toneClass,
  onSelect,
  onRemove,
  removing,
}: {
  item: ImageTaskPanelItem;
  now: number;
  toneClass?: string;
  onSelect: (conversationId: string) => void;
  onRemove?: (item: ImageTaskPanelItem) => void;
  removing?: boolean;
}) {
  const elapsed = wallElapsedSecs(item, now);
  return (
    <div className={`rounded px-1.5 py-0.5 ${toneClass || "border border-stone-100 bg-white/90"}`}>
      <div className="flex items-start gap-1">
        <button type="button" className="min-w-0 flex-1 text-left" onClick={() => onSelect(item.conversationId)}>
          <div className="flex items-center justify-between gap-1">
            <span className="truncate text-[10px] font-medium leading-4 text-stone-700">
              #{item.slotLabel} · {item.conversationTitle}
            </span>
            <span className="shrink-0 tabular-nums text-[10px] leading-4 text-stone-500">{formatSecs(elapsed)}</span>
          </div>
          <div className="line-clamp-1 text-[9px] leading-3 text-stone-400">{item.prompt}</div>
          {item.status === "error" && item.error ? (
            <div className="line-clamp-1 text-[9px] leading-3 text-rose-500">{item.error}</div>
          ) : null}
        </button>
        {onRemove ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="mt-0.5 h-4 w-4 shrink-0 rounded p-0 text-stone-400 hover:bg-rose-50 hover:text-rose-600"
            disabled={removing}
            onClick={() => onRemove(item)}
            title={
              item.status === "running"
                ? "取消生成并移除显示"
                : item.status === "queued"
                  ? "删除排队请求"
                  : "仅从列表移除显示"
            }
          >
            {removing ? <LoaderCircle className="size-3 animate-spin" /> : <X className="size-3" />}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function Section({
  title,
  count,
  children,
  tone = "stone",
}: {
  title: string;
  count: number;
  children: React.ReactNode;
  tone?: "stone" | "sky" | "amber" | "emerald";
}) {
  const toneClass =
    tone === "sky"
      ? "text-sky-700"
      : tone === "amber"
        ? "text-amber-700"
        : tone === "emerald"
          ? "text-emerald-700"
          : "text-stone-600";
  return (
    <section className="flex min-h-0 flex-col gap-1 border-b border-stone-100 pb-2 last:border-b-0">
      <div className={`flex items-center justify-between px-0.5 text-[10px] font-semibold ${toneClass}`}>
        <span>{title}</span>
        <span className="tabular-nums text-stone-400">{count}</span>
      </div>
      <div className="min-h-0 max-h-[28vh] space-y-0.5 overflow-y-auto pr-0.5">{children}</div>
    </section>
  );
}

export function ImageTaskPanel({
  conversations,
  concurrencyLimit = 4,
  onSelectConversation,
  onRemoveItem,
  removingIds,
  className = "",
}: {
  conversations: ImageConversation[];
  concurrencyLimit?: number;
  onSelectConversation: (conversationId: string) => void;
  onRemoveItem: (item: ImageTaskPanelItem) => void;
  removingIds?: Set<string>;
  className?: string;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, []);

  const items = useMemo(() => collectPanelItems(conversations), [conversations]);
  const running = items
    .filter((item) => item.status === "running")
    .sort((a, b) => b.startTime - a.startTime);
  const queued = items
    .filter((item) => item.status === "queued")
    .sort((a, b) => a.startTime - b.startTime);
  const done = items
    .filter((item) => item.status === "success" || item.status === "error")
    .sort((a, b) => (b.endTime || 0) - (a.endTime || 0))
    .slice(0, 40);

  const conversationTone = useMemo(() => {
    const order: string[] = [];
    for (const item of done) {
      if (!order.includes(item.conversationId)) order.push(item.conversationId);
    }
    const map = new Map<string, string>();
    order.forEach((id, index) => {
      map.set(id, index % 2 === 0 ? "border border-stone-200/80 bg-stone-200/80" : "border border-stone-100 bg-stone-50");
    });
    return map;
  }, [done]);

  return (
    <aside
      className={`flex min-h-0 flex-col rounded-2xl border border-stone-200/80 bg-stone-50/90 shadow-sm ${className}`}
    >
      <div className="flex items-center justify-between gap-1 border-b border-stone-100 px-2 py-1.5">
        <div className="flex items-center gap-1 text-[11px] font-semibold text-stone-800">
          <ListTodo className="size-3.5 text-stone-500" />
          生图任务
        </div>
        <div className="text-[10px] text-stone-400">并发≤{concurrencyLimit}</div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-hidden p-1.5">
        <Section title={`正在生图（≤${concurrencyLimit}）`} count={running.length} tone="sky">
          {running.length === 0 ? (
            <div className="px-1 py-3 text-center text-[10px] text-stone-400">暂无</div>
          ) : (
            running.map((item) => (
              <Row
                key={`run:${item.taskId}`}
                item={item}
                now={now}
                onSelect={onSelectConversation}
                onRemove={onRemoveItem}
                removing={removingIds?.has(item.taskId)}
              />
            ))
          )}
        </Section>

        <Section title="排队中" count={queued.length} tone="amber">
          {queued.length === 0 ? (
            <div className="px-1 py-3 text-center text-[10px] text-stone-400">暂无</div>
          ) : (
            queued.map((item) => (
              <Row
                key={`q:${item.taskId}`}
                item={item}
                now={now}
                onSelect={onSelectConversation}
                onRemove={onRemoveItem}
                removing={removingIds?.has(item.taskId)}
              />
            ))
          )}
        </Section>

        <Section title="已完成" count={done.length} tone="emerald">
          {done.length === 0 ? (
            <div className="px-1 py-3 text-center text-[10px] text-stone-400">暂无</div>
          ) : (
            done.map((item) => (
              <div key={`done:${item.taskId}`} className="relative">
                <div className="absolute left-1 top-1 z-10">
                  {item.status === "success" ? (
                    <CheckCircle2 className="size-3 text-emerald-500" />
                  ) : (
                    <CircleAlert className="size-3 text-rose-500" />
                  )}
                </div>
                <div className="pl-4">
                  <Row
                    item={item}
                    now={now}
                    toneClass={conversationTone.get(item.conversationId)}
                    onSelect={onSelectConversation}
                    onRemove={onRemoveItem}
                    removing={removingIds?.has(item.taskId)}
                  />
                </div>
              </div>
            ))
          )}
        </Section>
      </div>
    </aside>
  );
}
