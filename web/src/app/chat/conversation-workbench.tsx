"use client";

import {
  Children,
  isValidElement,
  memo,
  startTransition,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { flushSync } from "react-dom";
import {
  Archive,
  ChevronDown,
  ChevronUp,
  Copy,
  Download,
  ExternalLink,
  FolderOpen,
  Globe2,
  LoaderCircle,
  Paperclip,
  Pencil,
  Pin,
  Plus,
  RotateCcw,
  Send,
  Trash2,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { fetchAccounts, fetchModels, type Account } from "@/lib/api";
import {
  absolutizeHref,
  humanizeUpstreamError,
  normalizeChatMarkdown,
  stripLeakedToolCalls,
} from "@/lib/chat-format";
import { deltaContentFromChatChunk, fetchSseJson, sourcesFromChatChunk } from "@/lib/sse";
import { cn } from "@/lib/utils";

import { CodeBlock } from "./code-block";
import {
  exportSessionMarkdown,
  exportSessionText,
  useChatSessions,
  type ChatMessage,
  type ChatSession,
} from "./session-store";

type SearchSource = { title?: string; url?: string; snippet?: string };

type PendingFile = {
  id: string;
  name: string;
  mime: string;
  kind: "image" | "text";
  dataUrl?: string;
  text?: string;
};

const MAX_CONCURRENT_SSE = 4;
const FALLBACK_MODELS = ["auto", "gpt-5-5", "gpt-5", "gpt-4o", "o4-mini"];
const IMAGE_CHAT_MODEL = "gpt-image-2";

function isImageModelId(id: string): boolean {
  const value = String(id || "").trim().toLowerCase();
  return value.includes("image") || value.startsWith("codex-gpt-image");
}

function looksLikeImagePrompt(text: string): boolean {
  const prompt = text.trim();
  if (!prompt) return false;
  return /生成图片|生图|画一[张幅个]|帮我画|画个|画张|生成一[张幅].{0,12}图|make (an? |me )?(image|picture|photo)|generate (an? )?(image|picture)|draw (an? |me )?/i.test(
    prompt,
  );
}

function linkText(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(linkText).join("");
  return "";
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    /* ignore */
  }
}

function SourcesCollapsible({ sources }: { sources: SearchSource[] }) {
  if (!sources.length) return null;
  return (
    <details className="mt-3 border-t border-border pt-2">
      <summary className="cursor-pointer select-none text-sm font-medium text-foreground">
        来源（{sources.length}）
      </summary>
      <div className="mt-2 space-y-2">
        {sources.map((s, i) => {
          const href = absolutizeHref(s.url, s.title);
          return (
            <a
              key={`${s.url}-${i}`}
              href={href || undefined}
              target="_blank"
              rel="noopener noreferrer"
              className="relative z-10 flex items-start gap-2 text-sm text-sky-700 hover:underline"
              onClick={(e) => {
                if (!href || !/^https?:\/\//i.test(href)) return;
                e.preventDefault();
                e.stopPropagation();
                window.open(href, "_blank", "noopener,noreferrer");
              }}
            >
              <ExternalLink className="mt-0.5 size-3.5 shrink-0" />
              <span>
                <span className="font-medium">{s.title || s.url}</span>
                {s.snippet ? <span className="mt-0.5 block text-xs text-muted-foreground">{s.snippet}</span> : null}
              </span>
            </a>
          );
        })}
      </div>
    </details>
  );
}

const MarkdownBubble = memo(function MarkdownBubble({
  content,
  role,
  isStreaming = false,
}: {
  content: string;
  role: string;
  isStreaming?: boolean;
}) {
  if (role === "user") {
    return <div className="whitespace-pre-wrap text-sm leading-relaxed text-foreground">{content}</div>;
  }
  const cleaned = normalizeChatMarkdown(content);
  return (
    <div className="chat-prose text-sm leading-relaxed text-foreground">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ className, href, children, ...props }) => {
            const label = linkText(children);
            const url = absolutizeHref(href, label);
            return (
              <a
                {...props}
                href={url || undefined}
                target="_blank"
                rel="noopener noreferrer"
                className={cn(
                  "relative z-10 font-medium text-sky-700 underline underline-offset-4 hover:text-sky-800",
                  className,
                )}
                onClick={(e) => {
                  if (!url || !/^https?:\/\//i.test(url)) return;
                  e.preventDefault();
                  e.stopPropagation();
                  window.open(url, "_blank", "noopener,noreferrer");
                }}
              >
                {children}
              </a>
            );
          },
          h1: ({ className, ...props }) => (
            <h1 className={cn("mb-3 mt-4 scroll-m-20 text-xl font-semibold tracking-tight", className)} {...props} />
          ),
          h2: ({ className, ...props }) => (
            <h2 className={cn("mb-2 mt-4 scroll-m-20 text-lg font-semibold tracking-tight", className)} {...props} />
          ),
          h3: ({ className, ...props }) => (
            <h3 className={cn("mb-2 mt-3 scroll-m-20 text-base font-semibold tracking-tight", className)} {...props} />
          ),
          p: ({ className, ...props }) => (
            <p className={cn("my-2.5 leading-relaxed [&:not(:first-child)]:mt-3", className)} {...props} />
          ),
          ul: ({ className, ...props }) => <ul className={cn("my-3 ml-5 list-disc space-y-1.5", className)} {...props} />,
          ol: ({ className, ...props }) => <ol className={cn("my-3 ml-5 list-decimal space-y-1.5", className)} {...props} />,
          li: ({ className, ...props }) => <li className={cn("leading-relaxed", className)} {...props} />,
          blockquote: ({ className, ...props }) => (
            <blockquote
              className={cn("my-3 border-l-2 border-border pl-4 italic text-muted-foreground", className)}
              {...props}
            />
          ),
          hr: ({ className, ...props }) => <hr className={cn("my-4 border-border", className)} {...props} />,
          code: ({ className, children, ...props }) => {
            const isBlock = Boolean(className?.includes("language-"));
            if (isBlock) {
              return (
                <code className={cn("font-mono text-[13px] leading-relaxed text-stone-800", className)} {...props}>
                  {children}
                </code>
              );
            }
            return (
              <code
                className={cn(
                  "relative rounded bg-stone-100 px-[0.35rem] py-[0.15rem] font-mono text-[0.85em] text-stone-800",
                  className,
                )}
                {...props}
              >
                {children}
              </code>
            );
          },
          pre: ({ children }) => {
            let language: string | undefined;
            let code = "";
            Children.forEach(children, (child) => {
              if (!isValidElement(child)) return;
              const props = child.props as { className?: string; children?: ReactNode };
              const cls = String(props.className || "");
              const m = /language-([\w-]+)/.exec(cls);
              if (m) language = m[1];
              const raw = props.children;
              code = String(Array.isArray(raw) ? raw.join("") : (raw ?? "")).replace(/\n$/, "");
            });
            return <CodeBlock code={code} language={language} highlight={!isStreaming} />;
          },
          table: ({ className, ...props }) => (
            <div className="my-3 w-full overflow-x-auto rounded-lg border border-border bg-stone-50/80">
              <table className={cn("w-full caption-bottom text-sm", className)} {...props} />
            </div>
          ),
          thead: ({ className, ...props }) => <thead className={cn("bg-stone-100 [&_tr]:border-b", className)} {...props} />,
          tbody: ({ className, ...props }) => <tbody className={cn("[&_tr:last-child]:border-0", className)} {...props} />,
          tr: ({ className, ...props }) => (
            <tr className={cn("border-b border-border transition-colors hover:bg-muted/40", className)} {...props} />
          ),
          th: ({ className, ...props }) => (
            <th className={cn("h-9 px-3 text-left align-middle text-xs font-medium text-muted-foreground", className)} {...props} />
          ),
          td: ({ className, ...props }) => <td className={cn("p-3 align-middle text-sm", className)} {...props} />,
          img: ({ className, alt, src, ...props }) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              {...props}
              src={src}
              alt={alt || "generated"}
              className={cn(
                "my-2 max-h-[420px] max-w-full rounded-xl border border-border object-contain shadow-sm",
                className,
              )}
            />
          ),
        }}
      >
        {cleaned}
      </ReactMarkdown>
    </div>
  );
});

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

function readFileAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsText(file);
  });
}

function downloadBlob(filename: string, text: string, mime: string) {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function exportSingleMessage(m: ChatMessage, title: string) {
  const who = m.role === "user" ? "你" : m.kind === "search" ? "搜索" : "助手";
  const base = (title || "chat").replace(/[\\/:*?"<>|]+/g, "_").slice(0, 40);
  const md = [`# ${who}`, "", m.content || "", ""].join("\n");
  const txt = [`[${who}]`, m.content || "", ""].join("\n");
  downloadBlob(`${base}-${who}.md`, md, "text/markdown;charset=utf-8");
  downloadBlob(`${base}-${who}.txt`, txt, "text/plain;charset=utf-8");
}

function sessionSubtitle(s: ChatSession, accounts: Account[]): string {
  const email = String(s.accountEmail || s.preferredEmail || "").trim().toLowerCase();
  if (!email) return "未绑定账号";
  const acc = accounts.find((a) => String(a.email || "").trim().toLowerCase() === email);
  if (!acc) return "已绑定";
  const quota = typeof acc.quota === "number" ? acc.quota : "-";
  return `额度 ${quota}`;
}

export function ConversationWorkbench() {
  const { store, active, setActive, createSession, deleteSession, updateActive, updateSession, togglePin } =
    useChatSessions();
  const [input, setInput] = useState("");
  const [loadingBySessionId, setLoadingBySessionId] = useState<Record<string, boolean>>({});
  const [error, setError] = useState("");
  const [webSearch, setWebSearch] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [modelOptions, setModelOptions] = useState<string[]>(FALLBACK_MODELS);
  const [preferredEmail, setPreferredEmail] = useState("");
  const [elapsedMs, setElapsedMs] = useState(0);
  const [streamPhase, setStreamPhase] = useState<"idle" | "waiting" | "streaming" | "done">("idle");
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [showEmailDetail, setShowEmailDetail] = useState(false);
  const [accountSwitchNote, setAccountSwitchNote] = useState("");
  const [turnCursor, setTurnCursor] = useState(0);
  const startedAtRef = useRef(0);
  const abortBySessionRef = useRef<Record<string, AbortController>>({});
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const messagesScrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const activeLoading = Boolean(active?.id && loadingBySessionId[active.id]);
  const inflightCount = useMemo(
    () => Object.values(loadingBySessionId).filter(Boolean).length,
    [loadingBySessionId],
  );

  const turnIndices = useMemo(() => {
    const msgs = active?.messages || [];
    const idxs: number[] = [];
    msgs.forEach((m, i) => {
      if (m.role === "user") idxs.push(i);
    });
    return idxs;
  }, [active?.messages]);

  useEffect(() => {
    setTurnCursor(Math.max(0, turnIndices.length - 1));
  }, [active?.id, turnIndices.length]);

  useEffect(() => {
    void fetchAccounts({ offset: 0, limit: 100 })
      .then((res) => setAccounts(res.items || []))
      .catch(() => setAccounts([]));
    void fetchModels()
      .then((res) => {
        const ids = (res.data || [])
          .map((m) => String(m.id || "").trim())
          .filter((id) => id && !isImageModelId(id));
        const merged = Array.from(new Set(["auto", ...ids, ...FALLBACK_MODELS])).filter(
          (id) => !isImageModelId(id),
        );
        setModelOptions(merged);
      })
      .catch(() => setModelOptions(FALLBACK_MODELS));
  }, []);

  useEffect(() => {
    const model = String(active?.model || "").trim();
    if (model && isImageModelId(model)) {
      updateActive({ model: "auto" });
    }
  }, [active?.id, active?.model, updateActive]);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "0px";
    const styles = window.getComputedStyle(el);
    const lineHeight = Number.parseFloat(styles.lineHeight) || 20;
    const pad =
      (Number.parseFloat(styles.paddingTop) || 0) + (Number.parseFloat(styles.paddingBottom) || 0);
    const maxHeight = Math.round(lineHeight * 6 + pad);
    const next = Math.min(Math.max(el.scrollHeight, Math.round(lineHeight + pad)), maxHeight);
    el.style.height = `${next}px`;
  }, [input]);

  useEffect(() => {
    // Sticky: locked accountEmail wins over manual prefer until user clears binding.
    const locked = String(active?.accountEmail || "").trim();
    setPreferredEmail(locked || active?.preferredEmail || "");
    setShowEmailDetail(false);
    setAccountSwitchNote("");
  }, [active?.id, active?.accountEmail, active?.preferredEmail]);

  useEffect(() => {
    if (!activeLoading || !startedAtRef.current) return;
    const timer = window.setInterval(() => setElapsedMs(Date.now() - startedAtRef.current), 100);
    return () => window.clearInterval(timer);
  }, [activeLoading]);

  const accountOptions = useMemo(() => {
    return accounts
      .filter((a) => a.status !== "禁用" && a.status !== "异常" && String(a.email || "").trim())
      .map((a) => String(a.email || "").trim());
  }, [accounts]);

  const boundAccount = useMemo(() => {
    const email = String(active?.accountEmail || preferredEmail || "")
      .trim()
      .toLowerCase();
    if (!email) return null;
    return accounts.find((a) => String(a.email || "").trim().toLowerCase() === email) || null;
  }, [accounts, active?.accountEmail, preferredEmail]);

  const visibleSessions = useMemo(() => {
    const list = store.sessions.filter((s) => (showArchived ? s.archived : !s.archived));
    return [...list].sort((a, b) => {
      const pinDiff = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
      if (pinDiff !== 0) return pinDiff;
      return Number(b.updatedAt || 0) - Number(a.updatedAt || 0);
    });
  }, [store.sessions, showArchived]);

  const preferredHeaders = useCallback(() => {
    const locked = String(active?.accountEmail || "").trim();
    const email = (locked || preferredEmail).trim();
    return email ? { "X-Preferred-Account-Email": email } : undefined;
  }, [active?.accountEmail, preferredEmail]);

  const switchAccountNote = useMemo(() => {
    if (accountSwitchNote) return accountSwitchNote;
    const bound = String(active?.accountEmail || "").trim();
    const prefer = preferredEmail.trim();
    if (prefer && bound && prefer.toLowerCase() !== bound.toLowerCase()) {
      return `换号续聊：将把当前历史发给 ${prefer}（新开上游对话，非同一 conversation_id）`;
    }
    return "";
  }, [active?.accountEmail, preferredEmail, accountSwitchNote]);

  const scrollToTurn = (turnIdx: number) => {
    if (turnIdx < 0 || turnIdx >= turnIndices.length) return;
    setTurnCursor(turnIdx);
    const el = document.getElementById(`turn-${turnIdx}`);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const addFiles = async (files: FileList | File[]) => {
    const list = Array.from(files);
    const next: PendingFile[] = [];
    for (const file of list) {
      const mime = file.type || "application/octet-stream";
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      if (mime.startsWith("image/")) {
        if (file.size > 8 * 1024 * 1024) {
          setError(`图片过大：${file.name}（上限 8MB）`);
          continue;
        }
        const dataUrl = await readFileAsDataUrl(file);
        next.push({ id, name: file.name, mime, kind: "image", dataUrl });
      } else if (
        mime.startsWith("text/") ||
        /\.(txt|md|csv|json|log|py|ts|tsx|js|jsx|html|css)$/i.test(file.name)
      ) {
        if (file.size > 512 * 1024) {
          setError(`文本文件过大：${file.name}（上限 512KB）`);
          continue;
        }
        const text = await readFileAsText(file);
        next.push({ id, name: file.name, mime: mime || "text/plain", kind: "text", text });
      } else {
        setError(`暂不支持该类型：${file.name}（请上传图片或文本）`);
      }
    }
    if (next.length) setPendingFiles((prev) => [...prev, ...next].slice(0, 6));
  };

  const buildApiUserContent = (text: string, files: PendingFile[]) => {
    const images = files.filter((f) => f.kind === "image" && f.dataUrl);
    const texts = files.filter((f) => f.kind === "text" && f.text);
    let prompt = text;
    for (const t of texts) {
      prompt += `\n\n----- 附件 ${t.name} -----\n${t.text}\n-----`;
    }
    if (!images.length) return prompt;
    return [
      { type: "text", text: prompt || "请查看附件图片。" },
      ...images.map((img) => ({
        type: "image_url",
        image_url: { url: img.dataUrl },
      })),
    ];
  };

  const buildSearchPrompt = (text: string, files: PendingFile[]) => {
    let prompt = text;
    for (const t of files.filter((f) => f.kind === "text" && f.text)) {
      prompt += `\n\n----- 附件 ${t.name} -----\n${t.text}\n-----`;
    }
    if (!prompt.trim() && files.some((f) => f.kind === "image")) {
      prompt = "请结合附件图片进行联网搜索，并给出结论与来源。";
    }
    return prompt.trim();
  };

  const setSessionLoading = (sessionId: string, loading: boolean) => {
    setLoadingBySessionId((prev) => {
      if (loading) return { ...prev, [sessionId]: true };
      const next = { ...prev };
      delete next[sessionId];
      return next;
    });
  };

  const runCompletion = async (
    sessionId: string,
    nextMessages: ChatMessage[],
    opts: {
      content: string;
      files: PendingFile[];
      title: string;
      lockedBefore: string;
      useWebSearch: boolean;
      model: string;
    },
  ) => {
    const { content, files, title, lockedBefore, useWebSearch, model } = opts;
    const wantsImage = !useWebSearch && (looksLikeImagePrompt(content) || isImageModelId(model));
    const requestModel = wantsImage
      ? IMAGE_CHAT_MODEL
      : isImageModelId(model)
        ? "auto"
        : (model || "auto").trim() || "auto";
    setSessionLoading(sessionId, true);
    setError("");
    setStreamPhase("waiting");
    startedAtRef.current = Date.now();
    setElapsedMs(0);
    abortBySessionRef.current[sessionId]?.abort();
    const ac = new AbortController();
    abortBySessionRef.current[sessionId] = ac;

    const patchThis = (patch: Partial<ChatSession>) => updateSession(sessionId, patch);

    try {
      if (useWebSearch) {
        const prompt = buildSearchPrompt(content, files);
        const images = files.filter((f) => f.kind === "image" && f.dataUrl).map((f) => String(f.dataUrl));
        const assistant: ChatMessage = {
          role: "assistant",
          content: "",
          at: Date.now(),
          kind: "search",
          sources: [],
        };
        patchThis({ messages: [...nextMessages, assistant], title });
        let assembled = "";
        let sources: SearchSource[] = [];
        const { accountEmail } = await fetchSseJson(
          "/v1/search",
          { prompt, images, stream: true },
          {
            signal: ac.signal,
            headers: preferredHeaders(),
            onEvent: (data) => {
              const piece = deltaContentFromChatChunk(data);
              const nextSources = sourcesFromChatChunk(data);
              if (nextSources) sources = nextSources;
              if (!piece && !nextSources) return;
              if (piece) assembled = stripLeakedToolCalls(assembled + piece);
              setStreamPhase("streaming");
              flushSync(() => {
                patchThis({
                  messages: [
                    ...nextMessages,
                    { ...assistant, content: assembled, sources: sources.length ? sources : undefined },
                  ],
                  title,
                });
              });
            },
          },
        );
        const took = Date.now() - startedAtRef.current;
        const reply = normalizeChatMarkdown(assembled.trim()) || "（空搜索结果）";
        const finalEmail = accountEmail || preferredEmail.trim() || lockedBefore;
        if (lockedBefore && finalEmail && lockedBefore.toLowerCase() !== finalEmail.toLowerCase()) {
          setAccountSwitchNote(`账号已切换：${lockedBefore} → ${finalEmail}`);
        }
        patchThis({
          messages: [
            ...nextMessages,
            {
              ...assistant,
              content: reply,
              sources: sources.length ? sources : undefined,
              elapsedMs: took,
            },
          ],
          title,
          accountEmail: finalEmail,
          preferredEmail: finalEmail || preferredEmail.trim(),
        });
        if (finalEmail) setPreferredEmail(finalEmail);
        setStreamPhase("done");
        return;
      }

      const assistant: ChatMessage = {
        role: "assistant",
        content: "",
        at: Date.now(),
        kind: wantsImage ? "image" : "chat",
      };
      patchThis({ messages: [...nextMessages, assistant], title });
      let assembled = "";
      const apiMessages = nextMessages.map((m, idx) => {
        if (idx === nextMessages.length - 1 && m.role === "user") {
          return {
            role: "user" as const,
            content: files.length ? buildApiUserContent(content, files) : content,
          };
        }
        return { role: m.role, content: m.content };
      });

      const { accountEmail } = await fetchSseJson(
        "/v1/chat/completions",
        {
          model: requestModel,
          stream: true,
          messages: apiMessages,
          ...(wantsImage ? { modalities: ["image", "text"] } : {}),
        },
        {
          signal: ac.signal,
          headers: preferredHeaders(),
          onEvent: (data) => {
            const piece = deltaContentFromChatChunk(data);
            if (!piece) return;
            assembled = stripLeakedToolCalls(assembled + piece);
            setStreamPhase("streaming");
            flushSync(() => {
              patchThis({
                messages: [...nextMessages, { ...assistant, content: assembled }],
                title,
              });
            });
          },
        },
      );
      const finalEmail = accountEmail || preferredEmail.trim() || lockedBefore;
      if (lockedBefore && finalEmail && lockedBefore.toLowerCase() !== finalEmail.toLowerCase()) {
        setAccountSwitchNote(`账号已切换：${lockedBefore} → ${finalEmail}`);
      }
      const took = Date.now() - startedAtRef.current;
      const finalText = normalizeChatMarkdown(assembled) || "（空回复）";
      patchThis({
        messages: [...nextMessages, { ...assistant, content: finalText, elapsedMs: took }],
        title,
        accountEmail: finalEmail,
        preferredEmail: finalEmail || preferredEmail.trim(),
      });
      if (finalEmail) setPreferredEmail(finalEmail);
      setStreamPhase("done");
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      const msg = humanizeUpstreamError(err instanceof Error ? err.message : String(err));
      setError(msg);
      setStreamPhase("idle");
      if (/403|cf_edge|chat.requirements|invalid|token/i.test(msg) && lockedBefore) {
        setAccountSwitchNote(`账号 ${lockedBefore} 失败，已解除绑定；下次将自动调度其它账号`);
        patchThis({ accountEmail: "", preferredEmail: "" });
        setPreferredEmail("");
      }
    } finally {
      setSessionLoading(sessionId, false);
      setElapsedMs(Date.now() - startedAtRef.current);
      delete abortBySessionRef.current[sessionId];
    }
  };

  const sendChat = async () => {
    const content = input.trim();
    if ((!content && pendingFiles.length === 0) || !active) return;
    if (loadingBySessionId[active.id]) return;
    if (inflightCount >= MAX_CONCURRENT_SSE) {
      setError(`同时进行的对话已达上限（${MAX_CONCURRENT_SSE}），请等待其中一路完成`);
      return;
    }

    const sessionId = active.id;
    const filesSnapshot = [...pendingFiles];
    const displayContent =
      content ||
      (filesSnapshot.length ? `（附件：${filesSnapshot.map((f) => f.name).join("、")}）` : "");
    const nextMessages: ChatMessage[] = [
      ...active.messages,
      {
        role: "user",
        content: displayContent,
        at: Date.now(),
        attachments: filesSnapshot.map((f) => ({ name: f.name, mime: f.mime, kind: f.kind })),
      },
    ];
    const title = active.messages.length === 0 ? displayContent.slice(0, 28) : active.title;
    const lockedBefore = String(active.accountEmail || "").trim();
    updateSession(sessionId, {
      messages: nextMessages,
      title,
      preferredEmail: preferredEmail.trim() || active.preferredEmail,
    });
    setInput("");
    setPendingFiles([]);

    await runCompletion(sessionId, nextMessages, {
      content,
      files: filesSnapshot,
      title,
      lockedBefore,
      useWebSearch: webSearch,
      model: active.model || "auto",
    });
  };

  const editUserAt = (msgIndex: number) => {
    if (!active || activeLoading) return;
    const msg = active.messages[msgIndex];
    if (!msg || msg.role !== "user") return;
    setInput(msg.content);
    setPendingFiles([]);
    updateActive({ messages: active.messages.slice(0, msgIndex) });
    setError("");
  };

  const retryAssistantAt = async (assistantIndex: number) => {
    if (!active || activeLoading) return;
    if (inflightCount >= MAX_CONCURRENT_SSE) {
      setError(`同时进行的对话已达上限（${MAX_CONCURRENT_SSE}），请等待其中一路完成`);
      return;
    }
    const asst = active.messages[assistantIndex];
    if (!asst || asst.role !== "assistant") return;
    let userIdx = -1;
    for (let i = assistantIndex - 1; i >= 0; i--) {
      if (active.messages[i].role === "user") {
        userIdx = i;
        break;
      }
    }
    if (userIdx < 0) return;
    const userMsg = active.messages[userIdx];
    const nextMessages = active.messages.slice(0, userIdx + 1);
    const sessionId = active.id;
    const title = active.title;
    const lockedBefore = String(active.accountEmail || "").trim();
    updateSession(sessionId, { messages: nextMessages });
    await runCompletion(sessionId, nextMessages, {
      content: userMsg.content,
      files: [],
      title,
      lockedBefore,
      useWebSearch: asst.kind === "search" || webSearch,
      model: active.model || "auto",
    });
  };

  const turnTotal = turnIndices.length;
  const turnDisplay = turnTotal === 0 ? 0 : Math.min(turnCursor + 1, turnTotal);

  return (
    <div className="grid h-full min-h-0 gap-2 overflow-hidden lg:grid-cols-[300px_minmax(0,1fr)]">
      <aside className="flex min-h-0 flex-col overflow-hidden rounded-2xl border border-border bg-card/90 p-2.5 shadow-sm">
        <div className="mb-2 flex shrink-0 items-center justify-between gap-2 px-0.5">
          <div className="text-sm font-medium text-foreground">会话</div>
          <div className="flex items-center gap-1">
            <Button
              size="sm"
              variant="ghost"
              className="h-8 rounded-lg px-2"
              title={showArchived ? "查看进行中" : "查看归档"}
              onClick={() => setShowArchived((v) => !v)}
            >
              {showArchived ? <FolderOpen className="size-4" /> : <Archive className="size-4" />}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-8 rounded-lg px-2"
              onClick={() => createSession(active?.model || "auto")}
            >
              <Plus className="size-4" />
            </Button>
          </div>
        </div>
        <div className="mb-2 px-0.5 text-[10px] text-muted-foreground">
          {showArchived ? "归档" : "进行中"} · 并发 {inflightCount}/{MAX_CONCURRENT_SSE}
        </div>
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden">
          {visibleSessions.map((s) => (
            <div
              key={s.id}
              className={cn(
                "group flex flex-col gap-0.5 rounded-xl px-2 py-2 text-sm",
                s.id === active?.id
                  ? "bg-muted text-foreground ring-1 ring-border"
                  : "text-muted-foreground hover:bg-muted/60",
              )}
            >
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  className="min-w-0 flex-1 truncate text-left font-medium"
                  onClick={() => startTransition(() => setActive(s.id))}
                >
                  {loadingBySessionId[s.id] ? "… " : ""}
                  {s.title || "新对话"}
                </button>
                <button
                  type="button"
                  className={cn(
                    "text-muted-foreground",
                    s.pinned ? "opacity-100 text-amber-600" : "opacity-0 group-hover:opacity-100",
                  )}
                  title={s.pinned ? "取消置顶" : "置顶"}
                  onClick={() => togglePin(s.id)}
                >
                  <Pin className={cn("size-3.5", s.pinned ? "fill-current" : "")} />
                </button>
                <button
                  type="button"
                  className="text-muted-foreground opacity-0 group-hover:opacity-100"
                  title={s.archived ? "取消归档" : "归档"}
                  onClick={() => updateSession(s.id, { archived: !s.archived })}
                >
                  <Archive className="size-3.5" />
                </button>
                <button
                  type="button"
                  className="text-muted-foreground opacity-0 group-hover:opacity-100"
                  onClick={() => deleteSession(s.id)}
                  aria-label="删除会话"
                >
                  <Trash2 className="size-3.5" />
                </button>
              </div>
              <div className="truncate text-[10px] text-muted-foreground">
                {s.groupId ? `[${s.groupId}] ` : ""}
                {sessionSubtitle(s, accounts)}
              </div>
            </div>
          ))}
          {!visibleSessions.length ? (
            <div className="px-2 py-6 text-center text-xs text-muted-foreground">暂无会话</div>
          ) : null}
        </div>
      </aside>

      <section className="flex min-h-0 flex-col overflow-hidden rounded-2xl border border-border bg-card/90 shadow-sm">
        {showEmailDetail && boundAccount ? (
          <div className="shrink-0 border-b border-border bg-muted/40 px-4 py-2 text-xs text-muted-foreground">
            {String(boundAccount.email || "")} · 状态 {boundAccount.status} · 额度{" "}
            {typeof boundAccount.quota === "number" ? boundAccount.quota : "—"}
          </div>
        ) : null}
        {switchAccountNote ? (
          <div className="shrink-0 border-b border-amber-100 bg-amber-50 px-4 py-2 text-xs text-amber-800">
            {switchAccountNote}
          </div>
        ) : null}

        {turnTotal > 0 ? (
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-4 py-1.5">
            <div className="text-[11px] text-muted-foreground">
              第 {turnDisplay}/{turnTotal} 轮
            </div>
            <div className="flex items-center gap-1">
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-7 rounded-lg px-2 text-xs"
                disabled={turnCursor <= 0}
                onClick={() => scrollToTurn(turnCursor - 1)}
              >
                <ChevronUp className="size-3.5" />
                上一轮
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-7 rounded-lg px-2 text-xs"
                disabled={turnCursor >= turnTotal - 1}
                onClick={() => scrollToTurn(turnCursor + 1)}
              >
                下一轮
                <ChevronDown className="size-3.5" />
              </Button>
            </div>
          </div>
        ) : null}

        <div ref={messagesScrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-4">
          {(active?.messages || []).length === 0 ? (
            <div className="flex h-full min-h-48 items-center justify-center text-sm text-muted-foreground">
              发送一条消息开始对话。可同时开多路会话（最多 {MAX_CONCURRENT_SSE}）。
            </div>
          ) : (
            (() => {
              let userTurn = -1;
              const lastIdx = (active?.messages.length || 0) - 1;
              return active?.messages.map((m, i) => {
                if (m.role === "user") userTurn += 1;
                const turnId = m.role === "user" ? userTurn : -1;
                const isStreamingBubble =
                  activeLoading && m.role === "assistant" && i === lastIdx;
                return (
                  <div
                    key={`${m.role}-${i}-${m.at || i}`}
                    id={turnId >= 0 ? `turn-${turnId}` : undefined}
                    className={cn(
                      "relative z-0 max-w-[92%] rounded-2xl px-4 py-3 text-sm",
                      m.role === "user"
                        ? "ml-auto bg-muted text-foreground"
                        : "mr-auto border border-border bg-background text-foreground shadow-sm",
                    )}
                  >
                    <div className="mb-1 text-[11px] uppercase tracking-wide text-muted-foreground">
                      {m.role === "user" ? "你" : m.kind === "search" ? "搜索" : m.kind === "image" ? "生图" : "助手"}
                    </div>
                    {m.attachments?.length ? (
                      <div className="mb-2 flex flex-wrap gap-1">
                        {m.attachments.map((a) => (
                          <span
                            key={`${a.name}-${a.mime}`}
                            className="rounded-md bg-stone-100 px-2 py-0.5 text-[11px] text-stone-600"
                          >
                            {a.kind === "image" ? "图片" : "文本"} · {a.name}
                          </span>
                        ))}
                      </div>
                    ) : null}
                    <MarkdownBubble content={m.content} role={m.role} isStreaming={isStreamingBubble} />
                    {m.sources?.length ? <SourcesCollapsible sources={m.sources} /> : null}
                    <div className="mt-2 flex flex-wrap items-center gap-1">
                      {m.role === "user" ? (
                        <>
                          <button
                            type="button"
                            className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-background hover:text-foreground disabled:opacity-40"
                            disabled={activeLoading}
                            onClick={() => editUserAt(i)}
                          >
                            <Pencil className="size-3" />
                            编辑
                          </button>
                          <button
                            type="button"
                            className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-background hover:text-foreground"
                            onClick={() => void copyText(m.content)}
                          >
                            <Copy className="size-3" />
                            复制
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            type="button"
                            className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground"
                            onClick={() => void copyText(m.content)}
                          >
                            <Copy className="size-3" />
                            复制
                          </button>
                          <button
                            type="button"
                            className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
                            disabled={activeLoading || isStreamingBubble}
                            onClick={() => void retryAssistantAt(i)}
                          >
                            <RotateCcw className="size-3" />
                            重来
                          </button>
                          <button
                            type="button"
                            className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground"
                            disabled={!m.content}
                            onClick={() => exportSingleMessage(m, active?.title || "chat")}
                          >
                            <Download className="size-3" />
                            单独导出
                          </button>
                          {typeof m.elapsedMs === "number" && m.elapsedMs > 0 ? (
                            <span className="ml-1 text-[11px] text-muted-foreground">
                              耗时 {(m.elapsedMs / 1000).toFixed(1)}s
                            </span>
                          ) : null}
                        </>
                      )}
                    </div>
                  </div>
                );
              });
            })()
          )}
          {activeLoading ? (
            <div className="inline-flex items-center gap-2 rounded-2xl bg-muted px-3 py-2 text-sm text-muted-foreground">
              <LoaderCircle className="size-4 animate-spin" />
              {(elapsedMs / 1000).toFixed(1)}s
            </div>
          ) : null}
        </div>

        {error ? (
          <div className="mx-4 mb-2 shrink-0 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </div>
        ) : null}

        <form
          className="shrink-0 border-t border-border p-3"
          onSubmit={(e) => {
            e.preventDefault();
            void sendChat();
          }}
        >
          {pendingFiles.length > 0 ? (
            <div className="mb-2 flex flex-wrap gap-2">
              {pendingFiles.map((f) => (
                <div
                  key={f.id}
                  className="flex items-center gap-2 rounded-lg border border-border bg-stone-50 px-2 py-1 text-xs text-stone-700"
                >
                  {f.kind === "image" && f.dataUrl ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={f.dataUrl} alt={f.name} className="size-8 rounded object-cover" />
                  ) : (
                    <Paperclip className="size-3.5" />
                  )}
                  <span className="max-w-[140px] truncate">{f.name}</span>
                  <button
                    type="button"
                    className="text-muted-foreground hover:text-foreground"
                    onClick={() => setPendingFiles((prev) => prev.filter((x) => x.id !== f.id))}
                    aria-label="移除附件"
                  >
                    <X className="size-3.5" />
                  </button>
                </div>
              ))}
            </div>
          ) : null}
          <div className="flex items-end gap-2">
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              multiple
              accept="image/*,.txt,.md,.csv,.json,.log,.py,.ts,.tsx,.js,.jsx,.html,.css,text/*"
              onChange={(e) => {
                const files = e.target.files;
                if (files?.length) void addFiles(files);
                e.target.value = "";
              }}
            />
            <Button
              type="button"
              variant="outline"
              className="h-10 shrink-0 rounded-xl px-3"
              disabled={activeLoading}
              title="上传图片或文本文件"
              onClick={() => fileInputRef.current?.click()}
            >
              <Paperclip className="size-4" />
            </Button>
            <Textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              rows={1}
              placeholder={
                webSearch ? "输入要搜索的内容…" : "输入消息，Enter 发送（Shift+Enter 换行）"
              }
              className="max-h-[9.5rem] min-h-10 flex-1 resize-none overflow-y-auto rounded-xl leading-5"
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void sendChat();
                }
              }}
              onPaste={(e) => {
                const items = e.clipboardData?.files;
                if (items?.length) {
                  e.preventDefault();
                  void addFiles(items);
                }
              }}
            />
            <Button
              type="submit"
              disabled={activeLoading || (!input.trim() && pendingFiles.length === 0)}
              className="h-10 shrink-0 rounded-xl px-4"
            >
              {activeLoading ? <LoaderCircle className="size-4 animate-spin" /> : <Send className="size-4" />}
            </Button>
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted-foreground">模型</span>
            <select
              className="h-8 max-w-[160px] rounded-lg border border-input bg-background px-2 text-xs text-foreground"
              value={active?.model || "auto"}
              onChange={(e) => updateActive({ model: e.target.value })}
            >
              {modelOptions.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
            <span className="text-xs text-muted-foreground">账号</span>
            <select
              className="h-8 max-w-[220px] rounded-lg border border-input bg-background px-2 text-xs text-foreground"
              value={preferredEmail}
              onChange={(e) => {
                const v = e.target.value;
                setPreferredEmail(v);
                updateActive({ preferredEmail: v, accountEmail: v || undefined });
                setAccountSwitchNote("");
              }}
            >
              <option value="">自动调度</option>
              {accountOptions.map((email) => (
                <option key={email} value={email}>
                  {email}
                </option>
              ))}
            </select>
            {boundAccount ? (
              <button
                type="button"
                className="rounded-lg border border-input px-2 py-1 text-[11px] text-muted-foreground hover:bg-muted"
                onClick={() => setShowEmailDetail((v) => !v)}
                title="今日额度摘要"
              >
                聊/图/剩：— / — / {typeof boundAccount.quota === "number" ? boundAccount.quota : "—"}
              </button>
            ) : null}
            <button
              type="button"
              className={cn(
                "inline-flex h-8 items-center gap-1 rounded-lg border px-2 text-xs",
                webSearch ? "border-sky-300 bg-sky-50 text-sky-800" : "border-input text-muted-foreground",
              )}
              onClick={() => setWebSearch((v) => !v)}
              title="开启后本条走联网搜索"
            >
              <Globe2 className="size-3.5" />
              联网
            </button>
            <Button
              size="sm"
              variant="ghost"
              className="h-8 rounded-lg px-2"
              title="导出 Markdown"
              disabled={!active?.messages.length}
              onClick={() => {
                if (!active) return;
                downloadBlob(`${active.title || "chat"}.md`, exportSessionMarkdown(active), "text/markdown;charset=utf-8");
              }}
            >
              <Download className="size-3.5" />
              md
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-8 rounded-lg px-2"
              title="导出纯文本"
              disabled={!active?.messages.length}
              onClick={() => {
                if (!active) return;
                downloadBlob(`${active.title || "chat"}.txt`, exportSessionText(active), "text/plain;charset=utf-8");
              }}
            >
              txt
            </Button>
            <input
              className="h-8 w-24 rounded-lg border border-input bg-background px-2 text-xs"
              placeholder="分组"
              value={active?.groupId || ""}
              onChange={(e) => updateActive({ groupId: e.target.value.trim() || undefined })}
            />
            {activeLoading ? (
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <LoaderCircle className="size-3.5 animate-spin" />
                {(elapsedMs / 1000).toFixed(1)}s
              </span>
            ) : null}
          </div>
        </form>
      </section>
    </div>
  );
}
