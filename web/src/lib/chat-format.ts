/** Shared chat UI helpers: strip tool stubs, absolutize links, humanize upstream errors. */

export function stripLeakedToolCalls(text: string): string {
  let value = String(text || "");
  const re =
    /\b(?:search|web_search|browser|open_url|web\.search)\s*\(\s*(?:\[[\s\S]*?\]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\s*\)\s*/gi;
  let prev = "";
  while (prev !== value) {
    prev = value;
    value = value.replace(re, "");
  }
  return value;
}

export function normalizeChatMarkdown(text: string): string {
  return stripLeakedToolCalls(text)
    .replace(/\ue200url\ue202([^\ue202\ue201]*)\ue202([^\ue201]*)\ue201/g, "[$1]($2)")
    .replace(/\ue200cite\ue202[^\ue201]*\ue201/g, "")
    .replace(/\ue200[^\ue201]*\ue201/g, "")
    .replace(/\ue200[^\ue201]*$/g, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function absolutizeHref(href: string | undefined | null, linkText?: string): string {
  let raw = String(href || "").trim();
  const label = String(linkText || "").trim();

  // Relative site paths that are really owner/repo
  if (raw.startsWith("/") && /^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+/.test(raw)) {
    raw = raw.slice(1);
  }

  if (!raw || raw === "#" || raw.startsWith("mailto:") || raw.startsWith("javascript:")) {
    // Fall back to label if it looks like a repo or URL
    if (/^https?:\/\//i.test(label)) return label;
    if (/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(label)) return `https://github.com/${label}`;
    return raw;
  }
  if (/^https?:\/\//i.test(raw)) return raw;
  if (raw.startsWith("//")) return `https:${raw}`;
  if (raw.startsWith("github.com/") || raw.startsWith("www.")) return `https://${raw}`;
  if (/^[A-Za-z0-9_.-]+\.[A-Za-z]{2,}([/:?#].*)?$/.test(raw) && !raw.includes(" ")) {
    return `https://${raw}`;
  }
  // owner/repo
  if (/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(\/.*)?$/.test(raw) && !raw.includes(" ")) {
    return `https://github.com/${raw.replace(/\/$/, "")}`;
  }
  // Last resort: label is owner/repo
  if (/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(label)) {
    return `https://github.com/${label}`;
  }
  return raw;
}

export function humanizeUpstreamError(raw: unknown): string {
  const text = typeof raw === "string" ? raw : JSON.stringify(raw ?? "");
  const lower = text.toLowerCase();
  if (
    lower.includes("chat_requirements_prepare") ||
    lower.includes("chat_requirements_finalize")
  ) {
    if (lower.includes("403") || lower.includes("cloudflare") || lower.includes("html")) {
      return (
        "对话鉴权（chat-requirements）被 Cloudflare/出口拦截（403 HTML）。" +
        "通常是代理节点抽风或边缘挑战，不是账号密码错。请换节点/重试，或取消粘账号改自动调度。"
      );
    }
    return "对话鉴权（chat-requirements）失败，请稍后重试或换账号/节点。";
  }
  if (
    lower.includes("cloudflare_or_edge_html_block") ||
    lower.includes("cf_edge_block") ||
    (lower.includes("conversation") && lower.includes("403"))
  ) {
    return (
      "上游对话接口被 Cloudflare/出口拦截（403）。" +
      "已会自动短重试并尝试换号；若仍失败请取消粘账号或换节点。这与「养号」队列无关。"
    );
  }
  if (lower.includes("<html") && lower.includes("403")) {
    return "上游 403 返回了 HTML 挑战页（非 JSON）。多为 CF/代理拦截，请重试或换节点。";
  }
  // Trim huge HTML dumps
  if (text.includes("<html") || text.includes("<!DOCTYPE")) {
    const head = text.slice(0, 160).replace(/\s+/g, " ");
    return `上游错误（含 HTML）：${head}…`;
  }
  return text.length > 320 ? `${text.slice(0, 320)}…` : text;
}

export function humanizeRiskFinding(item: string, detail: string): string {
  const name = String(item || "").trim();
  const d = String(detail || "").trim();
  if (name === "deepseek_error" || name.toLowerCase().includes("jsondecode")) {
    return (
      `DeepSeek 巡检草稿没返回合法 JSON（解析失败）。` +
      `这只说明「文案助手挂了」，不等于号池真有中等风险；请看同条的「号池判定」。` +
      (d ? ` 原始：${d}` : "")
    );
  }
  return d ? `${name}：${d}` : name || d;
}
