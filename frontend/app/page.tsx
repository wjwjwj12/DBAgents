"use client";

import React, { FormEvent, useEffect, useRef, useState } from "react";
import Image from "next/image";
import styles from "./page.module.css";

const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:14499").replace(/\/$/, "");
const apiUrl = (path: string) => /^https?:\/\//.test(path) ? path : `${API_BASE_URL}${path}`;
const createConversationId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024;

interface Action {
  text: string;
  status: "loading" | "done";
}

interface TaskGroup {
  id: string;
  title: string;
  thoughts: string[];
  actions: Action[];
  isOpen: boolean;
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
  groups?: TaskGroup[];
  artifacts?: ArtifactView[];
  hasFinalOutput?: boolean;
}

interface ApprovalView {
  runId: string;
  toolCallId: string;
  name: string;
  arguments: Record<string, unknown>;
  message: string;
}

const approvalToolLabels: Record<string, string> = {
  apply_pptx_template_fill: "执行 PPTX 模板填充",
  apply_pptx_enhancement: "执行 PPTX 原生增强",
};

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
  artifacts: StoredArtifact[];
  groups?: Array<Omit<TaskGroup, "isOpen">>;
}

interface StoredConversation {
  id: string;
  title: string;
  messages: StoredMessage[];
}

interface StreamEvent {
  type: "run_started" | "task_group" | "thought" | "action" | "content_delta" | "content" | "approval_required" | "error";
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
}

type WorkspaceView = "tools" | "tasks";
type ConversationDialog = { type: "rename" | "delete"; conversation: ConversationSummary } | null;

const sanitizeModelText = (text: string) => text
  .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
  .replace(/<\s*(?:img|svg)\b[^>]*>(?:[^]*?<\s*\/\s*svg\s*>)?/gi, "")
  .replace(/[\u2600-\u27BF\u2300-\u23FF\u200D\uFE0E\uFE0F]|[\uD800-\uDBFF][\uDC00-\uDFFF]/g, "");

function Icon({ name, className }: { name: "brand" | "tools" | "task" | "upload" | "send" | "stop" | "more" | "moreVertical" | "pin" | "rename" | "delete" | "package" | "chevron" | "file" | "search" | "close" | "fullscreen" | "restore" | "panelClose"; className?: string }) {
  const paths: Record<typeof name, React.ReactNode> = {
    brand: <><path d="M12 3 4.5 7.2 12 11.5l7.5-4.3L12 3Z"/><path d="m4.5 12 7.5 4.3 7.5-4.3M4.5 16.8 12 21l7.5-4.2"/></>,
    tools: <><path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4z"/><path d="M17 14v6m-3-3h6"/></>,
    task: <><path d="M5 5.5h14v10H9l-4 3v-13Z"/><path d="M9 9h6m-6 3h4"/></>,
    upload: <><path d="M12 16V4m-4 4 4-4 4 4"/><path d="M5 13v6h14v-6"/></>,
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
  const tokens = text.split(/(\*\*.+?\*\*|__.+?__|`.+?`|\[.+?\]\(.+?\))/g);
  return tokens.filter(Boolean).map((token, index) => {
    const key = `${keyPrefix}-${index}`;
    if ((token.startsWith("**") && token.endsWith("**")) || (token.startsWith("__") && token.endsWith("__"))) {
      return <strong key={key}>{token.slice(2, -2)}</strong>;
    }
    if (token.startsWith("`") && token.endsWith("`")) return <code key={key}>{token.slice(1, -1)}</code>;
    const link = token.match(/^\[(.+?)\]\((https?:\/\/[^\s)]+)\)$/);
    if (link) {
      return <a key={key} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>;
    }
    return <React.Fragment key={key}>{token}</React.Fragment>;
  });
}

