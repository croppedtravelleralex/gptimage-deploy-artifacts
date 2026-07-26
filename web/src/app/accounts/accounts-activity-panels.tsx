"use client";

import { useEffect, useMemo, useState } from "react";
import { LoaderCircle } from "lucide-react";

import { DateRangeControls, InteractiveLineChart } from "@/components/charts/InteractiveLineChart";
import { Card, CardContent } from "@/components/ui/card";
import { fetchAccountActivityDaily, type AccountActivityDailyResponse } from "@/lib/api";

type AccountsActivityPanelsProps = {
  refreshToken?: number;
};

export default function AccountsActivityPanels({ refreshToken = 0 }: AccountsActivityPanelsProps) {
  const [accountActivity, setAccountActivity] = useState<AccountActivityDailyResponse | null>(null);
  const [activityFrom, setActivityFrom] = useState("");
  const [activityTo, setActivityTo] = useState("");
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setIsLoading(true);
    void fetchAccountActivityDaily(30)
      .then((data) => {
        if (!active) return;
        setAccountActivity(data);
        const items = data.items || [];
        if (items.length) {
          setActivityFrom(items[Math.max(0, items.length - 14)].date);
          setActivityTo(items[items.length - 1].date);
        }
      })
      .catch(() => {
        if (active) setAccountActivity(null);
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [refreshToken]);

  const activityChart = useMemo(() => {
    const all = accountActivity?.items ?? [];
    const from = activityFrom || (all[0]?.date ?? "");
    const to = activityTo || (all[all.length - 1]?.date ?? "");
    const items = all.filter((item) => (!from || item.date >= from) && (!to || item.date <= to));
    return {
      items,
      from: all[0]?.date ?? "",
      to: all[all.length - 1]?.date ?? "",
      rangeFrom: from,
      rangeTo: to,
      syncLabel: accountActivity?.sync_label ?? "上传",
      series: [
        { key: "registered", label: "注册/入库", color: "#10b981", values: items.map((i) => i.registered) },
        { key: "uploaded", label: "上传", color: "#3b82f6", values: items.map((i) => i.uploaded) },
        { key: "received", label: "接收", color: "#0ea5e9", values: items.map((i) => i.received) },
        { key: "deleted", label: "删除", color: "#f43f5e", values: items.map((i) => i.deleted) },
        { key: "images_api", label: "api生图", color: "#a855f7", values: items.map((i) => Number(i.images_api || 0)) },
        { key: "images_chat", label: "对话生图", color: "#c026d3", values: items.map((i) => Number(i.images_chat || 0)) },
        {
          key: "dialogues_nurture",
          label: "拟人对话",
          color: "#f59e0b",
          values: items.map((i) => Number(i.dialogues_nurture || 0)),
        },
        {
          key: "dialogues_real",
          label: "真实对话",
          color: "#0284c7",
          values: items.map((i) => Number(i.dialogues_real || 0)),
        },
      ],
    };
  }, [accountActivity, activityFrom, activityTo]);

  if (isLoading && !accountActivity) {
    return (
      <div className="flex min-h-[200px] items-center justify-center rounded-2xl border border-white/80 bg-white/90 text-sm text-stone-500">
        <LoaderCircle className="mr-2 size-4 animate-spin" />
        加载账号流水…
      </div>
    );
  }

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="text-sm font-medium text-stone-700">账号流水 · 注册/入库/接收/删除</div>
            <DateRangeControls
              from={activityChart.rangeFrom}
              to={activityChart.rangeTo}
              min={activityChart.from}
              max={activityChart.to}
              onChange={(from, to) => {
                setActivityFrom(from);
                setActivityTo(to);
              }}
            />
          </div>
          <InteractiveLineChart
            dates={activityChart.items.map((i) => i.date)}
            series={activityChart.series.filter((s) =>
              ["registered", "uploaded", "received", "deleted"].includes(s.key),
            )}
            yLabel="数量"
            xLabel="日期"
            sharedScale
          />
          <div className="mt-2 text-xs text-stone-500">
            注册/入库 · 上传 · 接收 · 删除（窗口 {activityChart.items.length} 天
            {activityChart.syncLabel ? ` · 本机角色偏「${activityChart.syncLabel}」` : ""}）
          </div>
        </CardContent>
      </Card>
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="text-sm font-medium text-stone-700">账号流水 · 生图对话</div>
            <DateRangeControls
              from={activityChart.rangeFrom}
              to={activityChart.rangeTo}
              min={activityChart.from}
              max={activityChart.to}
              onChange={(from, to) => {
                setActivityFrom(from);
                setActivityTo(to);
              }}
            />
          </div>
          <InteractiveLineChart
            dates={activityChart.items.map((i) => i.date)}
            series={activityChart.series.filter((s) =>
              ["images_api", "images_chat", "dialogues_nurture", "dialogues_real"].includes(s.key),
            )}
            yLabel="数量"
            xLabel="日期"
            sharedScale
          />
          <div className="mt-2 text-xs text-stone-500">
            api生图 · 对话生图 · 拟人对话 · 真实对话（窗口 {activityChart.items.length} 天）
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
