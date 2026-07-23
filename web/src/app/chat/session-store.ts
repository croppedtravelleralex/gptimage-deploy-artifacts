"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const CHAT_STORE_KEY = "gptimage.chat.v1";
const SEARCH_STORE_KEY = "gptimage.search.v1";
const TAB_STORE_KEY = "gptimage.chat.tab.v1";

export type ChatMessage = {
  role: "user" | "assistant" | "system";
  content: string;
  at?: number;
  kind?: "chat" | "search" | "image";
  sources?: Array<{ title?: string; url?: string; snippet?: string }>;
  /** Completed reply duration in ms (assistant only). */
  elapsedMs?: number;
  /** Lightweight attachment labels (no base64 in localStorage). */
  attachments?: Array<{ name: string; mime: string; kind: "image" | "text" }>;
};

export type ChatSession = {
  id: string;
  title: string;
  model: string;
  messages: ChatMessage[];
  updatedAt: number;
  accountEmail?: string;
  preferredEmail?: string;
  /** Optional sidebar group label */
  groupId?: string;
  archived?: boolean;
  /** Pin to top of session list */
  pinned?: boolean;
};

export type SearchSnapshot = {
  prompt: string;
  answer?: string;
  sources?: Array<{ title?: string; url?: string; snippet?: string }>;
  error?: string;
  elapsedMs?: number;
  updatedAt: number;
};

type ChatStore = {
  sessions: ChatSession[];
  activeId: string;
};

function uid() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function emptySession(model = "auto"): ChatSession {
  return {
    id: uid(),
    title: "新对话",
    model,
    messages: [],
    updatedAt: Date.now(),
  };
}

function normalizeSession(raw: Partial<ChatSession> & { id?: string }): ChatSession {
  const base = emptySession(String(raw.model || "auto"));
  return {
    ...base,
    ...raw,
    id: String(raw.id || base.id),
    title: String(raw.title || "新对话"),
    model: String(raw.model || "auto"),
    messages: Array.isArray(raw.messages) ? raw.messages : [],
    updatedAt: Number(raw.updatedAt || Date.now()),
    archived: Boolean(raw.archived),
    pinned: Boolean(raw.pinned),
    groupId: raw.groupId ? String(raw.groupId) : undefined,
  };
}

function readChatStore(): ChatStore {
  try {
    const raw = localStorage.getItem(CHAT_STORE_KEY);
    if (!raw) {
      const s = emptySession();
      return { sessions: [s], activeId: s.id };
    }
    const parsed = JSON.parse(raw) as ChatStore;
    if (!parsed?.sessions?.length) {
      const s = emptySession();
      return { sessions: [s], activeId: s.id };
    }
    return {
      sessions: parsed.sessions.map((s) => normalizeSession(s)),
      activeId: parsed.activeId || parsed.sessions[0].id,
    };
  } catch {
    const s = emptySession();
    return { sessions: [s], activeId: s.id };
  }
}

function stripHeavyMedia(content: string): string {
  return String(content || "").replace(
    /!\[[^\]]*\]\(data:image\/[^)]+\)/gi,
    "![image](about:blank#generated-image-not-persisted)",
  );
}

function sanitizeStoreForPersist(store: ChatStore): ChatStore {
  return {
    ...store,
    sessions: store.sessions.map((session) => ({
      ...session,
      messages: (session.messages || []).map((message) => ({
        ...message,
        content: stripHeavyMedia(String(message.content || "")),
      })),
    })),
  };
}

function writeChatStore(store: ChatStore) {
  try {
    localStorage.setItem(CHAT_STORE_KEY, JSON.stringify(sanitizeStoreForPersist(store)));
  } catch {
    try {
      localStorage.setItem(
        CHAT_STORE_KEY,
        JSON.stringify({
          ...sanitizeStoreForPersist(store),
          sessions: sanitizeStoreForPersist(store).sessions.slice(0, 20),
        }),
      );
    } catch {
      /* ignore quota errors */
    }
  }
}

