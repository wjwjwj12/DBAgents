"use client";

import React, { FormEvent, useEffect, useRef, useState } from "react";
import Image from "next/image";
import { createPortal } from "react-dom";
import styles from "./page.module.css";

const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:14499").replace(/\/$/, "");
const apiUrl = (path: string) => /^https?:\/\//.test(path) ? path : `${API_BASE_URL}${path}`;
const createConversationId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024;
const RETRYABLE_API_STATUS = new Set([502, 503, 504]);

const fetchApiWithRetry = async (path: string, attempts = 5) => {
  let lastError: unknown;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const response = await fetch(apiUrl(path), { cache: "no-store" });
      if (!RETRYABLE_API_STATUS.has(response.status) || attempt === attempts - 1) return response;
      lastError = new Error(`HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
      if (attempt === attempts - 1) throw error;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 500 * (2 ** attempt)));
  }
  throw lastError instanceof Error ? lastError : new Error("API 请求失败");
};

interface Action {
  text: string;
  status: "loading" | "done";
  actionKey?: string;
}

interface TaskGroup {
  id: string;
  title: string;
  thoughts: string[];
  draftThought?: string;
  actions: Action[];
  started_at?: string | null;
}

type ProcessStatus = "pending" | "running" | "awaiting_approval" | "completed" | "failed" | "cancelled";

interface ProcessView {
  runId: string;
  status: ProcessStatus;
  startedAt: string;
  completedAt?: string | null;
}

interface ArtifactView {
  artifactId?: string;
  artifactType: string;
  title: string;
  previewKind: string;
  html?: string;
  markdown?: string;
  text?: string;
  downloadUrl?: string;
  previewUrl?: string;
  missingFields?: string[];
}

interface Message {
  id?: string;
  type: "user" | "ai" | "ai-stream" | "error";
  text?: string;
  attachedFile?: string | null;
  attachedFiles?: string[];
  groups?: TaskGroup[];
  artifacts?: ArtifactView[];
  hasFinalOutput?: boolean;
  process?: ProcessView;
}

interface ApprovalView {
  runId: string;
  toolCallId: string;
  name: string;
  arguments: Record<string, unknown>;
  message: string;
}

interface ConversationSummary {
  id: string;
  title: string;
  last_message: string;
  updated_at: string;
  is_archived: boolean;
  is_pinned: boolean;
}

interface StoredArtifact {
  artifact_id: string;
  artifact_type: string;
  title: string;
  preview_kind: string;
  html?: string;
  markdown?: string;
  text?: string;
  download_url?: string;
  preview_url?: string;
  missing_fields?: string[];
}

interface StoredMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  attachment_name?: string | null;
  attachment_names?: string[];
  artifacts: StoredArtifact[];
  groups?: TaskGroup[];
  process?: {
    run_id: string;
    status: ProcessStatus;
    started_at: string;
    completed_at?: string | null;
  } | null;
}

interface StoredConversation {
  id: string;
  title: string;
  pending_approval?: {
    run_id: string;
    tool_call_id: string;
    name: string;
    arguments?: Record<string, unknown>;
    message?: string;
  } | null;
  messages: StoredMessage[];
}

interface StreamEvent {
  type: "run_started" | "task_group" | "thought" | "thought_delta" | "turn_delta" | "turn_commit" | "turn_clear" | "action" | "content_delta" | "content_reset" | "content" | "approval_required" | "skills_changed" | "error";
  run_id?: string;
  title?: string;
  text?: string;
  status?: "loading" | "done";
  artifact_id?: string;
  artifact_type?: string;
  preview_kind?: string;
  html?: string;
  markdown?: string;
  download_url?: string;
  preview_url?: string;
  missing_fields?: string[];
  msg?: string;
  engine?: "react" | "plan_execute" | "static_plan" | "dag";
  router_reasons?: string[];
  tool_call_id?: string;
  name?: string;
  arguments?: Record<string, unknown>;
  message?: string;
  thread_sequence?: number;
  created_at?: string;
  action_key?: string;
}

type WorkspaceView = "tools" | "skills" | "tasks";
type ConversationDialog = { type: "rename" | "delete"; conversation: ConversationSummary } | null;

const sanitizeModelText = (text: string) => text
  .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
  .replace(/<\s*(?:img|svg)\b[^>]*>(?:[^]*?<\s*\/\s*svg\s*>)?/gi, "")
  .replace(/[\u2600-\u27BF\u2300-\u23FF\u200D\uFE0E\uFE0F]|[\uD800-\uDBFF][\uDC00-\uDFFF]/g, "");

const formatElapsed = (startedAt?: string, completedAt?: string | null, now = Date.now()) => {
  if (!startedAt) return "0s";
  const parseTimestamp = (value: string) => Date.parse(/[zZ]|[+-]\d{2}:\d{2}$/.test(value) ? value : `${value}Z`);
  const started = parseTimestamp(startedAt);
  const completed = completedAt ? parseTimestamp(completedAt) : now;
  const totalSeconds = Number.isFinite(started) && Number.isFinite(completed)
    ? Math.max(0, Math.floor((completed - started) / 1000))
    : 0;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
};

const processStatusLabel: Record<ProcessStatus, string> = {
  pending: "准备中",
  running: "处理中",
  awaiting_approval: "等待确认",
  completed: "已处理",
  failed: "处理失败",
  cancelled: "已中断",
};

function Icon({ name, className }: { name: "brand" | "userSystem" | "tools" | "task" | "upload" | "plus" | "back" | "send" | "stop" | "more" | "moreVertical" | "pin" | "rename" | "delete" | "package" | "chevron" | "file" | "search" | "close" | "fullscreen" | "restore" | "panelClose" | "retry"; className?: string }) {
  const paths: Record<typeof name, React.ReactNode> = {
    brand: <><path d="M12 3 4.5 7.2 12 11.5l7.5-4.3L12 3Z"/><path d="m4.5 12 7.5 4.3 7.5-4.3M4.5 16.8 12 21l7.5-4.2"/></>,
    userSystem: <><path d="M5 18c3.2-1.1 4.2-4.1 6.8-5.5 2.2-1.2 4.2-.8 7.2-2.5"/><path d="M7 7.5c2.1.1 3.5 1.1 4.8 3"/><circle cx="6.5" cy="7.5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="10" r="1.5"/><path d="M12 15v4"/></>,
    tools: <><path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4z"/><path d="M17 14v6m-3-3h6"/></>,
    task: <><path d="M5 5.5h14v10H9l-4 3v-13Z"/><path d="M9 9h6m-6 3h4"/></>,
    upload: <><path d="M12 16V4m-4 4 4-4 4 4"/><path d="M5 13v6h14v-6"/></>,
    plus: <><path d="M12 5v14M5 12h14"/></>,
    back: <><path d="m15 18-6-6 6-6"/></>,
    send: <><path d="m4 5 16 7-16 7 3-7-3-7Z"/><path d="M7 12h13"/></>,
    stop: <rect x="7" y="7" width="10" height="10" rx="1.5"/>,
    more: <><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></>,
    moreVertical: <><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></>,
    pin: <><path d="m9 4 6 6-2 2 3 4-1 1-4-3-2 2-1-1 8-8-1-1-2 2-4-3 1-1Z"/><path d="m8 16-4 4"/></>,
    rename: <><path d="M4 20h4l11-11-4-4L4 16v4Z"/><path d="m13 7 4 4"/></>,
    delete: <><path d="M5 7h14M9 7V4h6v3m2 0-1 13H8L7 7"/><path d="M10 11v5m4-5v5"/></>,
    package: <><path d="m4 7 8-4 8 4-8 4-8-4Z"/><path d="M4 7v10l8 4 8-4V7M12 11v10"/></>,
    chevron: <path d="m9 6 6 6-6 6"/>,
    file: <><path d="M6 3h8l4 4v14H6V3Z"/><path d="M14 3v5h5"/></>,
    search: <><circle cx="11" cy="11" r="6"/><path d="m16 16 4 4"/></>,
    close: <><path d="M6 6l12 12M18 6 6 18"/></>,
    fullscreen: <><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5"/></>,
    restore: <><path d="M9 4H4v5M15 20h5v-5"/><path d="M4 9l5-5M20 15l-5 5"/></>,
    panelClose: <><rect x="4" y="5" width="16" height="14" rx="3"/><path d="m12.5 9-3 3 3 3"/></>,
    retry: <><path d="M4 4v6h6"/><path d="M5.5 15a7 7 0 1 0 .8-7.7L4 10"/></>,
  };
  return <svg className={className} viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

interface ToolItem {
  id: string;
  name: string;
  category: "便携办公" | "AI问数" | "政务服务" | "企业服务" | "模型底座" | "开发工具";
  tier: "智能系统" | "AI应用" | "模型底座" | "开发工具";
  description: string;
  url: string;
  logo?: string;
  logoKind?: "image" | "mask";
  stage: string;
  tags: string[];
}

interface SkillItem {
  id: string;
  package: string;
  description: string;
  version: string;
  aliases: string[];
  tools: string[];
}

const compactFileName = (name: string, maxLength = 24) => {
  if (name.length <= maxLength) return name;
  const dot = name.lastIndexOf(".");
  const extension = dot > 0 ? name.slice(dot) : "";
  return `${name.slice(0, Math.max(8, maxLength - extension.length - 1))}…${extension}`;
};

const tools: ToolItem[] = [
  { id: "zhelixun", name: "浙里巡AI-巡视巡察辅助系统", category: "政务服务", tier: "智能系统", description: "面向巡视巡察业务提供智能辅助能力。", url: "http://10.126.20.144:3009", logo: "/tool-logos/zhelixun-mask.png", logoKind: "mask", stage: "对外交付", tags: ["巡察监督", "材料分析"] },
  { id: "xunqian-ai", name: "巡前AI共性问题分析", category: "政务服务", tier: "智能系统", description: "面向巡前业务归纳共性问题，辅助开展问题分析与研判。", url: "http://10.126.20.144:3010", logo: "/tool-logos/xunqian-ai.png", stage: "对外交付", tags: ["巡前分析", "问题研判"] },
  { id: "xiansuo-line", name: "AI辅助问题线索处置", category: "政务服务", tier: "智能系统", description: "智能研判问题线索并提供处置方式建议。", url: "http://10.126.20.144:3019/#/aiFileNew?idkey=1", logo: "/tool-logos/xiansuo-line.png", stage: "对外交付", tags: ["问题线索", "线索处置"] },
  { id: "ai-bidding", name: "AI招投标辅助系统", category: "企业服务", tier: "智能系统", description: "辅助招投标文档编制、内容检查与业务流程处理。", url: "http://10.126.20.144:3000", logo: "/tool-logos/ai-bidding.png", stage: "初步上线", tags: ["招投标", "文档辅助"] },
  { id: "zhicai-qianwen", name: "采购盘点合规督察", category: "AI问数", tier: "智能系统", description: "导入历史采购数据开展盘点分析，辅助采购合规督察与问答。", url: "http://10.126.20.4:7000/", logo: "/tool-logos/zhicai-qianwen.png", stage: "企业试用", tags: ["采购盘点", "合规督察"] },
  { id: "zhishu", name: "高速ETC问数", category: "AI问数", tier: "智能系统", description: "针对高速公路ETC业务数据提供指标查询与多维度分析问答。", url: "http://10.126.13.221:8006/", logo: "/tool-logos/zhishu.png", stage: "初步上线", tags: ["ETC问数", "数据分析"] },
  { id: "contract-review", name: "合同审查", category: "便携办公", tier: "AI应用", description: "对合同文本进行条款审查、风险提示与要点提取。", url: "http://10.126.13.221:1080/workflow/xzk9N5Mudak4kGAo", stage: "生产运行", tags: ["合同审查", "风险提示"] },
  { id: "resume-selection", name: "简历优选", category: "便携办公", tier: "AI应用", description: "辅助筛选和比较候选人简历，提高初步评估效率。", url: "http://10.126.13.149:1080/workflow/HtQwSc1REUGzKfYg", stage: "企业试用", tags: ["简历筛选", "人才评估"] },
  { id: "meeting-minutes", name: "音视频会议纪要", category: "便携办公", tier: "AI应用", description: "从音视频内容中提炼会议结论、重点议题和行动事项。", url: "http://10.126.13.149:1080/workflow/8GiH2o34nYKNjsJI", stage: "企业试用", tags: ["音视频", "会议纪要"] },
  { id: "file-extractor", name: "文件提取器", category: "便携办公", tier: "AI应用", description: "提取文件中的文本内容，快速整理为可继续处理的信息。", url: "http://10.126.13.149:1080/workflow/DfFclYuCDRTLn5hf", stage: "企业试用", tags: ["文本提取", "内容整理"] },
  { id: "ai-image", name: "AI成图", category: "便携办公", tier: "AI应用", description: "根据文字描述快速生成视觉图片，辅助内容创作与表达。", url: "http://10.126.13.149:1080/workflow/qcOFl52fzWuOwpyI", stage: "企业试用", tags: ["图像生成", "创意设计"] },
  { id: "luba-chat", name: "鹿宝智能对话助手", category: "便携办公", tier: "AI应用", description: "面向日常办公场景提供连续、便捷的智能问答服务。", url: "http://10.126.13.149:1080/chat/XUDCGUhoBpVBVmxm", stage: "企业试用", tags: ["智能问答", "办公助手"] },
  { id: "workflow-cf0pmi", name: "写作搭档", category: "便携办公", tier: "AI应用", description: "通过预设智能工作流处理业务任务。", url: "http://10.126.13.149:1080/workflow/cF0pmIpXDclZU8NJ", stage: "企业试用", tags: ["智能工作流", "AI应用"] },
  { id: "mineru-gateway", name: "MinerU Gateway", category: "模型底座", tier: "模型底座", description: "提供多格式文档解析、任务管理与按页计费的统一网关服务。", url: "http://10.126.13.2:6017/", logo: "/tool-logos/mineru-gateway.svg", stage: "高频开发", tags: ["文档解析", "计费网关"] },
  { id: "new-api", name: "New API", category: "模型底座", tier: "模型底座", description: "统一管理模型渠道、接口调用与使用额度。", url: "http://10.126.13.149:12945/console", logo: "/tool-logos/new-api.png", stage: "高频开发", tags: ["模型接口", "渠道管理"] },
  { id: "epai", name: "EPAI", category: "模型底座", tier: "模型底座", description: "提供企业级AI能力接入与统一服务管理。", url: "https://10.126.13.2:32206/#/login", logo: "/tool-logos/epai.svg", stage: "高频开发", tags: ["AI平台", "统一接入"] },
  { id: "gpustack", name: "GPUStack", category: "模型底座", tier: "模型底座", description: "管理GPU资源、模型部署与推理服务。", url: "http://10.126.13.221:890/#/login", logo: "/tool-logos/gpustack.png", stage: "高频开发", tags: ["GPU管理", "模型部署"] },
  { id: "dify", name: "Dify", category: "开发工具", tier: "开发工具", description: "开发、编排和管理大模型应用与工作流。", url: "http://10.126.13.221:1080/apps", logo: "/tool-logos/dify.ico", stage: "高频开发", tags: ["应用开发", "工作流"] },
  { id: "ai-knowledge-base", name: "AI知识库", category: "开发工具", tier: "开发工具", description: "提供资料入库、智能检索与知识内容管理能力。", url: "http://10.126.20.144:8000/", logo: "/tool-logos/ai-knowledge-base.svg", stage: "高频开发", tags: ["知识库", "智能检索"] },
  { id: "opencode", name: "opencode", category: "开发工具", tier: "开发工具", description: "开源的 AI 编程智能体终端（类 Claude Code）。", url: "http://10.126.33.14:14096", logo: "/tool-logos/opencode.png", stage: "高频开发", tags: ["AI编程", "开发助手"] },
];

const toolCategories = ["全部", "便携办公", "AI问数", "政务服务", "企业服务", "模型底座", "开发工具"] as const;
const toolTiers: ToolItem["tier"][] = ["智能系统", "AI应用", "模型底座", "开发工具"];

const toArtifact = (artifact: StoredArtifact): ArtifactView => ({
  artifactId: artifact.artifact_id,
  artifactType: artifact.artifact_type,
  title: artifact.title,
  previewKind: artifact.preview_kind,
  html: artifact.html,
  markdown: artifact.markdown,
  text: artifact.text,
  downloadUrl: artifact.download_url,
  previewUrl: artifact.preview_url,
  missingFields: artifact.missing_fields,
});

function renderInlineMarkdown(text: string, keyPrefix: string): React.ReactNode[] {
  const pattern = /(\*\*[^*\n]+\*\*|__[^_\n]+__|``[^`\n]+``|`[^`\n]+`|\[[^\]\n]+\]\(https?:\/\/[^)\s]+\))/g;
  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index));
    const token = match[0];
    const key = `${keyPrefix}-${match.index}`;
    if ((token.startsWith("**") && token.endsWith("**")) || (token.startsWith("__") && token.endsWith("__"))) {
      nodes.push(<strong key={key}>{renderInlineMarkdown(token.slice(2, -2), `${key}-strong`)}</strong>);
    } else if (token.startsWith("`") && token.endsWith("`")) {
      const delimiterLength = token.startsWith("``") ? 2 : 1;
      nodes.push(<code key={key}>{token.slice(delimiterLength, -delimiterLength)}</code>);
    } else {
      const link = token.match(/^\[(.+?)\]\((https?:\/\/[^\s)]+)\)$/);
      if (link) nodes.push(<a key={key} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>);
    }
    cursor = pattern.lastIndex;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

function CodeBlock({ code, language }: { code: string; language: string }) {
  const [copied, setCopied] = useState(false);
  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch (error) {
      console.error("复制代码失败", error);
    }
  };
  return (
    <div className={styles.codeBlock}>
      <div className={styles.codeBlockHeader}>
        <span>{language || "text"}</span>
        <button type="button" onClick={copyCode} aria-label="复制代码">{copied ? "已复制" : "复制"}</button>
      </div>
      <pre><code className={language ? `language-${language}` : undefined}>{code}</code></pre>
    </div>
  );
}

function MarkdownContent({ content }: { content: string }) {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const nodes: React.ReactNode[] = [];
  let listItems: { ordered: boolean; text: string }[] = [];
  let codeLines: string[] | null = null;
  let codeLanguage = "";
  let tableLines: string[] = [];

  const flushList = () => {
    if (!listItems.length) return;
    const ordered = listItems[0].ordered;
    const Tag = ordered ? "ol" : "ul";
    nodes.push(<Tag key={`list-${nodes.length}`}>{listItems.map((item, index) => <li key={index}>{renderInlineMarkdown(item.text, `list-${nodes.length}-${index}`)}</li>)}</Tag>);
    listItems = [];
  };

  const flushTable = () => {
    if (!tableLines.length) return;
    const rows = tableLines
      .filter((line, index) => index !== 1 || !/^\s*\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?\s*$/.test(line))
      .map(line => line.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim()));
    if (rows.length) {
      nodes.push(
        <div className={styles.markdownTableWrap} key={`table-${nodes.length}`}>
          <table>
            <thead><tr>{rows[0].map((cell, index) => <th key={index}>{renderInlineMarkdown(cell, `table-head-${index}`)}</th>)}</tr></thead>
            {rows.length > 1 && <tbody>{rows.slice(1).map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{renderInlineMarkdown(cell, `table-${rowIndex}-${cellIndex}`)}</td>)}</tr>)}</tbody>}
          </table>
        </div>,
      );
    }
    tableLines = [];
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim().startsWith("|") && line.trim().endsWith("|")) {
      flushList();
      tableLines.push(line);
      continue;
    }
    flushTable();
    const codeFence = line.match(/^\s*```([^\s`]*)\s*$/);
    if (codeFence) {
      flushList();
      if (codeLines === null) {
        codeLines = [];
        codeLanguage = codeFence[1].trim().toLowerCase();
      }
      else {
        nodes.push(<CodeBlock key={`code-${index}`} code={codeLines.join("\n")} language={codeLanguage} />);
        codeLines = null;
        codeLanguage = "";
      }
      continue;
    }
    if (codeLines !== null) {
      codeLines.push(line);
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (heading) {
      flushList();
      const level = heading[1].length;
      const Tag = (`h${level}` as "h1" | "h2" | "h3");
      nodes.push(<Tag key={`heading-${index}`}>{renderInlineMarkdown(heading[2], `heading-${index}`)}</Tag>);
    } else if (bullet || ordered) {
      const item = { ordered: Boolean(ordered), text: (bullet || ordered)?.[1] || "" };
      if (listItems.length && listItems[0].ordered !== item.ordered) flushList();
      listItems.push(item);
    } else {
      flushList();
      if (/^(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) nodes.push(<hr key={`rule-${index}`} />);
      else if (line.startsWith(">")) nodes.push(<blockquote key={`quote-${index}`}>{renderInlineMarkdown(line.replace(/^>\s?/, ""), `quote-${index}`)}</blockquote>);
      else if (line.trim()) nodes.push(<p key={`paragraph-${index}`}>{renderInlineMarkdown(line, `paragraph-${index}`)}</p>);
    }
  }
  flushList();
  flushTable();
  if (codeLines !== null) nodes.push(<CodeBlock key="code-final" code={codeLines.join("\n")} language={codeLanguage} />);
  return <div className={styles.markdownContent}>{nodes}</div>;
}

export default function Home() {
  const [files, setFiles] = useState<File[]>([]);
  const [contextText, setContextText] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [isLoadingConversations, setIsLoadingConversations] = useState(true);
  const [isProcessing, setIsProcessing] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [inputValue, setInputValue] = useState("");
  const [currentConversationId, setCurrentConversationId] = useState<string>(createConversationId);
  const [activePreviewType, setActivePreviewType] = useState("text");
  const [activePreviewContent, setActivePreviewContent] = useState("");
  const [activePreviewUrl, setActivePreviewUrl] = useState("");
  const [activePreviewTitle, setActivePreviewTitle] = useState("");
  const [isPreviewOpen, setIsPreviewOpen] = useState(false);
  const [isPreviewFullscreen, setIsPreviewFullscreen] = useState(false);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView>("tasks");
  const [selectedTool, setSelectedTool] = useState<ToolItem | null>(null);
  const [toolQuery, setToolQuery] = useState("");
  const [toolCategory, setToolCategory] = useState<(typeof toolCategories)[number]>("全部");
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [skillQuery, setSkillQuery] = useState("");
  const [isLoadingSkills, setIsLoadingSkills] = useState(false);
  const [isUploadingSkill, setIsUploadingSkill] = useState(false);
  const [skillMessage, setSkillMessage] = useState("");
  const [skillMessageIsError, setSkillMessageIsError] = useState(false);
  const [selectedSkill, setSelectedSkill] = useState<SkillItem | null>(null);
  const [attachmentMenu, setAttachmentMenu] = useState<"root" | "skills" | null>(null);
  const [openConversationMenu, setOpenConversationMenu] = useState<string | null>(null);
  const [conversationMenuPosition, setConversationMenuPosition] = useState<{ top: number; left: number } | null>(null);
  const [brandText, setBrandText] = useState("");
  const [conversationDialog, setConversationDialog] = useState<ConversationDialog>(null);
  const [dialogTitle, setDialogTitle] = useState("");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [pendingApproval, setPendingApproval] = useState<ApprovalView | null>(null);
  const [expandedProcessId, setExpandedProcessId] = useState<string | null>(null);
  const [elapsedNow, setElapsedNow] = useState(Date.now());
  const chatContainerRef = useRef<HTMLDivElement>(null);
  const chatAutoScrollRef = useRef(true);
  const activeAssistantMessageIdRef = useRef<string | null>(null);
  const currentConversationIdRef = useRef(currentConversationId);
  const activeRunConversationIdRef = useRef<string | null>(null);
  const activeRunMessagesRef = useRef<Message[]>([]);
  const activeStreamControllerRef = useRef<AbortController | null>(null);
  const processPanelRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const historyMenuAnchorRef = useRef<HTMLButtonElement | null>(null);
  const processAutoScrollRef = useRef<Record<string, boolean>>({});
  const textInputRef = useRef<HTMLTextAreaElement>(null);
  const skillUploadRef = useRef<HTMLInputElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const conversationRequestRef = useRef<Promise<void> | null>(null);
  const skillRequestRef = useRef<Promise<void> | null>(null);
  const skillsLoadedAtRef = useRef(0);

  const refreshConversations = async () => {
    if (conversationRequestRef.current) return conversationRequestRef.current;
    setIsLoadingConversations(true);
    const request = (async () => {
      try {
        const response = await fetchApiWithRetry("/api/v1/conversations");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        setConversations(await response.json());
      } catch (error) {
        console.error("加载对话列表失败", error);
      } finally {
        setIsLoadingConversations(false);
        conversationRequestRef.current = null;
      }
    })();
    conversationRequestRef.current = request;
    return request;
  };

  const refreshSkills = async (force = false) => {
    if (!force && skills.length && Date.now() - skillsLoadedAtRef.current < 60_000) return;
    if (skillRequestRef.current) {
      await skillRequestRef.current;
      if (!force) return;
    }
    setIsLoadingSkills(true);
    const request = (async () => {
      try {
        const response = await fetchApiWithRetry("/api/v1/skills");
        if (!response.ok) throw new Error(`加载失败 (HTTP ${response.status})`);
        const payload = await response.json() as { skills: SkillItem[] };
        setSkills(payload.skills);
        skillsLoadedAtRef.current = Date.now();
      } catch (error) {
        setSkillMessageIsError(true);
        setSkillMessage(error instanceof Error ? error.message : "技能列表加载失败");
      } finally {
        setIsLoadingSkills(false);
        skillRequestRef.current = null;
      }
    })();
    skillRequestRef.current = request;
    return request;
  };

  const openSkillPlaza = () => {
    setWorkspaceView("skills");
    setSkillMessage("");
    setSkillMessageIsError(false);
    void refreshSkills();
  };

  const uploadSkill = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = Array.from(event.target.files || []);
    event.target.value = "";
    if (!selectedFiles.length) return;
    setIsUploadingSkill(true);
    setSkillMessage("");
    setSkillMessageIsError(false);
    try {
      const formData = new FormData();
      formData.append("file", selectedFiles[0]);
      const response = await fetch(apiUrl("/api/v1/skills/upload"), { method: "POST", body: formData });
      const payload = await response.json() as { detail?: string; skill?: SkillItem };
      if (!response.ok) throw new Error(payload.detail || `上传失败 (HTTP ${response.status})`);
      setSkillMessage(`预检通过，技能 ${payload.skill?.id || selectedFiles[0].name} 已导入`);
      await refreshSkills(true);
    } catch (error) {
      setSkillMessageIsError(true);
      setSkillMessage(error instanceof Error ? error.message : "技能上传失败");
    } finally {
      setIsUploadingSkill(false);
    }
  };

  useEffect(() => {
    void refreshConversations();
    const preloadTimer = window.setTimeout(() => { void refreshSkills(); }, 400);
    const refreshAfterIdle = () => {
      if (document.visibilityState === "visible") void refreshConversations();
    };
    window.addEventListener("pageshow", refreshAfterIdle);
    document.addEventListener("visibilitychange", refreshAfterIdle);
    return () => {
      window.clearTimeout(preloadTimer);
      window.removeEventListener("pageshow", refreshAfterIdle);
      document.removeEventListener("visibilitychange", refreshAfterIdle);
    };
  }, []);

  useEffect(() => {
    currentConversationIdRef.current = currentConversationId;
  }, [currentConversationId]);

  useEffect(() => {
    const fullText = "让交通数据更清晰，让智能协作更高效。";
    let index = 0;
    const timer = window.setInterval(() => {
      index += 1;
      setBrandText(fullText.slice(0, index));
      if (index >= fullText.length) window.clearInterval(timer);
    }, 76);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!isProcessing) return;
    const timer = window.setInterval(() => setElapsedNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isProcessing]);

  useEffect(() => {
    if (!chatAutoScrollRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      const container = chatContainerRef.current;
      if (container) container.scrollTop = container.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages]);

  useEffect(() => {
    const textArea = textInputRef.current;
    if (!textArea) return;
    textArea.style.height = "auto";
    const computedStyle = window.getComputedStyle(textArea);
    const lineHeight = Number.parseFloat(computedStyle.lineHeight) || 24;
    const verticalPadding = Number.parseFloat(computedStyle.paddingTop) + Number.parseFloat(computedStyle.paddingBottom);
    const singleLineHeight = lineHeight + verticalPadding;
    const maxHeight = lineHeight * 10 + verticalPadding;
    textArea.style.height = `${Math.min(textArea.scrollHeight, maxHeight)}px`;
    textArea.style.overflowY = textArea.scrollHeight > maxHeight ? "auto" : "hidden";
    textArea.dataset.expanded = textArea.scrollHeight > singleLineHeight + 1 ? "true" : "false";
  }, [inputValue]);

  useEffect(() => {
    if (!isPreviewFullscreen) return;
    const exitFullscreen = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsPreviewFullscreen(false);
    };
    window.addEventListener("keydown", exitFullscreen);
    return () => window.removeEventListener("keydown", exitFullscreen);
  }, [isPreviewFullscreen]);

  const startNewChat = () => {
    if (isProcessing) return;
    chatAutoScrollRef.current = true;
    setMessages([]);
    setContextText("");
    setFiles([]);
    setInputValue("");
    setIsPreviewOpen(false);
    setIsPreviewFullscreen(false);
    const conversationId = createConversationId();
    currentConversationIdRef.current = conversationId;
    setCurrentConversationId(conversationId);
    setSelectedTool(null);
    setPendingApproval(null);
    setWorkspaceView("tasks");
    setExpandedProcessId(null);
    activeAssistantMessageIdRef.current = null;
  };

  const loadConversation = async (conversationId: string) => {
    setWorkspaceView("tasks");
    if (conversationId === currentConversationIdRef.current) return;
    if (isProcessing && conversationId === activeRunConversationIdRef.current && activeRunMessagesRef.current.length) {
      currentConversationIdRef.current = conversationId;
      setCurrentConversationId(conversationId);
      setMessages(structuredClone(activeRunMessagesRef.current));
      setContextText("");
      setFiles([]);
      setIsPreviewOpen(false);
      setSelectedTool(null);
      setExpandedProcessId(activeAssistantMessageIdRef.current);
      chatAutoScrollRef.current = true;
      scrollChatAfterUpdate(true);
      return;
    }
    setIsLoadingHistory(true);
    chatAutoScrollRef.current = true;
    try {
      const response = await fetchApiWithRetry(`/api/v1/conversations/${conversationId}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const stored = await response.json() as StoredConversation;
      setPendingApproval(stored.pending_approval ? {
        runId: stored.pending_approval.run_id,
        toolCallId: stored.pending_approval.tool_call_id,
        name: stored.pending_approval.name,
        arguments: stored.pending_approval.arguments || {},
        message: stored.pending_approval.message || "请确认是否继续执行。",
      } : null);
      setMessages(stored.messages.map((message): Message => {
        if (message.role === "user") {
          return {
            id: message.id,
            type: "user",
            text: message.content,
            attachedFile: message.attachment_name,
            attachedFiles: message.attachment_names?.length ? message.attachment_names : message.attachment_name?.split("、").filter(Boolean) || [],
          };
        }
        const process = message.process ? {
          runId: message.process.run_id,
          status: message.process.status,
          startedAt: message.process.started_at,
          completedAt: message.process.completed_at,
        } : undefined;
        return {
          id: message.id,
          type: process?.status === "failed" || process?.status === "cancelled" ? "error" : "ai",
          text: message.content,
          groups: message.groups || [],
          artifacts: message.artifacts.map(toArtifact),
          process,
        };
      }));
      currentConversationIdRef.current = stored.id;
      setCurrentConversationId(stored.id);
      setContextText("");
      setFiles([]);
      setIsPreviewOpen(false);
      setSelectedTool(null);
      setWorkspaceView("tasks");
      setExpandedProcessId(null);
    } catch (error) {
      console.error("加载对话失败", error);
    } finally {
      setIsLoadingHistory(false);
    }
  };

  const uploadAndParseFile = async (selectedFile: File, signal?: AbortSignal): Promise<{ text: string; attachmentId: string }> => {
    const formData = new FormData();
    formData.append("file", selectedFile);
    formData.append("conversation_id", currentConversationId);
    const response = await fetch(apiUrl("/upload"), { method: "POST", body: formData, signal });
    if (!response.ok) {
      let detail = "文件解析失败";
      try {
        const errorData = await response.json();
        detail = errorData.detail || detail;
      } catch {
        // Keep the fallback message when the server does not return JSON.
      }
      throw new Error(`${detail} (HTTP ${response.status})`);
    }
    const payload = await response.json();
    const results = payload?.result?.results;
    const firstResult = results && typeof results === "object"
      ? Object.values(results)[0] as { md_content?: string } | undefined
      : undefined;
    const extractedText = payload?.extracted_text || firstResult?.md_content;
    if (!extractedText) throw new Error("文件中没有可读取的文本内容");
    if (!payload?.attachment_id) throw new Error("文件已解析但未建立附件记录");
    return { text: extractedText, attachmentId: payload.attachment_id };
  };

  const updateConversationState = async (conversationId: string, state: Partial<Pick<ConversationSummary, "is_archived" | "is_pinned">>) => {
    const response = await fetch(apiUrl(`/api/v1/conversations/${conversationId}/state`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state),
    });
    if (!response.ok) throw new Error(`更新对话状态失败 (HTTP ${response.status})`);
    if (state.is_archived && conversationId === currentConversationId) startNewChat();
    await refreshConversations();
  };

  const ensureGroup = (message: Message): TaskGroup => {
    message.groups ??= [];
    if (message.groups.length === 0) {
      message.groups.push({
        id: `${Date.now()}-${Math.random()}`,
        title: "任务执行",
        thoughts: [],
        actions: [],
      });
    }
    return message.groups[message.groups.length - 1];
  };

  const scrollChatAfterUpdate = (force = false) => {
    const container = chatContainerRef.current;
    if (!container) return;
    if (force) chatAutoScrollRef.current = true;
    if (!chatAutoScrollRef.current) return;
    window.requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
    });
  };

  const toggleProcess = (messageId: string) => {
    setExpandedProcessId(current => current === messageId ? null : messageId);
  };

  const scrollProcessAfterUpdate = (messageId: string) => {
    if (processAutoScrollRef.current[messageId] === false) return;
    window.requestAnimationFrame(() => {
      const panel = processPanelRefs.current[messageId];
      if (panel) panel.scrollTo({ top: panel.scrollHeight, behavior: "smooth" });
    });
  };

  const updateActiveMessages = (update: (messages: Message[]) => Message[]) => {
    const next = update(activeRunMessagesRef.current);
    activeRunMessagesRef.current = next;
    if (currentConversationIdRef.current === activeRunConversationIdRef.current) {
      setMessages(next);
    }
  };

  const activeConversationIsVisible = () =>
    currentConversationIdRef.current === activeRunConversationIdRef.current && workspaceView === "tasks";

  const applyStreamEvent = (event: StreamEvent) => {
    const assistantId = activeAssistantMessageIdRef.current;
    if (event.type === "skills_changed") {
      skillsLoadedAtRef.current = 0;
      void refreshSkills(true);
      return;
    }
    if (event.type === "run_started" && event.run_id) {
      setActiveRunId(event.run_id);
      setElapsedNow(Date.now());
      if (assistantId) setExpandedProcessId(assistantId);
      updateActiveMessages(previous => {
        const next = structuredClone(previous);
        const assistant = next.find(message => message.id === assistantId);
        if (assistant) {
          assistant.process = {
            runId: event.run_id!,
            status: "running",
            startedAt: assistant.process?.startedAt || new Date().toISOString(),
          };
        }
        return next;
      });
      if (activeConversationIsVisible()) scrollChatAfterUpdate(true);
      return;
    }
    if (event.type === "approval_required" && event.run_id && event.tool_call_id && event.name) {
      const argumentsValue = event.arguments || {};
      const confirmationText = typeof argumentsValue.confirmation_summary === "string"
        ? argumentsValue.confirmation_summary
        : event.message || "请确认是否继续执行。";
      if (currentConversationIdRef.current === activeRunConversationIdRef.current) {
        setPendingApproval({
          runId: event.run_id,
          toolCallId: event.tool_call_id,
          name: event.name,
          arguments: argumentsValue,
          message: event.message || "该工具需要你的确认。",
        });
      }
      updateActiveMessages(previous => previous.map(message => message.id === assistantId ? {
        ...message,
        type: "ai",
        text: confirmationText,
        process: message.process ? { ...message.process, status: "awaiting_approval" } : message.process,
      } : message));
      setIsProcessing(false);
      if (activeConversationIsVisible()) scrollChatAfterUpdate(true);
      return;
    }
    const groupId = event.type === "task_group"
      ? `${event.run_id || "run"}-${event.thread_sequence ?? Date.now()}`
      : null;
    if (groupId && assistantId) setExpandedProcessId(assistantId);
    if (event.type === "content" || event.type === "content_delta" || event.type === "error") {
      setExpandedProcessId(current => current === assistantId ? null : current);
    }
    updateActiveMessages(previous => {
      const next = structuredClone(previous);
      const assistant = next.find(message => message.id === assistantId);
      if (!assistant || (assistant.type !== "ai-stream" && assistant.type !== "ai")) return previous;

      if (event.type === "task_group") {
        assistant.hasFinalOutput = false;
        assistant.groups ??= [];
        assistant.groups.push({
          id: groupId!,
          title: event.title || "任务执行",
          thoughts: [],
          actions: [],
          started_at: event.created_at,
        });
      } else if (event.type === "thought") {
        assistant.hasFinalOutput = false;
        const group = ensureGroup(assistant);
        if (event.text) group.thoughts.push(event.text);
      } else if (event.type === "thought_delta") {
        assistant.hasFinalOutput = false;
        const group = ensureGroup(assistant);
        if (event.text) {
          const thoughtIndex = group.thoughts.length - 1;
          if (thoughtIndex >= 0) group.thoughts[thoughtIndex] += event.text;
          else group.thoughts.push(event.text);
        }
      } else if (event.type === "action") {
        assistant.hasFinalOutput = false;
        const group = ensureGroup(assistant);
        const status = event.status || "done";
        let updated = false;
        if (status === "done" && event.action_key) {
          for (let index = group.actions.length - 1; index >= 0; index -= 1) {
            const action = group.actions[index];
            if (action.actionKey === event.action_key && action.status === "loading") {
              action.status = "done";
              if (event.text) action.text = event.text;
              updated = true;
              break;
            }
          }
        }
        if (!updated) group.actions.push({ text: event.text || "执行工具", status, actionKey: event.action_key });
      } else if (event.type === "content") {
        assistant.hasFinalOutput = true;
        const lastGroup = assistant.groups?.[assistant.groups.length - 1];
        if (lastGroup && lastGroup.thoughts.length === 0 && lastGroup.actions.length === 0) {
          assistant.groups?.pop();
        }
        if (assistant.process) {
          assistant.process.status = "completed";
          assistant.process.completedAt = event.created_at || new Date().toISOString();
        }
        if (event.text) assistant.text = sanitizeModelText(event.text);
        if (event.artifact_id || event.html || event.markdown) {
          assistant.artifacts ??= [];
          const artifact: ArtifactView = {
            artifactId: event.artifact_id,
            artifactType: event.artifact_type || "artifact",
            title: event.title || "任务产物",
            previewKind: event.preview_kind || (event.html ? "html" : "markdown"),
            html: event.html,
            markdown: event.markdown,
            downloadUrl: event.download_url,
            previewUrl: event.preview_url,
            missingFields: event.missing_fields,
          };
          const existingIndex = assistant.artifacts.findIndex(item => item.artifactId && item.artifactId === artifact.artifactId);
          if (existingIndex >= 0) assistant.artifacts[existingIndex] = artifact;
          else assistant.artifacts.push(artifact);
        }
      } else if (event.type === "content_delta") {
        assistant.hasFinalOutput = true;
        if (event.text) assistant.text = `${assistant.text || ""}${sanitizeModelText(event.text)}`;
      } else if (event.type === "turn_delta") {
        const group = ensureGroup(assistant);
        if (event.text) group.draftThought = `${group.draftThought || ""}${sanitizeModelText(event.text)}`;
      } else if (event.type === "turn_commit") {
        const group = ensureGroup(assistant);
        const text = sanitizeModelText(event.text || group.draftThought || "");
        if (text) group.thoughts.push(text);
        group.draftThought = "";
      } else if (event.type === "turn_clear") {
        ensureGroup(assistant).draftThought = "";
      } else if (event.type === "content_reset") {
        assistant.hasFinalOutput = false;
        assistant.text = "";
      } else if (event.type === "error") {
        if (assistant.process) {
          assistant.process.status = event.msg === "任务已取消。" ? "cancelled" : "failed";
          assistant.process.completedAt = event.created_at || new Date().toISOString();
        }
        assistant.type = "error";
        assistant.text = event.msg || "任务执行失败";
      }
      return next;
    });
    if (assistantId && activeConversationIsVisible()) scrollProcessAfterUpdate(assistantId);
    if (activeConversationIsVisible()) scrollChatAfterUpdate();
  };

  const handleFormSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const typedInput = inputValue.trim();
    if (pendingApproval) {
      if (!typedInput || isProcessing) return;
      await respondToApproval(typedInput);
      return;
    }
    const query = typedInput || (files.length ? "请阅读附件并根据内容完成合适的任务。" : "");
    if (!query || isProcessing) return;

    const currentFiles = files;
    const currentSkill = selectedSkill;
    const oversizedFile = currentFiles.find(selectedFile => selectedFile.size > MAX_UPLOAD_FILE_BYTES);
    if (oversizedFile) {
      setMessages(previous => [
        ...previous,
        { type: "error", text: `附件 ${oversizedFile.name} 超过 100MB 上传上限` },
      ]);
      return;
    }
    const attachmentName = currentFiles.map(selectedFile => selectedFile.name).join("、").slice(0, 200);
    const assistantMessageId = createConversationId();
    const runConversationId = currentConversationId;
    const streamController = new AbortController();
    const optimisticConversation: ConversationSummary = {
      id: currentConversationId,
      title: query.slice(0, 48) || "新对话",
      last_message: query.slice(0, 80),
      updated_at: new Date().toISOString(),
      is_archived: false,
      is_pinned: false,
    };
    setConversations(previous => [
      optimisticConversation,
      ...previous.filter(conversation => conversation.id !== currentConversationId),
    ]);
    activeAssistantMessageIdRef.current = assistantMessageId;
    activeRunConversationIdRef.current = runConversationId;
    activeStreamControllerRef.current = streamController;
    setInputValue("");
    setFiles([]);
    setSelectedSkill(null);
    setAttachmentMenu(null);
    setIsProcessing(true);
    setIsCancelling(false);
    setActiveRunId(null);
    setExpandedProcessId(assistantMessageId);
    const initialMessages: Message[] = [
      ...messages,
      { id: createConversationId(), type: "user", text: query, attachedFile: attachmentName || null, attachedFiles: currentFiles.map(file => file.name) },
      {
        id: assistantMessageId,
        type: "ai-stream",
        groups: [],
        artifacts: [],
        process: { runId: "", status: "pending", startedAt: new Date().toISOString() },
      },
    ];
    activeRunMessagesRef.current = initialMessages;
    setMessages(initialMessages);
    scrollChatAfterUpdate(true);

    let currentContext = contextText;
    const attachmentIds: string[] = [];
    if (currentFiles.length) {
      updateActiveMessages(previous => previous.map(message => message.id === assistantMessageId && message.process
        ? { ...message, process: { ...message.process, status: "running" } }
        : message));
      try {
        const uploadedTexts: string[] = [];
        for (const [fileIndex, currentFile] of currentFiles.entries()) {
          const actionKey = `parse-attachment-${fileIndex}`;
          applyStreamEvent({
            type: "task_group",
            run_id: `upload-${assistantMessageId}`,
            thread_sequence: fileIndex,
            title: "正在处理附件",
          });
          applyStreamEvent({ type: "action", text: `正在解析附件：${currentFile.name}`, status: "loading", action_key: actionKey });
          const upload = await uploadAndParseFile(currentFile, streamController.signal);
          uploadedTexts.push(`## 附件：${currentFile.name}\n\n${upload.text}`);
          attachmentIds.push(upload.attachmentId);
          applyStreamEvent({ type: "action", text: `已解析附件：${currentFile.name}`, status: "done", action_key: actionKey });
        }
        currentContext = [contextText, ...uploadedTexts].filter(Boolean).join("\n\n---\n\n");
        setContextText(currentContext);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          if (activeStreamControllerRef.current === streamController) activeStreamControllerRef.current = null;
          setIsProcessing(false);
          setIsCancelling(false);
          setActiveRunId(null);
          return;
        }
        const detail = error instanceof Error ? error.message : "未知错误";
        updateActiveMessages(previous => {
          const next = structuredClone(previous);
          const assistantIndex = next.findIndex(message => message.id === assistantMessageId);
          if (assistantIndex >= 0) next[assistantIndex] = { id: assistantMessageId, type: "error", text: `附件解析失败：${detail}` };
          return next;
        });
        if (activeStreamControllerRef.current === streamController) activeStreamControllerRef.current = null;
        setIsProcessing(false);
        return;
      }
    }

    try {
      const response = await fetch(apiUrl("/chat/stream"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          context_text: attachmentIds.length ? contextText : currentContext,
          conversation_id: runConversationId,
          attachment_name: attachmentName || null,
          attachment_id: null,
          attachment_ids: attachmentIds,
          selected_skill_ids: currentSkill ? [currentSkill.id] : [],
        }),
        signal: streamController.signal,
      });
      if (!response.ok) throw new Error(`后端请求失败 (HTTP ${response.status})`);
      await consumeEventStream(response);

      updateActiveMessages(previous => {
        const next = structuredClone(previous);
        const assistant = next.find(message => message.id === assistantMessageId);
        if (assistant?.type === "ai-stream") {
          assistant.type = "ai";
          assistant.text ||= assistant.artifacts?.length
            ? "任务已完成，生成的产物可在下方查看。"
            : "模型没有返回有效内容，请重试。";
          if (assistant.process && assistant.process.status !== "awaiting_approval") {
            assistant.process.status = "completed";
            assistant.process.completedAt ||= new Date().toISOString();
          }
        }
        return next;
      });
      setExpandedProcessId(current => current === assistantMessageId ? null : current);
      await refreshConversations();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      updateActiveMessages(previous => {
        const next = structuredClone(previous);
        const assistantIndex = next.findIndex(message => message.id === assistantMessageId);
        if (assistantIndex >= 0) next[assistantIndex] = { id: assistantMessageId, type: "error", text: `通信失败：${String(error)}` };
        return next;
      });
      } finally {
        if (activeStreamControllerRef.current === streamController) activeStreamControllerRef.current = null;
        setIsProcessing(false);
        setIsCancelling(false);
        setActiveRunId(null);
        scrollChatAfterUpdate();
      }
  };

  const cancelActiveRun = () => {
    if (!isProcessing || isCancelling) return;
    const runId = activeRunId;
    const assistantId = activeAssistantMessageIdRef.current;
    setIsCancelling(true);
    activeStreamControllerRef.current?.abort();
    updateActiveMessages(previous => previous.map(message => message.id === assistantId ? {
      ...message,
      type: "error",
      text: "任务已取消。",
      process: message.process ? {
        ...message.process,
        status: "cancelled",
        completedAt: new Date().toISOString(),
      } : message.process,
    } : message));
    setIsProcessing(false);
    setActiveRunId(null);
    setExpandedProcessId(current => current === assistantId ? null : current);
    if (!runId) {
      setIsCancelling(false);
      return;
    }
    void fetch(apiUrl(`/api/v1/runs/${runId}/cancel`), { method: "POST", keepalive: true })
      .then(response => {
        if (!response.ok && response.status !== 409) throw new Error(`中断失败 (HTTP ${response.status})`);
      })
      .catch(error => console.error("中断任务失败", error));
    setIsCancelling(false);
  };

  const consumeEventStream = async (response: Response) => {
    if (!response.body) throw new Error("后端未返回数据流");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split(/\r?\n\r?\n/);
      buffer = chunks.pop() ?? "";
      for (const chunk of chunks) {
        const dataLine = chunk.split(/\r?\n/).find(line => line.startsWith("data: "));
        if (!dataLine) continue;
        try {
          applyStreamEvent(JSON.parse(dataLine.slice(6)) as StreamEvent);
        } catch (error) {
          console.error("无法解析流事件", error);
        }
      }
    }
  };

  const retryRun = async (sourceMessageId: string, sourceRunId: string) => {
    if (!sourceMessageId || !sourceRunId || isProcessing) return;
    const assistantMessageId = sourceMessageId;
    const runConversationId = currentConversationId;
    const streamController = new AbortController();
    activeAssistantMessageIdRef.current = assistantMessageId;
    activeRunConversationIdRef.current = runConversationId;
    activeStreamControllerRef.current = streamController;
    setWorkspaceView("tasks");
    setIsProcessing(true);
    setIsCancelling(false);
    setActiveRunId(null);
    setExpandedProcessId(assistantMessageId);
    const replacement: Message = {
        id: assistantMessageId,
        type: "ai-stream",
        groups: [],
        artifacts: [],
        process: { runId: "", status: "pending", startedAt: new Date().toISOString() },
      };
    const initialMessages = messages.map(message => message.id === sourceMessageId ? replacement : message);
    activeRunMessagesRef.current = initialMessages;
    setMessages(initialMessages);
    scrollChatAfterUpdate(true);
    try {
      const response = await fetch(apiUrl(`/api/v1/runs/${sourceRunId}/retry`), { method: "POST", signal: streamController.signal });
      if (!response.ok) throw new Error(`重试失败 (HTTP ${response.status})`);
      await consumeEventStream(response);
      updateActiveMessages(previous => previous.map(message => {
        if (message.id !== assistantMessageId || message.type !== "ai-stream") return message;
        return {
          ...message,
          type: "ai",
          text: message.text || (message.artifacts?.length
            ? "任务已完成，生成的产物可在下方查看。"
            : "模型没有返回有效内容，请重试。"),
          process: message.process && message.process.status !== "awaiting_approval"
            ? { ...message.process, status: "completed", completedAt: message.process.completedAt || new Date().toISOString() }
            : message.process,
        };
      }));
      await refreshConversations();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      updateActiveMessages(previous => previous.map(message => message.id === assistantMessageId ? {
        ...message,
        type: "error",
        text: `通信失败：${String(error)}`,
        process: message.process ? { ...message.process, status: "failed", completedAt: new Date().toISOString() } : undefined,
      } : message));
    } finally {
      if (activeStreamControllerRef.current === streamController) activeStreamControllerRef.current = null;
      setIsProcessing(false);
      setIsCancelling(false);
      setActiveRunId(null);
      scrollChatAfterUpdate();
    }
  };

  const respondToApproval = async (responseText: string) => {
    const approval = pendingApproval;
    if (!approval || isProcessing) return;
    const assistantMessageId = createConversationId();
    const streamController = new AbortController();
    activeAssistantMessageIdRef.current = assistantMessageId;
    activeRunConversationIdRef.current = currentConversationId;
    activeStreamControllerRef.current = streamController;
    const initialMessages: Message[] = [
      ...messages,
      { id: createConversationId(), type: "user", text: responseText },
      {
        id: assistantMessageId,
        type: "ai-stream",
        groups: [],
        artifacts: [],
        process: { runId: approval.runId, status: "running", startedAt: new Date().toISOString() },
      },
    ];
    activeRunMessagesRef.current = initialMessages;
    setMessages(initialMessages);
    setInputValue("");
    setActiveRunId(approval.runId);
    setExpandedProcessId(assistantMessageId);
    setIsProcessing(true);
    try {
      const response = await fetch(apiUrl(`/api/v1/runs/${approval.runId}/approval`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ response: responseText }),
        signal: streamController.signal,
      });
      if (!response.ok) throw new Error(`确认回复失败 (HTTP ${response.status})`);
      setPendingApproval(null);
      await consumeEventStream(response);
      updateActiveMessages(previous => previous.map(message => {
        if (message.id !== assistantMessageId || message.type !== "ai-stream") return message;
        return {
          ...message,
          type: "ai",
          text: message.text || (message.artifacts?.length
            ? "任务已完成，生成的产物可在下方查看。"
            : "模型没有返回有效内容，请重试。"),
          process: message.process && message.process.status !== "awaiting_approval"
            ? { ...message.process, status: "completed", completedAt: message.process.completedAt || new Date().toISOString() }
            : message.process,
        };
      }));
      await refreshConversations();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      updateActiveMessages(previous => previous.map(message => message.id === assistantMessageId ? {
        ...message,
        type: "error",
        text: `确认回复失败：${String(error)}`,
        process: message.process ? { ...message.process, status: "failed", completedAt: new Date().toISOString() } : undefined,
      } : message));
    } finally {
      if (activeStreamControllerRef.current === streamController) activeStreamControllerRef.current = null;
      setIsProcessing(false);
      setActiveRunId(null);
      scrollChatAfterUpdate();
    }
  };

  const showArtifact = (artifact: ArtifactView) => {
    setActivePreviewType(artifact.previewKind);
    setActivePreviewUrl(artifact.previewUrl || "");
    const nativePreview = artifact.previewKind === "pdf" || artifact.previewKind === "image";
    setActivePreviewContent(
      nativePreview
        ? artifact.previewUrl || ""
        : artifact.html || artifact.markdown || artifact.text || "",
    );
    setActivePreviewTitle(artifact.title);
    setIsPreviewOpen(true);
    setIsPreviewFullscreen(false);
  };

  const renameConversation = async () => {
    if (conversationDialog?.type !== "rename") return;
    const conversation = conversationDialog.conversation;
    const title = dialogTitle.trim();
    if (!title || title === conversation.title) {
      setConversationDialog(null);
      return;
    }
    const response = await fetch(apiUrl(`/api/v1/conversations/${conversation.id}`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    if (!response.ok) throw new Error(`重命名失败 (HTTP ${response.status})`);
    setOpenConversationMenu(null);
    setConversationDialog(null);
    await refreshConversations();
  };

  const deleteConversation = async () => {
    if (conversationDialog?.type !== "delete") return;
    const conversation = conversationDialog.conversation;
    const response = await fetch(apiUrl(`/api/v1/conversations/${conversation.id}`), { method: "DELETE" });
    if (!response.ok) throw new Error(`删除失败 (HTTP ${response.status})`);
    if (conversation.id === currentConversationId) startNewChat();
    setOpenConversationMenu(null);
    setConversationDialog(null);
    await refreshConversations();
  };

  const openRenameDialog = (conversation: ConversationSummary) => {
    setDialogTitle(conversation.title);
    setOpenConversationMenu(null);
    setConversationDialog({ type: "rename", conversation });
  };

  const openDeleteDialog = (conversation: ConversationSummary) => {
    setOpenConversationMenu(null);
    setConversationDialog({ type: "delete", conversation });
  };

  const positionConversationMenu = (anchor: HTMLButtonElement) => {
    const rect = anchor.getBoundingClientRect();
    const menuWidth = 132;
    const menuHeight = 126;
    const gap = 6;
    const margin = 8;
    const top = window.innerHeight - rect.bottom >= menuHeight + gap
      ? rect.bottom + gap
      : Math.max(margin, rect.top - menuHeight - gap);
    const left = Math.min(
      window.innerWidth - menuWidth - margin,
      Math.max(margin, rect.right - menuWidth),
    );
    setConversationMenuPosition({ top, left });
  };

  const toggleConversationMenu = (event: React.MouseEvent<HTMLButtonElement>, conversationId: string) => {
    if (openConversationMenu === conversationId) {
      setOpenConversationMenu(null);
      setConversationMenuPosition(null);
      historyMenuAnchorRef.current = null;
      return;
    }
    historyMenuAnchorRef.current = event.currentTarget;
    positionConversationMenu(event.currentTarget);
    setOpenConversationMenu(conversationId);
  };

  useEffect(() => {
    if (!openConversationMenu) return;
    const updatePosition = () => {
      const anchor = historyMenuAnchorRef.current;
      if (!anchor?.isConnected) {
        setOpenConversationMenu(null);
        setConversationMenuPosition(null);
        return;
      }
      positionConversationMenu(anchor);
    };
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [openConversationMenu]);

  const visibleTools = tools.filter(tool => {
    const keyword = toolQuery.trim().toLowerCase();
    const matchesCategory = toolCategory === "全部" || tool.category === toolCategory;
    const matchesKeyword = !keyword || `${tool.name} ${tool.category} ${tool.description}`.toLowerCase().includes(keyword);
    return matchesCategory && matchesKeyword;
  });
  const visibleToolTiers = toolTiers
    .map(tier => ({ tier, tools: visibleTools.filter(tool => tool.tier === tier) }))
    .filter(group => group.tools.length > 0);
  const visibleSkills = skills.filter(skill => {
    const keyword = skillQuery.trim().toLowerCase();
    return !keyword || `${skill.id} ${skill.description} ${skill.aliases.join(" ")} ${skill.tools.join(" ")}`.toLowerCase().includes(keyword);
  });
  return (
    <div className={`${styles.container} ${isSidebarCollapsed ? styles.sidebarCollapsed : ""}`}>
      <aside className={styles.sidebar}>
        <div className={styles.sidebarLogo}>
          <button
            type="button"
            className={styles.logoMark}
            onClick={() => isSidebarCollapsed && setIsSidebarCollapsed(false)}
            aria-label={isSidebarCollapsed ? "展开导航栏" : undefined}
            tabIndex={isSidebarCollapsed ? 0 : -1}
          >
            <span className={styles.logoBrandIcon}><Image src="/platform-logo.png" alt="" width={28} height={28} priority /></span>
            <span className={styles.logoExpandIcon}><Icon name="panelClose" /></span>
          </button>
          <span className={styles.sidebarBrandText}>交数智航</span>
          <button
            type="button"
            className={styles.sidebarCollapseBtn}
            onClick={() => setIsSidebarCollapsed(current => !current)}
            aria-label={isSidebarCollapsed ? "展开导航栏" : "折叠导航栏"}
            title={isSidebarCollapsed ? "展开导航栏" : "折叠导航栏"}
          >
            <Icon name="panelClose" />
          </button>
        </div>

        <div className={styles.workspaceLabel}>工作区</div>
        <nav className={styles.workspaceNav} aria-label="工作区导航">
          <button className={`${styles.workspaceNavItem} ${workspaceView === "tools" ? styles.workspaceNavItemActive : ""}`} onClick={() => setWorkspaceView("tools")}><Icon name="tools" /><span className={styles.navLabel}>智能体中心</span></button>
          <button className={`${styles.workspaceNavItem} ${workspaceView === "skills" ? styles.workspaceNavItemActive : ""}`} onClick={openSkillPlaza}><Icon name="package" /><span className={styles.navLabel}>技能广场</span></button>
          <button className={styles.workspaceNavItem} onClick={startNewChat}><Icon name="task" /><span className={styles.navLabel}>发起新任务</span></button>
        </nav>

        <div className={styles.taskListContainer}>
          <div className={styles.taskListTitle}>任务记录</div>
          <div className={styles.taskListRecords}>
          {isLoadingConversations && conversations.length === 0 ? (
            <div className={styles.emptyHistory}>正在加载任务记录...</div>
          ) : conversations.length === 0 ? (
            <div className={styles.emptyHistory}>暂无历史对话</div>
          ) : conversations.map(conversation => (
            <div key={conversation.id} className={`${styles.historyRow} ${conversation.id === currentConversationId ? styles.historyItemActive : ""}`}>
              <button
                className={styles.historyItem}
                onClick={() => void loadConversation(conversation.id)}
                disabled={isLoadingHistory}
              >
                <span className={styles.historyTitle}>{conversation.is_pinned && <Icon name="pin" />}{conversation.title}</span>
                <span className={styles.historyPreview}>{conversation.last_message || "点击查看对话"}</span>
              </button>
              <button className={styles.historyMenuTrigger} type="button" aria-label={`操作 ${conversation.title}`} onClick={event => toggleConversationMenu(event, conversation.id)}><Icon name="moreVertical" /></button>
            </div>
          ))}
          </div>
        </div>

        <div className={styles.sidebarUser}>
          <span className={styles.userAvatar}><Icon name="userSystem" /></span>
          <div className={styles.userInfo}>
            <span className={styles.userName}>浙江综合交通大数据</span>
            <span className={styles.planName}>智能体平台</span>
          </div>
        </div>

      </aside>

      {typeof document !== "undefined" && openConversationMenu && conversationMenuPosition && createPortal((() => {
        const conversation = conversations.find(item => item.id === openConversationMenu);
        if (!conversation) return null;
        return <div className={styles.historyMenu} style={conversationMenuPosition}>
          <button type="button" onClick={() => void updateConversationState(conversation.id, { is_pinned: !conversation.is_pinned }).then(() => setOpenConversationMenu(null))}><Icon name="pin" />{conversation.is_pinned ? "取消固定" : "固定"}</button>
          <button type="button" onClick={() => openRenameDialog(conversation)}><Icon name="rename" />重命名</button>
          <button type="button" className={styles.destructiveMenuItem} onClick={() => openDeleteDialog(conversation)}><Icon name="delete" />删除</button>
        </div>;
      })(), document.body)}

      {workspaceView === "tools" ? (
        <main className={styles.toolCenter}>
          <header className={styles.toolCenterHeader}>
            <span className={styles.toolEyebrow}>TOOLS</span>
            <h1>智能体中心</h1>
            <p>选择一个工具，直接开始工作</p>
          </header>
          <div className={styles.toolFilterBar}>
            <label className={styles.toolSearch}>
              <span aria-hidden="true"><Icon name="search" /></span>
              <input value={toolQuery} onChange={event => setToolQuery(event.target.value)} placeholder="搜索工具名称或用途" />
            </label>
            <div className={styles.toolCategories} aria-label="工具分类">
              {toolCategories.map(category => (
                <button key={category} type="button" className={toolCategory === category ? styles.toolCategoryActive : ""} onClick={() => setToolCategory(category)}>{category}</button>
              ))}
            </div>
          </div>
          {visibleToolTiers.map(group => (
            <section className={styles.toolTier} key={group.tier} aria-labelledby={`tool-tier-${group.tier}`}>
              <div className={styles.toolSectionTitle}>
                <span id={`tool-tier-${group.tier}`}>{group.tier}</span>
                <span>{group.tools.length} 个</span>
              </div>
              <div className={styles.toolGrid}>
                {group.tools.map(tool => (
                  <a key={tool.id} className={styles.toolCard} href={tool.url} target="_blank" rel="noopener noreferrer" aria-label={`打开${tool.name}`}>
                    <span className={`${styles.toolCardHeader} ${tool.logo ? "" : styles.toolCardHeaderNoLogo}`}>
                      {tool.logo && (tool.logoKind === "mask" ? (
                        <span className={`${styles.toolLogo} ${styles.toolLogoMask}`} style={{ WebkitMaskImage: `url(${tool.logo})`, maskImage: `url(${tool.logo})` }} aria-hidden="true" />
                      ) : (
                        <span className={styles.toolLogo} style={{ backgroundImage: `url(${tool.logo})` }} aria-hidden="true" />
                      ))}
                      <span className={styles.toolIdentity}>
                        <strong>{tool.name}</strong>
                        <small>{tool.category}</small>
                      </span>
                    </span>
                    <span className={styles.toolDescription}>{tool.description}</span>
                    <span className={styles.toolTags}>
                      {tool.tags.map(tag => <small key={tag}>{tag}</small>)}
                    </span>
                    <span className={styles.toolCardFooter}>
                      <small>{tool.stage}</small>
                      <strong>打开工具 <span aria-hidden="true">↗</span></strong>
                    </span>
                  </a>
                ))}
              </div>
            </section>
          ))}
          {visibleTools.length === 0 && <div className={styles.toolEmpty}>没有找到相关工具</div>}
        </main>
      ) : workspaceView === "skills" ? (
        <main className={`${styles.toolCenter} ${styles.skillPlaza}`}>
          <header className={`${styles.toolCenterHeader} ${styles.skillPlazaHeader}`}>
            <div>
              <span className={styles.toolEyebrow}>SKILLS</span>
              <h1>技能广场</h1>
              <p>仅支持 ZIP 压缩包，包内至少需要一个非空的 SKILL.md</p>
            </div>
          </header>
          <div className={styles.skillToolbar}>
            <label className={`${styles.toolSearch} ${styles.skillSearch}`}>
              <span aria-hidden="true"><Icon name="search" /></span>
              <input value={skillQuery} onChange={event => setSkillQuery(event.target.value)} placeholder="搜索技能名称或用途" />
            </label>
            <div className={styles.skillUploadActions}>
              <input ref={skillUploadRef} className={styles.skillFileInput} type="file" accept=".zip,application/zip" onChange={event => void uploadSkill(event)} />
              <button type="button" className={styles.skillUploadButton} disabled={isUploadingSkill} onClick={() => skillUploadRef.current?.click()}>
                <Icon name="upload" />{isUploadingSkill ? "正在预检" : "上传技能"}
              </button>
            </div>
          </div>
          {skillMessage && <div role={skillMessageIsError ? "alert" : "status"} className={`${styles.skillMessage} ${skillMessageIsError ? styles.skillMessageError : ""}`}>{skillMessage}</div>}
          {isLoadingSkills ? <div className={styles.toolEmpty}>正在加载技能...</div> : (
            <div className={`${styles.toolGrid} ${styles.skillGrid}`}>
              {visibleSkills.map(skill => (
                <article className={`${styles.toolCard} ${styles.skillCard}`} key={skill.id}>
                  <div className={styles.toolIdentity}><strong>{skill.id}</strong></div>
                  <span className={styles.toolDescription}>{skill.description}</span>
                  {skill.tools.length > 0 && <span className={styles.toolTags}>
                    {skill.tools.slice(0, 2).map(tool => <small key={tool}>{tool}</small>)}
                    {skill.tools.length > 2 && <small title={skill.tools.slice(2).join("、")}>…</small>}
                  </span>}
                </article>
              ))}
            </div>
          )}
          {!isLoadingSkills && visibleSkills.length === 0 && <div className={styles.toolEmpty}>没有找到相关技能</div>}
        </main>
      ) : <main className={styles.mainArea}>
        <div
          className={styles.chatContainer}
          ref={chatContainerRef}
          onScroll={event => {
            const container = event.currentTarget;
            chatAutoScrollRef.current = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
          }}
        >
          {messages.length === 0 ? (
            <div className={styles.welcomeState}>
              {selectedTool ? <span className={styles.welcomeIcon}>AI</span> : <span className={styles.welcomeKicker}>JIAO SHU ZHI HANG</span>}
              <h2>{selectedTool?.name || "交数智航"}</h2>
              <p className={styles.typewriterText}>{selectedTool?.description || brandText}<span className={styles.typewriterCursor} aria-hidden="true" /></p>
              {!selectedTool && <p className={styles.companyIntro}>面向交通与政企业务的数据分析和智能任务协作平台</p>}
            </div>
          ) : (
            <div className={styles.messagesWrapper}>
              {messages.map((message, messageIndex) => {
                const retryTarget = message.type === "user" ? messages[messageIndex + 1] : undefined;
                const canRetry = Boolean(
                  retryTarget?.id
                  && retryTarget.process?.runId
                  && ["completed", "failed", "cancelled"].includes(retryTarget.process.status),
                );
                return <div
                  key={message.id || messageIndex}
                  className={`${styles.messageRow} ${message.type === "user" ? styles.rowUser : styles.rowAi}`}
                >
                  {message.type === "user" ? (
                    <div className={styles.userMessage}>
                      <div className={styles.userBubble}>
                        {(message.attachedFiles?.length ? message.attachedFiles : message.attachedFile ? [message.attachedFile] : []).map((fileName, fileIndex) => (
                          <div className={styles.attachmentCard} key={`${fileName}-${fileIndex}`} title={fileName}><Icon name="file" /><span>{compactFileName(fileName)}</span></div>
                        ))}
                        <div className={styles.userText}>{message.text}</div>
                      </div>
                      {canRetry && retryTarget?.id && retryTarget.process?.runId && (
                        <div className={styles.retryActionRow}>
                          <button
                            type="button"
                            className={styles.retryButton}
                            disabled={isProcessing}
                            onClick={() => void retryRun(retryTarget.id!, retryTarget.process!.runId)}
                            title="重试"
                            aria-label="重试该问题"
                          >
                            <Icon name="retry" />
                          </button>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className={styles.aiBubble}>
                      {Boolean(message.process || message.groups?.length) && (
                        <div className={styles.processGroup}>
                          <button
                            type="button"
                            className={styles.processHeader}
                            onClick={() => toggleProcess(message.id || String(messageIndex))}
                            aria-expanded={expandedProcessId === (message.id || String(messageIndex))}
                          >
                            <span>{processStatusLabel[message.process?.status || (message.type === "ai-stream" ? "running" : "completed")]}</span>
                            <time>{formatElapsed(message.process?.startedAt, message.process?.completedAt, elapsedNow)}</time>
                            <Icon name="chevron" className={`${styles.processChevron} ${expandedProcessId === (message.id || String(messageIndex)) ? styles.chevronOpen : ""}`} />
                          </button>
                          {expandedProcessId === (message.id || String(messageIndex)) && (
                            <div
                              className={styles.processViewport}
                              ref={element => { processPanelRefs.current[message.id || String(messageIndex)] = element; }}
                              onScroll={event => {
                                const viewport = event.currentTarget;
                                processAutoScrollRef.current[message.id || String(messageIndex)] =
                                  viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 40;
                              }}
                            >
                              <div className={styles.agentGroups}>
                                {message.groups?.map(group => (
                                  <React.Fragment key={group.id}>
                                    {group.thoughts.filter(thought => thought.trim()).map((thought, thoughtIndex) => (
                                      <div key={`${group.id}-thought-${thoughtIndex}`} className={styles.thoughtText}>{thought}</div>
                                    ))}
                                    {group.draftThought?.trim() && (
                                      <div className={styles.thoughtText}>{group.draftThought}</div>
                                    )}
                                    {group.actions.map((action, actionIndex) => (
                                      <div key={`${group.id}-action-${actionIndex}`} className={styles.actionItem}>
                                        <span className={styles.actionLine} />
                                        <span>{action.text}</span>
                                        <small>{action.status === "loading" ? "进行中" : "已完成"}</small>
                                      </div>
                                    ))}
                                  </React.Fragment>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                      {message.type === "error" && <div className={styles.errorBubble}>{message.text}</div>}
                      {message.type === "ai-stream" && !message.process && !message.text && !message.groups?.length && <span className={styles.streamPending}>正在思考</span>}
                      {message.type === "ai-stream" && message.text && <div className={styles.aiFinalText}><MarkdownContent content={message.text} /></div>}
                      {message.type === "ai" && <div className={styles.aiFinalText}><MarkdownContent content={message.text || ""} /></div>}
                      {message.type === "ai" && message.artifacts?.map((artifact, artifactIndex) => (
                        <div key={artifact.artifactId || artifactIndex}>
                          {Boolean(artifact.missingFields?.length) && (
                            <div className={styles.auditWarning}>
                              <strong>待人工核验：</strong>{artifact.missingFields?.join("、")}
                            </div>
                          )}
                          <div className={styles.fileCard} onClick={() => showArtifact(artifact)}>
                            <div className={styles.fileCardName}>{artifact.title} · {artifact.artifactType.toUpperCase()}</div>
                            {artifact.downloadUrl && (
                              <a
                                href={apiUrl(artifact.downloadUrl)}
                                download
                                className={styles.fileCardDownload}
                                onClick={clickEvent => clickEvent.stopPropagation()}
                              >
                                下载
                              </a>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>;
              })}
            </div>
          )}
        </div>

        <div className={styles.inputAreaWrapper}>
          <div className={styles.inputContainer}>
            {selectedSkill && (
              <div className={`${styles.fileTag} ${styles.skillTag}`} title={selectedSkill.description}>
                <Icon name="package" /><strong>{selectedSkill.id}</strong>
                <button type="button" className={styles.removeFileBtn} onClick={() => setSelectedSkill(null)} aria-label={`移除技能 ${selectedSkill.id}`}><Icon name="close" /></button>
              </div>
            )}
            {files.map((selectedFile, fileIndex) => (
              <div className={styles.fileTag} key={`${selectedFile.name}-${selectedFile.size}-${fileIndex}`} title={selectedFile.name}>
                <Icon name="file" /> <strong>{compactFileName(selectedFile.name)}</strong>
                <button type="button" className={styles.removeFileBtn} onClick={() => setFiles(current => current.filter((_, index) => index !== fileIndex))} aria-label={`移除附件 ${selectedFile.name}`}><Icon name="close" /></button>
              </div>
            ))}
            <form onSubmit={handleFormSubmit} className={styles.inputForm}>
              <div className={styles.attachMenuWrapper}>
                <button type="button" className={styles.attachBtn} title="添加" aria-label="添加文件或技能" aria-expanded={attachmentMenu !== null} onClick={() => setAttachmentMenu(current => current ? null : "root")}><Icon name="plus" /></button>
                {attachmentMenu && (
                  <div className={styles.attachMenu}>
                    {attachmentMenu === "root" ? (
                      <>
                        <button type="button" onClick={() => attachmentInputRef.current?.click()}><Icon name="file" /><span>上传文件</span></button>
                        <button type="button" onClick={() => { setAttachmentMenu("skills"); setSkillMessage(""); void refreshSkills(); }}><Icon name="package" /><span>选择技能</span><Icon name="chevron" /></button>
                      </>
                    ) : (
                      <>
                        <button type="button" className={styles.attachMenuBack} onClick={() => setAttachmentMenu("root")}><Icon name="back" /><span>返回</span></button>
                        <div className={styles.attachSkillList}>
                          {isLoadingSkills ? <span className={styles.attachMenuHint}>正在加载...</span> : skills.map(skill => (
                            <button type="button" className={selectedSkill?.id === skill.id ? styles.attachSkillActive : ""} key={skill.id} onClick={() => { setSelectedSkill(skill); setAttachmentMenu(null); }}>
                              <span><strong>{skill.id}</strong><small>{skill.description}</small></span>
                            </button>
                          ))}
                          {!isLoadingSkills && !skills.length && <span className={styles.attachMenuHint}>暂无可用技能</span>}
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
              <input
                ref={attachmentInputRef}
                id="file-input"
                type="file"
                multiple
                accept=".pdf,.docx,.txt,.md,.markdown,.pptx,.mp3,.m4a,.wav,.aac,.ogg,.flac"
                hidden
                onChange={event => {
                  const selectedFiles = Array.from(event.target.files || []);
                  setFiles(current => [...current, ...selectedFiles].slice(0, 20));
                  setAttachmentMenu(null);
                  event.target.value = "";
                }}
              />
              <textarea
                ref={textInputRef}
                className={styles.textInput}
                placeholder="输入问题，或描述要完成的任务…"
                value={inputValue}
                onChange={event => setInputValue(event.target.value)}
                onKeyDown={event => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                disabled={isProcessing}
                rows={1}
              />
              <button
                type={isProcessing ? "button" : "submit"}
                disabled={isProcessing ? isCancelling : !inputValue.trim() && !files.length}
                className={`${styles.sendBtn} ${isProcessing ? styles.stopBtn : (inputValue.trim() || files.length) ? styles.sendBtnActive : styles.sendBtnDisabled}`}
                aria-label={isProcessing ? (isCancelling ? "正在中断" : "中断生成") : "发送"}
                title={isProcessing ? (isCancelling ? "正在中断" : "中断生成") : "发送"}
                onClick={isProcessing ? cancelActiveRun : undefined}
              >
                <Icon name={isProcessing ? "stop" : "send"} />
              </button>
            </form>
          </div>
        </div>
      </main>}

      {workspaceView === "tasks" && isPreviewOpen && (
        <section className={`${styles.previewArea} ${isPreviewFullscreen ? styles.previewAreaFullscreen : ""}`}>
          <div className={styles.previewContent}>
            <div className={styles.previewHeader}>
              <span className={styles.previewTitle}>{activePreviewTitle}</span>
              <span className={styles.previewActions}>
                {isPreviewFullscreen ? (
                  <button type="button" onClick={() => setIsPreviewFullscreen(false)} title="收回预览" aria-label="收回预览"><Icon name="restore" /></button>
                ) : (
                  <>
                    <button type="button" onClick={() => setIsPreviewFullscreen(true)} title="全屏预览" aria-label="全屏预览"><Icon name="fullscreen" /></button>
                    <button type="button" onClick={() => setIsPreviewOpen(false)} title="关闭预览" aria-label="关闭预览"><Icon name="panelClose" /></button>
                  </>
                )}
              </span>
            </div>
            <div className={styles.iframeContainer}>
              {activePreviewType === "html" ? (
                activePreviewContent ? (
                  <iframe key={isPreviewFullscreen ? "fullscreen" : "panel"} className={styles.previewIframe} title="产物预览" sandbox="allow-scripts" srcDoc={activePreviewContent} />
                ) : activePreviewUrl ? (
                  <iframe key={isPreviewFullscreen ? "fullscreen-url" : "panel-url"} className={styles.previewIframe} title="产物预览" sandbox="allow-scripts" src={apiUrl(activePreviewUrl)} />
                ) : (
                  <div className={styles.documentPreview}>暂无预览内容</div>
                )
              ) : activePreviewType === "pdf" ? (
                <iframe className={styles.filePreviewIframe} title="PDF 预览" src={apiUrl(activePreviewContent)} />
              ) : activePreviewType === "image" ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img className={styles.imagePreview} src={apiUrl(activePreviewContent)} alt={activePreviewTitle} />
              ) : (
                <div className={styles.documentPreview}><MarkdownContent content={activePreviewContent} /></div>
              )}
            </div>
          </div>
        </section>
      )}

      {conversationDialog && <div className={styles.dialogBackdrop} onMouseDown={() => setConversationDialog(null)}>
        <section className={styles.dialogPanel} role="dialog" aria-modal="true" aria-labelledby="conversation-dialog-title" onMouseDown={event => event.stopPropagation()}>
          <header>
            <h2 id="conversation-dialog-title">{conversationDialog.type === "rename" ? "重命名任务" : "删除任务"}</h2>
            <button type="button" onClick={() => setConversationDialog(null)} aria-label="关闭"><Icon name="close" /></button>
          </header>
          {conversationDialog.type === "rename" ? (
            <label className={styles.dialogField}>任务名称<input autoFocus value={dialogTitle} maxLength={200} onChange={event => setDialogTitle(event.target.value)} onKeyDown={event => { if (event.key === "Enter") void renameConversation(); }} /></label>
          ) : <p>删除“{conversationDialog.conversation.title}”后，任务记录和关联文件将无法恢复。</p>}
          <footer>
            <button type="button" className={styles.dialogSecondary} onClick={() => setConversationDialog(null)}>取消</button>
            <button type="button" className={conversationDialog.type === "delete" ? styles.dialogDanger : styles.dialogPrimary} onClick={() => void (conversationDialog.type === "rename" ? renameConversation() : deleteConversation())}>{conversationDialog.type === "rename" ? "保存" : "确认删除"}</button>
          </footer>
        </section>
      </div>}

    </div>
  );
}
