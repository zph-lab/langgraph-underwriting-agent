/* 核保规则助手 · 零构建 Vue3 客户端（多会话）
 * 事件口径（LangGraph，兼容 Pi 回退）：
 *  message_update.assistantMessageEvent: text_delta / thinking_delta ...
 *  message_end.message: {role, content[], stopReason, error}（权威定稿）
 *  tool_execution_start/end: {toolCallId, toolName, isError}
 *  agent_settled: 一轮结束
 * 多会话口径：每个会话独立 sid，后端持久化模型上下文；
 * 消息/草稿存 localStorage。
 */
const { createApp, ref, reactive, computed, onMounted, onBeforeUnmount, nextTick, watch } = Vue;

const RETRIEVAL_TOOLS = new Set(["bom_search", "rule_search"]);

/* ---------- 轻量 Markdown 渲染（零依赖、离线可用） ----------
 * 安全口径：先把整段文本 HTML 转义，再只按白名单生成标签；
 * 链接仅放行 http/https，其余协议（javascript: 等）不会被渲染成 <a>。 */
function escHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function inlineMd(raw) {
  let out = escHtml(raw);
  out = out.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`);
  out = out.replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g,
    (_m, a, u) => `<a href="${u}" target="_blank" rel="noopener">${a || u}</a>`);
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (_m, t, u) => `<a href="${u}" target="_blank" rel="noopener">${t}</a>`);
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  return out;
}

function renderMarkdown(text) {
  if (!text) return "";
  const lines = String(text).replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let para = [];
  const flushPara = () => {
    if (para.length) { html.push(`<p>${para.map(inlineMd).join("<br>")}</p>`); para = []; }
  };
  const isTableSep = (l) => /\|/.test(l) && /^\s*\|?[\s:|-]+\|?\s*$/.test(l) && /-/.test(l);
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(/^\s*(```|~~~)\s*\S*$/);
    if (fence) {
      flushPara();
      const mark = fence[1];
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(mark)) { buf.push(lines[i]); i++; }
      i++; // 跳过结束围栏；流式未闭合时直接吃到行尾，渲染仍成立
      html.push(`<pre class="code"><code>${escHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    if (!line.trim()) { flushPara(); i++; continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushPara(); const n = h[1].length; html.push(`<h${n}>${inlineMd(h[2])}</h${n}>`); i++; continue; }

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flushPara(); html.push("<hr>"); i++; continue; }

    if (/^\s*>\s?/.test(line)) {
      flushPara();
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
      html.push(`<blockquote>${buf.map(inlineMd).join("<br>")}</blockquote>`);
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      flushPara();
      const head = cells(line);
      const aligns = cells(lines[i + 1]).map((c) =>
        /^:.*:$/.test(c) ? "center" : /:$/.test(c) ? "right" : "");
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) { rows.push(cells(lines[i])); i++; }
      let t = "<table><thead><tr>";
      head.forEach((c, ci) => { t += `<th${aligns[ci] ? ` class="al-${aligns[ci]}"` : ""}>${inlineMd(c)}</th>`; });
      t += "</tr></thead><tbody>";
      rows.forEach((r) => {
        t += "<tr>";
        head.forEach((_c, ci) => { t += `<td${aligns[ci] ? ` class="al-${aligns[ci]}"` : ""}>${inlineMd(r[ci] != null ? r[ci] : "")}</td>`; });
        t += "</tr>";
      });
      t += "</tbody></table>";
      html.push(t);
      continue;
    }

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      flushPara();
      const ordered = !!ol;
      const items = [];
      while (i < lines.length) {
        const m1 = lines[i].match(/^\s*[-*+]\s+(.*)$/);
        const m2 = lines[i].match(/^\s*\d+[.)]\s+(.*)$/);
        if (!ordered && m1) { items.push(m1[1]); i++; continue; }
        if (ordered && m2) { items.push(m2[1]); i++; continue; }
        if (items.length && /^\s{2,}\S/.test(lines[i])) { items[items.length - 1] += " " + lines[i].trim(); i++; continue; }
        break;
      }
      html.push(`<${ordered ? "ol" : "ul"}>${items.map((it) => `<li>${inlineMd(it)}</li>`).join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }

    para.push(line);
    i++;
  }
  flushPara();
  return html.join("\n");
}

