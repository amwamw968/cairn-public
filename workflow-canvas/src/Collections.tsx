import { useCallback, useEffect, useState } from "react";
import { BookOpenCheck, X } from "lucide-react";

type Metadata = { name: string; subject: string; tags: string; license: string; author: string; source: string };
type Summary = { id: number; name: string; subject: string; item_count: number; revision: number };
type Item = { question_id: number; content: string; tags: string[]; difficulty: string | null; source: string };
type Detail = Omit<Metadata, "tags"> & { id: number; tags: string[]; revision: number; items: Item[];
  versions: Array<{ number: number; revoked: boolean; published_at: string }> };
type DraftItem = { question_id: number; content: string; tags: string; difficulty: string; source: string };
type Candidate = { id: number; content: string; question_type: string };
type Props = { api: (path: string, init?: RequestInit) => Promise<any>; origin: string; token: string;
  locale: "en" | "zh-Hans"; onClose: () => void };

const blank = (): Metadata => ({ name: "", subject: "", tags: "", license: "private", author: "", source: "" });
const splitTags = (text: string) => text.split(/[,，]/).map((part) => part.trim()).filter(Boolean);

export default function Collections({ api, origin, token, locale, onClose }: Props) {
  const t = (zh: string, en: string) => locale === "en" ? en : zh;
  const [collections, setCollections] = useState<Summary[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [meta, setMeta] = useState<Metadata>(blank);
  const [draft, setDraft] = useState<DraftItem[]>([]);
  const [metaDirty, setMetaDirty] = useState(false);
  const [itemsDirty, setItemsDirty] = useState(false);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const list = useCallback(async () => {
    const result = await api("/api/collections");
    setCollections(result.collections as Summary[]);
  }, [api]);
  useEffect(() => { void list().catch((error) => setMessage(String(error))); }, [list]);
  const dirty = metaDirty || itemsDirty;
  const mayLeave = () => !dirty || window.confirm(t("尚有未保存的题集修改，确定放弃吗？", "Discard unsaved collection changes?"));
  const close = () => { if (!busy && mayLeave()) onClose(); };
  const loadDetail = async (id: number) => {
    const result = await api(`/api/collections/${id}`) as Detail;
    setDetail(result);
    setMeta({ name: result.name, subject: result.subject, tags: result.tags.join(", "),
      license: result.license, author: result.author, source: result.source });
    setDraft(result.items.map((item) => ({ ...item, tags: item.tags.join(", "), difficulty: item.difficulty || "" })));
    setMetaDirty(false); setItemsDirty(false); setOffset(0); setMessage("");
  };
  useEffect(() => {
    if (!detail) { setCandidates([]); return; }
    let active = true;
    void api(`/api/collection-candidates?subject=${encodeURIComponent(detail.subject)}&offset=${offset}&limit=50`)
      .then((result) => { if (active) { setCandidates(result.items as Candidate[]); setHasMore(result.has_more); } })
      .catch((error) => { if (active) setMessage(String(error)); });
    return () => { active = false; };
  }, [api, detail?.id, detail?.subject, offset]);

  const run = async (task: () => Promise<void>) => {
    setBusy(true); setMessage("");
    try { await task(); } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  const payload = () => ({ name: meta.name, subject: meta.subject, tags: splitTags(meta.tags),
    license: meta.license, author: meta.author, source: meta.source });
  const newCollection = () => { if (mayLeave()) { setDetail(null); setMeta(blank()); setDraft([]); setMetaDirty(false); setItemsDirty(false); setMessage(""); } };
  const changeMeta = (key: keyof Metadata, value: string) => { setMeta((old) => ({ ...old, [key]: value })); setMetaDirty(true); };
  const saveMeta = () => void run(async () => {
    const result = detail
      ? await api(`/api/collections/${detail.id}/metadata`, { method: "POST", body: JSON.stringify({ ...payload(), expected_revision: detail.revision }) })
      : await api("/api/collections", { method: "POST", body: JSON.stringify(payload()) });
    await list();
    if (detail) { setDetail({ ...detail, ...result }); setMetaDirty(false); }
    else await loadDetail(result.id);
    setMessage(t("题集信息已保存。", "Collection details saved."));
  });
  const toggle = (question: Candidate) => {
    setDraft((old) => old.some((item) => item.question_id === question.id)
      ? old.filter((item) => item.question_id !== question.id)
      : [...old, { question_id: question.id, content: question.content, tags: "", difficulty: "", source: meta.source }]);
    setItemsDirty(true);
  };
  const updateItem = (id: number, key: "tags" | "difficulty" | "source", value: string) => {
    setDraft((old) => old.map((item) => item.question_id === id ? { ...item, [key]: value } : item)); setItemsDirty(true);
  };
  const saveItems = () => detail && void run(async () => {
    if (metaDirty) throw new Error(t("请先保存题集信息。", "Save collection details first."));
    await api(`/api/collections/${detail.id}/items`, { method: "POST", body: JSON.stringify({ expected_revision: detail.revision,
      items: draft.map((item) => ({ question_id: item.question_id, tags: splitTags(item.tags),
        difficulty: item.difficulty.trim() || null, source: item.source })) }) });
    await list(); await loadDetail(detail.id); setMessage(t("题目选择已保存。", "Question selection saved."));
  });
  const publish = () => detail && void run(async () => {
    if (dirty) throw new Error(t("请先保存所有修改。", "Save all changes first."));
    await api(`/api/collections/${detail.id}/publish`, { method: "POST", body: JSON.stringify({ expected_revision: detail.revision }) });
    await loadDetail(detail.id); setMessage(t("已创建只读版本。", "A read-only version was created."));
  });
  const addToPractice = () => detail && void run(async () => {
    if (dirty) throw new Error(t("请先保存所有修改。", "Save all changes first."));
    const result = await api(`/api/collections/${detail.id}/add-to-practice`, { method: "POST",
      body: JSON.stringify({ expected_revision: detail.revision }) });
    await list(); await loadDetail(detail.id);
    setMessage(t(`已加入 ${result.added} 题，${result.unchanged} 题已有学习记录并保持原样。`,
      `Added ${result.added}; ${result.unchanged} existing learning records were preserved.`));
  });
  const remove = () => detail && void run(async () => {
    if (!window.confirm(t("删除这个题集和它的全部版本？原来的题目会保留。", "Delete this collection and all its versions? The original questions remain."))) return;
    await api(`/api/collections/${detail.id}?expected_revision=${detail.revision}`, { method: "DELETE" });
    setDetail(null); setMeta(blank()); setDraft([]); setMetaDirty(false); setItemsDirty(false); await list();
    setMessage(t("题集已删除。", "Collection deleted."));
  });
  const transfer = async (path: string, init: RequestInit) => {
    const headers = new Headers(init.headers);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(`${origin.trim().replace(/\/$/, "")}${path}`, { ...init, headers });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(locale === "en" ? `Request failed (${response.status}).` : (error.detail || `请求失败 (${response.status})`));
    }
    return response;
  };
  const download = (number: number) => detail && void run(async () => {
    const response = await transfer(`/api/collections/${detail.id}/versions/${number}/package`, {});
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a"); link.href = url; link.download = `cairn-collection-${detail.id}-v${number}.zip`;
    document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const upload = (file: File) => void run(async () => {
    if (!mayLeave()) return;
    const response = await transfer("/api/collections/import", { method: "POST", body: file,
      headers: { "Content-Type": "application/zip" } });
    const imported = await response.json() as { id: number };
    await list(); await loadDetail(imported.id); setMessage(t("题集已导入，原有题目未改变。", "Collection imported; existing questions are unchanged."));
  });

  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
    <section className="collections-modal" role="dialog" aria-modal="true" aria-labelledby="collection-title">
      <header className="collections-header"><div className="modal-icon"><BookOpenCheck size={20} /></div>
        <div><span className="eyebrow">COLLECTION LIBRARY</span><h2 id="collection-title">{t("题集资产", "Question collections")}</h2></div>
        <button className="modal-close" onClick={close} aria-label={t("关闭", "Close")}><X size={17} /></button></header>
      <p className="collections-note">{t("文字包包含题干、答案与来源，不含照片或练习记录。文字仍可能包含个人信息，分享前请检查。", "Packages contain question text, answers and provenance, not photos or practice history. Text may still contain personal information; review before sharing.")}</p>
      {message && <p className="collections-message" role="status">{message}</p>}
      <div className="collections-layout"><aside className="collections-list">
        <button className="button secondary" type="button" disabled={busy} onClick={newCollection}>{t("新建题集", "New collection")}</button>
        <label className="collections-file button secondary">{t("导入文字包", "Import package")}<input type="file" accept=".zip,application/zip" disabled={busy}
          onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ""; if (file) upload(file); }} /></label>
        {collections.map((collection) => <button key={collection.id} type="button" className={`collection-list-item${detail?.id === collection.id ? " selected" : ""}`}
          disabled={busy} onClick={() => { if (mayLeave()) void run(() => loadDetail(collection.id)); }}>
          <strong>{collection.name}</strong><span>{collection.subject} · {collection.item_count} {t("题", collection.item_count === 1 ? "question" : "questions")}</span></button>)}
      </aside><div className="collections-content">
        <div className="collections-section-title"><h3>{detail ? t("题集信息", "Collection details") : t("新建题集", "New collection")}</h3>
          {detail && <span>{t("修订", "Revision")} {detail.revision}</span>}</div>
        <div className="collections-fields">
          {([ ["name", t("名称", "Name")], ["subject", t("学科", "Subject")], ["tags", t("标签（逗号分隔）", "Tags (comma separated)")],
            ["author", t("署名", "Author")], ["source", t("来源", "Source")] ] as Array<[keyof Metadata, string]>).map(([key, label]) =>
              <label key={key}>{label}<input className="text-input" value={meta[key]} disabled={busy || (!!detail && key === "subject")}
                onChange={(event) => changeMeta(key, event.target.value)} /></label>)}
          <label>{t("许可", "License")}<select className="text-input" value={meta.license} disabled={busy} onChange={(event) => changeMeta("license", event.target.value)}>
            <option value="private">{t("私有", "Private")}</option><option value="CC-BY-4.0">CC BY 4.0</option><option value="CC-BY-NC-4.0">CC BY-NC 4.0</option></select></label>
        </div>
        <div className="collections-actions"><button className="button primary" type="button" disabled={busy || (!!detail && !metaDirty)} onClick={saveMeta}>
          {detail ? t("保存信息", "Save details") : t("创建题集", "Create collection")}</button>
          {detail && <button className="button secondary" type="button" disabled={busy} onClick={remove}>{t("删除题集", "Delete collection")}</button>}</div>
        {detail && <><div className="collections-section-title"><h3>{t("选择题目", "Select questions")}</h3><span>{draft.length} {t("题已选", "selected")}</span></div>
          <p className="collections-help">{t("只显示当前学习者已核对、且与题集学科一致的题。", "Only reviewed questions belonging to the current learner and subject are shown.")}</p>
          <div className="candidate-list">{candidates.map((question) => <label key={question.id} className="candidate-row"><input type="checkbox"
            checked={draft.some((item) => item.question_id === question.id)} disabled={busy} onChange={() => toggle(question)} />
            <span>{question.content}</span><small>#{question.id}</small></label>)}
            {candidates.length === 0 && <p className="collections-help">{t("没有可选题目。", "No questions available.")}</p>}</div>
          <div className="collections-actions"><button className="button secondary" disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>{t("上一页", "Previous")}</button>
            <button className="button secondary" disabled={busy || !hasMore} onClick={() => setOffset(offset + 50)}>{t("下一页", "Next")}</button></div>
          {draft.map((item, index) => <div className="collection-selected" key={item.question_id}><strong>{index + 1}. {item.content}</strong>
            <div className="collections-fields"><label>{t("标签", "Tags")}<input className="text-input" disabled={busy} value={item.tags} onChange={(e) => updateItem(item.question_id, "tags", e.target.value)} /></label>
              <label>{t("难度", "Difficulty")}<input className="text-input" disabled={busy} value={item.difficulty} onChange={(e) => updateItem(item.question_id, "difficulty", e.target.value)} /></label>
              <label>{t("来源", "Source")}<input className="text-input" disabled={busy} value={item.source} onChange={(e) => updateItem(item.question_id, "source", e.target.value)} /></label></div>
            <div className="collections-actions"><button className="button secondary" disabled={index === 0 || busy} onClick={() => {
              setDraft((old) => { const copy = [...old]; [copy[index - 1], copy[index]] = [copy[index], copy[index - 1]]; return copy; }); setItemsDirty(true);
            }}>{t("上移", "Move up")}</button><button className="button secondary" disabled={busy} onClick={() => { setDraft((old) => old.filter((q) => q.question_id !== item.question_id)); setItemsDirty(true); }}>{t("移除", "Remove")}</button></div></div>)}
          <div className="collections-actions"><button className="button primary" disabled={busy || !itemsDirty} onClick={saveItems}>{t("保存题目", "Save questions")}</button>
            <button className="button secondary" disabled={busy || dirty || !draft.length} onClick={publish}>{t("发布版本", "Publish version")}</button>
            <button className="button secondary" disabled={busy || dirty || !draft.length} onClick={addToPractice}>{t("将当前题集加入练习", "Add current collection to practice")}</button></div>
          <div className="collections-section-title"><h3>{t("版本", "Versions")}</h3></div>
          {detail.versions.length === 0 && <p className="collections-help">{t("保存题目后可发布版本。", "Save questions, then publish a version.")}</p>}
          {detail.versions.map((version) => <div className="collection-version" key={version.number}><span>v{version.number} · {version.revoked ? t("已撤销", "Revoked") : t("可导出", "Available")}</span>
            <button className="button secondary" disabled={busy || version.revoked} onClick={() => download(version.number)}>{t("下载文字包", "Download package")}</button></div>)}
        </>}
      </div></div>
    </section>
  </div>;
}
