"use client";

import { useEffect, useState } from "react";
import { ExternalLink, Globe2, LoaderCircle, Search } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { httpRequest } from "@/lib/request";
import { cn } from "@/lib/utils";

import { loadSearchSnapshot, saveSearchSnapshot, type SearchSnapshot } from "./session-store";

type SearchResult = {
  answer?: string;
  content?: string;
  sources?: Array<{ title?: string; url?: string; snippet?: string }>;
};

const normalizeMarkdown = (text: string) =>
  text
    .replace(/\ue200url\ue202([^\ue202\ue201]*)\ue202([^\ue201]*)\ue201/g, "[$1]($2)")
    .replace(/\ue200cite\ue202[^\ue201]*\ue201/g, "")
    .replace(/\ue200[^\ue201]*\ue201/g, "")
    .replace(/\ue200[^\ue201]*$/g, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

function MarkdownResult({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ className, ...props }) => (
          <a className={cn("font-medium text-blue-700 underline underline-offset-4", className)} target="_blank" rel="noreferrer" {...props} />
        ),
        p: ({ className, ...props }) => <p className={cn("my-4 leading-8 text-stone-800", className)} {...props} />,
        ul: ({ className, ...props }) => <ul className={cn("my-4 list-disc space-y-2 pl-6", className)} {...props} />,
        ol: ({ className, ...props }) => <ol className={cn("my-4 list-decimal space-y-2 pl-6", className)} {...props} />,
        code: ({ className, ...props }) => <code className={cn("rounded bg-stone-100 px-1.5 py-0.5 font-mono text-[0.9em]", className)} {...props} />,
        pre: ({ className, ...props }) => <pre className={cn("my-5 overflow-x-auto rounded-xl bg-stone-950 p-4 text-sm text-stone-50", className)} {...props} />,
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

export function PersistentSearchPanel() {
  const [prompt, setPrompt] = useState("帮我搜索 chatgpt2api 相关项目");
  const [snap, setSnap] = useState<SearchSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [startedAt, setStartedAt] = useState(0);

  useEffect(() => {
    const saved = loadSearchSnapshot();
    if (saved) {
      setSnap(saved);
      setPrompt(saved.prompt || "");
    }
  }, []);

  useEffect(() => {
    if (!loading || !startedAt) return;
    const timer = window.setInterval(() => setElapsedMs(Date.now() - startedAt), 100);
    return () => window.clearInterval(timer);
  }, [loading, startedAt]);

  const runSearch = async () => {
    const value = prompt.trim();
    if (!value || loading) return;
    const start = Date.now();
    setStartedAt(start);
    setElapsedMs(0);
    setLoading(true);
    try {
      const result = await httpRequest<SearchResult>("/v1/search", { method: "POST", body: { prompt: value } });
      const next: SearchSnapshot = {
        prompt: value,
        answer: normalizeMarkdown(String(result.answer || result.content || "")),
        sources: result.sources || [],
        elapsedMs: Date.now() - start,
        updatedAt: Date.now(),
      };
      setSnap(next);
      saveSearchSnapshot(next);
    } catch (err) {
      const next: SearchSnapshot = {
        prompt: value,
        error: err instanceof Error ? err.message : String(err),
        elapsedMs: Date.now() - start,
        updatedAt: Date.now(),
      };
      setSnap(next);
      saveSearchSnapshot(next);
    } finally {
      setElapsedMs(Date.now() - start);
      setLoading(false);
    }
  };

  const searched = loading || !!snap;

  return (
    <section className={cn("mx-auto flex min-h-[calc(100vh-180px)] w-full max-w-6xl flex-col", searched ? "py-2" : "justify-center")}>
      <div className={cn("mx-auto w-full max-w-3xl", searched && "sticky top-3 z-10")}>
        {!searched ? <p className="mb-5 text-center text-sm text-stone-500">网页搜索结果会保存在本机，切换页签不会清空</p> : null}
        <form
          className={cn(
            "mx-auto flex w-full items-center gap-3 rounded-full border border-stone-200 bg-white/95 backdrop-blur",
            searched ? "px-4 py-2" : "px-5 py-3",
          )}
          onSubmit={(e) => {
            e.preventDefault();
            void runSearch();
          }}
        >
          <Search className="size-4 shrink-0 text-stone-400" />
          <input
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="搜索网页"
            className={cn("min-w-0 flex-1 bg-transparent text-[15px] outline-none", searched ? "h-8" : "h-10")}
          />
          <button type="submit" disabled={loading || !prompt.trim()} className="inline-flex size-8 items-center justify-center rounded-full hover:bg-stone-100 disabled:opacity-40">
            {loading ? <LoaderCircle className="size-4 animate-spin" /> : <Globe2 className="size-4" />}
          </button>
        </form>
        {searched ? (
          <div className="mt-2 flex justify-between text-xs text-stone-400">
            <span>{loading ? `搜索中 ${(elapsedMs / 1000).toFixed(1)}s` : snap?.elapsedMs != null ? `耗时 ${(snap.elapsedMs / 1000).toFixed(1)}s` : ""}</span>
            <button
              type="button"
              className="underline"
              onClick={() => {
                setSnap(null);
                saveSearchSnapshot(null);
              }}
            >
              清除结果
            </button>
          </div>
        ) : null}
      </div>

      {snap?.error ? <div className="mx-auto mt-6 w-full max-w-3xl rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{snap.error}</div> : null}
      {snap?.answer ? (
        <div className="mx-auto mt-6 w-full max-w-3xl rounded-2xl border border-stone-100 bg-white/90 p-5 shadow-sm">
          <MarkdownResult content={snap.answer} />
          {snap.sources?.length ? (
            <div className="mt-6 space-y-2 border-t border-stone-100 pt-4">
              <div className="text-sm font-medium text-stone-700">来源</div>
              {snap.sources.map((s, i) => (
                <a key={`${s.url}-${i}`} href={s.url} target="_blank" rel="noreferrer" className="flex items-start gap-2 text-sm text-sky-700 hover:underline">
                  <ExternalLink className="mt-0.5 size-3.5 shrink-0" />
                  <span>
                    <span className="font-medium">{s.title || s.url}</span>
                    {s.snippet ? <span className="mt-0.5 block text-xs text-stone-500">{s.snippet}</span> : null}
                  </span>
                </a>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