/* ---------- 会话存储 ---------- */
const SESSIONS_KEY = "piweb.sessions_v1";
const ACTIVE_KEY = "piweb.active_v1";
const LEGACY_SID_KEY = "piweb.sid";
const MAX_SESSIONS = 50;   // 会话数量上限（超出按 updatedAt 从旧到新丢弃）
const MAX_MSGS = 300;      // 单会话持久化消息上限

function newSid() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : String(Date.now()) + "-" + Math.random().toString(16).slice(2);
}

function blankSession(sid) {
  return { sid, title: "", createdAt: Date.now(), updatedAt: Date.now(), messages: [], draft: "" };
}

function normalizeSession(s) {
  const out = Object.assign(blankSession(String((s && s.sid) || newSid())), s || {});
  out.sid = String(out.sid);
  if (!Array.isArray(out.messages)) out.messages = [];
  out.messages.forEach((m) => { if (m && m.streaming) m.streaming = false; }); // 刷新遗留的流式态 → 定稿
  return out;
}

function readInitialStore() {
  let raw = {};
  try {
    const p = JSON.parse(localStorage.getItem(SESSIONS_KEY) || "{}");
    if (p && typeof p === "object" && !Array.isArray(p)) raw = p;
  } catch (e) { raw = {}; }
  const legacy = localStorage.getItem(LEGACY_SID_KEY); // 兼容旧版单会话 sid
  if (legacy && !raw[legacy]) raw[legacy] = blankSession(legacy);
  const map = {};
  Object.keys(raw).forEach((k) => { map[k] = normalizeSession(Object.assign({}, raw[k], { sid: k })); });
  let active = localStorage.getItem(ACTIVE_KEY) || "";
  if (!map[active]) {
    active = Object.keys(map).sort((a, b) => (map[b].updatedAt || 0) - (map[a].updatedAt || 0))[0] || "";
  }
  if (!active) { active = newSid(); map[active] = blankSession(active); }
  return { map, active };
}

function fmtTime(ts) {
  const d = Date.now() - (ts || 0);
  if (d < 60e3) return "刚刚";
  if (d < 3600e3) return Math.floor(d / 60e3) + " 分钟前";
  if (d < 86400e3) return Math.floor(d / 3600e3) + " 小时前";
  if (d < 7 * 86400e3) return Math.floor(d / 86400e3) + " 天前";
  const dt = new Date(ts);
  return (dt.getMonth() + 1) + "-" + dt.getDate();
}

