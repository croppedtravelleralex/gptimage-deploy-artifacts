"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, CircleAlert, LoaderCircle } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { fetchSettingsConfig } from "@/lib/api";
import { httpRequest } from "@/lib/request";
import { cn } from "@/lib/utils";

type ProbeState = {
  api: "checking" | "ok" | "auth" | "fail";
  search: "checking" | "ok" | "auth" | "fail" | "skipped";
  apiDetail: string;
  searchDetail: string;
};

/** 右上角 Skill / 接口状态指示灯 */
export function SkillStatusLight({ className }: { className?: string }) {
  const [state, setState] = useState<ProbeState>({
    api: "checking",
    search: "checking",
    apiDetail: "",
    searchDetail: "",
  });

  useEffect(() => {
    void (async () => {
      try {
        await fetchSettingsConfig();
      } catch {
        /* ignore */
      }
      try {
        await httpRequest("/v1/models");
        setState((s) => ({ ...s, api: "ok", apiDetail: "/v1/models 可用" }));
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        const auth = /401|403|鉴权|unauthorized/i.test(msg);
        setState((s) => ({
          ...s,
          api: auth ? "auth" : "fail",
          apiDetail: msg.slice(0, 160),
          search: "skipped",
          searchDetail: "跳过",
        }));
        return;
      }
      try {
        await httpRequest("/v1/search", { method: "POST", body: { prompt: "ping" } });
        setState((s) => ({ ...s, search: "ok", searchDetail: "搜索可用" }));
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        const auth = /401|403|鉴权|unauthorized/i.test(msg);
        setState((s) => ({
          ...s,
          search: auth ? "auth" : "fail",
          searchDetail: msg.slice(0, 160),
        }));
      }
    })();
  }, []);

  const worst =
    state.api === "checking" || state.search === "checking"
      ? "checking"
      : state.api === "fail" || state.search === "fail"
        ? "fail"
        : state.api === "auth" || state.search === "auth"
          ? "auth"
          : "ok";

  const color =
    worst === "ok"
      ? "bg-emerald-500"
      : worst === "checking"
        ? "bg-stone-300"
        : worst === "auth"
          ? "bg-amber-400"
          : "bg-rose-500";

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border border-stone-200 bg-white px-2.5 py-1 text-xs text-stone-600",
            className,
          )}
          title="Skill / 接口状态"
        >
          <span className={cn("size-2 rounded-full", color)} />
          Skill
          {worst === "checking" ? <LoaderCircle className="size-3 animate-spin text-stone-400" /> : null}
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-72 space-y-2 text-sm" align="end">
        <div className="font-medium text-stone-800">接口探测</div>
        <div className="flex items-start gap-2">
          {state.api === "ok" ? (
            <CheckCircle2 className="mt-0.5 size-4 text-emerald-600" />
          ) : (
            <CircleAlert className="mt-0.5 size-4 text-amber-600" />
          )}
          <div>
            <div>API：{state.api}</div>
            <div className="text-xs text-stone-500">{state.apiDetail}</div>
          </div>
        </div>
        <div className="flex items-start gap-2">
          {state.search === "ok" ? (
            <CheckCircle2 className="mt-0.5 size-4 text-emerald-600" />
          ) : (
            <CircleAlert className="mt-0.5 size-4 text-amber-600" />
          )}
          <div>
            <div>搜索：{state.search}</div>
            <div className="text-xs text-stone-500">{state.searchDetail}</div>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** @deprecated 保留旧全页面板导出名，避免其它引用断裂 */
export { SkillStatusLight as SkillStatusPanel };
