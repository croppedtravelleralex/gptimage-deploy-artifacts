"use client";

import { memo, useMemo, useState } from "react";
import { Check, Copy } from "lucide-react";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import css from "highlight.js/lib/languages/css";
import go from "highlight.js/lib/languages/go";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import kotlin from "highlight.js/lib/languages/kotlin";
import markdown from "highlight.js/lib/languages/markdown";
import php from "highlight.js/lib/languages/php";
import python from "highlight.js/lib/languages/python";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

import { cn } from "@/lib/utils";

import "highlight.js/styles/atom-one-light.css";

let registered = false;
function ensureLangs() {
  if (registered) return;
  registered = true;
  const langs: Array<[string, typeof python]> = [
    ["bash", bash],
    ["sh", bash],
    ["shell", bash],
    ["c", c],
    ["cpp", cpp],
    ["c++", cpp],
    ["csharp", csharp],
    ["cs", csharp],
    ["css", css],
    ["go", go],
    ["java", java],
    ["javascript", javascript],
    ["js", javascript],
    ["json", json],
    ["kotlin", kotlin],
    ["markdown", markdown],
    ["md", markdown],
    ["php", php],
    ["python", python],
    ["py", python],
    ["ruby", ruby],
    ["rust", rust],
    ["sql", sql],
    ["mysql", sql],
    ["postgresql", sql],
    ["pgsql", sql],
    ["typescript", typescript],
    ["ts", typescript],
    ["tsx", typescript],
    ["xml", xml],
    ["html", xml],
    ["yaml", yaml],
    ["yml", yaml],
  ];
  for (const [name, mod] of langs) {
    try {
      hljs.registerLanguage(name, mod);
    } catch {
      /* ignore dup */
    }
  }
}

const LANG_LABEL: Record<string, string> = {
  cpp: "C++",
  "c++": "C++",
  csharp: "C#",
  cs: "C#",
  javascript: "JavaScript",
  js: "JavaScript",
  typescript: "TypeScript",
  ts: "TypeScript",
  tsx: "TSX",
  python: "Python",
  py: "Python",
  sql: "SQL",
  mysql: "MySQL",
  postgresql: "PostgreSQL",
  pgsql: "PostgreSQL",
  rust: "Rust",
  go: "Go",
  java: "Java",
  bash: "Bash",
  sh: "Shell",
  shell: "Shell",
  json: "JSON",
  yaml: "YAML",
  yml: "YAML",
  html: "HTML",
  xml: "XML",
  css: "CSS",
  kotlin: "Kotlin",
  php: "PHP",
  ruby: "Ruby",
  markdown: "Markdown",
  md: "Markdown",
  c: "C",
};

function normalizeLang(raw: string | undefined): string {
  const s = String(raw || "")
    .trim()
    .toLowerCase()
    .replace(/^language-/, "");
  if (!s) return "text";
  if (s === "c++") return "cpp";
  if (s === "mysql" || s === "postgresql" || s === "pgsql") return "sql";
  return s;
}

type Props = {
  code: string;
  language?: string;
  className?: string;
  /** Skip highlight while streaming to keep long chats smooth */
  highlight?: boolean;
};

export const CodeBlock = memo(function CodeBlock({
  code,
  language,
  className,
  highlight = true,
}: Props) {
  const [copied, setCopied] = useState(false);
  const lang = normalizeLang(language);
  const label = LANG_LABEL[lang] || (lang === "text" ? "Text" : lang);

  const html = useMemo(() => {
    if (!highlight) return "";
    ensureLangs();
    try {
      if (lang && lang !== "text" && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang }).value;
      }
      // 无语言标记时自动着色（截图里纯 TEXT 灰块的根因）
      return hljs.highlightAuto(code).value;
    } catch {
      return "";
    }
  }, [code, lang, highlight]);

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* ignore */
    }
  };

  return (
    <div className={cn("group relative my-3 overflow-hidden rounded-lg border border-stone-200 bg-stone-50", className)}>
      <div className="flex items-center justify-between gap-2 border-b border-stone-200 bg-stone-100/80 px-3 py-1.5">
        <span className="text-[11px] font-medium uppercase tracking-wide text-stone-500">{label}</span>
        <button
          type="button"
          onClick={() => void onCopy()}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-stone-600 hover:bg-white hover:text-stone-900"
          title="复制代码"
        >
          {copied ? <Check className="size-3.5 text-emerald-600" /> : <Copy className="size-3.5" />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      {html ? (
        <pre className="overflow-x-auto p-3 text-[13px] leading-relaxed">
      <code
            className="hljs font-mono text-[13px] leading-relaxed text-stone-800 [&_.hljs-comment]:text-[#a0a1a7] [&_.hljs-keyword]:text-[#a626a4] [&_.hljs-string]:text-[#50a14f] [&_.hljs-number]:text-[#986801] [&_.hljs-built_in]:text-[#c18401] [&_.hljs-title]:text-[#4078f2] [&_.hljs-attr]:text-[#986801] [&_.hljs-literal]:text-[#0184bc] [&_.hljs-type]:text-[#c18401] [&_.hljs-meta]:text-[#4078f2] [&_.hljs-params]:text-[#383a42] [&_.hljs-subst]:text-[#383a42]"
            dangerouslySetInnerHTML={{ __html: html }}
          />
        </pre>
      ) : (
        <pre className="overflow-x-auto p-3 font-mono text-[13px] leading-relaxed text-stone-800">
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
});
