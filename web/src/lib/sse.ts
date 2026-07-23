/** Fetch SSE (text/event-stream) and yield parsed JSON data objects. */

import webConfig from "@/constants/common-env";
import { getStoredAuthKey } from "@/store/auth";

export type SseHandlers = {
  onEvent?: (data: unknown) => void;
  signal?: AbortSignal;
  headers?: Record<string, string>;
};

export async function fetchSseJson(
  path: string,
  body: unknown,
  handlers: SseHandlers = {},
): Promise<{ accountEmail: string }> {
  const authKey = await getStoredAuthKey();
  const base = webConfig.apiUrl.replace(/\/$/, "");
  const url = path.startsWith("http") ? path : `${base}${path.startsWith("/") ? "" : "/"}${path}`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...(authKey ? { Authorization: `Bearer ${authKey}` } : {}),
      ...(handlers.headers || {}),
    },
    body: JSON.stringify(body),
    signal: handlers.signal,
  });
  const accountEmail = String(resp.headers.get("X-Account-Email") || "").trim();
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(text.slice(0, 240) || `HTTP ${resp.status}`);
  }
  if (!resp.body) {
    throw new Error("empty stream body");
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE events are separated by blank lines; also accept \r\n
    const chunks = buffer.split(/\r?\n\r?\n/);
    buffer = chunks.pop() || "";
    for (const chunk of chunks) {
      for (const line of chunk.split(/\r?\n/)) {
        const trimmed = line.trim();
        if (!trimmed.startsWith("data:")) continue;
        const payload = trimmed.slice(5).trim();
        if (!payload || payload === "[DONE]") continue;
        try {
          handlers.onEvent?.(JSON.parse(payload));
        } catch {
          /* ignore non-json */
        }
      }
    }
  }
  // flush trailing buffer
  if (buffer.trim()) {
    for (const line of buffer.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const payload = trimmed.slice(5).trim();
      if (!payload || payload === "[DONE]") continue;
      try {
        handlers.onEvent?.(JSON.parse(payload));
      } catch {
        /* ignore */
      }
    }
  }
  return { accountEmail };
}

export function deltaContentFromChatChunk(data: unknown): string {
  if (!data || typeof data !== "object") return "";
  const choices = (data as { choices?: Array<{ delta?: { content?: string } }> }).choices;
  const delta = choices?.[0]?.delta?.content;
  return typeof delta === "string" ? delta : "";
}

export function sourcesFromChatChunk(data: unknown): Array<{ title?: string; url?: string; snippet?: string }> | null {
  if (!data || typeof data !== "object") return null;
  const sources = (data as { sources?: unknown }).sources;
  if (!Array.isArray(sources)) return null;
  return sources.filter((item) => item && typeof item === "object") as Array<{
    title?: string;
    url?: string;
    snippet?: string;
  }>;
}