createApp({
  setup() {
    const initial = readInitialStore();
    const storeMap = reactive(initial.map);
    const currentSid = ref(initial.active);
    localStorage.setItem(ACTIVE_KEY, initial.active);

    const messages = computed(() => (storeMap[currentSid.value] ? storeMap[currentSid.value].messages : []));
    const sessionList = computed(() =>
      Object.keys(storeMap).map((k) => storeMap[k]).sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)));

    const draft = ref(storeMap[currentSid.value].draft || "");
    const busy = ref(false);
    const connected = ref(false);
    const statusText = ref("");
    const activity = reactive({ active: false, text: "" });
    const sidebarOpen = ref(true);
    // 懒加载后保持挂载，切换工作区不丢失编辑器登录、草稿或聊天流。
    const activeView = ref('assistant');
    const editorOpened = ref(false);
    const mergedMode = typeof location !== 'undefined' && location.pathname.startsWith('/assistant/');
    const apiUrl = path => mergedMode ? '/assistant' + path : path;
    const editorUrl = mergedMode ? '/' : 'http://127.0.0.1:8009/';
    const editorNotice = ref('');
    function showEditor() {
      if (mergedMode) {
        location.href = editorUrl;
        return;
      }
      editorOpened.value = true;
      activeView.value = 'editor';
      sidebarOpen.value = false;
    }
    function showAssistant() {
      activeView.value = 'assistant';
      nextTick(() => grow(0));
    }
    async function copyToEditor(text) {
      try {
        await navigator.clipboard.writeText(text);
        editorNotice.value = '已复制完整答复，请在编辑器中粘贴需要验证的规则（Ctrl+V）。';
        showEditor();
      } catch (_err) {
        editorNotice.value = '复制失败，请返回助手手动选择并复制规则，再到编辑器粘贴。';
        showEditor();
      }
    }
    const scroller = ref(null);
    const taEl = ref(null);
    const fileInput = ref(null);

    // 待发送附件：{ uid, name, status: uploading|ready|error, text, chars, truncated, error }
    // 附件只在当前输入区，不持久化（切换会话即清空）
    const attachments = reactive([]);
    let attachSeq = 0;
    const MAX_FILE_BYTES = 20 * 1024 * 1024;
    const hasReadyAttachment = computed(() => attachments.some((a) => a.status === "ready"));

    function pickFiles() {
      if (fileInput.value) fileInput.value.click();
    }
    function removeAttachment(i) {
      attachments.splice(i, 1);
    }
    async function uploadOne(item, file) {
      const fd = new FormData();
      fd.append("file", file);
      try {
        const r = await fetch(apiUrl("/api/upload"), { method: "POST", body: fd });
        const j = await r.json().catch(() => null);
        if (!r.ok) throw new Error((j && (j.detail || j.error)) || `HTTP ${r.status}`);
        if (!j || !j.ok) {
          item.status = "error";
          item.error = (j && j.error) || "提取失败";
          return;
        }
        item.status = "ready";
        item.text = j.text;
        item.chars = j.chars;
        item.truncated = !!j.truncated;
      } catch (err) {
        item.status = "error";
        item.error = err.message || String(err);
      }
    }
    function onFilesPicked(e) {
      for (const file of e.target.files) {
        const item = reactive({
          uid: ++attachSeq, name: file.name, status: "uploading",
          text: "", chars: 0, truncated: false, error: "",
        });
        attachments.push(item);
        if (file.size > MAX_FILE_BYTES) {
          item.status = "error";
          item.error = "超过 20MB";
          continue;
        }
        uploadOne(item, file);
      }
      e.target.value = ""; // 允许再次选择同一文件
    }
    const examples = [
      "你好，你能帮我做什么？",
      "帮我查询业务名词：险别",
      "查一下现有规则里有哪些涉及“机构”的",
    ];
    /* ---------- 持久化 ---------- */
    let saveTimer = null;
    function persist() {
      try {
        let ids = Object.keys(storeMap);
        if (ids.length > MAX_SESSIONS) {
          ids.sort((a, b) => (storeMap[b].updatedAt || 0) - (storeMap[a].updatedAt || 0));
          ids.slice(MAX_SESSIONS).forEach((id) => { delete storeMap[id]; });
        }
        const out = {};
        Object.keys(storeMap).forEach((id) => {
          const s = storeMap[id];
          const msgs = s.messages.length > MAX_MSGS ? s.messages.slice(-MAX_MSGS) : s.messages;
          out[id] = {
            sid: id, title: s.title || "", createdAt: s.createdAt, updatedAt: s.updatedAt,
            draft: s.draft || "",
            messages: msgs.map((m) => Object.assign({}, m, { streaming: false })),
          };
        });
        localStorage.setItem(SESSIONS_KEY, JSON.stringify(out));
      } catch (e) {
        statusText.value = "本地会话历史保存失败（浏览器存储可能已满）";
      }
    }
    function schedulePersist() {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(persist, 600);
    }
    function touch() {
      const s = storeMap[currentSid.value];
      if (s) { s.updatedAt = Date.now(); schedulePersist(); }
    }

    watch(draft, (v) => {
      const s = storeMap[currentSid.value];
      if (s) { s.draft = v; schedulePersist(); }
      nextTick(() => grow(0));
    });

    let cur = null; // 当前流式中的助手气泡

    function scrollBottom() {
      nextTick(() => {
        const el = scroller.value;
        if (el) el.scrollTop = el.scrollHeight;
      });
    }

    function ensureAssistant() {
      if (!cur) {
        cur = reactive({ role: "assistant", text: "", thinking: "", process: "", streaming: true });
        messages.value.push(cur);
        scrollBottom();
      }
      return cur;
    }

    function finishAssistant(finalText) {
      if (!cur) return;
      if (typeof finalText === "string" && finalText) cur.text = finalText;
      if (!cur.text && !cur.thinking && !cur.process) {
        // 中间轮（只发了工具调用，无文本）→ 删掉空气泡，不留“无输出”占位
        const i = messages.value.indexOf(cur);
        if (i >= 0) messages.value.splice(i, 1);
      } else {
        cur.streaming = false;
      }
      cur = null;
      touch();
    }

    function pushSystem(text, role) {
      messages.value.push(reactive({ role: role || "system", text }));
      touch();
      scrollBottom();
    }

    function handleEvent(ev) {
      switch (ev.type) {
        case "task_status": {
          if (busy.value) { activity.active = true; activity.text = ev.text || "任务执行中…"; scrollBottom(); }
          break;
        }
        case "message_update": {
          const ame = ev.assistantMessageEvent || {};
          if (ame.type === "text_delta") {
            ensureAssistant().text += ame.delta || "";
            scrollBottom();
          } else if (ame.type === "thinking_delta" || ame.type === "reasoning_delta") {
            ensureAssistant().thinking += ame.delta || "";
          } else if (ame.type === "process_delta") {
            const bubble = ensureAssistant();
            bubble.process += ame.delta || "";
            touch();
          }
          break;
        }
        case "message_end": {
          const msg = ev.message || {};
          if (msg.role !== "assistant") break;
          const parts = Array.isArray(msg.content) ? msg.content : [];
          const finalText = parts.filter((c) => c && c.type === "text").map((c) => c.text).join("");
          const hasToolCall = parts.some((c) => c && (c.type === "toolCall" || c.type === "tool_use"));
          const finalThinking = parts.filter((c) => c && c.type === "thinking").map((c) => c.thinking || "").join("");
          if (finalText || finalThinking || hasToolCall) {
            const bubble = ensureAssistant();
            bubble.text = finalText;
            if (finalThinking) bubble.thinking = finalThinking;
          }
          if (msg.stopReason === "error" || msg.error) {
            finishAssistant();
            pushSystem(
              String(msg.error || "模型调用失败"),
              "error"
            );
          } else if (hasToolCall) {
            const bubble = ensureAssistant();
            bubble.process = [bubble.process, bubble.thinking, bubble.text].filter(Boolean).join("\n\n");
            bubble.text = "";
            bubble.thinking = "";
            touch();
          } else {
            // Keep the same bubble until agent_settled; final-only events also render.
            if (cur) cur.streaming = false;
            touch();
            scrollBottom();
          }
          break;
        }
        case "tool_execution_start": {
          activity.active = true;
          activity.text = RETRIEVAL_TOOLS.has(ev.toolName) ? "正在检索业务数据…" : `正在执行 ${ev.toolName || "工具"}…`;
          scrollBottom();
          break;
        }
        case "tool_execution_end": {
          activity.active = false;
          if (ev.isError) pushSystem(`工具 ${ev.toolName || ""} 执行失败`, "system");
          break;
        }
        case "agent_settled": {
          busy.value = false;
          activity.active = false;
          if (cur) finishAssistant();
          persist();
          break;
        }
        case "auto_retry_start": {
          statusText.value = `请求失败，正在重试（${ev.attempt}/${ev.maxAttempts}）…`;
          break;
        }
        case "compaction_start": {
          statusText.value = "正在压缩会话上下文…";
          break;
        }
        case "extension_ui_request": {
          if (ev.method === "notify") pushSystem(String(ev.message || ""), "system");
          else if (ev.method === "setStatus") statusText.value = ev.statusText || "";
          break;
        }
        case "bridge_dead": {
          busy.value = false;
          activity.active = false;
          if (cur) finishAssistant();
          pushSystem(`运行会话已中断：${ev.reason || "未知原因"}\n下条消息会自动重启会话（历史上下文从磁盘恢复）。`, "error");
          break;
        }
        default:
          break;
      }
    }

    /* ---------- SSE（每个会话一个事件流，切换时重连） ---------- */
    let es = null;
    function openEs(sid) {
      if (es) { es.close(); es = null; }
      connected.value = false;
      const my = new EventSource(apiUrl(`/api/events?sid=${encodeURIComponent(sid)}`));
      es = my;
      my.onopen = () => { if (es === my) connected.value = true; };
      my.onerror = () => { if (es === my) { connected.value = false; activity.active = false; } }; // EventSource 自动重连
      my.onmessage = (e) => {
        if (es !== my) return; // 已切换会话，迟到的旧流事件丢弃
        try {
          handleEvent(JSON.parse(e.data));
        } catch (err) {
          /* 忽略坏行 */
        }
      };
    }

    async function post(url, body) {
      const r = await fetch(apiUrl(url), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const j = await r.json().catch(() => null);
      if (!r.ok) {
        const detail = j && (j.detail || j.error);
        throw new Error(detail || `HTTP ${r.status}`);
      }
      return j;
    }

    function buildAttachmentBlocks(ready) {
      return ready.map(
        (a) =>
          `【上传文件：${a.name}｜共${a.chars.toLocaleString()}字${a.truncated ? "，内容过长已截断" : ""}】\n${a.text}\n【文件结束】`
      );
    }

    async function send() {
      if (busy.value) return;
      const s = storeMap[currentSid.value];
      if (!s) return;
      const text = draft.value.trim();
      if (attachments.some((a) => a.status === "uploading")) {
        statusText.value = "附件文本提取中，请稍候…";
        return;
      }
      const ready = attachments.filter((a) => a.status === "ready");
      if (!text && !ready.length) return;
      const full = [text, ...buildAttachmentBlocks(ready)].filter(Boolean).join("\n\n\n");
      s.messages.push(
        reactive({
          role: "user",
          text,
          files: ready.map((a) => ({ name: a.name, chars: a.chars, truncated: a.truncated })),
        })
      );
      if (!s.title) s.title = String(text || (ready[0] && ready[0].name) || "新会话").trim().slice(0, 24);
      s.updatedAt = Date.now();
      attachments.splice(0, attachments.length); // error chip 一并清掉，失败可重新上传
      draft.value = "";
      nextTick(() => grow(0));
      busy.value = true;
      statusText.value = "";
      cur = null;
      scrollBottom();
      persist();
      try {
        await post("/api/prompt", { sid: currentSid.value, message: full });
      } catch (err) {
        busy.value = false;
        pushSystem(`发送失败：${err.message}`, "error");
      }
    }

    async function stop() {
      try {
        await post("/api/abort", { sid: currentSid.value });
      } catch (err) {
        pushSystem(`停止失败：${err.message}`, "system");
      }
    }
    /* ---------- 会话切换 / 新建 / 删除 ---------- */
    function leaveCurrent() {
      const s = storeMap[currentSid.value];
      if (s) s.draft = draft.value;
      // 生成中切走：中止旧会话本轮生成（切回后事件流已断，不收残局）
      if (busy.value) post("/api/abort", { sid: currentSid.value }).catch(() => {});
      if (cur) finishAssistant();
      busy.value = false;
      activity.active = false;
      statusText.value = "";
      persist();
    }

    function enter(sid) {
      currentSid.value = sid;
      localStorage.setItem(ACTIVE_KEY, sid);
      const s = storeMap[sid];
      s.messages.forEach((m) => { if (m.streaming) m.streaming = false; });
      draft.value = s.draft || "";
      attachments.splice(0);
      openEs(sid);
      nextTick(() => { grow(0); scrollBottom(); });
    }

    function switchTo(sid) {
      if (sid === currentSid.value || !storeMap[sid]) return;
      leaveCurrent();
      enter(sid);
    }

    function newSession() {
      const s = storeMap[currentSid.value];
      if (s && !s.messages.length) {
        statusText.value = "当前已是新会话";
        return;
      }
      leaveCurrent();
      const sid = newSid();
      storeMap[sid] = blankSession(sid);
      enter(sid);
      persist();
    }

    function removeSession(sid) {
      if (!storeMap[sid]) return;
      const wasActive = sid === currentSid.value;
      if (wasActive) leaveCurrent();
      delete storeMap[sid];
      post("/api/close", { sid }).catch(() => {}); // 释放后端会话和工具进程（持久化记录保留）
      if (wasActive) {
        const next = sessionList.value[0];
        if (next) {
          enter(next.sid);
        } else {
          const ns = newSid();
          storeMap[ns] = blankSession(ns);
          enter(ns);
        }
      }
      persist();
    }

    function grow(rows) {
      const el = taEl.value;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = Math.min(Math.max(rows || el.scrollHeight, 38), 160) + "px";
    }
    function autoGrow() {
      grow(0);
    }

    onMounted(() => {
      openEs(currentSid.value);
      nextTick(() => grow(0));
      window.addEventListener("beforeunload", persist);
      window.addEventListener("pagehide", persist);
    });
    onBeforeUnmount(() => {
      window.removeEventListener("beforeunload", persist);
      window.removeEventListener("pagehide", persist);
      persist();
      if (es) es.close();
    });

    return { sidebarOpen, sessionList, currentSid, messages, draft, busy, connected, statusText,
             activeView, editorOpened, editorUrl, editorNotice, showEditor, showAssistant, copyToEditor,
             activity, examples, scroller, taEl, send, stop, autoGrow, renderMarkdown, fmtTime,
             newSession, switchTo, removeSession,
             attachments, fileInput, hasReadyAttachment, pickFiles, onFilesPicked, removeAttachment };
  },
}).mount("#app");