export function useChatSessions() {
  const [store, setStore] = useState<ChatStore>(() => {
    if (typeof window === "undefined") {
      const s = emptySession();
      return { sessions: [s], activeId: s.id };
    }
    return readChatStore();
  });
  const persistTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    // 切会话时防抖写盘，避免同步 JSON.stringify 卡 UI
    if (persistTimerRef.current) clearTimeout(persistTimerRef.current);
    persistTimerRef.current = setTimeout(() => writeChatStore(store), 280);
    return () => {
      if (persistTimerRef.current) clearTimeout(persistTimerRef.current);
    };
  }, [store]);

  const active = useMemo(
    () => store.sessions.find((s) => s.id === store.activeId) || store.sessions[0],
    [store],
  );

  const setActive = useCallback((id: string) => {
    setStore((prev) => (prev.activeId === id ? prev : { ...prev, activeId: id }));
  }, []);

  const createSession = useCallback((model = "auto") => {
    const s = emptySession(model);
    setStore((prev) => ({ sessions: [s, ...prev.sessions].slice(0, 80), activeId: s.id }));
    return s.id;
  }, []);

  const deleteSession = useCallback((id: string) => {
    setStore((prev) => {
      const sessions = prev.sessions.filter((s) => s.id !== id);
      if (!sessions.length) {
        const s = emptySession();
        return { sessions: [s], activeId: s.id };
      }
      const activeId = prev.activeId === id ? sessions[0].id : prev.activeId;
      return { sessions, activeId };
    });
  }, []);

  const updateSession = useCallback((id: string, patch: Partial<ChatSession>) => {
    setStore((prev) => ({
      ...prev,
      sessions: prev.sessions.map((s) =>
        s.id === id ? { ...s, ...patch, updatedAt: Date.now() } : s,
      ),
    }));
  }, []);

  const updateActive = useCallback((patch: Partial<ChatSession>) => {
    setStore((prev) => ({
      ...prev,
      sessions: prev.sessions.map((s) =>
        s.id === prev.activeId ? { ...s, ...patch, updatedAt: Date.now() } : s,
      ),
    }));
  }, []);

  const togglePin = useCallback((id: string) => {
    setStore((prev) => ({
      ...prev,
      sessions: prev.sessions.map((s) =>
        s.id === id ? { ...s, pinned: !s.pinned, updatedAt: Date.now() } : s,
      ),
    }));
  }, []);

  return { store, active, setActive, createSession, deleteSession, updateActive, updateSession, togglePin };
}

export function loadSearchSnapshot(): SearchSnapshot | null {
  try {
    const raw = localStorage.getItem(SEARCH_STORE_KEY);
    return raw ? (JSON.parse(raw) as SearchSnapshot) : null;
  } catch {
    return null;
  }
}

export function saveSearchSnapshot(snap: SearchSnapshot | null) {
  if (!snap) {
    localStorage.removeItem(SEARCH_STORE_KEY);
    return;
  }
  localStorage.setItem(SEARCH_STORE_KEY, JSON.stringify(snap));
}

export function loadChatTab(defaultTab = "chat") {
  try {
    return localStorage.getItem(TAB_STORE_KEY) || defaultTab;
  } catch {
    return defaultTab;
  }
}

export function saveChatTab(tab: string) {
  try {
    localStorage.setItem(TAB_STORE_KEY, tab);
  } catch {
    /* ignore */
  }
}

export function exportSessionMarkdown(session: ChatSession): string {
  const lines = [`# ${session.title || "对话"}`, "", `模型: ${session.model || "auto"}`, ""];
  if (session.accountEmail) lines.push(`账号: ${session.accountEmail}`, "");
  for (const m of session.messages) {
    const who = m.role === "user" ? "你" : m.kind === "search" ? "搜索" : "助手";
    lines.push(`## ${who}`, "", m.content || "", "");
  }
  return lines.join("\n");
}

export function exportSessionText(session: ChatSession): string {
  const lines: string[] = [`${session.title || "对话"}`, ""];
  for (const m of session.messages) {
    const who = m.role === "user" ? "你" : m.kind === "search" ? "搜索" : "助手";
    lines.push(`[${who}]`, m.content || "", "");
  }
  return lines.join("\n");
}
