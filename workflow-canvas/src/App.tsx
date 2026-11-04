import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import Collections from "./Collections";
import cairnLogo from "./assets/logo-cairn.svg";
import {
  addEdge,
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import {
  Activity,
  ArrowDownToLine,
  BookOpenCheck,
  Check,
  ChevronDown,
  CircleHelp,
  Clock3,
  Cloud,
  Code2,
  GitBranch,
  History,
  Image,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  MousePointer2,
  Play,
  Plus,
  ScanLine,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  WandSparkles,
  X,
} from "lucide-react";

type CatalogEntry = {
  node_id: string;
  type: "auto" | "human";
  tool: string | null;
  params: Record<string, string>;
};
type GraphNode = Node<{ node_id: string; params: Record<string, unknown> }>;
type GraphDoc = {
  nodes: Array<{
    id: string;
    node_id: string;
    params: Record<string, unknown>;
    position: { x: number; y: number };
  }>;
  edges: Array<{ source: string; target: string }>;
};
type WorkflowData = {
  workflow_id: string;
  title: string;
  revision: number;
  graph: GraphDoc;
  active_version_id: string | null;
};

const API_ORIGIN_KEY = "cairn-studio-api-origin";
const TOKEN_KEY = "cairn-studio-token";
const LOCALE_KEY = "cairn-studio-locale";
type Locale = "zh-Hans" | "en";
const LocaleContext = createContext<{ locale: Locale; setLocale: (locale: Locale) => void }>({
  locale: "zh-Hans", setLocale: () => {},
});
const labels: Record<
  string,
  { title: string; subtitle: string; icon: typeof ScanLine; color: string }
> = {
  upright: {
    title: "摆正页面",
    subtitle: "自动识别纸张边缘",
    icon: ScanLine,
    color: "violet",
  },
  crop: {
    title: "裁切确认",
    subtitle: "家长确认页面范围",
    icon: Image,
    color: "amber",
  },
  cut: {
    title: "切分题目",
    subtitle: "识别每一道题",
    icon: Layers3,
    color: "blue",
  },
  select: {
    title: "圈选入库",
    subtitle: "选择要加入的题目",
    icon: MousePointer2,
    color: "green",
  },
  recognize: {
    title: "读取文字",
    subtitle: "提取题干与题号",
    icon: Code2,
    color: "cyan",
  },
  review: {
    title: "人工复核",
    subtitle: "确认识别内容",
    icon: ShieldCheck,
    color: "rose",
  },
  enhance: {
    title: "图像增强",
    subtitle: "优化纸面与字迹",
    icon: Sparkles,
    color: "orange",
  },
  strategy: {
    title: "复习策略",
    subtitle: "为这批题目安排复习",
    icon: Clock3,
    color: "green",
  },
  guided_tutor: {
    title: "引导讲解",
    subtitle: "练习时提供文字提示",
    icon: BookOpenCheck,
    color: "violet",
  },
  text_recap: {
    title: "文字复述",
    subtitle: "练习时解释解题思路",
    icon: BookOpenCheck,
    color: "violet",
  },
  collection_append: {
    title: "收录到题集",
    subtitle: "将已确认的题目加入题集",
    icon: BookOpenCheck,
    color: "green",
  },
  collection_source: { title: "题集版本", subtitle: "使用已发布的题集", icon: BookOpenCheck, color: "green" },
};
const labelsToIcons: Record<string, typeof ScanLine> = {
  upright: ScanLine,
  crop: Image,
  cut: Layers3,
  select: MousePointer2,
  recognize: Code2,
  review: ShieldCheck,
  enhance: Sparkles,
  strategy: Clock3,
  guided_tutor: BookOpenCheck,
  text_recap: BookOpenCheck,
  collection_append: BookOpenCheck,
  collection_source: BookOpenCheck,
};
const englishNodeText: Record<string, { title: string; subtitle: string }> = {
  upright: { title: "Straighten page", subtitle: "Find the paper edges" },
  crop: { title: "Confirm crop", subtitle: "Check the page boundary" },
  cut: { title: "Split questions", subtitle: "Find individual questions" },
  select: { title: "Select questions", subtitle: "Choose questions to keep" },
  recognize: { title: "Read text", subtitle: "Extract text and numbers" },
  review: { title: "Human review", subtitle: "Check the recognized content" },
  enhance: { title: "Enhance image", subtitle: "Improve the page and writing" },
  strategy: { title: "Review strategy", subtitle: "Schedule review for these questions" },
  guided_tutor: { title: "Guided help", subtitle: "Offer text hints during practice" },
  text_recap: { title: "Text recall", subtitle: "Explain your reasoning" },
  collection_append: { title: "Add to collection", subtitle: "Save reviewed questions to a collection" },
  collection_source: { title: "Collection version", subtitle: "Use a published collection" },
};
const englishWorkflowErrors: Record<string, string> = {
  "工作流有未连接节点": "Some nodes are not connected.",
  "工作流必须只有一个起点；存在环或多个断开的链路": "The workflow must have one starting point; check for loops or disconnected paths.",
  "工作流存在环": "The workflow contains a loop.",
  "工作流必须是一条从头到尾的完整单链路": "Connect the workflow as one complete path.",
  "MVP 仅支持单链路；不允许分支或汇合": "Only one path is supported; remove branches or merging connections.",
  "扫描流程必须按顺序包含摆正、裁切、切题、圈选、认字和复核": "The scan workflow must include straighten, crop, split, select, read text, and review, in that order.",
  "增强必须位于裁切之后、切题之前": "Image enhancement must come after crop and before split questions.",
  "引导讲解必须位于人工复核之后": "Guided help must come after human review.",
  "文字复述必须位于人工复核之后": "Text recall must come after human review.",
  "收录到题集必须位于人工复核之后": "Add to collection must come after human review.",
  "请选择有效的题集": "Choose a valid collection.",
  "题集不存在或不属于当前学习者": "The collection is missing or belongs to another learner.",
  "复习策略必须在认字之后设置": "The review strategy must come after read text.",
  "请选择复习策略": "Choose a review strategy.",
  "复习策略未注册": "This review strategy is not registered.",
};

function nodeText(nodeId: string, locale: Locale) {
  const meta = labels[nodeId] || { ...labels.cut, title: nodeId, subtitle: "" };
  return locale === "en" ? { ...meta, ...(englishNodeText[nodeId] || {}) } : meta;
}

function StepNode({ data, selected }: NodeProps<GraphNode>) {
  const { locale } = useContext(LocaleContext);
  const meta = nodeText(data.node_id, locale);
  const Icon = labelsToIcons[data.node_id] || Activity;
  const manual = ["crop", "select", "review"].includes(data.node_id);
  return (
    <div className={`step-node ${selected ? "is-selected" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <div className={`step-node__icon ${meta.color}`}>
        <Icon size={18} strokeWidth={1.8} />
      </div>
      <div className="step-node__copy">
        <strong>{meta.title}</strong>
        <span>{meta.subtitle}</span>
      </div>
      <span className={`node-kind ${manual ? "human" : "auto"}`}>
        {manual ? (locale === "en" ? "Human" : "人工") : (locale === "en" ? "Auto" : "自动")}
      </span>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes: NodeTypes = { step: StepNode };

function originDefault() {
  const saved = localStorage.getItem(API_ORIGIN_KEY);
  if (saved) return saved;
  const port = import.meta.env.VITE_CAIRN_API_PORT;
  if (port && /^\d{1,5}$/.test(port) && Number(port) <= 65535 && Number(port) > 0) {
    const api = new URL(window.location.origin);
    api.port = port;
    return api.origin;
  }
  return window.location.origin;
}

export default function App() {
  const [locale, setLocale] = useState<Locale>(() => localStorage.getItem(LOCALE_KEY) === "en" ? "en" : "zh-Hans");
  useEffect(() => {
    localStorage.setItem(LOCALE_KEY, locale);
    document.documentElement.lang = locale;
    document.title = locale === "en" ? "Cairn Studio · Workflows" : "Cairn Studio · 工作流";
  }, [locale]);
  return (
    <LocaleContext.Provider value={{ locale, setLocale }}>
      <ReactFlowProvider><Studio /></ReactFlowProvider>
    </LocaleContext.Provider>
  );
}

function Studio() {
  const { locale, setLocale } = useContext(LocaleContext);
  const t = (zh: string, en: string) => locale === "en" ? en : zh;
  const flowApi = useReactFlow();
  const [apiOrigin, setApiOrigin] = useState(originDefault);
  const [token, setToken] = useState(
    () => sessionStorage.getItem(TOKEN_KEY) || "",
  );
  const [connectedOrigin, setConnectedOrigin] = useState(apiOrigin);
  const [connectedToken, setConnectedToken] = useState(token);
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [strategies, setStrategies] = useState<Array<{ id: string; name: string }>>([]);
  const [workflow, setWorkflow] = useState<WorkflowData | null>(null);
  const [nodes, setNodes, onNodesChange] = useNodesState<GraphNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState<{
    kind: "ok" | "error" | "info";
    text: string;
  } | null>(null);
  const [busy, setBusy] = useState("");
  const [panel, setPanel] = useState<"inspect" | "settings">("inspect");
  const [search, setSearch] = useState("");
  const [showPublish, setShowPublish] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showRuns, setShowRuns] = useState(false);
  const [showCollections, setShowCollections] = useState(false);
  const [availableCollections, setAvailableCollections] = useState<Array<{ id: number; name: string; subject: string }>>([]);
  const [collectionVersions, setCollectionVersions] = useState<Array<{ id: number; number: number; name: string; subject: string }>>([]);
  const [runs, setRuns] = useState<Array<{ scan_id: number; version_id: string; status: string; updated_at: string }>>([]);
  const [versions, setVersions] = useState<
    Array<{
      version_id: string;
      number: number;
      published_at: string;
      runtime: { steps: Array<{ node_id: string }> };
    }>
  >([]);
  const [preview, setPreview] = useState<{
    active_version_id?: string | null;
    current?: { steps: Array<{ node_id: string }> } | null;
    candidate: { steps: Array<{ node_id: string; type: string }> };
  } | null>(null);

  const api = useCallback(
    async (path: string, init: RequestInit = {}, connection?: { origin: string; token: string }) => {
      const base = (connection?.origin ?? connectedOrigin).trim().replace(/\/$/, "");
      const activeToken = connection?.token ?? connectedToken;
      const headers = new Headers(init.headers);
      if (init.body) headers.set("Content-Type", "application/json");
      if (activeToken) headers.set("Authorization", `Bearer ${activeToken}`);
      const response = await fetch(`${base}${path}`, { ...init, headers });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (locale === "en") {
          const message = response.status === 409 ? (path.startsWith("/api/collections")
            ? "The collection changed elsewhere or is still used by a workflow. Reload and check its dependencies."
            : "The draft changed elsewhere. Reload it before saving or publishing.")
            : response.status === 422 ? englishWorkflowErrors[data.detail?.message] || "This workflow is invalid. Check the nodes, connections, and settings."
            : response.status === 401 ? "The operator token is invalid. Check connection settings."
            : response.status === 503 ? "The workflow service or operator token is not configured."
            : `Request failed (${response.status}).`;
          throw new Error(message);
        }
        const detail =
          data.detail?.message ||
          data.detail ||
          `请求失败 (${response.status})`;
        throw new Error(
          typeof detail === "string" ? detail : JSON.stringify(detail),
        );
      }
      return data;
    },
    [connectedOrigin, connectedToken, locale],
  );

  const hydrate = useCallback(
    (doc: WorkflowData) => {
      setWorkflow(doc);
      setNodes(
        doc.graph.nodes.map((item) => ({
          id: item.id,
          type: "step",
          position: item.position || { x: 80, y: 80 },
          data: { node_id: item.node_id, params: item.params || {} },
        })),
      );
      setEdges(
        doc.graph.edges.map((edge, i) => ({
          id: `e-${edge.source}-${edge.target}-${i}`,
          source: edge.source,
          target: edge.target,
          animated: false,
          markerEnd: { type: MarkerType.ArrowClosed, color: "#94a3a8" },
          style: { stroke: "#9caaa5", strokeWidth: 1.7 },
        })),
      );
      setSelectedId(null);
    },
    [setEdges, setNodes],
  );

  const load = useCallback(async (connection?: { origin: string; token: string }) => {
    setBusy("load");
    try {
      const [catalogData, doc] = await Promise.all([
        api("/api/tools", {}, connection),
        api(`/api/workflows/${workflow?.workflow_id || "default"}`, {}, connection),
      ]);
      if (connection) {
        setConnectedOrigin(connection.origin);
        setConnectedToken(connection.token);
        localStorage.setItem(API_ORIGIN_KEY, connection.origin);
        if (connection.token) sessionStorage.setItem(TOKEN_KEY, connection.token);
        else sessionStorage.removeItem(TOKEN_KEY);
      }
      setCatalog(catalogData.nodes);
      setStrategies(catalogData.strategies || []);
      hydrate(doc);
      setNotice(null);
    } catch (error) {
      setNotice({
        kind: "error",
        text: error instanceof Error ? error.message : (locale === "en" ? "Connection failed" : "连接失败"),
      });
    } finally {
      setBusy("");
    }
  }, [api, hydrate, locale, workflow?.workflow_id]);

  useEffect(() => {
    void load();
    // Connection edits are applied only by the explicit Connect action.
    // Re-fetching on every keystroke would overwrite unsaved canvas edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (!workflow) return;
    const timer = window.setTimeout(() => { void flowApi.fitView({ padding: 0.2, maxZoom: 1 }); }, 80);
    return () => window.clearTimeout(timer);
  }, [workflow?.workflow_id, flowApi]);
  const selectedNode = nodes.find((node) => node.id === selectedId) || null;
  useEffect(() => {
    if (selectedNode?.data.node_id !== "collection_append" || showCollections) return;
    let active = true;
    void api("/api/collections").then((result) => {
      if (active) setAvailableCollections(result.collections);
    }).catch(() => { if (active) setAvailableCollections([]); });
    return () => { active = false; };
  }, [api, selectedNode?.data.node_id, showCollections]);
  useEffect(() => {
    if (selectedNode?.data.node_id !== "collection_source" || showCollections) return;
    let active = true;
    void api("/api/collection-versions").then((result) => {
      if (active) setCollectionVersions(result.versions);
    }).catch(() => { if (active) setCollectionVersions([]); });
    return () => { active = false; };
  }, [api, selectedNode?.data.node_id, showCollections]);
  const filteredCatalog = catalog.filter((item) => {
    if (workflow?.workflow_id === "collection") {
      if (!["collection_source", "strategy", "guided_tutor", "text_recap"].includes(item.node_id)) return false;
    } else if (item.node_id === "collection_source") return false;
    const meta = nodeText(item.node_id, locale);
    const text = `${meta.title || item.node_id} ${meta.subtitle || ""}`;
    return text.toLowerCase().includes(search.toLowerCase());
  });
  const graph = useMemo<GraphDoc>(
    () => ({
      nodes: nodes.map((node) => ({
        id: node.id,
        node_id: node.data.node_id,
        params: node.data.params || {},
        position: node.position,
      })),
      edges: edges.map((edge) => ({
        source: edge.source!,
        target: edge.target!,
      })),
    }),
    [nodes, edges],
  );

  const addNode = useCallback(
    (
      nodeId: string,
      position?: { x: number; y: number },
    ) => {
      const id = `${nodeId}-${crypto.randomUUID().slice(0, 6)}`;
      setNodes((current) => {
        const rightmost = current.reduce<GraphNode | null>((last, node) =>
          !last || node.position.x > last.position.x ? node : last, null);
        const nextPosition = position || (rightmost
          ? { x: rightmost.position.x + 280, y: rightmost.position.y }
          : { x: 80, y: 90 });
        return [...current, { id, type: "step", position: nextPosition, data: { node_id: nodeId, params: {} } }];
      });
      setSelectedId(id);
      if (!position) window.setTimeout(() => { void flowApi.fitView({ padding: 0.2, maxZoom: 1 }); }, 80);
    },
    [setNodes, flowApi],
  );

  const onConnect = useCallback(
    (connection: Connection) =>
      setEdges((current) =>
        addEdge(
          {
            ...connection,
            id: `e-${crypto.randomUUID()}`,
            markerEnd: { type: MarkerType.ArrowClosed, color: "#94a3a8" },
            style: { stroke: "#9caaa5", strokeWidth: 1.7 },
          },
          current,
        ),
      ),
    [setEdges],
  );

  const save = async () => {
    if (!workflow) return;
    setBusy("save");
    try {
      const result = await api(`/api/workflows/${workflow.workflow_id}`, {
        method: "POST",
        body: JSON.stringify({
          expected_revision: workflow.revision,
          title: workflow.title,
          graph,
        }),
      });
      setWorkflow({ ...workflow, revision: result.revision, graph });
      setPreview(null);
      setNotice({ kind: "ok", text: result.compiled_preview
        ? t(`草稿已保存 · 修订 ${result.revision}`, `Draft saved · revision ${result.revision}`)
        : t(`草稿已保存 · 修订 ${result.revision}，发布前请完成校验`, `Draft saved · revision ${result.revision}. Validate it before publishing.`) });
    } catch (error) {
      setNotice({
        kind: "error",
        text: error instanceof Error ? error.message : t("保存失败", "Could not save"),
      });
    } finally {
      setBusy("");
    }
  };

  const validate = async () => {
    setBusy("validate");
    try {
      const result = await api(`/api/workflows/${workflow?.workflow_id || "default"}/preview`, {
        method: "POST",
        body: JSON.stringify({ graph }),
      });
      setPreview(result);
      setNotice({
        kind: "ok",
        text: t(`校验通过 · ${result.candidate.steps.length} 个运行步骤`, `Validation passed · ${result.candidate.steps.length} steps`),
      });
    } catch (error) {
      setNotice({
        kind: "error",
        text: error instanceof Error ? error.message : t("校验失败", "Validation failed"),
      });
    } finally {
      setBusy("");
    }
  };

  const publish = async () => {
    setBusy("publish");
    try {
      if (!workflow) throw new Error(t("工作流尚未加载", "The workflow has not loaded"));
      const saved = await api(`/api/workflows/${workflow.workflow_id}`, {
        method: "POST",
        body: JSON.stringify({
          expected_revision: workflow.revision,
          title: workflow.title,
          graph,
        }),
      });
      setWorkflow({ ...workflow, revision: saved.revision, graph });
      if (!saved.compiled_preview) {
        await api(`/api/workflows/${workflow.workflow_id}/preview`, { method: "POST", body: JSON.stringify({ graph }) });
        throw new Error(t("草稿已保存，请先完成校验。", "Draft saved. Validate before publishing."));
      }
      const result = await api(`/api/workflows/${workflow.workflow_id}/publish`, {
        method: "POST",
        body: JSON.stringify({ expected_revision: saved.revision }),
      });
      setWorkflow((current) =>
        current
          ? { ...current, active_version_id: result.active_version_id }
          : current,
      );
      setNotice({ kind: "ok", text: t(`已发布 ${result.version_id}`, `Published ${result.version_id}`) });
      setShowPublish(false);
    } catch (error) {
      setNotice({
        kind: "error",
        text: error instanceof Error ? error.message : t("发布失败", "Could not publish"),
      });
    } finally {
      setBusy("");
    }
  };

  const openHistory = async () => {
    try {
      const result = await api(`/api/workflows/${workflow?.workflow_id || "default"}/versions`);
      setVersions(result.versions);
      setShowHistory(true);
    } catch (error) {
      setNotice({
        kind: "error",
        text: error instanceof Error ? error.message : t("无法读取版本记录", "Could not load versions"),
      });
    }
  };

  const openRuns = async () => {
    try {
      const result = await api("/api/workflows/default/runs");
      setRuns(result.runs);
      setShowRuns(true);
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : t("无法读取运行记录", "Could not load runs") });
    }
  };

  const rollback = async (versionId: string) => {
    setBusy("rollback");
    try {
      const result = await api(
        `/api/workflows/${workflow?.workflow_id || "default"}/rollback/${encodeURIComponent(versionId)}`,
        { method: "POST" },
      );
      setWorkflow((current) =>
        current
          ? { ...current, active_version_id: result.active_version_id }
          : current,
      );
      setNotice({
        kind: "ok",
        text: workflow?.workflow_id === "collection"
          ? t(`已切换到 ${versionId}；已有学习记录保留。`, `Activated ${versionId}; existing practice records are kept.`)
          : t(`已切换到 ${versionId}；正在处理的卷子不受影响。`, `Activated ${versionId}; scans already in progress are unchanged.`),
      });
      setShowHistory(false);
    } catch (error) {
      setNotice({
        kind: "error",
        text: error instanceof Error ? error.message : t("回滚失败", "Could not activate the version"),
      });
    } finally {
      setBusy("");
    }
  };

  const updateParam = (key: string, value: unknown) => {
    if (!selectedNode) return;
    setNodes((current) =>
      current.map((node) =>
        node.id === selectedNode.id
          ? {
              ...node,
              data: {
                ...node.data,
                params: { ...node.data.params, [key]: value },
              },
            }
          : node,
      ),
    );
  };

  const switchWorkflow = async (id: string) => {
    if (!workflow || id === workflow.workflow_id) return;
    if (JSON.stringify(graph) !== JSON.stringify(workflow.graph) &&
        !window.confirm(t("切换会放弃未保存的画布修改，确定继续吗？", "Switching will discard unsaved canvas changes. Continue?"))) return;
    setBusy("load");
    try {
      const doc = await api(`/api/workflows/${id}`);
      hydrate(doc);
      setPreview(null);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : t("切换失败", "Could not switch") });
    } finally { setBusy(""); }
  };

  const deactivateCollection = async () => {
    setBusy("deactivate");
    try {
      await api("/api/workflows/collection/deactivate", { method: "POST" });
      setWorkflow((current) => current ? { ...current, active_version_id: null } : current);
      setShowHistory(false);
      setNotice({ kind: "ok", text: t("题集流程已停用。", "Collection workflow deactivated.") });
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : t("停用失败", "Could not deactivate") });
    } finally { setBusy(""); }
  };

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      const nodeId = event.dataTransfer.getData("application/cairn-node");
      if (!nodeId) return;
      addNode(
        nodeId,
        flowApi.screenToFlowPosition({ x: event.clientX, y: event.clientY }),
      );
    },
    [addNode, flowApi],
  );

  return (
    <main className="studio-shell">
      <header className="topbar">
        <div className="brand">
          <img className="brand-mark" src={cairnLogo} alt="" />
          <b>Cairn 凯恩</b>
          <span className="brand-divider" />
          <span className="product-name">Studio</span>
          <span className="beta-tag">WORKFLOWS</span>
        </div>
        <div className="topbar-center">
          <span className="crumb-muted">{t("工作流", "Workflow")}</span>
          <ChevronDown size={14} />
          <select aria-label={t("选择工作流", "Select workflow")} value={workflow?.workflow_id || "default"} disabled={!!busy}
            onChange={(event) => void switchWorkflow(event.target.value)}>
            <option value="default">{t("扫描流程", "Scan workflow")}</option>
            <option value="collection">{t("题集练习流程", "Collection workflow")}</option>
          </select>
          <span className="draft-status">
            <i />
            {t("草稿", "Draft")}
          </span>
        </div>
        <div className="topbar-actions">
          <button className="button secondary" type="button" onClick={() => setShowCollections(true)} disabled={!!busy}>
            <BookOpenCheck size={15} />{t("题集", "Collections")}
          </button>
          <button className="button secondary" type="button" aria-label={t("切换语言", "Change language")}
            onClick={() => { setNotice(null); setLocale(locale === "en" ? "zh-Hans" : "en"); }}>
            {locale === "en" ? "中文" : "English"}
          </button>
          <button
            className="icon-button ghost"
            onClick={() =>
              setPanel(panel === "settings" ? "inspect" : "settings")
            }
            title={t("连接设置", "Connection settings")}
          >
            <Settings2 size={17} />
          </button>
          <button
            className="button secondary"
            onClick={() => void openHistory()}
            disabled={!!busy}
          >
            <History size={15} />
            {t("版本记录", "Versions")}
          </button>
          <button
            className="button secondary"
            onClick={() => void validate()}
            disabled={!!busy}
          >
            <Play size={15} />
            {t("校验预览", "Validate & preview")}
          </button>
          <button
            className="button primary"
            onClick={() => setShowPublish(true)}
            disabled={!!busy}
          >
            <Cloud size={16} />
            {t("发布流程", "Publish")}
          </button>
          <button className="avatar">B</button>
        </div>
      </header>
      <div className="workspace">
        <aside className="left-rail">
          <button className="rail-button active" title={t("画布", "Canvas")}>
            <GitBranch size={19} />
          </button>
          <button
            className="rail-button"
            title={t("运行记录", "Runs")}
            onClick={() => void openRuns()}
            disabled={workflow?.workflow_id === "collection"}
          >
            <Activity size={19} />
          </button>
          <button
            className="rail-button"
            title={t("文档", "Documentation")}
            onClick={() =>
              setNotice({
                kind: "info",
                text: t("工作流契约见 docs/FRONTEND-PLAN-2026-11.md §4.5–4.6。", "See docs/FRONTEND-PLAN-2026-11.md §4.5–4.6 for the workflow contract."),
              })
            }
          >
            <CircleHelp size={19} />
          </button>
          <div className="rail-spacer" />
          <div className="rail-bottom">
            <div className="online-dot" />
            <span>{t("本地工作区", "Local workspace")}</span>
          </div>
        </aside>
        <section className="node-sidebar">
          <div className="sidebar-heading">
            <div>
              <span className="eyebrow">BUILD</span>
              <h1>{t("节点库", "Nodes")}</h1>
            </div>
          </div>
          <p className="sidebar-intro">{t("拖动节点到画布，连接后组成学习流程。", "Drag nodes onto the canvas and connect them to build a workflow.")}</p>
          <label className="search-box">
            <Search size={15} />
            <input
              placeholder={t("查找节点", "Find a node")}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <div className="category-label">
            <span>{t("学习流程", "Learning workflow")}</span>
            <span>{filteredCatalog.length}</span>
          </div>
          <div className="node-list">
            {filteredCatalog.map((item) => {
              const meta = nodeText(item.node_id, locale);
              const Icon = labelsToIcons[item.node_id] || Activity;
              return (
                <button
                  key={item.node_id}
                  className="palette-card"
                  draggable
                  onDragStart={(event) =>
                    event.dataTransfer.setData(
                      "application/cairn-node",
                      item.node_id,
                    )
                  }
                  onClick={() => addNode(item.node_id)}
                >
                  <span className={`palette-icon ${meta.color}`}>
                    <Icon size={17} />
                  </span>
                  <span className="palette-copy">
                    <strong>{meta.title}</strong>
                    <small>{meta.subtitle}</small>
                  </span>
                  <Plus size={15} className="palette-add" />
                </button>
              );
            })}
          </div>
          <div className="sidebar-tip">
            <Sparkles size={16} />
            <span>{t("连接节点即可定义执行顺序。当前版本支持一条线性流程。", "Connect nodes in order. This version supports one linear path.")}</span>
          </div>
        </section>
        <section
          className="canvas-area"
          onDrop={onDrop}
          onDragOver={(event) => event.preventDefault()}
        >
          <div className="canvas-toolbar">
            <div className="canvas-title">
              <span className="live-dot" />
              {t("工作流画布", "Workflow canvas")}
              <span className="canvas-divider" />
              {t("自动保存已关闭", "Autosave off")}
            </div>
            <div className="canvas-tools">
              <button
                className="tool-btn"
                onClick={() => void save()}
                title={t("保存草稿", "Save draft")}
              >
                <ArrowDownToLine size={16} />
              </button>
            </div>
          </div>
          <div className="flow-wrap">
            {busy === "load" && (
              <div className="loading-card">
                <LoaderCircle className="spin" size={20} />
                {t("正在连接 Cairn 工作流服务…", "Connecting to Cairn…")}
              </div>
            )}
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onNodeClick={(_, node) => setSelectedId(node.id)}
              onPaneClick={() => setSelectedId(null)}
              fitView
              fitViewOptions={{ padding: 0.24 }}
            >
              <Background
                variant={BackgroundVariant.Dots}
                gap={22}
                size={1}
                color="#dfe5e2"
              />
              <Controls position="bottom-left" showInteractive={false} />
              <MiniMap
                position="bottom-right"
                nodeColor={(node) =>
                  labels[node.data?.node_id as string]?.color === "green"
                    ? "#96cdb7"
                    : "#bac9c2"
                }
                maskColor="rgba(245,248,246,.65)"
              />
            </ReactFlow>
            {!nodes.length && (
              <div className="empty-canvas">
                <div className="empty-orb">
                  <WandSparkles size={24} />
                </div>
                <h2>{t("从一个节点开始", "Start with a node")}</h2>
                <p>{t("从左侧拖入节点，然后用连接点串起你的学习流程。", "Drag in a node, then connect it to build your workflow.")}</p>
                <button
                  className="button secondary"
                  onClick={() => addNode("upright")}
                >
                  <Plus size={15} />
                  {t("添加第一个节点", "Add a node")}
                </button>
              </div>
            )}
            <div className="canvas-footnote">
              <span>
                <span className="legend-dot auto-dot" />
                {t("自动处理", "Automatic")}
              </span>
              <span>
                <span className="legend-dot human-dot" />
                {t("需要人工确认", "Human review")}
              </span>
              <span className="footnote-separator" />
              {t("拖拽节点以重新排序", "Drag nodes to arrange them")}
            </div>
          </div>
        </section>
        <aside className="inspector">
          <div className="inspector-header">
            <div className="inspector-tabs">
              <button
                className={panel === "inspect" ? "selected" : ""}
                onClick={() => setPanel("inspect")}
              >
                {t("配置", "Configure")}
              </button>
              <button
                className={panel === "settings" ? "selected" : ""}
                onClick={() => setPanel("settings")}
              >
                {t("设置", "Settings")}
              </button>
            </div>
          </div>
          {panel === "settings" ? (
            <div className="inspector-body">
              <span className="eyebrow">CONNECTION</span>
              <h2>{t("服务连接", "Connection")}</h2>
              <p className="field-help">
                {t("配置 Cairn 主应用地址和 operator 凭据。凭据仅保存在当前标签页。", "Enter the Cairn service address and operator token. The token stays in this browser tab.")}
              </p>
              <label className="field-label">
                {t("API 地址", "API address")}
                <input
                  className="text-input"
                  value={apiOrigin}
                  onChange={(e) => setApiOrigin(e.target.value)}
                  placeholder="https://cairn.example.com"
                />
              </label>
              <label className="field-label">
                Operator token
                <div className="token-field">
                  <LockKeyhole size={15} />
                  <input
                    type="password"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    placeholder="CAIRN_WORKFLOW_ADMIN_TOKEN"
                  />
                </div>
              </label>
              <button
                className="button primary full"
                onClick={() => {
                  if (workflow && JSON.stringify(graph) !== JSON.stringify(workflow.graph) &&
                      !window.confirm(t("重新加载会替换未保存的画布修改，确定继续吗？", "Reloading will replace your unsaved canvas changes. Continue?"))) return;
                  void load({ origin: apiOrigin, token });
                }}
              >
                <Check size={15} />
                {t("连接并加载", "Connect and load")}
              </button>
              <p className="security-note">
                <LockKeyhole size={14} />
                {t("Token 不会写入工作流或本地永久存储。", "The token is not saved in the workflow or permanent local storage.")}
              </p>
            </div>
          ) : selectedNode ? (
            <div className="inspector-body">
              <div className="inspector-node-title">
                <span
                  className={`palette-icon ${labels[selectedNode.data.node_id]?.color || "blue"}`}
                >
                  {(() => {
                    const Icon =
                      labelsToIcons[selectedNode.data.node_id] || Activity;
                    return <Icon size={18} />;
                  })()}
                </span>
                <div>
                  <span className="eyebrow">NODE SETTINGS</span>
                  <h2>
                    {nodeText(selectedNode.data.node_id, locale)?.title ||
                      selectedNode.data.node_id}
                  </h2>
                </div>
              </div>
              <label className="field-label">
                {t("节点名称", "Node name")}
                <input
                  className="text-input"
                  value={
                    nodeText(selectedNode.data.node_id, locale)?.title ||
                    selectedNode.data.node_id
                  }
                  readOnly
                />
              </label>
              <div className="field-label">
                {t("执行方式", "Execution")}
                <div className="read-only-field">
                  <span
                    className={`kind-dot ${["crop", "select", "review"].includes(selectedNode.data.node_id) ? "human" : "auto"}`}
                  />
                  {["crop", "select", "review"].includes(
                    selectedNode.data.node_id,
                  )
                    ? t("人工处理", "Human")
                    : t("自动处理", "Automatic")}
                  <span className="read-only-tag">{t("由节点类型决定", "Set by node type")}</span>
                </div>
              </div>
              {selectedNode.data.node_id === "select" && (
                <label className="field-label">
                  {t("默认预选机器标记题", "Preselect machine-marked questions")}
                  <div className="switch-row">
                    <span>{t("自动预选标记题", "Preselect marked questions")}</span>
                    <button
                      className={`switch ${selectedNode.data.params.preselect ? "on" : ""}`}
                      aria-pressed={!!selectedNode.data.params.preselect}
                      onClick={() =>
                        updateParam(
                          "preselect",
                          !selectedNode.data.params.preselect,
                        )
                      }
                    >
                      <i />
                    </button>
                  </div>
                  <small className="field-help">
                    {t("家长仍可在确认前逐题修改选择。", "A parent can change each selection before confirming.")}
                  </small>
                </label>
              )}
              {selectedNode.data.node_id === "strategy" && (
                <label className="field-label">
                  {t("复习方法", "Review method")}
                  <select className="text-input" value={String(selectedNode.data.params.strategy || "fsrs")}
                    onChange={(event) => updateParam("strategy", event.target.value)}>
                    {strategies.map((strategy) => <option key={strategy.id} value={strategy.id}>{locale === "en" ? ({fsrs: "FSRS-6 adaptive intervals", ebbinghaus: "Ebbinghaus fixed intervals", leitner: "Leitner boxes"} as Record<string, string>)[strategy.id] || strategy.name : strategy.name}</option>)}
                  </select>
                  <small className="field-help">{t("仅作用于此流程新收录的题目。", "Only applies to questions added by this workflow.")}</small>
                </label>
              )}
              {selectedNode.data.node_id === "collection_append" && (
                <label className="field-label">
                  {t("目标题集", "Destination collection")}
                  <select className="text-input" value={String(selectedNode.data.params.collection_id || "")}
                    onChange={(event) => updateParam("collection_id", event.target.value ? Number(event.target.value) : undefined)}>
                    <option value="">{t("选择题集", "Choose a collection")}</option>
                    {availableCollections.map((collection) => <option key={collection.id} value={collection.id}>
                      {collection.name} · {collection.subject}
                    </option>)}
                  </select>
                  <small className="field-help">{t("仅收录本卷已确认且学科一致的题，写入题集草稿。", "Adds confirmed questions from this scan to the matching-subject collection draft.")}</small>
                </label>
              )}
              {selectedNode.data.node_id === "collection_source" && (
                <label className="inspector-field">
                  <span>{t("已发布题集版本", "Published collection version")}</span>
                  <select className="text-input" value={String(selectedNode.data.params.version_id || "")}
                    onChange={(event) => updateParam("version_id", event.target.value ? Number(event.target.value) : undefined)}>
                    <option value="">{t("选择题集版本", "Choose a collection version")}</option>
                    {collectionVersions.map((version) => <option key={version.id} value={version.id}>
                      {version.name} · {version.subject} · v{version.number}
                    </option>)}
                  </select>
                </label>
              )}
              <div className="inspector-divider" />
              <div className="meta-row">
                <span>{t("节点 ID", "Node ID")}</span>
                <code>{selectedNode.data.node_id}</code>
              </div>
              <div className="meta-row">
                <span>{t("节点引用", "Node reference")}</span>
                <code>{selectedNode.id}</code>
              </div>
              <button
                className="delete-node"
                onClick={() => {
                  setNodes((current) =>
                    current.filter((node) => node.id !== selectedNode.id),
                  );
                  setEdges((current) =>
                    current.filter(
                      (edge) =>
                        edge.source !== selectedNode.id &&
                        edge.target !== selectedNode.id,
                    ),
                  );
                  setSelectedId(null);
                }}
              >
                <X size={14} />
                {t("移除节点", "Remove node")}
              </button>
            </div>
          ) : (
            <div className="inspector-body overview-body">
              <div className="overview-icon">
                <BookOpenCheck size={21} />
              </div>
              <span className="eyebrow">WORKFLOW OVERVIEW</span>
              <h2>{t("学习流程", "Learning workflow")}</h2>
              <p>
                {workflow?.workflow_id === "collection"
                  ? t("选择题集的已发布版本，再连接需要的学习节点。", "Choose a published collection version and connect the learning steps you need.")
                  : t("将节点按希望执行的顺序连接。发布后，新创建的卷子将固定使用当前版本。", "Connect nodes in order. New scans use the published version active when they are created.")}
              </p>
              <div className="overview-stat">
                <div>
                  <span>{t("当前节点", "Nodes")}</span>
                  <strong>
                    {nodes.length}
                    <small>{t(" 个", " nodes")}</small>
                  </strong>
                </div>
                <div>
                  <span>{t("自动保存", "Autosave")}</span>
                  <strong>{t("关闭", "Off")}</strong>
                </div>
              </div>
              <div className="overview-card">
                <div className="overview-card-icon">
                  <Clock3 size={16} />
                </div>
                <div>
                  <strong>{t("版本固定", "Pinned versions")}</strong>
                  <small>{workflow?.workflow_id === "collection" ? t("题集内容固定在所选版本。", "The collection content is fixed to the selected version.") : t("进行中的卷子不会被新发布打断。", "Publishing won't interrupt scans already in progress.")}</small>
                </div>
              </div>
              <div className="overview-card">
                <div className="overview-card-icon">
                  <GitBranch size={16} />
                </div>
                <div>
                  <strong>{t("线性运行", "Linear execution")}</strong>
                  <small>{t("当前支持单一路径，不支持分支和循环。", "One path is supported; branches and loops are not yet available.")}</small>
                </div>
              </div>
            </div>
          )}
        </aside>
      </div>
      <footer className="statusbar">
        <span className="status-left">
          <span className="status-led" />
          {notice?.text || t("所有改动需手动保存", "Changes must be saved manually")}
        </span>
        <span className="status-right">
          {workflow ? t(`修订 ${workflow.revision}`, `Revision ${workflow.revision}`) : t("未连接", "Not connected")}
          <span className="status-sep" />
          {workflow?.active_version_id
            ? t(`活动版本 ${workflow.active_version_id}`, `Active ${workflow.active_version_id}`)
            : t("未发布", "Not published")}
          <span className="status-sep" />
          Cairn Studio
        </span>
      </footer>
      {notice && (
        <div className={`toast ${notice.kind}`}>
          <span>
            {notice.kind === "ok" ? (
              <Check size={16} />
            ) : notice.kind === "error" ? (
              <X size={16} />
            ) : (
              <CircleHelp size={16} />
            )}
          </span>
          {notice.text}
          <button onClick={() => setNotice(null)}>
            <X size={14} />
          </button>
        </div>
      )}
      {preview && (
        <div className="preview-panel">
          <div className="preview-head">
            <strong>
              <Code2 size={15} />
              {t("编译预览", "Compiled preview")}
            </strong>
            <button onClick={() => setPreview(null)}>
              <X size={15} />
            </button>
          </div>
          <div className="preview-diff">
            <span>{t("当前版本", "Current version")} {preview.active_version_id || t("尚未发布", "Not published")}</span>
            <strong>
              {(preview.current?.steps || [])
                .map((step) => nodeText(step.node_id, locale)?.title || step.node_id)
                .join(" → ") || t("空流程", "Empty workflow")}
              <b> → </b>
              {preview.candidate.steps
                .map((step) => nodeText(step.node_id, locale)?.title || step.node_id)
                .join(" → ") || t("空流程", "Empty workflow")}
            </strong>
          </div>
          <pre>{JSON.stringify(preview.candidate, null, 2)}</pre>
        </div>
      )}
      {showCollections && <Collections api={api} origin={connectedOrigin} token={connectedToken} locale={locale} onClose={() => setShowCollections(false)} />}
      {showPublish && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setShowPublish(false);
          }}
        >
          <div className="publish-modal">
            <div className="modal-icon">
              <Cloud size={21} />
            </div>
            <button
              className="modal-close"
              onClick={() => setShowPublish(false)}
              aria-label={t("关闭", "Close")}
            >
              <X size={17} />
            </button>
            <span className="eyebrow">PUBLISH WORKFLOW</span>
            <h2>{t("发布新的工作流版本？", "Publish a new version?")}</h2>
            <p>{workflow?.workflow_id === "collection" ? t("发布会固定所选题集版本和学习设置。", "Publishing pins the selected collection version and learning settings.") : t("发布会创建不可变版本。正在处理的卷子继续使用创建时的版本。", "Publishing creates an immutable version. Scans already in progress keep their original version.")}</p>
            <div className="publish-summary">
              <span>{t("将要发布", "To publish")}</span>
              <strong>
                {workflow?.title === "默认扫描流程" ? t("默认扫描流程", "Default scan workflow") : (workflow?.title || t("工作流", "Workflow"))}{" "}
                <small>· {nodes.length} {t("个节点", "nodes")}</small>
              </strong>
              <span>{t("当前活动版本", "Active version")}</span>
              <strong>{workflow?.active_version_id || t("尚未发布", "Not published")}</strong>
            </div>
            <div className="modal-actions">
              <button
                className="button secondary"
                onClick={() => setShowPublish(false)}
              >
                {t("再检查一下", "Review again")}
              </button>
              <button
                className="button primary"
                onClick={() => void publish()}
                disabled={!!busy}
              >
                {busy === "publish" ? (
                  <LoaderCircle className="spin" size={15} />
                ) : (
                  <Cloud size={15} />
                )}
                {t("确认发布", "Publish version")}
              </button>
            </div>
          </div>
        </div>
      )}
      {showRuns && (
        <div className="modal-backdrop" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setShowRuns(false);
        }}>
          <div className="publish-modal history-modal" role="dialog" aria-modal="true" aria-labelledby="runs-title">
            <div className="modal-icon"><Activity size={21} /></div>
            <button className="modal-close" onClick={() => setShowRuns(false)} aria-label={t("关闭", "Close")}><X size={17} /></button>
            <span className="eyebrow">RECENT RUNS</span>
            <h2 id="runs-title">{t("运行记录", "Runs")}</h2>
            <p>{t("最近 50 条记录，不包含题目或照片内容。", "The latest 50 runs, without question text or images.")}</p>
            <div className="version-list">
              {runs.length === 0 && <p>{t("还没有运行记录。", "No runs yet.")}</p>}
              {runs.map((run) => (
                <div className="version-row" key={run.scan_id}>
                  <div><strong>{t("卷子", "Scan")} #{run.scan_id} · {run.version_id}</strong>
                    <small>{(locale === "en" ? ({ pending: "Not started", waiting: "Waiting for review", failed: "Needs attention", completed: "Complete" } as Record<string, string>) : ({ pending: "待开始", waiting: "等待人工处理", failed: "需要处理", completed: "已完成" } as Record<string, string>))[run.status] || t("未知状态", "Unknown status")} · {new Date(run.updated_at).toLocaleString(locale)}</small>
                  </div>
                </div>
              ))}
            </div>
            <div className="modal-actions"><button className="button secondary" onClick={() => setShowRuns(false)}>{t("关闭", "Close")}</button></div>
          </div>
        </div>
      )}
      {showHistory && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setShowHistory(false);
          }}
        >
          <div className="publish-modal history-modal">
            <div className="modal-icon">
              <History size={21} />
            </div>
            <button
              className="modal-close"
              onClick={() => setShowHistory(false)}
              aria-label={t("关闭", "Close")}
            >
              <X size={17} />
            </button>
            <span className="eyebrow">IMMUTABLE VERSIONS</span>
            <h2>{t("发布历史", "Published versions")}</h2>
            {workflow?.workflow_id === "collection" && workflow.active_version_id &&
              <button className="button secondary" type="button" disabled={!!busy} onClick={() => void deactivateCollection()}>
                {t("停用题集流程", "Deactivate collection workflow")}
              </button>}
            <p>{t("每个版本都是只读快照。切换活动版本不会改变已开始的流程。", "Each version is an immutable snapshot. Changing the active version won't affect existing runs.")}</p>
            <div className="version-list">
              {versions.map((version) => (
                <div className="version-row" key={version.version_id}>
                  <div>
                    <strong>
                      {version.version_id}
                      {workflow?.active_version_id === version.version_id && (
                        <span className="active-version-tag">{t("当前活动", "Active")}</span>
                      )}
                    </strong>
                    <small>
                      {version.runtime.steps.length} {t("个步骤", "steps")} ·{" "}
                      {new Date(version.published_at).toLocaleString(locale)}
                    </small>
                  </div>
                  {workflow?.active_version_id !== version.version_id && (
                    <button
                      className="button secondary"
                      onClick={() => void rollback(version.version_id)}
                      disabled={!!busy}
                    >
                      {busy === "rollback" ? t("切换中…", "Activating…") : t("设为活动", "Set active")}
                    </button>
                  )}
                </div>
              ))}
            </div>
            <div className="modal-actions">
              <button
                className="button secondary"
                onClick={() => setShowHistory(false)}
              >
                {t("关闭", "Close")}
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