function MarkdownContent({ content }: { content: string }) {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const nodes: React.ReactNode[] = [];
  let listItems: { ordered: boolean; text: string }[] = [];
  let codeLines: string[] | null = null;
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
    if (line.trim().startsWith("```")) {
      flushList();
      if (codeLines === null) codeLines = [];
      else {
        nodes.push(<pre key={`code-${index}`}><code>{codeLines.join("\n")}</code></pre>);
        codeLines = null;
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
  if (codeLines !== null) nodes.push(<pre key="code-final"><code>{codeLines.join("\n")}</code></pre>);
  return <div className={styles.markdownContent}>{nodes}</div>;
}

export default function Home() {
  const [files, setFiles] = useState<File[]>([]);
  const [contextText, setContextText] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [isProcessing, setIsProcessing] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [inputValue, setInputValue] = useState("");
  const [currentConversationId, setCurrentConversationId] = useState<string>(createConversationId);
  const [activePreviewType, setActivePreviewType] = useState("text");
  const [activePreviewContent, setActivePreviewContent] = useState("");
  const [activePreviewTitle, setActivePreviewTitle] = useState("");
  const [isPreviewOpen, setIsPreviewOpen] = useState(false);
  const [isPreviewFullscreen, setIsPreviewFullscreen] = useState(false);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView>("tasks");
  const [selectedTool, setSelectedTool] = useState<ToolItem | null>(null);
  const [toolQuery, setToolQuery] = useState("");
  const [toolCategory, setToolCategory] = useState<(typeof toolCategories)[number]>("全部");
  const [openConversationMenu, setOpenConversationMenu] = useState<string | null>(null);
  const [brandText, setBrandText] = useState("");
  const [conversationDialog, setConversationDialog] = useState<ConversationDialog>(null);
  const [dialogTitle, setDialogTitle] = useState("");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [pendingApproval, setPendingApproval] = useState<ApprovalView | null>(null);
  const [isDecidingApproval, setIsDecidingApproval] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textInputRef = useRef<HTMLTextAreaElement>(null);

  const refreshConversations = async () => {
    try {
      const response = await fetch(apiUrl("/api/v1/conversations"), { cache: "no-store" });
      if (response.ok) setConversations(await response.json());
    } catch (error) {
      console.error("加载对话列表失败", error);
    }
  };

  useEffect(() => {
    void refreshConversations();
  }, []);

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
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isProcessing]);

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
    setMessages([]);
    setContextText("");
    setFiles([]);
    setInputValue("");
    setIsPreviewOpen(false);
    setIsPreviewFullscreen(false);
    setCurrentConversationId(createConversationId());
    setSelectedTool(null);
    setWorkspaceView("tasks");
  };

  const loadConversation = async (conversationId: string) => {
    setWorkspaceView("tasks");
    if (isProcessing || conversationId === currentConversationId) return;
    setIsLoadingHistory(true);
    try {
      const response = await fetch(apiUrl(`/api/v1/conversations/${conversationId}`), { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const stored = await response.json() as StoredConversation;
      setMessages(stored.messages.map((message): Message => {
        if (message.role === "user") {
          return {
            id: message.id,
            type: "user",
            text: message.content,
            attachedFile: message.attachment_name,
          };
        }
        return {
          id: message.id,
          type: "ai",
          text: message.content,
          groups: (message.groups || []).map(group => ({ ...group, isOpen: false })),
          artifacts: message.artifacts.map(toArtifact),
        };
      }));
      setCurrentConversationId(stored.id);
      setContextText("");
      setFiles([]);
      setIsPreviewOpen(false);
      setSelectedTool(null);
      setWorkspaceView("tasks");
    } catch (error) {
      console.error("加载对话失败", error);
    } finally {
      setIsLoadingHistory(false);
    }
  };

  const uploadAndParseFile = async (selectedFile: File): Promise<{ text: string; attachmentId: string }> => {
    const formData = new FormData();
    formData.append("file", selectedFile);
    formData.append("conversation_id", currentConversationId);
    const response = await fetch(apiUrl("/upload"), { method: "POST", body: formData });
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
        isOpen: true,
      });
    }
    return message.groups[message.groups.length - 1];
  };

  const toggleTaskGroup = (messageIndex: number, groupId: string) => {
    setMessages(previous => previous.map((message, index) => {
      if (index !== messageIndex || !message.groups) return message;
      return {
        ...message,
        groups: message.groups.map(group => group.id === groupId ? { ...group, isOpen: !group.isOpen } : group),
      };
    }));
  };

  const applyStreamEvent = (event: StreamEvent) => {
    if (event.type === "run_started" && event.run_id) {
      setActiveRunId(event.run_id);
      const engineNames = { react: "ReAct", plan_execute: "Plan & Execute", static_plan: "静态计划", dag: "DAG 并发" };
      if (event.engine) applyStreamEvent({ type: "action", text: `已选择 ${engineNames[event.engine]}：${event.router_reasons?.join("；") || "自动路由"}`, status: "done" });
      return;
    }
    if (event.type === "approval_required" && event.run_id && event.tool_call_id && event.name) {
      setPendingApproval({
        runId: event.run_id,
        toolCallId: event.tool_call_id,
        name: event.name,
        arguments: event.arguments || {},
        message: event.message || "该工具需要你的确认。",
      });
      setIsProcessing(false);
      return;
    }
    setMessages(previous => {
      const next = structuredClone(previous);
      const assistant = next[next.length - 1];
      if (!assistant || (assistant.type !== "ai-stream" && assistant.type !== "ai")) return previous;

      if (event.type === "task_group") {
        assistant.hasFinalOutput = false;
        assistant.groups ??= [];
        assistant.groups.forEach(group => { group.isOpen = false; });
        assistant.groups.push({
          id: `${Date.now()}-${Math.random()}`,
          title: event.title || "任务执行",
          thoughts: [],
          actions: [],
          isOpen: true,
        });
      } else if (event.type === "thought") {
        assistant.hasFinalOutput = false;
        const group = ensureGroup(assistant);
        group.isOpen = true;
        if (event.text) group.thoughts.push(event.text);
      } else if (event.type === "action") {
        assistant.hasFinalOutput = false;
        const group = ensureGroup(assistant);
        group.isOpen = true;
        group.actions.push({ text: event.text || "执行工具", status: event.status || "done" });
      } else if (event.type === "content") {
        if (!assistant.hasFinalOutput) {
          assistant.groups = (assistant.groups || []).map(group => ({ ...group, isOpen: false }));
          assistant.hasFinalOutput = true;
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
        if (!assistant.hasFinalOutput) {
          assistant.groups = (assistant.groups || []).map(group => ({ ...group, isOpen: false }));
          assistant.hasFinalOutput = true;
        }
        assistant.text = sanitizeModelText((assistant.text || "") + (event.text || ""));
      } else if (event.type === "error") {
        assistant.groups = (assistant.groups || []).map(group => ({ ...group, isOpen: false }));
        assistant.type = "error";
        assistant.text = event.msg || "任务执行失败";
      }
      return next;
    });
  };

  const handleFormSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const query = inputValue.trim() || (files.length ? "请阅读附件并根据内容完成合适的任务。" : "");
    if (!query || isProcessing) return;

    const currentFiles = files;
    const oversizedFile = currentFiles.find(selectedFile => selectedFile.size > MAX_UPLOAD_FILE_BYTES);
    if (oversizedFile) {
      setMessages(previous => [
        ...previous,
        { type: "error", text: `附件 ${oversizedFile.name} 超过 100MB 上传上限` },
      ]);
      return;
    }
    const attachmentName = currentFiles.map(selectedFile => selectedFile.name).join("、").slice(0, 200);
    setInputValue("");
    setFiles([]);
    setIsProcessing(true);
    setIsCancelling(false);
    setActiveRunId(null);
    setMessages(previous => [
      ...previous,
      { type: "user", text: query, attachedFile: attachmentName || null },
      { type: "ai-stream", groups: [], artifacts: [] },
    ]);

    let currentContext = contextText;
    const attachmentIds: string[] = [];
    if (currentFiles.length) {
      try {
        const uploadedTexts: string[] = [];
        for (const currentFile of currentFiles) {
          const upload = await uploadAndParseFile(currentFile);
          uploadedTexts.push(`## 附件：${currentFile.name}\n\n${upload.text}`);
          attachmentIds.push(upload.attachmentId);
          applyStreamEvent({ type: "action", text: `已读取附件：${currentFile.name}`, status: "done" });
        }
        currentContext = [contextText, ...uploadedTexts].filter(Boolean).join("\n\n---\n\n");
        setContextText(currentContext);
      } catch (error) {
        const detail = error instanceof Error ? error.message : "未知错误";
        setMessages(previous => {
          const next = [...previous];
          next[next.length - 1] = { type: "error", text: `附件解析失败：${detail}` };
          return next;
        });
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
          conversation_id: currentConversationId,
          attachment_name: attachmentName || null,
          attachment_id: null,
          attachment_ids: attachmentIds,
        }),
      });
      if (!response.ok) throw new Error(`后端请求失败 (HTTP ${response.status})`);
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

      setMessages(previous => {
        const next = structuredClone(previous);
        const assistant = next[next.length - 1];
        if (assistant?.type === "ai-stream") {
          assistant.type = "ai";
          assistant.text ||= assistant.artifacts?.length
            ? "任务已完成，生成的产物可在下方查看。"
            : "模型没有返回有效内容，请重试。";
          assistant.groups = (assistant.groups || []).map(group => ({ ...group, isOpen: false }));
        }
        return next;
      });
      await refreshConversations();
    } catch (error) {
      setMessages(previous => {
        const next = [...previous];
        next[next.length - 1] = { type: "error", text: `通信失败：${String(error)}` };
        return next;
      });
      } finally {
        setIsProcessing(false);
        setIsCancelling(false);
        setActiveRunId(null);
      }
  };

  const cancelActiveRun = async () => {
    if (!isProcessing || !activeRunId || isCancelling) return;
    setIsCancelling(true);
    try {
      const response = await fetch(apiUrl(`/api/v1/runs/${activeRunId}/cancel`), { method: "POST" });
      if (!response.ok && response.status !== 409) throw new Error(`中断失败 (HTTP ${response.status})`);
    } catch (error) {
      console.error("中断任务失败", error);
      setIsCancelling(false);
    }
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
        if (dataLine) applyStreamEvent(JSON.parse(dataLine.slice(6)) as StreamEvent);
      }
    }
  };

  const decideApproval = async (decision: "approve" | "reject") => {
    if (!pendingApproval || isDecidingApproval) return;
    setIsDecidingApproval(true);
    setIsProcessing(true);
    try {
      const response = await fetch(apiUrl(`/api/v1/runs/${pendingApproval.runId}/approval`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      if (!response.ok) throw new Error(`审批失败 (HTTP ${response.status})`);
      setPendingApproval(null);
      await consumeEventStream(response);
      await refreshConversations();
    } catch (error) {
      applyStreamEvent({ type: "error", msg: `审批处理失败：${String(error)}` });
    } finally {
      setIsDecidingApproval(false);
      setIsProcessing(false);
      setActiveRunId(null);
    }
  };

  const showArtifact = (artifact: ArtifactView) => {
    setActivePreviewType(artifact.previewKind);
    setActivePreviewContent(artifact.html || artifact.markdown || artifact.text || artifact.previewUrl || "暂无预览内容");
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

  const visibleTools = tools.filter(tool => {
    const keyword = toolQuery.trim().toLowerCase();
    const matchesCategory = toolCategory === "全部" || tool.category === toolCategory;
    const matchesKeyword = !keyword || `${tool.name} ${tool.category} ${tool.description}`.toLowerCase().includes(keyword);
    return matchesCategory && matchesKeyword;
  });
  const visibleToolTiers = toolTiers
    .map(tier => ({ tier, tools: visibleTools.filter(tool => tool.tier === tier) }))
    .filter(group => group.tools.length > 0);
  const approvalSummary = pendingApproval && typeof pendingApproval.arguments.confirmation_summary === "string"
    ? pendingApproval.arguments.confirmation_summary
    : pendingApproval?.message;
  const approvalDetails = pendingApproval
    ? Object.fromEntries(Object.entries(pendingApproval.arguments).filter(([key]) => key !== "confirmation_summary"))
    : {};
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
          <button className={styles.workspaceNavItem} onClick={startNewChat}><Icon name="task" /><span className={styles.navLabel}>发起新任务</span></button>
        </nav>

        <div className={styles.taskListContainer}>
          <div className={styles.taskListTitle}>任务记录</div>
          {conversations.length === 0 ? (
            <div className={styles.emptyHistory}>暂无历史对话</div>
          ) : conversations.map(conversation => (
            <div key={conversation.id} className={`${styles.historyRow} ${conversation.id === currentConversationId ? styles.historyItemActive : ""}`}>
              <button
                className={styles.historyItem}
                onClick={() => void loadConversation(conversation.id)}
                disabled={isLoadingHistory || isProcessing}
              >
                <span className={styles.historyTitle}>{conversation.is_pinned && <Icon name="pin" />}{conversation.title}</span>
                <span className={styles.historyPreview}>{conversation.last_message || "点击查看对话"}</span>
              </button>
              <button className={styles.historyMenuTrigger} type="button" aria-label={`操作 ${conversation.title}`} onClick={() => setOpenConversationMenu(current => current === conversation.id ? null : conversation.id)}><Icon name="moreVertical" /></button>
              {openConversationMenu === conversation.id && <div className={styles.historyMenu}>
                <button type="button" onClick={() => void updateConversationState(conversation.id, { is_pinned: !conversation.is_pinned }).then(() => setOpenConversationMenu(null))}><Icon name="pin" />{conversation.is_pinned ? "取消固定" : "固定"}</button>
                <button type="button" onClick={() => openRenameDialog(conversation)}><Icon name="rename" />重命名</button>
                <button type="button" className={styles.destructiveMenuItem} onClick={() => openDeleteDialog(conversation)}><Icon name="delete" />删除</button>
              </div>}
            </div>
          ))}
        </div>

        <div className={styles.sidebarUser}>
          <span className={styles.userAvatar}><Icon name="brand" /></span>
          <div className={styles.userInfo}>
            <span className={styles.userName}>浙江综合交通大数据</span>
            <span className={styles.planName}>智能体平台</span>
          </div>
        </div>

      </aside>

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
      ) : <main className={styles.mainArea}>
        <div className={styles.chatContainer}>
          {messages.length === 0 ? (
            <div className={styles.welcomeState}>
              {selectedTool ? <span className={styles.welcomeIcon}>AI</span> : <span className={styles.welcomeKicker}>JIAO SHU ZHI HANG</span>}
              <h2>{selectedTool?.name || "交数智航"}</h2>
              <p className={styles.typewriterText}>{selectedTool?.description || brandText}<span className={styles.typewriterCursor} aria-hidden="true" /></p>
              {!selectedTool && <p className={styles.companyIntro}>面向交通与政企业务的数据分析和智能任务协作平台</p>}
            </div>
          ) : (
            <div className={styles.messagesWrapper}>
              {messages.map((message, messageIndex) => (
                <div
                  key={message.id || messageIndex}
                  className={`${styles.messageRow} ${message.type === "user" ? styles.rowUser : styles.rowAi}`}
                >
                  {message.type === "user" ? (
                    <div className={styles.userBubble}>
                      {message.attachedFile && <div className={styles.attachmentCard}><Icon name="file" />{message.attachedFile}</div>}
                      <div className={styles.userText}>{message.text}</div>
                    </div>
                  ) : message.type === "error" ? (
                    <div className={styles.errorBubble}>{message.text}</div>
                  ) : (
                    <div className={styles.aiBubble}>
                      {Boolean(message.groups?.length) && (
                        <div className={styles.agentGroups}>
                          {message.groups?.map(group => (
                            <section key={group.id} className={`${styles.taskGroup} ${group.isOpen ? styles.taskGroupOpen : ""}`}>
                              <button
                                type="button"
                                className={styles.groupHeader}
                                onClick={() => toggleTaskGroup(messageIndex, group.id)}
                                aria-expanded={group.isOpen}
                              >
                                <span className={styles.groupTitle}>{group.title}</span>
                                <Icon name="chevron" className={`${styles.groupChevron} ${group.isOpen ? styles.chevronOpen : ""}`} />
                              </button>
                              {group.isOpen && (
                                <div className={styles.groupContent}>
                                  {group.thoughts.map((thought, thoughtIndex) => (
                                    <div key={`${group.id}-thought-${thoughtIndex}`} className={styles.thoughtText}>{thought}</div>
                                  ))}
                                  {group.actions.map((action, actionIndex) => (
                                    <div key={`${group.id}-${actionIndex}`} className={styles.actionItem}>
                                      <span className={styles.actionLine} />
                                      <span>{action.text}</span>
                                      <small>{action.status === "loading" ? "进行中" : "已完成"}</small>
                                    </div>
                                  ))}
                                </div>
                              )}
                            </section>
                          ))}
                        </div>
                      )}
                      {message.type === "ai-stream" && !message.text && !message.groups?.length && <span className={styles.streamPending}>正在思考</span>}
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
                            <div className={styles.fileCardName}><Icon name="package" />{artifact.title} · {artifact.artifactType.toUpperCase()}</div>
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
                </div>
              ))}
              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        <div className={styles.inputAreaWrapper}>
          <div className={styles.inputContainer}>
            {files.map((selectedFile, fileIndex) => (
              <div className={styles.fileTag} key={`${selectedFile.name}-${selectedFile.size}-${fileIndex}`}>
                <Icon name="file" /> <strong>{selectedFile.name}</strong>
                <button type="button" className={styles.removeFileBtn} onClick={() => setFiles(current => current.filter((_, index) => index !== fileIndex))} aria-label={`移除附件 ${selectedFile.name}`}><Icon name="close" /></button>
              </div>
            ))}
            <form onSubmit={handleFormSubmit} className={styles.inputForm}>
              <label htmlFor="file-input" className={styles.attachBtn} title="上传参考文件"><Icon name="upload" /></label>
              <input
                id="file-input"
                type="file"
                multiple
                accept=".pdf,.docx,.txt,.md,.markdown,.pptx,.mp3,.m4a,.wav,.aac,.ogg,.flac"
                hidden
                onChange={event => {
                  const selectedFiles = Array.from(event.target.files || []);
                  setFiles(current => [...current, ...selectedFiles].slice(0, 20));
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
                disabled={isProcessing ? !activeRunId || isCancelling : !inputValue.trim() && !files.length}
                className={`${styles.sendBtn} ${isProcessing ? styles.stopBtn : (inputValue.trim() || files.length) ? styles.sendBtnActive : styles.sendBtnDisabled}`}
                aria-label={isProcessing ? (isCancelling ? "正在中断" : "中断生成") : "发送"}
                title={isProcessing ? (isCancelling ? "正在中断" : "中断生成") : "发送"}
                onClick={isProcessing ? () => void cancelActiveRun() : undefined}
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
                <iframe key={isPreviewFullscreen ? "fullscreen" : "panel"} className={styles.previewIframe} title="产物预览" sandbox="allow-scripts" srcDoc={activePreviewContent} />
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

      {pendingApproval && <div className={styles.dialogBackdrop}>
        <section className={styles.dialogPanel} role="dialog" aria-modal="true" aria-labelledby="approval-dialog-title">
          <header><h2 id="approval-dialog-title">确认执行计划</h2></header>
          <p>{approvalSummary}</p>
          <div className={styles.approvalToolName}>{approvalToolLabels[pendingApproval.name] || pendingApproval.name}</div>
          {Object.keys(approvalDetails).length > 0 && <pre className={styles.approvalArguments}>{JSON.stringify(approvalDetails, null, 2)}</pre>}
          <footer>
            <button type="button" className={styles.dialogSecondary} disabled={isDecidingApproval} onClick={() => void decideApproval("reject")}>拒绝</button>
            <button type="button" className={styles.dialogPrimary} disabled={isDecidingApproval} onClick={() => void decideApproval("approve")}>{isDecidingApproval ? "执行中" : "确认并执行"}</button>
          </footer>
        </section>
      </div>}
    </div>
  );
}
