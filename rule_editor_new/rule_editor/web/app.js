// adp 规则文本编辑器 v2：自由文本 + 空格分隔词检索
(function () {
  "use strict";

  // ---------- adp-token 注入（2026-09-17：postMessage 主 + URL 参数兜底）----------
  // 两通道同口径：手动输入优先，一旦用户手填则不再覆盖；注入过的值允许父页面
  // 热更新（token 续期后重推即可，无需重开 iframe）。
  //  ① postMessage：嵌入 iframe 时本页就绪后向父页面发 {type:"ruleEditorReady"}，
  //     父页面回推 {type:"adpToken", token:"..."}。token 不落 URL/访问日志（推荐）。
  //  ② URL ?token=：旧口径兜底（主系统未改造 / 直接开页面单测时仍可用）。
  // 安全：只接收 event.source === window.parent 的消息（第三方页面拿不到本 iframe
  // 窗口引用）；本页不回传任何凭据，ready 信号不含敏感信息可 targetOrigin '*'。
  const _tokenEl = document.getElementById("inToken");
  let _tokenAuto = false;          // 当前值是否为注入所得（区别用户手填）
  function applyInjectedToken(t) {
    if (!t || !_tokenEl) return;
    const v = _tokenEl.value.trim();
    if (!v || _tokenAuto) {
      _tokenEl.value = String(t).trim();
      _tokenAuto = true;
    }
  }
  window.addEventListener("message", function (ev) {
    try {
      if (window.parent === window || ev.source !== window.parent) return;
      const d = ev.data;
      if (d && d.type === "adpToken" && d.token) applyInjectedToken(d.token);
    } catch (e) { /* 非法规整的消息忽略 */ }
  });
  if (_tokenEl) {   // 用户手动改动后不再被注入/热更新覆盖
    _tokenEl.addEventListener("input", function () { _tokenAuto = false; });
  }
  try {   // 通知父页面就绪，等待回推 token
    if (window.parent !== window) window.parent.postMessage({ type: "ruleEditorReady" }, "*");
  } catch (e) { /* 跨域限制等异常静默 */ }
  try {   // ② URL 兜底
    const _tq = new URLSearchParams(window.location.search).get("token");
    if (_tq) applyInjectedToken(_tq);
  } catch (e) { /* URLSearchParams 不可用时静默跳过 */ }

  const box = document.getElementById("ruleBox");
  const list = document.getElementById("suggestList");
  const preview = document.getElementById("descPreview");
  const status = document.getElementById("status");
  // 2026-09-30：#stats 路径索引统计元素已隐藏，statsEl 引用移除

  let hl = -1;           // 当前高亮候选索引
  // 方向键导航过 = 用户"真选中"，回车优先插入该候选（区别于 render 的默认高亮）
  let hlManual = false;
  let cands = [];        // 当前候选列表
  let open = false;      // 下拉是否打开
  let timer = null;
  let paramRange = null; // then/else 参数位上下文：{start,end} 参数值在整行的绝对区间（供整段替换）
  // 回车换行进行中标记：keydown 置位，input 事件跳过常规 refresh，
  // 候选统一交给 showAfterNewline 分发（同步弹静态入口词候选，零检索）
  let pendingNewline = false;
  // 2026-09-23：用户主动关候选（Esc/点外）序号。递增后，在途的候选检索响应
  // 到达时作废——防止"已按 Esc 关闭、迟到的响应又把列表复活"，
  // 那会让随后的回车落入"列表开着但无高亮"死路（换行生效、尾巴丢失）
  let uiSeq = 0;

  // ---------- 编辑模式状态机 ----------
  // entry:     入口态（编辑框为空）——只弹 预定义 / 如果 / 那么 / 否则 四个引导候选
  // rule:      "如果"模式——现有规则编辑逻辑（原校验 + 原候选机制）
  // predefine: 预定义模式——正文为 指定'别名'…（入口词是"预定义"，指定仅为语句词）
  // then:      "那么"模式——条件成立动作（结果方法调用等），交互复用规则模式
  // else:      "否则"模式——条件不成立动作，交互复用规则模式
  let mode = "entry";

  // 入口候选：word 为引导词（用于展示/过滤/填入）；entry 为切换到的模式
  // 2026-09-03：预定义入口首词改为"预定义"（行文：预定义 指定'别名'…，类似"如果"之于规则行）
  const ENTRY_CANDS = [
    { label: "预定义", kind: "entry", word: "预定义", entry: "predefine" },
    { label: "如果", kind: "entry", word: "如果", entry: "rule" },
    { label: "那么", kind: "entry", word: "那么", entry: "then" },
    { label: "否则", kind: "entry", word: "否则", entry: "else" },
  ];

  // 引导词 -> 模式 映射（入口候选 / 自动切换共用）
  // 2026-09-23："指定"不再兼作入口词——预定义模式唯一入口词为"预定义"，
  // "指定"仅是语句正文词（行文：预定义↵指定'别名'…；块内第2…N条续行
  // 直接以"指定"开头，由 modeForLine 向上扫描归属预定义块，不走本映射）。
  const WORD_MODE = { 预定义: "predefine", 如果: "rule", 那么: "then", 否则: "else" };
  // 模式 -> 引导词（反向映射）：enterMode 补空格时取当前模式对应的引导词。
  // 此前误用 WORD_MODE[m]（m 是模式名，恒为 undefined）→ 补空格从未生效，
  // 敲"如果"后词尾无空格，rule 模式检索 word=如果 命中词典 keyword，
  // 候选弹回"如果"自身（tag 引导），回车 insert 同词视觉无变化。
  const MODE_WORD = { predefine: "预定义", rule: "如果", then: "那么", else: "否则" };

  // 去掉首尾单引号与尾部冒号：手动输入 预定义 / '预定义' / 如果：（尾冒号，
  // 2026-09-10 冒号兼容）均按同一引导词匹配
  function normEntryWord(w) {
    return (w || "").replace(/^'+|'+$/g, "").replace(/[：:]+$/, "");
  }

  function setMode(m) {
    mode = m;
    if (m === "entry") {
      status.textContent = "入口 · 请输入或选择 预定义 / 如果 / 那么 / 否则";
    } else if (m === "rule") {
      status.textContent = "规则模式（如果）";
    } else if (m === "predefine") {
      status.textContent = "预定义模式";
    } else if (m === "then") {
      status.textContent = "动作模式（那么）";
    } else if (m === "else") {
      status.textContent = "动作模式（否则）";
    }
  }

  // 入口态弹出引导候选（可按已输入文字前缀过滤，输入带不带引号均可）
  function showEntry() {
    const text = box.value;
    const pos = box.selectionStart;
    const ls = text.lastIndexOf("\n", pos - 1) + 1;
    // 两行式兜底：本行光标前无内容且上一行是孤立引导词（如果/那么/否则/预定义
    // 独占一行）→ 直接进入对应模式，弹正文候选而非引导词候选。
    // （此前只有"预定义"行有该处理——见 refreshPredefine，"如果/那么/否则"
    //   手敲引导词后换行，弹的仍是入口候选，用户需再选一次引导词）
    if (!text.slice(ls, pos).trim() && ls > 0) {
      const pl = stripLeadColon(prevLineText(text, ls)).trim();
      const fm = pl.match(FOLD_ENTRY_RE);
      if (fm && pl === fm[1]) { enterMode(WORD_MODE[fm[1]]); return; }
    }
    const cw = currentWord();
    const w = normEntryWord(cw.word);
    // 光标前一个词已是引导词 → 直接切换模式（无需按空格确认）
    const m = WORD_MODE[w];
    if (m) { enterMode(m); return; }
    const cs = !w ? ENTRY_CANDS
      : ENTRY_CANDS.filter(function (c) { return c.word.indexOf(w) === 0; });
    render(cs);   // 空数组时 render 内部会自动 close
  }

  // 回车换行后的候选分发（2026-09-23 简化：与上方编辑内容彻底解耦）：
  // 不再按 modeForLine 推断"本块续行候选"——续行走异步检索，与回车前
  // 遗留的防抖/在途请求互相覆盖，实测表现为"两个候选互相抢、入口词丢失"。
  // 现直接同步渲染 4 个固定入口词候选：零检索、无竞态，入口词必在。
  // 想继续上方块（如预定义块续写"指定"）：点一下新行或继续敲字，
  // refresh 的行级模式纠偏（modeForLine）会弹该块续行候选（入口词同步垫底）。
  function showAfterNewline() {
    pendingNewline = false;
    // 作废回车前的防抖/在途检索，防迟到响应覆盖本列表
    uiSeq++;
    clearTimeout(timer);
    clearTimeout(pdTimer);
    clearTimeout(mtTimer);
    setMode("entry");
    render(ENTRY_CANDS);
  }

  // 由入口进入目标模式（保留已输入的引导词）
  function enterMode(m) {
    setMode(m);
    close();
    if (m === "predefine") {
      // 2026-09-03：预定义行 = 预定义 指定'别名'…（行首引导词类似"如果"）。
      // 不再自动生成"指定''"：入口候选"预定义"已插入"预定义 "（或自动纠错时只输入了"预定义"），
      // 补空格后由 refreshPredefine 弹"指定"候选进入别名定义。
      // 2026-09-23："指定"不再是入口词——块外裸"指定…"行不进预定义模式（modeForLine
      // 向上找不到"预定义"行即不归属）；块内续行（第2…N条预定义）仍正常归 predefine。
      const text = box.value;
      const pos = box.selectionStart;
      const ls = text.lastIndexOf("\n", pos - 1) + 1;
      const lineHead = text.slice(ls, pos);
      // 行首到光标为"预定义"或"预定义："（冒号兼容）均补空格
      if (/^预定义[：:]?$/.test(lineHead.trim()) && text[pos] !== " ") {
        box.value = text.slice(0, pos) + " " + text.slice(pos);
        box.selectionStart = box.selectionEnd = pos + 1;
      }
      updatePreview();
      refresh();
      return;
    }
    updatePreview();
    if (m === "rule" || m === "then" || m === "else") {
      // 引导词后自动补空格（自动切换场景没有空格），
      // 后续输入的词才能以引导词（如果/那么/否则）作为 prev 正确检索
      const text = box.value;
      const pos = box.selectionStart;
      const kw = MODE_WORD[m];   // 当前模式对应引导词（反向映射）
      // 仅当"行首到光标只有引导词"才补空格：行级模式对齐（光标回移到正文中间）
      // 时 slice(pos-2,pos) 恰为引导词不能补，否则会把正文误拆（如"…如果条款"）
      const ls2 = text.lastIndexOf("\n", pos - 1) + 1;
      const head = text.slice(ls2, pos).trim();
      if (text.slice(Math.max(0, pos - 2), pos) === kw && text[pos] !== " " &&
          (head === kw || head === kw + "：" || head === kw + ":")) {
        box.value = text.slice(0, pos) + " " + text.slice(pos);
        box.selectionStart = box.selectionEnd = pos + 1;
      }
      refresh();   // 自由文本模式：立即按当前词弹出现有候选
    }
    // predefine 模式：候选由空格推进/输入过滤触发（见 refreshPredefine）
  }

  // ---------- 工具 ----------
  // 取当前行（行首 index=ls）的上一行文本（不含行尾换行）；无则 ""。
  // 约定：ls 是当前行行首（即上一个 "\n" 之后的位置）。
  // 用于"入口词独立成行"场景：入口词独占上一行，光标在下一空行继续引导。
  function prevLineText(text, ls) {
    if (ls <= 0) return "";                     // 第一行无上一行
    const end = ls - 1;                          // 上一行行尾换行的下标
    const pls = text.lastIndexOf("\n", end - 1) + 1;   // 上一行行首（无则 0）
    return text.slice(pls, end);
  }
  // 取 beforeIndex 之前最近的"非空"空格分隔词；跳过连续空格（空词）。
  // 返回 {start, end, word}，无则 null。
  function prevWordAt(text, beforeIndex) {
    if (beforeIndex <= 0) return null;
    // 往前跳过空格
    let end = beforeIndex - 1;
    while (end >= 0 && text[end] === " ") end--;
    if (end < 0) return null;
    // 找该词起点（往前到空格或开头）
    let start = end;
    while (start > 0 && text[start - 1] !== " ") start--;
    return { start: start, end: end + 1, word: text.slice(start, end + 1) };
  }

  function currentWord() {
    const text = box.value;
    const pos = box.selectionStart;
    // 限定当前行：换行符分隔多条语句，取词/前词不跨行
    const ls = text.lastIndexOf("\n", pos - 1) + 1;
    let le = text.indexOf("\n", pos);
    if (le === -1) le = text.length;
    const line = text.slice(ls, le);
    const lpos = pos - ls;
    // 行内向前找最近空格
    let s = line.lastIndexOf(" ", lpos - 1);
    // 行内向后找最近空格（从 lpos 开始）
    let e = line.indexOf(" ", lpos);
    if (e === -1) e = line.length;
    // 上一个词 / 上上个词：跳过连续空格，取最近的非空词（均在行内）
    const p1 = prevWordAt(line, s);
    const prev = p1 ? p1.word : "";
    const p2 = p1 ? prevWordAt(line, p1.start) : null;
    const prevprev = p2 ? p2.word : "";
    return {
      word: line.slice(s + 1, e),
      prev: prev,
      prevprev: prevprev,
      start: ls + s + 1,
      end: ls + e,
    };
  }

  function kindTag(k) {
    const map = { operator: "运算符", arith: "运算", connector: "连接", object: "对象", field: "字段", keyword: "引导", entry: "入口", ptype: "类型", ppath: "路径", pconn: "连接", method: "方法", pklist: "列表", pkfield: "非列表", pfieldpath: "字段路径", pdstart: "入口", pdalias: "别名", pcond: "子句", pcondtype: "类型", pcondfield: "字段", pcondop: "运算符" };
    return map[k] || k || "项";
  }

  // ---------- 候选定位（跟随输入光标，类似 IDE 联想） ----------
  // textarea 无法直接读取光标像素位置，采用"镜像层"方案：复制编辑框的排版样式
  // （字体/行高/padding/换行宽度），与编辑框文本区完全对齐铺一层隐藏 div，
  // 把光标前的文本放入镜像并在文末放一个零宽标记，标记的视口位置即光标位置。
  function caretOffset() {
    const pos = box.selectionStart;
    const cs = window.getComputedStyle(box);
    const br = box.getBoundingClientRect();
    const pL = parseFloat(cs.paddingLeft) || 0;
    const pR = parseFloat(cs.paddingRight) || 0;
    const pT = parseFloat(cs.paddingTop) || 0;
    const mirror = document.createElement("div");
    // 复制影响排版/自动换行的样式
    [
      "fontFamily", "fontSize", "fontWeight", "fontStyle", "letterSpacing",
      "lineHeight", "boxSizing", "textTransform", "wordSpacing", "textIndent",
    ].forEach(function (p) { mirror.style[p] = cs[p]; });
    // 让镜像文本起点 = textarea 内容区起点（border 内、padding 外）：
    // textarea 文本真正起点在 border + padding 之后。
    mirror.style.position = "fixed";
    mirror.style.left = (br.left + (parseFloat(cs.borderLeftWidth) || 0) + pL) + "px";
    mirror.style.top = (br.top + (parseFloat(cs.borderTopWidth) || 0) + pT) + "px";
    // 文本换行宽度 = clientWidth - 左右 padding（clientWidth 已排除滚动条）
    mirror.style.width = (box.clientWidth - pL - pR) + "px";
    mirror.style.padding = "0";                    // 文本区从 0 开始，避免重复计 padding
    mirror.style.visibility = "hidden";
    mirror.style.pointerEvents = "none";
    mirror.style.whiteSpace = "pre-wrap";          // textarea 默认软换行
    mirror.style.wordWrap = "break-word";
    mirror.style.overflowWrap = "break-word";
    document.body.appendChild(mirror);
    const prefix = document.createElement("span");
    prefix.textContent = box.value.slice(0, pos);
    mirror.appendChild(prefix);
    const mark = document.createElement("span");
    mark.textContent = "\u200b";                   // 零宽空格：占字符位不占宽度
    mirror.appendChild(mark);
    const mRect = mark.getBoundingClientRect();
    document.body.removeChild(mirror);
    return {
      x: mRect.left - box.scrollLeft,
      y: mRect.top - box.scrollTop,
    };
  }

  // 下拉定位在光标所在行的正下方；底部放不下时翻到光标上方
  function positionList() {
    if (!open || !list.classList.contains("show")) return;
    const c = caretOffset();
    const lineH = parseFloat(window.getComputedStyle(box).lineHeight) || 28;
    const listW = list.offsetWidth || 300;
    const listH = list.offsetHeight || 260;
    let x = c.x;
    let y = c.y + lineH;
    if (y + listH > window.innerHeight - 12) y = Math.max(12, c.y - listH - 6);
    if (x + listW > window.innerWidth - 12) x = Math.max(12, c.x - (listW - 60));
    list.style.left = x + "px";
    list.style.top = y + "px";
  }

  // ---------- 渲染候选 ----------
  function render(cs) {
    cands = cs || [];
    hl = -1;
    list.innerHTML = "";
    if (!cands.length) { close(); return; }
    open = true;
    list.classList.add("show");
    cands.forEach(function (c, i) {
      const d = document.createElement("div");
      d.className = "sug-item";
      d.setAttribute("data-idx", i);
      const label = document.createElement("span");
      label.textContent = c.label;
      label.title = c.label;   // 过长时悬停查看全名
      d.appendChild(label);
      // 模糊兜底结果标注
      if (c.source === "fuzzy_ngram") {
        const ftag = document.createElement("span");
        ftag.className = "tag fuzzy";
        ftag.textContent = "模糊";
        d.appendChild(ftag);
      }
      // 预定义词条标注（规则预定义变量及其下级别名/子字段）
      if (c.source === "predefine") {
        const ptag = document.createElement("span");
        ptag.className = "tag fuzzy";
        ptag.textContent = "预定义";
        d.appendChild(ptag);
      }
      // 类型标签：有 kind 才标（2026-09-29，去"项"占位）
      if (c.kind) {
        const tag = document.createElement("span");
        tag.className = "tag " + c.kind;
        tag.textContent = kindTag(c.kind);
        d.appendChild(tag);
      }
      // 数据类型标签：候选自带 type（BOM 字段 string/date/list…）才标
      if (c.type) {
        const tt = document.createElement("span");
        tt.className = "tag type";
        tt.textContent = c.type;
        d.appendChild(tt);
      }
      d.addEventListener("mousedown", function (ev) {
        ev.preventDefault();  // 阻止 textarea 失焦
        insert(c);
      });
      list.appendChild(d);
    });
    // 默认高亮第一项（用户确认的交互）：候选弹出即可直接回车选中插入；
    // "整行输入完按回车换行"由 keydown 的行内容状态判定分流（见 hlManual 注释）
    hlManual = false;
    highlight(0);
    positionList(); // 定位到输入光标正下方
  }

  function close() {
    open = false;
    cands = [];
    hl = -1;
    hlManual = false;
    list.classList.remove("show");
    list.innerHTML = "";
    paramRange = null;   // 关闭候选即丢弃参数位替换区间（防误替换）
  }

  // 用户主动关闭候选：除了收起列表，还要取消"防抖未发/在途未回"的检索，
  // 否则迟到响应会把列表重新弹出
  function dismissSuggest() {
    uiSeq++;
    clearTimeout(timer);
    clearTimeout(pdTimer);
    clearTimeout(mtTimer);
    close();
  }

  function highlight(i) {
    if (!cands.length) return;
    hl = (i + cands.length) % cands.length;
    Array.prototype.forEach.call(list.children, function (el, idx) {
      el.classList.toggle("hl", idx === hl);
    });
    // 候选可达 50 条、列表限高带滚动条：键盘导航时高亮项滚动进可视区
    const el = list.children[hl];
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  }

  // ---------- 行级模式对齐（2026-09-08） ----------
  // 光标可在已输入文本中任意回移，全局 mode 必须跟随光标所在行归属的引导词块，
  // 否则点回"如果"行时仍残留 then 模式（或反之）→ 候选错乱：
  // "那么"行弹"如果"的候选、"如果"行末按空格不弹对象候选。
  // 推断规则（与 foldEntryLines 的折叠归属一致）：
  //   当前行有内容：行首引导词（如果/那么/否则/预定义）→ 对应模式；
  //                行首无引导词 → 向上找最近的孤立引导词行（两行式正文行归属其引导词块），
  //                向上遇空行终止（空行结束引导词块作用域，同 foldEntryLines 的 flush）
  //   当前行无内容：向上穿正文找最近孤立引导词行 → 其模式（两行式续行；中间/排版
  //                空行视作其上方正文块的延续——不能回退 entry，否则 refresh 对齐
  //                入口态后 updatePreview 走纯文本回显，整段预览着色消失，2026-09-10）
  //   向上扫描找不到引导词 → null（保持现模式，不强行纠偏；内容为空的入口态
  //   由 input 事件分支直接 setMode("entry")，不依赖此处推断）
  function modeForLine() {
    const text = box.value;
    const pos = box.selectionStart;
    // 行首"引导词："先归一（如果：/那么：… 同判），行数不变不影响光标行定位
    const lines = text.split("\n").map(stripLeadColon);
    let idx = text.slice(0, pos).split("\n").length - 1;
    if (idx >= lines.length) idx = lines.length - 1;
    const cur = (lines[idx] || "").trim();
    if (cur) {
      const fm = cur.match(FOLD_ENTRY_RE);
      if (fm) return WORD_MODE[fm[1]];
      // 2026-09-23："指定"不再是入口词。裸"指定…"行只有向上能扫到"预定义"引导块
      // （下方循环）才归 predefine 模式——块内续行（第2…N条预定义正文）正常归属；
      // 文档开头无"预定义"行的手敲/粘贴"指定…"行不再进预定义模式。
      for (let i = idx - 1; i >= 0; i--) {
        const ut = lines[i].trim();
        if (!ut) return null;                       // 空行：引导词块终止
        const um = ut.match(FOLD_ENTRY_RE);
        if (um && ut === um[1]) return WORD_MODE[um[1]];   // 两行式：孤立引导词行
      }
      return null;
    }
    // 光标在空行（或仅空白行）：向上穿正文/空行找最近孤立引导词行
    // （预定义块续行场景本就带"预定义"孤立行，循环可直接命中；2026-09-23 起
    //   不再认"见过 指定 行"作预定义块依据——指定 不是入口词）
    for (let i = idx - 1; i >= 0; i--) {
      const pt = (lines[i] || "").trim();
      if (!pt) continue;                            // 连续空行：继续向上
      const pm = pt.match(FOLD_ENTRY_RE);
      if (pm && pt === pm[1]) return WORD_MODE[pm[1]];
    }
    return null;
  }

  // ---------- 刷新候选（防抖） ----------
  function refresh() {
    // 行级模式对齐：先按光标所在行纠偏模式，再做原有引导词兜底与分发
    const lm = modeForLine();
    if (lm && mode !== lm) { enterMode(lm); return; }
    // 任意模式兜底：光标所在词是引导词 → 立即切换到对应模式
    // （换行后手动输入、粘贴等导致 mode 与内容不一致时自动纠正）
    const gw = normEntryWord(currentWord().word);
    const gm = WORD_MODE[gw];
    if (gm && mode !== gm) { enterMode(gm); return; }
    // 入口态：只弹 预定义 / 如果 / 那么 / 否则 引导候选，不走后端检索
    if (mode === "entry") { showEntry(); return; }
    // 预定义模式：按主句步骤检索（元素类型 / 列表路径）
    if (mode === "predefine") { refreshPredefine(); return; }
    // then/else 模式：动作候选只给 BOM 方法（见 refreshThenElse）
    if (mode === "then" || mode === "else") { refreshThenElse(); return; }
    if (refreshSelector()) return;
    const cw = currentWord();
    if (!cw.word.trim() && !box.value.length) {
      // 完全空输入：关闭候选
      close();
      return;
    }
    clearTimeout(timer);
    timer = setTimeout(function () {
      const s = uiSeq;   // 发出时快照：期间被 Esc/点外关闭则响应作废
      const url = "/api/search?word=" + encodeURIComponent(cw.word) +
                  "&prev=" + encodeURIComponent(cw.prev || "") +
                  "&prevprev=" + encodeURIComponent(cw.prevprev || "") + "&limit=200" +
                  "&predefines=" + encodeURIComponent(predefineLines());
      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (s !== uiSeq) return;
          // 请求返回时可能已切到别的词，校验当前词仍一致
          const cur = currentWord();
          if (cur.word !== cw.word) return;
          render(data.candidates || []);
        })
        .catch(function () { close(); });
    }, 120);
  }

  // 字典选择值使用整段区间，名称含空格时也不会破坏其它条件。
  function refreshSelector() {
    const text = box.value, caret = box.selectionStart;
    const ls = text.lastIndexOf("\n", caret - 1) + 1;
    const le0 = text.indexOf("\n", caret), le = le0 < 0 ? text.length : le0;
    const line = text.slice(ls, le);
    if (!/(不?属于)\s*[（(]\s*(单选|多选)\s*[）)]/.test(line.slice(0, caret - ls))) return false;
    clearTimeout(timer);
    const seq = ++uiSeq;
    timer = setTimeout(function () {
      fetch('/api/selector_context?line=' + encodeURIComponent(line) + '&caret=' + (caret - ls))
        .then(function (r) { if (!r.ok) throw new Error('字典加载失败'); return r.json(); })
        .then(function (ctx) {
          if (seq !== uiSeq || box.value !== text || box.selectionStart !== caret) return;
          if (!ctx.active) { refreshOrdinary(); return; }
          showSelector(ctx, text, ls);
        }).catch(function () {
          if (seq === uiSeq) { close(); status.textContent = '字典加载失败，请重试'; }
        });
    }, 120);
    return true;
  }

  function refreshOrdinary() {
    // 光标在同一行的其它条件时继续使用原有候选。
    const cw = currentWord(), seq = ++uiSeq, text = box.value, caret = box.selectionStart;
    fetch('/api/search?word=' + encodeURIComponent(cw.word) + '&prev=' + encodeURIComponent(cw.prev || '') +
      '&prevprev=' + encodeURIComponent(cw.prevprev || '') + '&limit=200&predefines=' + encodeURIComponent(predefineLines()))
      .then(function (r) { return r.json(); }).then(function (data) {
        if (seq === uiSeq && box.value === text && box.selectionStart === caret) render(data.candidates || []);
      }).catch(function () { if (seq === uiSeq) close(); });
  }

  function showSelector(ctx, original, lineStart) {
    close();
    open = true;
    list.classList.add('show');
    const selected = new Set();
    const raw = ctx.multiple ? ctx.raw.replace(/^\[|\]$/g, '').split(/[,，、]/) : [ctx.raw];
    ctx.options.forEach(function (o) {
      if (raw.some(function (v) { return [o.label, o.key, o.fieldPath].includes(v.trim()); })) selected.add(o.key);
    });
    const title = document.createElement('div');
    title.textContent = ctx.error || ((ctx.multiple ? '多选' : '单选') + ' · ' + ctx.codeService);
    list.appendChild(title);
    const search = document.createElement('input');
    search.type = 'search'; search.placeholder = '搜索名称或编码'; search.setAttribute('aria-label', '搜索字典选项');
    search.style.cssText = 'width:100%;box-sizing:border-box;margin:6px 0';
    list.appendChild(search);
    const rows = document.createElement('div');
    rows.style.cssText = 'max-height:220px;overflow:auto';
    list.appendChild(rows);
    function commit() {
      if (box.value !== original) { dismissSuggest(); return; }
      const values = ctx.options.filter(function (o) { return selected.has(o.key); });
      if (!values.length) { title.textContent = '请至少选择一个选项'; return; }
      // 同名项使用编码回填，避免显示名称无法唯一还原。
      const labels = values.map(function (o) {
        return ctx.options.filter(function (p) { return p.label === o.label; }).length > 1 ? o.key : o.label;
      });
      const value = ctx.multiple ? '[' + labels.join(',') + ']' : labels[0];
      const start = lineStart + ctx.start, end = lineStart + ctx.end;
      const leading = start > 0 && !/\s/.test(original[start - 1]) ? ' ' : '';
      box.value = original.slice(0, start) + leading + value + original.slice(end);
      box.selectionStart = box.selectionEnd = start + leading.length + value.length;
      dismissSuggest(); updatePreview(); box.focus();
    }
    function draw() {
      rows.replaceChildren();
      const query = search.value.trim().toLowerCase();
      const options = ctx.options.filter(function (o) { return (o.label + ' ' + o.key).toLowerCase().includes(query); });
      options.forEach(function (o) {
        const row = document.createElement('label');
        row.style.cssText = 'display:flex;gap:8px;align-items:center;padding:5px;cursor:pointer';
        const input = document.createElement('input');
        input.type = ctx.multiple ? 'checkbox' : 'radio'; input.name = 'rule-dictionary-value';
        input.checked = selected.has(o.key);
        input.addEventListener('change', function () {
          if (!ctx.multiple) selected.clear();
          if (input.checked) selected.add(o.key); else selected.delete(o.key);
          if (!ctx.multiple) commit();
        });
        const name = document.createElement('span'); name.textContent = o.label + ' (' + o.key + ')';
        row.append(input, name); rows.appendChild(row);
      });
      if (!options.length) rows.textContent = '没有匹配的字典选项';
    }
    search.addEventListener('input', draw);
    search.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { dismissSuggest(); box.focus(); }
      if (ev.key === 'Enter') { ev.preventDefault(); if (ctx.multiple) commit(); }
    });
    draw();
    if (ctx.multiple) {
      const apply = document.createElement('button'); apply.type = 'button'; apply.textContent = '确认选择';
      apply.addEventListener('click', commit); list.appendChild(apply);
    }
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = '取消';
    cancel.addEventListener('click', function () { dismissSuggest(); box.focus(); }); list.appendChild(cancel);
    positionList();
  }

  // ---------- 预定义主句：步骤解析与候选检索 ----------
  // 主句模板：指定'别名'为一个X来自(in) 'root'的路径
  // 步骤：① 引号内输入别名 → ② 空格弹元素类型(actualTypeNames)候选 →
  //       ③ 空格自动补 来自(in) → ④ 空格弹列表路径候选
  // 编辑框中空格会显示（与"如果"模式一致的视觉反馈），但这些是编辑辅助空格，
  // 不属于最终规则文本——生成规则内容（预览/复制）时用本函数清理：
  //   "为一个" 后的空格删除（为一个X 紧邻）
  //   "来自(in)" 前的空格删除（X来自(in) 紧邻）
  //   "来自(in)" 后归一为一个空格（来自(in) 'root'的路径）
  function normalizePredefine(text) {
    return text
      .replace(/为一个\s+/g, "为一个")
      .replace(/为\s+(?=')/g, "为")           // 非列表：为 '保单'的... → 为'保单'的...
      .replace(/\s+来自\s*[（(]\s*in\s*[）)]/g, "来自(in)")
      .replace(/来自\s*[（(]\s*in\s*[）)]/g, "来自(in)")   // 全角连接词→半角（幂等）
      .replace(/来自\(in\)\s+/g, "来自(in) ")
      // "符合条件"子句：归一"当前这个"后单空格；字段与运算符、运算符与值紧邻（样本口径）
      .replace(/符合条件[：:]\s*当前这个\s+/g, "符合条件：当前这个 ")
      .replace(/([^\s是属于包含]+)\s+(是|不是|包含)(?=\s*[“"])/g, "$1$2")
      .replace(/(是|不是|包含)\s+(?=[“"])/g, "$1");
  }

  // 多行混合归一化：'预定义 指定…'（新行文）/ '指定…'（旧格式）开头的行清理编辑辅助空格，其余行原样
  function normalizeMixed(text) {
    return text.split("\n").map(function (ln) {
      const t = stripLeadColon(ln).trim();
      return (/^预定义\s*指定/.test(t) || /^指定/.test(t)) ? normalizePredefine(t) : t;
    }).join("\n");
  }

  // 入口词独立成行折叠（2026-09-03）：候选选择"如果/那么/否则"后自动换行，
  // 使入口词独占一行、正文从下一行输入（编辑区两行：如果↵正文）。
  // 预览/上传/复制前先把"入口词独占行 + 后续正文行"折叠回一行逻辑语句——
  // 旧式同行写法（如果 保单的…）不经折叠原样保留，两者结果一致。
  // 独立入口词行 = 整行仅引导词（预定义/如果/那么/否则，无正文）。
  // 预定义行是"预定义 指定…"整行同行（含正文），不会被当独立入口词行折叠。
  // 行首引导词冒号兼容（2026-09-10）：用户手输"预定义：/如果：/那么：/否则："
  // （全/半角冒号均可、可连续多个）与"预定义 "（空格）同识别——stripLeadColon
  // 把行首"引导词+冒号"归一为"引导词 空格"（独立行归一为裸引导词）；正文内
  // 冒号（如预定义"符合条件：…"）不受影响。候选检索逻辑不变，仅识别侧归一。
  const FOLD_ENTRY_RE = /^(预定义|如果|那么|否则)(?=\s|$)/;
  const LEAD_COLON_RE = /^(预定义|如果|那么|否则)[：:]+\s*/;
  function stripLeadColon(ln) {
    const t = (ln || "").trim();
    const m = t.match(LEAD_COLON_RE);
    if (!m) return ln;                     // 非引导冒号行：原样返回
    const rest = t.slice(m[0].length);
    return rest ? m[1] + " " + rest : m[1];
  }
  // mapOut（2026-09-30，可选）：传入数组时按输出行序记录每条折叠行由编辑框
  // 哪些原始行（0-based 行号）合成——供编辑框错误标红垫层把"折叠后行号 _line"
  // 上的非法词映射回原始行定位。
  function foldEntryLines(text, mapOut) {
    const out = [];
    const rec = function (idxs) { if (mapOut) mapOut.push(idxs); };
    // {lead, parts, single} 入口词独占行，等待续行正文；parts 元素 {t, ri}
    //（2026-09-30：携带来源原始行号）；
    // single=true 用于 那么/否则——每条正文是独立动作，各成一行（多条上传）。
    let pend = null;
    const flush = function () {
      if (!pend) return;
      if (pend.single) {
        // 那么/否则：每条正文独立成一个带引导词的逻辑行，
        // 使后端 annotate_then_else_line / convert_then_else_line 按"每行一个动作"
        // 正确拆成 thenList/elseList 多个元素，而非把后一条吞成前一条的参数值。
        if (pend.parts.length) {
          pend.parts.forEach(function (p) {
            out.push(pend.lead + " " + p.t); rec([p.ri]);
          });
        } else {
          out.push(pend.lead); rec([pend.leadRaw]);   // 裸引导词（编辑中，尚无正文）
        }
      } else {
        out.push(pend.parts.length
          ? pend.lead + " " + pend.parts.map(function (p) { return p.t; }).join(" ")
          : pend.lead);
        rec([pend.leadRaw].concat(pend.parts.map(function (p) { return p.ri; })));
      }
      pend = null;
    };
    (text || "").split("\n").forEach(function (ln, ri) {
      const t = stripLeadColon(ln).trim();   // 行首"引导词："归一为"引导词 "
      if (!t) { flush(); return; }
      const m = t.match(FOLD_ENTRY_RE);
      if (m) {
        flush();
        const lead = m[1];
        const rest = t.slice(lead.length).trim();
        if (lead === "那么" || lead === "否则") {
          // 每条动作独占一行；即使首条与引导词同行也进入续行收集态，
          // 后续独立行的动作同样归属本引导词块（多条上传）。
          pend = { lead: lead, parts: rest ? [{ t: rest, ri: ri }] : [],
                   single: true, leadRaw: ri };
        } else if (rest) {
          out.push(t); rec([ri]);   // 入口词与正文同行（旧式单行）：原样
        } else {
          // 入口词独占行：后续正文续行
          pend = { lead: lead, parts: [], leadRaw: ri };
        }
        return;
      }
      if (pend) {
        // "预定义"块只收纳紧随的第一条主句：第二条 `指定…` 行是独立的预定义
        // （块内续行），折进同一逻辑行会使路径拼接吞入下一句 → 误报
        // "列表路径不存在"（2026-09-15）。其余引导词块正文允许多行续写。
        if (pend.lead === "预定义" && pend.parts.length && /^指定/.test(t)) {
          flush();
          out.push(t); rec([ri]);
          return;
        }
        pend.parts.push({ t: t, ri: ri });   // 入口词独占行后的正文续行
      } else {
        out.push(ln); rec([ri]);      // 无入口词前缀的行（如预定义续行 指定'…'、纯规则正文）原样
      }
    });
    flush();
    return out.join("\n");
  }

  // 收集编辑区预定义行（'预定义 指定…'/'指定…'开头，保留行原文含'预定义'前缀，
  // 后端 parse_predefine_lines 兼容剥前缀），随检索/标注请求发给后端：
  // 后端解析成功的预定义会注入规则模式（一级对象"规则预定义变量"及其下级别名/子字段）
  // 2026-09-03：先折叠入口词独立行——预定义可"预定义↵指定'…'"两行式（入口词独立行），
  // 折叠后统一为"预定义 指定'…'"逻辑行再收集，保证与预览/上传同口径。
  function predefineLines() {
    return foldEntryLines(box.value).split("\n")
      .filter(function (ln) {
        const t = ln.trim();
        return /^预定义\s*指定/.test(t) || /^指定/.test(t);
      })
      .join("\n");
  }

  function refreshPredefine() {
    // 仅取当前行光标前文本匹配（多行编辑时前面可能有其他规则行）
    const _t = box.value, _p = box.selectionStart;
    const _ls = _t.lastIndexOf("\n", _p - 1) + 1;
    let before = _t.slice(_ls, _p);
    // 2026-09-03：行首可选"预定义 "引导词（预定义 指定'别名'…）。
    // 仅剩"预定义 "（未选"指定"）→ 弹"指定"候选进入别名定义；
    // 否则剥前缀后按"指定…"步骤匹配（旧格式"指定…"无前缀同样兼容）。
    if (/^预定义\s*[：:]?$/.test(before.trim())) {
      status.textContent = "预定义 · 选择'指定'开始定义";
      render([{ label: "指定", kind: "pdstart" }]);
      return;
    }
    // 2026-09-03：预定义入口词独立成行（预定义↵指定'别名'…）——
    // 上一行是孤立"预定义"、光标在本空行时，同样弹"指定"候选。
    // 2026-09-23：候选同步补 4 个入口词（纯本地拼接、零检索无竞态），
    // 继续定义选"指定"、切换编辑选入口词。
    if (!before.trim() && stripLeadColon(prevLineText(_t, _ls)).trim() === "预定义") {
      status.textContent = "预定义 · 选'指定'开始定义，或选入口词切换";
      render([{ label: "指定", kind: "pdstart" }].concat(ENTRY_CANDS));
      return;
    }
    // 2026-09-23：预定义块内空行（上一行是已完成的 指定…行，点击/敲字到达）→
    // 弹"指定"续行候选 + 入口词垫底（同步渲染）
    {
      const _le2 = _t.indexOf("\n", _p);
      const _line = _t.slice(_ls, _le2 === -1 ? _t.length : _le2);
      if (!_line.trim()) {
        status.textContent = "预定义 · 选'指定'继续定义，或选入口词切换";
        render([{ label: "指定", kind: "pdstart" }].concat(ENTRY_CANDS));
        return;
      }
    }
    before = before.replace(/^预定义\s*[：:]?\s*/, "");
    // ---- 别名闭合后：选择"为一个(列表) / 为(非列表)" ----
    if (/^指定'[^']*'$/.test(before) || /^指定'[^']*'\s+$/.test(before)) {
      status.textContent = "预定义 · 选择一个(列表)或为(非列表)";
      render([
        { label: "为一个", kind: "pklist", note: "列表" },
        { label: "为", kind: "pkfield", note: "非列表" },
      ]);
      return;
    }
    // ---- 非列表：指定'别名'为 + 对象字段路径（排除"为一个"，容忍编辑辅助空格/引号） ----
    {
      const fm = before.match(/^指定'[^']*'为(?!一个)\s*(.*)$/);
      if (fm) {
        // 2026-09-29：右值已打到子句结束分号 → 本条预定义完成，不再弹候选
        // （旧行为：分号粘进检索词，精确 0 命中后模糊兜底召回一堆无关候选）
        if (pdClauseEnd(fm[1])) {
          status.textContent = "预定义 · 本条已结束（; 收尾，继续输入请另起一行）";
          close();
          return;
        }
        fetchPdParam(fm[1]);
        return;
      }
    }
    // ---- "符合条件"子句引导（主句完成后，优先于主句状态判断） ----
    // 主句完整 + 尾随空格 → 弹"符合条件："引导（可选项，也可直接结束）
    // 路径内部可能自带空格（如"'保单'的人身险信息的 063060…险别列表"），用 \S+(?:\s+(?!符合条件)\S+)* 容错
    // （每段必须真实空格分隔 + 段首排除"符合条件"，避免 \s*\S+ 粘连导致的灾难性回溯）
    if (/^指定'[^']*'为一个\s*\S+\s*来自\s*[（(]\s*in\s*[）)]\s+'?[^']*'?的\S+(?:\s+(?!符合条件)\S+)*\s+$/.test(before)) {
      status.textContent = "预定义 · 可追加过滤条件";
      render([{ label: "符合条件：", kind: "pcond" }]);
      return;
    }
    // 主句完整 + 已手打"符/符合/符合条件"前缀（可含冒号、允许路径后误输分号）→ 补全"符合条件："候选
    if (/^指定'[^']*'为一个\s*\S+\s*来自\s*[（(]\s*in\s*[）)]\s+'?[^']*'?的\S+(?:\s+(?!符合条件)\S+)*\s*;?\s*符合?(?:条件)?[：:]?\s*$/.test(before)) {
      status.textContent = "预定义 · 可追加过滤条件";
      render([{ label: "符合条件：", kind: "pcond" }]);
      return;
    }
    // "符合条件：当前这个"后（尾随空格）→ 弹条件类型名（主句 X，固定一项）
    let cm = before.match(/^指定'[^']*'为一个\s*(\S+)\s*来自\s*[（(]\s*in\s*[）)]\s+\S+(?:\s+(?!符合条件)\S+)*\s*符合条件[：:]\s*当前这个\s+$/);
    if (cm) {
      status.textContent = "预定义 · 条件元素类型";
      render([{ label: cm[1], kind: "pcondtype" }]);
      return;
    }
    // "当前这个 X的"后 → 条件字段候选（该类型列表节点的子字段）
    cm = before.match(/^指定'[^']*'为一个\s*(\S+)\s*来自\s*[（(]\s*in\s*[）)]\s+\S+(?:\s+(?!符合条件)\S+)*\s*符合条件[：:]\s*当前这个\s*(\S+)\s*的([^\s是属于；;]*)$/);
    if (cm) {
      status.textContent = "预定义 · 条件字段";
      fetchPredefine("cond_field", cm[3], cm[2]);
      return;
    }
    // 字段完成 + 尾随空格 → 条件运算符候选
    cm = before.match(/^指定'[^']*'为一个\s*(\S+)\s*来自\s*[（(]\s*in\s*[）)]\s+\S+(?:\s+(?!符合条件)\S+)*\s*符合条件[：:]\s*当前这个\s*\S+\s*的(\S+)\s+$/);
    if (cm) {
      status.textContent = "预定义 · 条件运算符";
      fetchPredefine("cond_op", "", cm[1], cm[2]);
      return;
    }
    // 运算符输入中 → 按已输入文字过滤运算符候选
    cm = before.match(/^指定'[^']*'为一个\s*(\S+)\s*来自\s*[（(]\s*in\s*[）)]\s+\S+(?:\s+(?!符合条件)\S+)*\s*符合条件[：:]\s*当前这个\s*\S+\s*的(\S+)\s*(是|不是|包含)$/);
    if (cm) {
      status.textContent = "预定义 · 条件运算符";
      fetchPredefine("cond_op", cm[3], cm[1], cm[2]);
      return;
    }
    // 运算符后 → 值（自由输入，以引号包裹、;结尾；不弹候选）
    if (/^指定'[^']*'为一个\s*\S+\s*来自\s*[（(]\s*in\s*[）)]\s+\S+(?:\s+(?!符合条件)\S+)*\s*符合条件[：:]\s*当前这个\s*\S+\s*的\S+\s*(是|不是|包含)\s*[“"]?[^“”；;]*$/.test(before)) {
      status.textContent = "预定义 · 输入条件值（引号包裹，; 结尾）";
      close();
      return;
    }
    // 步骤④ 输入中：按已输入文字过滤列表路径候选
    // （"为一个"与 X、X 与"来自(in)"之间都允许有编辑辅助空格）
    // 类型 X 与列表路径一一对应：按 X 精准过滤路径候选
    let m = before.match(/^指定'[^']*'为一个\s*(\S*)\s*来自\s*[（(]\s*in\s*[）)]\s+([^\s]*)$/);
    if (m) {
      status.textContent = "预定义 · 选择列表路径";
      fetchPredefine("path", m[2], m[1]);
      return;
    }
    // 步骤③ 元素类型已输入且按过空格（尾随空白）→ 弹"来自(in)"连接候选供选择
    if (/^指定'[^']*'为一个\s*\S+\s+$/.test(before)) {
      status.textContent = "预定义 · 选择连接词";
      render([{ label: "来自(in)", kind: "pconn" }]);
      return;
    }
    // 步骤② 元素类型输入中（无尾随空白）：按已输入文字过滤类型候选
    m = before.match(/^指定'[^']*'为一个\s*([^\s]+)$/);
    if (m) {
      status.textContent = "预定义 · 选择/输入元素类型";
      fetchPredefine("type", m[1]);
      return;
    }
    // 步骤② 元素类型为空（"为一个"后仅有空白）：弹全量类型候选
    if (/^指定'[^']*'为一个\s*$/.test(before)) {
      status.textContent = "预定义 · 选择/输入元素类型";
      fetchPredefine("type", "");
      return;
    }
    if (/^指定'[^']*$/.test(before)) {
      status.textContent = "预定义 · 输入别名";
    }
    close();
  }

  var pdTimer = null;
  var pdSeq = 0;
  function fetchPredefine(kind, word, ptype, field) {
    clearTimeout(pdTimer);
    const seq = ++pdSeq;
    pdTimer = setTimeout(function () {
      let url = "/api/predefine_search?kind=" + encodeURIComponent(kind) +
                "&word=" + encodeURIComponent(word) + "&limit=200";
      if (ptype) url += "&type_name=" + encodeURIComponent(ptype);
      if (field) url += "&field=" + encodeURIComponent(field);
      const s = uiSeq;
      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (s !== uiSeq) return;
          if (mode !== "predefine" || seq !== pdSeq) return;
          render(data.candidates || []);
        })
        .catch(function () { close(); });
    }, 120);
  }

  // 2026-09-11：预定义"指定''为"右值上下文——光标在方法实参位（含 <date>/<double>
  // 占位符上）→ 后端按 BOM detailsList 参数类型过滤路径候选，start/end 为占位符/
  // 文本片段区间（选中仅替换片段，a+b*c 表达式其余不动）；非参数位后端直接回传
  // field_path 混合候选（stage=rvalue 一次请求）；主句内回退原 fetchPredefine。
  // 光标偏移 → 所在行 {start: 行首绝对偏移, text: 行文本}
  function lineTextAt(pos) {
    const text = box.value;
    const ls = text.lastIndexOf("\n", pos - 1) + 1;
    let le = text.indexOf("\n", pos);
    if (le === -1) le = text.length;
    return { start: ls, text: text.slice(ls, le) };
  }

  // 子句结束判定（2026-09-29）：右值文本中出现引号外的裸分号 ;/； → 本条预定义
  // 已结束，不再弹候选。引号态用配对计数近似（分号前单引号族/双引号族各为偶数
  // 即处于引号外），方法实参字符串里的分号（"a;b"）不误判。
  function pdClauseEnd(txt) {
    const t = txt.replace(/[\u2018\u2019]/g, "'").replace(/[\u201c\u201d]/g, '"');
    for (let i = 0; i < t.length; i++) {
      const ch = t[i];
      if (ch !== ";" && ch !== "\uff1b") continue;
      const p = t.slice(0, i);
      const sq = (p.match(/'/g) || []).length;
      const dq = (p.match(/"/g) || []).length;
      if (sq % 2 === 0 && dq % 2 === 0) return true;
    }
    return false;
  }

  function fetchPdParam(fmWord) {
    clearTimeout(pdTimer);
    const seq = ++pdSeq;
    pdTimer = setTimeout(function () {
      const ln = lineTextAt(box.selectionStart);
      const cp = box.selectionStart - ln.start;
      const s = uiSeq;
      // 2026-09-29：带上全部预定义行 → 右值/表达式候选混入本块已定义别名
      fetch("/api/predefine_param_context?line=" + encodeURIComponent(ln.text) +
            "&caret=" + cp + "&limit=200" +
            "&predefines=" + encodeURIComponent(predefineLines()))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (s !== uiSeq) return;
          if (mode !== "predefine" || seq !== pdSeq) return;
          if (d.stage === "fallback") { paramRange = null; fetchPredefine("field_path", fmWord); return; }
          // 2026-09-29：后端判裸分号子句结束 → 不弹候选
          if (d.stage === "none") { paramRange = null; render([]); return; }
          const cands = d.candidates || [];
          if (d.stage === "param" && cands.length) {
            paramRange = { start: ln.start + d.start, end: ln.start + d.end };
            status.textContent = "预定义 · 选方法参数（第" + (d.param_index + 1) +
                                 "个 · 类型 " + (d.param_kind || "any") + "）";
          } else {
            paramRange = null;
            status.textContent = "预定义 · 选择对象字段路径或方法（非列表）";
          }
          render(cands);
        })
        .catch(function () { paramRange = null; fetchPredefine("field_path", fmWord); });
    }, 120);
  }

  // ---------- then/else 动作方法候选（只给 BOM 方法） ----------
  // 2026-09-03：then/else 检索升级为"参数位上下文"——
  // 后端根据整行文本 + 光标位置判定：仍在输方法名 → 弹方法候选；
  // 已进入参数位 → field 位弹 BOM 对象/字段路径候选，text 位不弹。
  var mtTimer = null;
  var mtSeq = 0;
  function refreshThenElse() {
    // 把光标所在整行 + 光标行内偏移发给后端做上下文定位
    const text = box.value;
    const pos = box.selectionStart;
    const ls = text.lastIndexOf("\n", pos - 1) + 1;
    let le = text.indexOf("\n", pos);
    if (le === -1) le = text.length;
    const line = text.slice(ls, le);
    const caret = pos - ls;
    fetchThenElseContext(line, caret, ls);
  }

  function fetchThenElseContext(line, caret, ls) {
    clearTimeout(mtTimer);
    const seq = ++mtSeq;
    mtTimer = setTimeout(function () {
      const url = "/api/method_param_context?line=" + encodeURIComponent(line) +
                  "&caret=" + encodeURIComponent(caret) + "&limit=200";
      const s = uiSeq;
      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (s !== uiSeq) return;   // Esc/点外关闭后迟到响应不再复活列表
          if (mode !== "then" && mode !== "else") return;
          if (seq !== mtSeq) return;
          if (data.stage === "param") {
            // 参数位：2026-09-28 与预定义实参位口径统一——占位符标明类型
            // （<double>/<string>/<date>…）的位给该类型过滤候选（空词也弹头部），
            // 类型未知的 field 位保留旧行为（有词才检索路径）。有候选即弹并整段替换。
            if (data.candidates && data.candidates.length) {
              paramRange = { start: ls + (data.start || 0),
                             end: ls + (data.end || 0) };
              status.textContent = (mode === "then" ? "那么" : "否则") +
                " · 选方法参数（第" + data.param_index +
                "个 · 类型 " + (data.param_ptype || "any") + "）";
              render(data.candidates);
            } else {
              paramRange = null;
              close();
            }
            return;
          }
          if (data.stage === "method") {
            paramRange = null;
            render(data.candidates || []);
            return;
          }
          paramRange = null;
          close();
        })
        .catch(function () { paramRange = null; close(); });
    }, 120);
  }

  // ---------- 插入候选 ----------
  // 2026-09-21: 光标停在词中间时选候选，替换区间须延伸到光标后词尾（第一个空白/换行），
  // 否则光标后的词残余会留在行内贴着新填入的候选文本（如在"…设置免赔费率%为(<double>)"
  // 中间选其他方法，只换掉光标前部分、"赔费率%为(<double>)"残留）。
  // \s 含全角空格\u3000；paramRange/通用分支本就整段或整词替换，无需此处理。
  function dropLeadToken(s) {
    return s.replace(/^[^\s]*/, "");
  }

  // 2026-09-22: 预定义"整段替换右值"区间计算。headRe 须以捕获组1给出主句前缀
  // （形如 ^指定'别名'为 / ^指定'别名'为一个X来自(in) ）。旧分支用 "?[^']*$"、
  // "[^\s]*$" 这类"只允许输入中片段"的整行正则，右值一旦成形（含闭合引号、
  // 路径内部自带空格）即整体失配 → 分支静默 return，表现为"点候选栏没反应"
  // （用户实测：指定'123'为'结果'的ruleInfos的规则名称 换引号候选无反应）。
  // 返回 {head, tail}：head=主句前缀（含前面所有行），替换区间=[head.length, tail起点)，
  // 区间终点取行尾或 ; / ； 子句分隔符（分隔符及其后内容原样保留）；不匹配返回 null。
  //
  // 2026-09-22 补充（算术表达式操作数级替换）：上面"整段右值"替换的前提是
  // 右值 = 单个操作数（可能含内部空格路径）。但右值常是算术表达式
  // （用户实测：指定'纯风险保费'为 '保单'…A - '保单'…B），整段替换会把
  // 光标所在操作数之外的其它操作数与运算符一并覆盖。故：右值内若含"独立算术
  // 运算符"（+ - * / 紧邻空白或位于右值首尾），把替换区间收敛到光标所在的那一段
  // 操作数（运算符及其前后空白作为分隔保留）；无独立运算符时维持整段替换不变。
  // 运算符判定要求紧邻空白，从而不误伤 BOM 路径内部的 保额/限额(元)（无邻空白）。
  function pdRvalueSpan(headRe) {
    const _t = box.value, selS = box.selectionStart;
    const _ls = _t.lastIndexOf("\n", selS - 1) + 1;
    let le = _t.indexOf("\n", selS);
    if (le === -1) le = _t.length;
    let before = _t.slice(_ls, selS);
    const pm = before.match(/^(预定义\s*)/);
    const pref = pm ? pm[1] : "";
    if (pref) before = before.slice(pref.length);
    const m = before.match(headRe);
    if (!m) return null;
    const headRaw = _t.slice(0, _ls) + pref + m[1].replace(/\s+$/, " ");
    // 右值扫描范围 [rvStart, rvEnd)：rvEnd = 行尾或 ; / ； 子句分隔符
    const rvStart = headRaw.length;
    let rvEnd = le;
    const s1 = _t.indexOf(";", rvStart);
    const s2 = _t.indexOf("；", rvStart);
    if (s1 !== -1 && s1 < rvEnd) rvEnd = s1;
    if (s2 !== -1 && s2 < rvEnd) rvEnd = s2;
    // 收集独立运算符分隔区间（含其紧邻前后空白），作为操作数边界。
    // 预定义右值运算符仅认符号（+ - * /，与后端 _PD_ARITH_OPS 一致——预定义
    // 子句整行直发 ADP，中文运算符词形不被接受，不参与边界切分）。
    function isOp(i) {
      const ch = _t.charAt(i);
      if (ch !== "+" && ch !== "-" && ch !== "*" && ch !== "/") return false;
      const prevWS = (i === rvStart) || /\s/.test(_t.charAt(i - 1));
      const nextWS = (i + 1 === rvEnd) || /\s/.test(_t.charAt(i + 1));
      return prevWS || nextWS;
    }
    const ops = [];
    for (let i = rvStart; i < rvEnd; i++) {
      if (!isOp(i)) continue;
      let a = i, b = i + 1;
      if (/\s/.test(_t.charAt(i - 1))) a = i - 1;      // 吸收前导空白
      while (b < rvEnd && /\s/.test(_t.charAt(b))) b++; // 吞掉尾随连续空白
      ops.push([a, b]);
      i = b - 1;
    }
    // 替换起点 s = 光标前最近运算符的右边界（或右值首），终点 e = 光标后
    // 首个运算符的左边界（或右值尾）；光标落在运算符/其空白内则视作"正在
    // 书写下一操作数"，s 取该运算符右边界。
    let s = rvStart, e = rvEnd;
    for (let k = 0; k < ops.length; k++) {
      const o = ops[k];
      if (o[1] <= selS) s = o[1];
      if (o[0] >= selS) { e = o[0]; break; }
    }
    // 2026-09-29: 替换区间不得跨过用户已输入的括号结构。场景：打完 `(`（含
    // 括号后空格）正想在括号内填第一个操作数时选候选——原区间=右值整段
    // [rvStart, 操作数尾)，把 `(` 当"待替换右值"整体覆盖（用户实测
    // 指定'123'为 ( <caret> 选候选吞括号；操作数在成对括号中间时尾 `)` 同理被吞）。
    // 口径：光标左侧存在未闭合 `(` → s 顶到最内层左括号后的操作数起点、
    // e 收到光标后第一个右括号；括号均已闭合且光标在括号外 → 维持原行为。
    // 全半角括号都算（与后端词法器一致）。
    const _stk = [];
    for (let i = rvStart; i < Math.min(selS, rvEnd); i++) {
      const ch = _t.charAt(i);
      if (ch === "(" || ch === "（") _stk.push(i);
      else if (ch === ")" || ch === "）") { if (_stk.length) _stk.pop(); }
    }
    if (_stk.length) {
      // s 取"光标所在空白词起点"，但不早于未闭合左括号内首位置：既保住
      // 括号结构，又与"替换光标当前词"的通用行为一致（嵌套/连续括号时
      // 只回退到当前词，不会把括号内前序内容一并覆盖）。
      const lb = _stk[_stk.length - 1] + 1;
      let p = selS;
      while (p > lb && !/\s/.test(_t.charAt(p - 1))) p--;
      if (p > s) s = p;
      for (let i = Math.max(selS, s); i < e; i++) {
        const ch = _t.charAt(i);
        if (ch === ")" || ch === "）") { e = i; break; }
      }
    }
    if (s < rvStart) s = rvStart;
    if (e < s) e = s;
    while (e > s && /\s/.test(_t.charAt(e - 1))) e--;   // 操作数尾随空白不参与替换
    // head：首个操作数（未越过运算符）→ 用归一化主句，保住 2026-09-22 空格归一；
    // 后续操作数 → 保留用户已输入的原始前段（含运算符与操作数），不动。
    const head = (s <= rvStart) ? headRaw : _t.slice(0, s);
    return { head: head, tail: _t.slice(e) };
  }

  function insert(c) {
    if (c.kind === 'operator' && /^(不?属于)（(单选|多选)）$/.test(c.label)) {
      const cw = currentWord();
      box.value = box.value.slice(0, cw.start) + c.label + ' ' + box.value.slice(cw.end);
      box.selectionStart = box.selectionEnd = cw.start + c.label.length + 1;
      close(); updatePreview(); box.focus(); refresh(); return;
    }
    // then/else 参数位 field 候选（2026-09-03）：替换整个参数值区间 [paramRange]，
    // 而非按空格分词替换当前词——对象路径内部可能含空格，整段替换最稳。
    // 2026-09-11：预定义方法实参位同样走区间替换（区间=占位符/片段，非整段参数）。
    if (paramRange && (mode === "then" || mode === "else" || mode === "predefine") &&
        c && !c.entry && c.kind !== "method") {
      const text = box.value;
      const s = Math.max(0, paramRange.start);
      const e = Math.min(text.length, paramRange.end);
      if (s <= e) {
        const newText = text.slice(0, s) + c.label + text.slice(e);
        box.value = newText;
        box.selectionStart = box.selectionEnd = s + c.label.length;
        paramRange = null;
        close();
        updatePreview();
        box.focus();
        return;
      }
    }
    // 入口候选（预定义 / 如果 / 那么 / 否则）：当前行填入引导词并切换编辑模式。
    // 回车选择与鼠标点击都走这里，即"选中入口候选才切换模式"。
    if (c.entry) {
      const text = box.value;
      const pos = box.selectionStart;
      const ls = text.lastIndexOf("\n", pos - 1) + 1;
      let le = text.indexOf("\n", pos);
      if (le === -1) le = text.length;
      // 2026-09-03：入口词选择后自动换行（入口词独立成行，正文从下一行写，
      // 预览/上传时由 foldEntryLines 折回逻辑行）。
      // 预定义同样换行：第一行留"预定义"，下一行引导"指定'别名'…"（= 预定义模式正文，
      // 第二行以"指定"开头，refreshPredefine 旧格式分支可直接匹配）。
      const entryNl = (c.word === "如果" || c.word === "那么" || c.word === "否则" || c.word === "预定义");
      const row = c.word + (entryNl ? "\n" : " ");
      let tail = text.slice(le);
      if (entryNl && tail[0] === "\n") tail = tail.slice(1);   // 避免与行尾原换行重复
      box.value = text.slice(0, ls) + row + tail;
      box.selectionStart = box.selectionEnd = ls + row.length;
      box.focus();
      enterMode(c.entry);
      return;
    }
    // 预定义连接候选（来自(in)）：追加到光标处，进入路径步骤
    if (c.kind === "pconn") {
      const selS = box.selectionStart, selE = box.selectionEnd;
      const before = box.value.slice(0, selS), after = box.value.slice(selE);
      const ins = "来自(in) ";
      box.value = before + ins + after;
      box.selectionStart = box.selectionEnd = before.length + ins.length;
      close();
      updatePreview();
      box.focus();
      return;
    }
    // 预定义入口子候选"指定"：补 指定''，光标进别名引号。
    // 两种形态都支持：
    //   A) 同行（行首"预定义"后）：预定义 指定''（旧格式）
    //   B) 入口词独立成行（上一行孤立"预定义"，本行为空）：直接补 指定''
    if (c.kind === "pdstart") {
      const selS = box.selectionStart, selE = box.selectionEnd;
      const _t = box.value;
      const _ls = _t.lastIndexOf("\n", selS - 1) + 1;
      let before = _t.slice(_ls, selS), after = _t.slice(selE);
      const m2 = before.match(/^(预定义)\s*$/);
      const _le0 = _t.indexOf("\n", selS);
      const _line0 = _t.slice(_ls, _le0 === -1 ? _t.length : _le0);
      let out;
      if (m2) {
        out = m2[1] + " " + "指定''";
      } else if (!before.trim() && !_line0.trim() &&
                 (prevLineText(_t, _ls).trim() === "预定义" || mode === "predefine")) {
        // 本行为空：上一行是孤立"预定义"，或光标位于预定义块内空行
        // （2026-09-23 回车换行续定义场景）→ 直接补 指定''
        out = "指定''";
      } else {
        close(); return;
      }
      box.value = _t.slice(0, _ls) + out + after;
      // 光标落在别名引号间（"指定"后第一个引号之后）
      const leadLen = m2 ? m2[1].length + 1 : 0;
      box.selectionStart = box.selectionEnd = _ls + leadLen + 3;
      close();
      updatePreview();
      box.focus();
      return;
    }
    // 预定义引导选择：为一个(列表) / 为(非列表)——替换当前行尾，进入对应路径步骤
    if (c.kind === "pklist" || c.kind === "pkfield") {
      const selS = box.selectionStart, selE = box.selectionEnd;
      const _t = box.value;
      const _ls = _t.lastIndexOf("\n", selS - 1) + 1;
      let before = _t.slice(_ls, selS), after = _t.slice(selE);
      const pm = before.match(/^(预定义\s*)/);
      const pref = pm ? pm[1] : "";
      if (pref) before = before.slice(pref.length);
      const ins = (c.kind === "pklist" ? "为一个 " : "为 ");
      const out = pref + before.replace(/\s*$/, "") + ins;
      box.value = _t.slice(0, _ls) + out + after;
      box.selectionStart = box.selectionEnd = _ls + out.length;
      close();
      updatePreview();
      box.focus();
      return;
    }
    // 非列表路径候选：指定'别名'为 + 'root'的路径（整段替换右值，2026-09-22）
    // 2026-09-29：本块预定义别名候选（pdalias 根及其 '别名'的子字段）同样
    // 走整段替换——pdRvalueSpan 已保证替换不跨括号/运算符，保住 `( ` 结构
    if (c.kind === "pfieldpath" || c.kind === "pdalias") {
      const sp = pdRvalueSpan(/^(指定'[^']*'为(?!一个)\s*)/);
      if (!sp) { close(); return; }
      box.value = sp.head + c.label + sp.tail;
      box.selectionStart = box.selectionEnd = sp.head.length + c.label.length;
      close();
      updatePreview();
      box.focus();
      return;
    }
    // then/else 动作方法候选：方法 label（参数化模板）替换当前行尾部输入词
    if (c.kind === "method") {
      // 2026-09-22: 预定义非列表右值位选方法 → 同样整段替换右值，保住"指定'别名'为"主句
      // （下面通用逻辑按"最后一个空白"切词，中文路径段之间常无空格 → m[1] 取空，
      // 整行主句会被方法模板吞掉）
      if (mode === "predefine" && !paramRange) {
        const sp = pdRvalueSpan(/^(指定'[^']*'为(?!一个)\s*)/);
        if (sp) {
          const ins = c.template || c.label;
          box.value = sp.head + ins + sp.tail;
          const ph0 = ins.search(/<[^>]*>/);
          if (ph0 >= 0) {
            const g0 = ins.slice(ph0).match(/^<[^>]*>/);
            box.selectionStart = sp.head.length + ph0;
            box.selectionEnd = sp.head.length + ph0 + g0[0].length;
          } else {
            box.selectionStart = box.selectionEnd = sp.head.length + ins.length;
          }
          close();
          updatePreview();
          box.focus();
          return;
        }
      }
      const selS = box.selectionStart, selE = box.selectionEnd;
      const text = box.value;
      const ls = text.lastIndexOf("\n", selS - 1) + 1;
      const before = text.slice(ls, selS), after = dropLeadToken(text.slice(selE));
      // 保留行首（含"那么/否则 "引导词），替换尾部当前输入词为方法模板：
      // 优先占位模板（expressionArray 渲染，参数位为 <语义>/<类型> 占位符），
      // 无模板（无 expressionArray 的方法）回退数字槽位 label。
      const m = before.match(/(.*?)(\S*)$/);
      let out = before;
      if (m) out = m[1] + (c.template || c.label);
      box.value = text.slice(0, ls) + out + after;
      // 光标选中第一个参数占位符（<...>），用户直接输入即替换
      const ph = out.search(/<[^>]*>/);
      if (ph >= 0) {
        const g = out.slice(ph).match(/^<[^>]*>/);
        box.selectionStart = ls + ph;
        box.selectionEnd = ls + ph + g[0].length;
      } else {
        box.selectionStart = box.selectionEnd = ls + out.length;
      }
      close();
      updatePreview();
      box.focus();
      return;
    }
    // 预定义列表路径候选：指定'别名'为一个X来自(in) + 'root'的路径
    // 整段替换"来自(in)"之后的路径（2026-09-22，同 pfieldpath：兼容路径内部自带空格
    // 与已成形路径，旧 [^\s]*$ 形态一旦含空格即失配 → 点候选无反应）
    if (c.kind === "ppath") {
      const sp = pdRvalueSpan(/^(指定'[^']*'为一个\s*\S*\s*来自\s*[（(]\s*in\s*[）)]\s*)/);
      if (sp) {
        box.value = sp.head + c.label + sp.tail;
        box.selectionStart = box.selectionEnd = sp.head.length + c.label.length;
        close();
        updatePreview();
        box.focus();
      }
      return;
    }
    // 预定义候选（元素类型）：按主句模板结构重构文本
    // （编辑辅助空格保留在编辑框中显示，仅生成规则内容时由 normalizePredefine 清理）
    if (c.kind === "ptype") {
      const selS = box.selectionStart, selE = box.selectionEnd;
      // 仅取当前行光标前文本匹配（多行编辑时前面可能有其他规则行）
      const _t = box.value;
      const _ls = _t.lastIndexOf("\n", selS - 1) + 1;
      let before = _t.slice(_ls, selS), after = dropLeadToken(_t.slice(selE));
      const pm = before.match(/^(预定义\s*)/);
      const pref = pm ? pm[1] : "";
      if (pref) before = before.slice(pref.length);
      // 指定'别名'为一个 + 类型名（保留"为一个"后的编辑辅助空格，替换已输入片段）
      const m = before.match(/^(指定'[^']*'为一个)(\s*)([^\s]*)$/);
      let r;
      if (m) r = m[1] + m[2] + c.label;
      if (r !== undefined) {
        box.value = _t.slice(0, _ls) + pref + r + after;
        box.selectionStart = box.selectionEnd = _ls + pref.length + r.length;
        close();
        updatePreview();
        box.focus();
      }
      return;
    }
    // 预定义"符合条件"子句候选：按当前行光标前文本尾部替换，逐段推进
    // pcond→补全"符合条件：当前这个 "；pcondtype→补"X的"；pcondfield→补字段名；
    // pcondop→补运算符+开引号（光标落在引号后输值）
    if (c.kind === "pcond" || c.kind === "pcondtype" ||
        c.kind === "pcondfield" || c.kind === "pcondop") {
      const selS = box.selectionStart, selE = box.selectionEnd;
      const _t = box.value;
      const _ls = _t.lastIndexOf("\n", selS - 1) + 1;
      let before = _t.slice(_ls, selS), after = dropLeadToken(_t.slice(selE));
      const pm = before.match(/^(预定义\s*)/);
      const pref = pm ? pm[1] : "";
      if (pref) before = before.slice(pref.length);
      let out, caretOff = 0;
      if (c.kind === "pcond") {
        // 兼容三种尾部：仅尾随空格 / 已手打"符…"前缀（含冒号）/ 误加分号";符…"
        // 一律清理并补全"符合条件：当前这个 "
        out = before.replace(/\s*(?:;?\s*符合?(?:条件)?)?[：:]?\s*$/, " 符合条件：当前这个 ");
      } else if (c.kind === "pcondtype") {
        out = before.replace(/(当前这个\s*)\S*\s*$/, "$1" + c.label + "的");
      } else if (c.kind === "pcondfield") {
        out = before.replace(/(当前这个\s*\S+\s*的)[^\s是属于；;]*$/, "$1" + c.label);
      } else {
        out = before.replace(/(\S+)\s*(?:是|不是|包含)?\s*$/, "$1" + c.label + "\"");
      }
      if (out !== undefined) {
        box.value = _t.slice(0, _ls) + pref + out + after;
        box.selectionStart = box.selectionEnd = _ls + pref.length + out.length;
        close();
        updatePreview();
        box.focus();
        // 字段选中后立即弹运算符候选（字段与运算符紧邻，无尾空格触发点）
        if (c.kind === "pcondfield") {
          const tm = out.match(/当前这个\s*(\S+)\s*的(\S+)$/);
          if (tm) {
            status.textContent = "预定义 · 条件运算符";
            fetchPredefine("cond_op", "", tm[1], tm[2]);
          }
        }
      }
      return;
    }
    const cw = currentWord();
    const text = box.value;
    const idx = c.label.indexOf("...");
    let insertText = c.label;
    let caretPos = cw.start + c.label.length;

    if (c.kind === "arith") {
      // 算术运算符（候选词形为中文 加/减/乘/除）：desc 解析端要求运算符两侧
      // 留空白，插入时补前导空格（紧贴字段时）+ 尾随空格、光标落在空格之后，
      // 随后 refresh 以空词+prev=运算符弹出下一级操作数候选。
      // （用户手输符号 + - * / 同样识别为 arith 词，上传时生成端归中文。）
      // 替换起点紧贴非空白字符时补前导空格（空词紧贴字段、或手输"+"被
      // 替换成"加"两种情形），保证中文运算符两侧留空白。
      let lead = "";
      if (cw.start > 0 && !/\s/.test(text[cw.start - 1])) {
        lead = " ";
      }
      insertText = lead + c.label + " ";
      caretPos = cw.start + insertText.length;
    }

    if (idx !== -1) {
      // 含 "..." 占位符的运算符（如"以...开头"、"属于...中的一个"、"在...和...之间"）：
      // 每个 "..." 都换成空格值位，光标落在第一个占位空格中供输入值
      const segs = c.label.split("...");
      insertText = segs.join("   ");              // 各段之间留三个空格（值位）
      caretPos = cw.start + segs[0].length + 1;   // 落在首个占位中间空格
    }

    // 替换当前词
    const newText = text.slice(0, cw.start) + insertText + text.slice(cw.end);
    box.value = newText;
    // 光标定位
    box.selectionStart = box.selectionEnd = caretPos;
    updatePreview();
    box.focus();
    if (idx !== -1) {
      // 占位运算符（含"..."）：让用户直接输入值，不弹候选
      close();
    } else {
      // 普通候选：填入后继续按当前词（刚填入内容）检索，弹出下一级候选
      refresh();
    }
  }

  // ---------- 预览（按词类型着色） ----------
  // 词类型 -> 颜色
  // 2026-09-29 对齐主系统（Element UI + 品牌蓝 #0079ff）语义色板：
  // 蓝=连接词 / 琥珀=运算符族 / 绿=对象字段 / 深墨蓝=布尔 / 灰=数字值 / 红=错误
  const TYPE_COLORS = {
    connector: "#409eff",   // 连接词：Element 主蓝
    operator: "#e6a23c",    // 运算符：warning 琥珀
    operator_tail: "#e6a23c", // 多词运算符尾部（属于...中的一个/以...开头/结尾）：同琥珀
    operator_mid: "#e6a23c",  // 区间句式中段连接词（在 起 和 止 之间 的"和"）：同琥珀
    len_func: "#e6a23c",      // 内容长度修饰符（.的内容长度）：同琥珀
    arith: "#e6a23c",         // 算术运算符（加/减/乘/除，含符号手输形）：同琥珀
    object: "#767602",      // 对象字段：深橄榄黄（2026-09-30 由 success 绿调整，护眼）
    field: "#767602",
    number: "#606266",      // 数字：与值同色（regular 灰）
    boolean: "#1e3a8a",     // 布尔值：深墨蓝（主系统统计卡色，与连接词浅蓝区分）
    value: "#606266",       // 值：灰
    unknown: "#909399",     // 未知：info 灰
    keyword: "#409eff",
    paren: "#e6a23c",       // 括号：并入运算符族琥珀（独立成词的括号）
    thenelse: "#606266",    // 那么/否则 动作行：灰（参数值段）
    methodtext: "#767602",  // then/else 方法模板固定文本（非参数位）：橄榄黄
    // 预定义行专用类型（annotate_predefine_line 返回）
    pd_keyword: "#409eff",  // 指定/为一个/来自(in)/符合条件：/当前这个：蓝
    pd_alias: "#767602",    // '别名'：橄榄黄
    pd_type: "#767602",     // 元素类型：橄榄黄
    pd_path: "#767602",     // 列表路径：橄榄黄
    pd_field: "#767602",    // 条件字段：橄榄黄
    pd_op: "#e6a23c",       // 条件运算符：琥珀
    pd_value: "#606266",    // 条件值：灰
    pd_draft: "#909399",    // 输入中未成形：info 灰
  };

  // 橄榄黄（图例“对象字段”色）类型集合：预览统一加粗
  const OLIVE_TYPES = new Set(["object", "field", "methodtext",
                               "pd_alias", "pd_type", "pd_path", "pd_field"]);

  // 最近一次标注结果（含 valid/error），供"复制规则内容"判断是否报错
  let lastWords = [];

  // 预览"上传用规范文本"缓存：与折叠后的逻辑行一一对应，每行内容与渲染行的
  // textContent 同口径（token 直接拼接 + connector/paren 两侧空格）。
  // 2026-09-09 起 preview 展示在连接词（并且/或者）前换行，preview 的物理
  // 行数与逻辑行不再一一对应，上传改取此缓存按逻辑行拆分（异常时回退旧行为）。
  let renderedPlainLines = [];
  let lineBuf = "";   // 当前行规范文本累加器（renderWords 内部维护）

  // 行内词渲染（不清空 preview，供单行/多行渲染复用）
  // 同时把该行的"上传用规范文本"累加进 lineBuf（与渲染行 textContent 完全
  // 一致的口径），供上传按逻辑行取文本。
  function renderWords(words) {
    (words || []).forEach(function (w) {
      // 语义单元之间不插入空格（删掉用户人为输入的分隔空格），
      // 对象字段路径内部的自带空格保留在 w.text 中。
      // 例外：连接词（并且/或者）、独立成词的括号（paren）与算术运算符（arith）
      // 两侧加空格隔离——desc 解析端 (_scan_value/_find_arith_op) 依赖空格
      // 界定逻辑运算符、括号块与算术运算符（加/减/乘/除 及符号手输形）。
      // 字段/方法名文本自带的括号（如 xxx()）在 w.text 内部，不做隔离。
      // 2026-09-09：展示层连接词前换行（每个并且/或者独立成行）；
      // 上传规范文本（lineBuf）仍记为空格，上传内容与旧行为完全一致。
      const spaced = w.type === "connector" || w.type === "paren" || w.type === "arith";
      if (spaced && preview.childNodes.length) {
        const last = preview.lastChild;
        if (!last || !/\s$/.test(last.textContent)) {
          if (w.type === "connector" && lineBuf.trim()) {
            preview.appendChild(document.createTextNode("\n"));
          } else {
            preview.appendChild(document.createTextNode(" "));
          }
          lineBuf += " ";
        } else if (w.type === "connector" && last.nodeType === 3 &&
                   last.nodeValue === " " && lineBuf.trim()) {
          // 前一节点已是空格（典型：括号") "渲染后补的尾空格）：
          // 连接词仍需换行展示——把该空格节点替换为换行。
          // lineBuf 已记过这个空格，上传规范文本不受影响。
          last.nodeValue = "\n";
        }
      }
      const span = document.createElement("span");
      span.textContent = w.text;
      // 非法词标红 + 悬停提示错误
      if (w.valid === false) {
        span.style.color = "#f56c6c";          // 标红（Element danger）
        span.style.textDecoration = "underline wavy #f56c6c";
        span.title = w.error || "类型不匹配";
        span.className = "invalid";
      } else {
        span.style.color = TYPE_COLORS[w.type] || "#303133";
        // 2026-09-30：对象字段（橄榄黄色族）统一加粗，预览里更突出——
        // 覆盖 如果行 object/field、预定义行 pd_*、那么行 methodtext 各形态
        if (OLIVE_TYPES.has(w.type)) span.style.fontWeight = "600";
      }
      preview.appendChild(span);
      lineBuf += w.text;
      if (spaced) {
        preview.appendChild(document.createTextNode(" "));
        lineBuf += " ";
      }
    });
  }

  function renderPreview(words) {
    preview.classList.remove("empty");
    preview.innerHTML = "";
    if (!words || !words.length) {
      preview.textContent = "（尚未输入）";
      preview.classList.add("empty");
      renderedPlainLines = [];
      return;
    }
    lineBuf = "";
    renderWords(words);
    renderedPlainLines = [lineBuf];
  }

  // 引导词段统一视觉样式（2026-09-09）：黑色、加粗、放大——覆盖
  // keyword/value/pd_draft/thenelse 各 token 类型原本的着色，
  // startIdx 起为本次渲染新产出的节点（正文段不受影响保持原色）
  function styleLeadNodes(startIdx) {
    const kids = preview.childNodes;
    for (let i = startIdx; i < kids.length; i++) {
      const el = kids[i];
      if (el.nodeType !== 1) continue;   // 只处理元素节点，跳过空格/换行文本
      el.style.color = "#303133";
      el.style.fontWeight = "700";
      el.style.fontSize = "1.2em";
    }
  }

  // 2026-09-09：预览中引导词（预定义/如果/那么/否则）独占一行展示。
  // 折叠后的逻辑行仍是"引导词+正文"，lineBuf 口径不变（上传文本不受影响）：
  // 展示层把引导词段渲染后插入换行，正文段另起一行继续渲染。
  // annotate 对引导词的 token 形态不定（keyword 独立 token / 粘连进首个
  // value / pd_draft·thenelse 整行单 token），故按字符拆点把 words 拆为
  // 两段，拆点落在 token 内部时拆成两个同类型 token（着色一致）。
  function renderEntryLine(ln, words, hideLead) {
    const m = ln.match(/^(预定义|如果|那么|否则)(?=\s|$)/);
    if (!m) { renderWords(words); return; }
    const n = m[1].length;
    const lead = [], rest = [];
    let acc = 0, done = false;
    (words || []).forEach(function (w) {
      if (done) { rest.push(w); return; }
      if (acc + w.text.length <= n) {
        lead.push(w);
        acc += w.text.length;
        if (acc === n) done = true;
        return;
      }
      // 拆点落在 token 内部：拆成两个同类型 token
      lead.push({ type: w.type, text: w.text.slice(0, n - acc),
                  valid: w.valid, error: w.error });
      rest.push({ type: w.type, text: w.text.slice(n - acc),
                  valid: w.valid, error: w.error });
      done = true;
    });
    // 同块续行（那么/否则 多条动作）：不重复渲染引导词，仅渲染正文；
    // lineBuf（上传规范文本）同样不含引导词——上传归属由 editLines 原文
    // （仍带引导词）判断，rl.replace(/^那么\s*/,'') 对无引导词的 rl 无副作用。
    if (hideLead && lead.length && rest.length) {
      const f = rest[0];
      const sp = f.text.match(/^\s+/);
      if (sp) rest[0] = { type: f.type, text: f.text.slice(sp[0].length),
                          valid: f.valid, error: f.error };
      renderWords(rest);
      return;
    }
    if (!rest.length || !lead.length) {
      // 纯引导词行（如末尾孤立的"如果"）：整行都按引导词样式
      const startIdx = preview.childNodes.length;
      renderWords(words);
      styleLeadNodes(startIdx);
      return;
    }
    const leadStart = preview.childNodes.length;
    renderWords(lead);
    styleLeadNodes(leadStart);
    // token 内拆段产生的前导空格（如 pd_draft "预定义 指定…"）挪到引导词
    // 行尾展示，lineBuf 仍保留该空格（与旧版规范文本逐字符一致）
    const first = rest[0];
    const spm = first.text.match(/^\s+/);
    if (spm) {
      preview.appendChild(document.createTextNode(spm[0]));
      lineBuf += spm[0];
      rest[0] = { type: first.type, text: first.text.slice(spm[0].length),
                  valid: first.valid, error: first.error };
    }
    preview.appendChild(document.createTextNode("\n"));
    // 正文以括号/连接词开头时，换行后仍需补前导空格（desc 解析端依赖空格
    // 界定括号块与逻辑运算符；旧版该空格来自引导词 token 后的 spaced 前补分支）
    const head = rest[0];
    if (head && (head.type === "connector" || head.type === "paren")) {
      preview.appendChild(document.createTextNode(" "));
      lineBuf += " ";
    }
    renderWords(rest);
  }

  // 多行渲染：每行按标注词着色（预定义行 pd_* 类型含"符合条件"校验，
  // 规则行规则类型；非法词标红）。无标注结果的行原样显示。
  // 每行渲染前重置 lineBuf，渲染后把行规范文本存入 renderedPlainLines
  // （与折叠逻辑行一一对应，供上传拆分取用）。
  function renderPreviewLines(lines, results) {
    preview.classList.remove("empty");
    preview.innerHTML = "";
    renderedPlainLines = [];
    // 同一 那么/否则 块的第 2/3… 条动作（折行后各成一行 "那么 X"）：
    // 与上一行引导词相同 → 视为续行，预览隐藏重复引导词，仅首行显示一次。
    const isCont = function (i) {
      if (i <= 0) return false;
      const cur = (lines[i] || "").trim().match(/^(那么|否则)(?=\s|$)/);
      if (!cur) return false;
      const prev = (lines[i - 1] || "").trim().match(/^(那么|否则)(?=\s|$)/);
      return !!(prev && prev[1] === cur[1]);
    };
    (lines || []).forEach(function (ln, i) {
      if (i) preview.appendChild(document.createTextNode("\n"));
      const words = (results || [])[i];
      lineBuf = "";
      const hideLead = isCont(i);
      if (!words || !words.length) {
        // 标注失败的行退化为编辑器原文（规范文本同原文）；
        // 纯引导词行保持引导词统一视觉（黑/粗/放大）；续行同样剥引导词。
        const span = document.createElement("span");
        span.textContent = hideLead ? ln.replace(/^(那么|否则)\s*/, "") : ln;
        preview.appendChild(span);
        if (!hideLead && /^(预定义|如果|那么|否则)\s*$/.test(ln)) styleLeadNodes(preview.childNodes.length - 1);
        lineBuf = span.textContent;
      } else {
        renderEntryLine(ln, words, hideLead);
      }
      renderedPlainLines.push(lineBuf);
    });
  }

  // ---------- 编辑框错误标红垫层（2026-09-30） ----------
  // 唯一职责：右侧预览里标红的非法词，在编辑框对应行的相同文字下面垫
  // 红色波浪线 + 浅红底（textarea 文字盖在垫层之上，字色本身不变、
  // 不干预输入/选区；上传链路零改动）。
  // 定位口径（2026-09-30 二修）：非法词带 _line（折叠后行号）→ 在该折叠行
  // 文本内按后端词序逐词 indexOf 游标定位出词偏移 → 再经 foldMap 把折叠行
  // 偏移映射回各原始行区间垫色。只标该词自身出现处，不再按文本全行搜索
  // （旧口径"大于"会误伤"大于等于"里的子串）。词定位不到则跳过（行已改写）。
  let lastFoldMap = [];
  let lastFoldLines = [];
  let lastInvalidFolds = [];

  function escHtml(s) {
    return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ranges: [{start,end,error}] 原始行内偏移区间（非词文本；词文本匹配
  // 会误伤同子串，如"大于"命中"大于等于"内部）
  function markLineHtml(line, ranges) {
    const segs = [];
    (ranges || []).forEach(function (r) {
      const a = Math.max(0, Math.min(line.length, r.start | 0));
      const b = Math.max(a, Math.min(line.length, r.end | 0));
      if (b > a) segs.push([a, b, r.error || ""]);
    });
    if (!segs.length) return escHtml(line);
    segs.sort(function (x, y) { return x[0] - y[0]; });
    const merged = [];
    segs.forEach(function (s) {
      const m = merged[merged.length - 1];
      if (m && s[0] <= m[1]) {
        if (s[1] > m[1]) m[1] = s[1];
        if (s[2] && !m[2]) m[2] = s[2];
      } else merged.push(s.slice());
    });
    let html = "", pos = 0;
    merged.forEach(function (s) {
      html += escHtml(line.slice(pos, s[0])) +
              '<span class="mark-err" title="' +
              escHtml(s[2]).replace(/"/g, "&quot;") + '">' +
              escHtml(line.slice(s[0], s[1])) + "</span>";
      pos = s[1];
    });
    return html + escHtml(line.slice(pos));
  }

  function renderEditorMarks() {
    const clip = document.getElementById("editorMarkClip");
    const inner = document.getElementById("editorMarks");
    if (!clip || !inner) return;
    if (!lastInvalidFolds.length || mode === "entry" || !box.value) {
      clip.style.display = "none";
      inner.innerHTML = "";
      return;
    }
    // 逐像素对齐 textarea 文本区（与 caretOffset 镜像同一口径：
    // 左上角跳过 border，宽度取 clientWidth 以扣除滚动条）
    const cs = window.getComputedStyle(box);
    clip.style.left = (parseFloat(cs.borderLeftWidth) || 0) + "px";
    clip.style.top = (parseFloat(cs.borderTopWidth) || 0) + "px";
    clip.style.width = box.clientWidth + "px";
    clip.style.height = box.clientHeight + "px";
    ["fontFamily", "fontSize", "fontWeight", "fontStyle", "letterSpacing",
     "lineHeight", "wordSpacing", "textIndent"].forEach(function (p) {
      inner.style[p] = cs[p];
    });
    inner.style.padding = cs.paddingTop + " " + cs.paddingRight + " " +
                          cs.paddingBottom + " " + cs.paddingLeft;
    const rawLines = box.value.split("\n");
    const perLine = {};
    // 折叠行文本 F 由各原始行（stripLeadColon+trim 后）按 foldMap 顺序以空格
    // 拼接而成（引导词行拼"如果"等）。逐段在 F 中顺序定位，得每段在 F 中的
    // 区间 [f0,f1) 及其在原始行内的起始偏移 off，供词偏移反算回原始行。
    const boundsCache = {};
    function foldBounds(li) {
      if (boundsCache[li]) return boundsCache[li];
      const F = lastFoldLines[li] || "";
      const srcs = lastFoldMap[li] || [li];
      const bs = [];
      let c = 0;
      srcs.forEach(function (ri) {
        if (ri < 0 || ri >= rawLines.length) return;
        const stripped = stripLeadColon(rawLines[ri]);
        const trimmed = stripped.trim();
        if (!trimmed) return;
        const p = F.indexOf(trimmed, c);
        if (p < 0) return;
        bs.push({ ri: ri, f0: p, f1: p + trimmed.length,
                  off: stripped.indexOf(trimmed) });
        c = p + trimmed.length;
      });
      return (boundsCache[li] = bs);
    }
    lastInvalidFolds.forEach(function (e) {
      const bs = foldBounds(e.fold);
      let pos = e.start;
      while (pos < e.end) {
        let hit = null, next = null;
        for (let i = 0; i < bs.length; i++) {
          if (pos >= bs[i].f0 && pos < bs[i].f1) { hit = bs[i]; break; }
          if (bs[i].f0 > pos && (!next || bs[i].f0 < next.f0)) next = bs[i];
        }
        if (!hit) {
          if (!next) break;
          pos = next.f0;   // 区间落在拼接空格/引导词段：跳到下一段继续标
          continue;
        }
        const segEnd = Math.min(e.end, hit.f1);
        (perLine[hit.ri] || (perLine[hit.ri] = [])).push({
          start: pos - hit.f0 + hit.off,
          end: segEnd - hit.f0 + hit.off,
          error: e.error });
        pos = segEnd;
      }
    });
    const parts = rawLines.map(function (ln, ri) {
      return markLineHtml(ln, perLine[ri]);
    });
    clip.style.display = "block";   // 必须显式覆盖类上的 display:none
    inner.innerHTML = parts.join("\n");
    syncMarkScroll();
  }

  function syncMarkScroll() {
    const inner = document.getElementById("editorMarks");
    if (inner) inner.style.transform = "translateY(" + (-box.scrollTop) + "px)";
  }
  // 垫层跟随编辑框滚动/尺寸变化（textarea 手动拉高后宽度布局需重算）
  box.addEventListener("scroll", syncMarkScroll);
  window.addEventListener("resize", renderEditorMarks);

  // 标注请求序号（防竞态：快速输入时旧请求回调丢弃）
  var annoSeq = 0;

  function updatePreview() {
    const text = box.value.trim();
    if (!text) {
      preview.textContent = "（尚未输入）";
      preview.classList.add("empty");
      renderedPlainLines = [];
      lastInvalidFolds = []; renderEditorMarks();   // 清空编辑框标红垫层
      setMode(mode);   // 恢复状态栏模式提示
      return;
    }
    // 入口态：只回显（引导词"指定/如果"不在规则词典中，标注会误报）
    if (mode === "entry") {
      lastWords = [];
      preview.classList.remove("empty");
      // 预览显示清理编辑辅助空格后的规则内容
      const plain = normalizeMixed(text);
      preview.textContent = plain;
      renderedPlainLines = [plain];
      lastInvalidFolds = []; renderEditorMarks();   // 入口态无标注口径，清垫层
      return;
    }
    status.textContent = "已输入 " + text.length + " 字";
    // 逐行标注（后端按行分流：'指定'行走预定义专用标注+"符合条件"校验，
    // 其余行走规则标注）——预定义模式与规则模式统一走此流程
    // 2026-09-03：入口词（如果/那么/否则）可选择后自动换行独立成行，正文在下一行；
    // 预览前先用 foldEntryLines 把"入口词独占行+正文行"折叠回逻辑行，
    // 再逐行标注/渲染，保证旧式单行写法与新式两行写法结果一致。
    // 2026-09-30：折叠源改用 box.value 原文（空行折叠前后不改变产出行数），
    // 使 foldMap 的"原始行号"直接对应编辑框行号，供错误标红垫层定位。
    const foldMap = [];
    const lines = foldEntryLines(box.value, foldMap).split("\n");
    const seq = ++annoSeq;
    // 2026-09-28：整段一次请求（替代逐行 N 个并发请求）——后端 _annotate_text
    // 本就按行分流，且新增跨行连接词校验需要完整上下文；返回词带 _line
    // （0-based 行号，与折叠后 lines 对齐），据此分组喂给 renderPreviewLines。
    fetch("/api/annotate?text=" + encodeURIComponent(lines.join("\n")) +
          "&predefines=" + encodeURIComponent(predefineLines()))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (seq !== annoSeq) return;
        const results = lines.map(function () { return []; });
        ((data && data.words) || []).forEach(function (w) {
          const i = typeof w._line === "number" ? w._line : -1;
          if (i >= 0 && i < results.length) results[i].push(w);
        });
        lastWords = [];
        results.forEach(function (ws) { lastWords = lastWords.concat(ws); });
        renderPreviewLines(lines, results);
        // 同步编辑框标红：按后端词序在折叠行文本内游标定位词偏移，
        // 非法词带（折叠行号+行内区间）→ renderEditorMarks 映射回原始行
        lastFoldMap = foldMap;
        lastFoldLines = lines;
        lastInvalidFolds = [];
        results.forEach(function (ws, li) {
          const F = lines[li] || "";
          let cur = 0;
          ws.forEach(function (w) {
            const t = w.text || "";
            if (!t.trim()) return;
            const p = F.indexOf(t, cur);
            if (p < 0) return;   // 词文本与折叠行对不上：跳过不标
            cur = p + t.length;
            if (w.valid === false)
              lastInvalidFolds.push({ fold: li, start: p, end: cur,
                                      error: w.error || "" });
          });
        });
        renderEditorMarks();
      })
      .catch(function () {
        if (seq !== annoSeq) return;
        renderPreviewLines(lines, lines.map(function () { return []; }));
        lastInvalidFolds = []; renderEditorMarks();   // 标注失败退回无标红
      });
  }

  // ---------- 事件 --------
  box.addEventListener("input", function () {
    // 手动删空文本：回到入口态（候选等点击/回车再弹）
    if (!box.value.trim()) {
      close();
      setMode("entry");
      updatePreview();
      scheduleDraft();
      return;
    }
    updatePreview();
    if (pendingNewline) { scheduleDraft(); return; }  // 回车换行：候选由 showAfterNewline 统一分发
    refresh();
    scheduleDraft();
  });

  box.addEventListener("keydown", function (ev) {
    // 中文输入法合成期间（上屏确认/候选翻页）不处理编辑器自定义按键，
    // 防止 IME 上屏用的 Enter/空格被误当候选确认或模式切换
    if (ev.isComposing || ev.keyCode === 229) return;
    // 入口态：输入"指定"/"如果"（带不带引号均可）后按空格 → 进入对应编辑模式
    // （两种都拦截空格：enterMode 内部统一补空格，避免双空格；
    //   实际上输入完引导词时 showEntry 已自动切模式，此分支是兜底）
    if (ev.key === " " && mode === "entry") {
      const w = normEntryWord(currentWord().word);
      const m = WORD_MODE[w];
      if (m) { ev.preventDefault(); enterMode(m); return; }
    }
    // 编辑模式（预定义/规则/动作）按回车：换行后候选与上方编辑内容解耦——
    // 统一弹 4 个固定入口词（同步渲染、零检索、无竞态，见 showAfterNewline）
    // 回车两种意图按光标前文本状态分流（化解与"回车=插入候选"的互相劫持）：
    // ① 换行意图：光标前本行已有内容 且 当前词为空（整行/整段输入告一段落，
    //    如 "指定'x'为 …为(123)↵"）且未用方向键真选过候选；
    //    或列表未开/无高亮（Esc 后、迟到响应复活列表）；
    // ② 插入意图（走后面分支）：当前词非空（正敲过滤词）、本行为空
    //    （上一行"预定义"↵ 后空行弹的"指定"联想）、或方向键导航真选中。
    {
      const _pos = box.selectionStart;
      const _lstart = box.value.lastIndexOf("\n", Math.max(0, _pos - 1)) + 1;
      let _le = box.value.indexOf("\n", _pos);
      if (_le === -1) _le = box.value.length;
      const _beforeOnLine = box.value.slice(_lstart, _pos);
      const _w = currentWord().word;
      // 回车"不插入"仅两种情形（光标须在行尾）：
      // ① 纯引导词行（本行只有 预定义/如果/那么/否则/指定，含尾冒号空格）：
      //    回车 = 消费引导词 → 换行弹固定入口词候选；下一空行回车
      //    才是插入高亮候选（选"预定义"即进入 指定 定义流程）；
      // ② 行尾词以右括号收尾——如 "…为(123)↵"，括号闭合说明实参槽已填完，
      //    此刻开着的列表多半是防抖未刷新的陈旧联想，回车按换行意图。
      // 其余"内容行打完空格弹出续行联想"（如 "…保单的机构 ↵"）回车一律
      // 真插入高亮候选——引导词行之外不允许出现"高亮却不插入"的伪选中
      const _bareEntry = /^(预定义|如果|那么|否则|指定)\s*[：:]?\s*$/.test(_beforeOnLine.trim());
      const _parenDone = _beforeOnLine.trim() && _pos === _le &&
        (_bareEntry || /[)）]$/.test(_w));
      var _enterIsNewline = _parenDone && !hlManual;
    }
    if (ev.key === "Enter" && box.value.trim() && (_enterIsNewline || !open || hl < 0) &&
        (mode !== "entry" || !WORD_MODE[normEntryWord(currentWord().word)])) {
      if (_enterIsNewline && open) close();  // 收掉陈旧列表，杜绝"高亮却不插入"观感
      pendingNewline = true;
      setTimeout(showAfterNewline, 0);
      return;
    }
    // then/else 模式：Tab 跳转到下一个参数占位符（<语义>/<类型>）并选中，
    // 用户直接输入即替换占位符，逐个填写参数
    if (ev.key === "Tab" && (mode === "then" || mode === "else")) {
      ev.preventDefault();
      const tm = box.value.slice(box.selectionEnd).match(/<[^>]*>/);
      if (tm) {
        const at = box.selectionEnd + tm.index;
        box.selectionStart = at;
        box.selectionEnd = at + tm[0].length;
      }
      box.focus();
      return;
    }
    // 预定义模式：仅别名输入中（光标在引号内）吞掉空格防止破坏模板；
    // 其余情况空格正常显示在编辑框中（与"如果"模式一致），
    // 由 input/keyup → refreshPredefine 按当前步骤弹出候选
    if (ev.key === " " && mode === "predefine" &&
        /^(预定义\s*)?指定'[^']*$/.test(box.value.slice(0, box.selectionStart))) {
      ev.preventDefault();
      return;
    }
    // 编辑框为空时按回车：弹出入口候选（"初次点击"之外的另一触发时机）
    if (ev.key === "Enter" && !open && !box.value.trim()) {
      ev.preventDefault();
      setMode("entry");
      showEntry();
      return;
    }
    if (!open) return;
    if (ev.key === "ArrowDown") { ev.preventDefault(); highlight((hl < 0 ? 0 : hl) + 1); hlManual = true; }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); highlight(hl < 0 ? cands.length - 1 : hl - 1); hlManual = true; }
    else if (ev.key === "Enter") {
      if (hl >= 0 && cands[hl]) { ev.preventDefault(); insert(cands[hl]); return; }
      // 未选中候选：入口态下当前词已是完整引导词（如手敲"如果"）→ 按回车
      // 确认进入对应模式，而非默认换行（换行后的模式进入由 showEntry 兜底）
      if (mode === "entry") {
        const m = WORD_MODE[normEntryWord(currentWord().word)];
        if (m) { ev.preventDefault(); enterMode(m); }
      }
    }
    else if (ev.key === "Escape") { dismissSuggest(); }
  });

  box.addEventListener("click", function () {
    // 初次点击编辑框（空文本）：弹出 '指定' / 如果 入口候选
    if (!box.value.trim()) {
      setMode("entry");
      showEntry();
      return;
    }
    refresh();
  });
  box.addEventListener("keyup", function (ev) {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp" || ev.key === "Enter") return;
    if (ev.key === "Escape") { dismissSuggest(); return; }
    // 空格后或移动光标：重新刷新
    refresh();
  });

  // ---------- 粘贴整理（2026-09-29） ----------
  // 从主系统规则库复制的文案："如果"行语义单元粘连、"那么"行带 1. 2. 序号，
  // 编辑框直接解析不了/不合规。粘贴时送后端 /api/normalize_rule 整理
  // （运算符锚点补空格、删序号，预定义行与方法实参内部排版原样保留）后再插入。
  box.addEventListener("paste", function (ev) {
    const cd = ev.clipboardData || window.clipboardData;
    const t = cd && cd.getData("text/plain");
    if (!t || !/\S/.test(t)) return;   // 空白内容走原生粘贴
    // 单行且不含规则特征词（填值粘贴等）不打扰：不发请求直接原生插入
    if (!/\n/.test(t) && !/(预定义|如果|那么|否则|指定|并且|或者|属于|等于|包含)/.test(t)) return;
    ev.preventDefault();
    fetch("/api/normalize_rule?text=" + encodeURIComponent(t))
      .then(function (r) { return r.json(); })
      .then(function (d) { insertAtCaret((d && d.text) || t); })
      .catch(function () { insertAtCaret(t); });   // 接口异常回退原文粘贴
  });

  function insertAtCaret(str) {
    box.focus();
    let ok = false;
    try { ok = document.execCommand("insertText", false, str); } catch (e) { ok = false; }
    if (!ok) {
      // 不支持 textarea execCommand 的浏览器：手动拼接插入（丢撤销栈，不丢内容）
      const s = box.selectionStart, e = box.selectionEnd;
      box.value = box.value.slice(0, s) + str + box.value.slice(e);
      box.selectionStart = box.selectionEnd = s + str.length;
      box.dispatchEvent(new Event("input", { bubbles: true }));
    }
    // execCommand 成功会原生触发 input → updatePreview/scheduleDraft 照常走
  }

  document.addEventListener("click", function (ev) {
    if (!list.contains(ev.target) && ev.target !== box) dismissSuggest();
  });

  // 编辑框内容滚动 / 页面及容器滚动 / 窗口尺寸变化时，保持候选跟随输入光标
  let posTimer = null;
  function rePosition() {
    if (!open) return;
    clearTimeout(posTimer);
    posTimer = setTimeout(positionList, 30);
  }
  box.addEventListener("scroll", rePosition);
  window.addEventListener("resize", rePosition);
  // scroll 不冒泡：捕获阶段监听以感知 .left 等滚动容器位移
  document.addEventListener("scroll", rePosition, true);

  // ---------- 清空 ----------
  document.getElementById("btnClear").addEventListener("click", function () {
    clearDraft();
    box.value = "";
    close();
    setMode("entry");
    updatePreview();
    box.focus();
    showEntry();   // 清空后回到入口态，直接弹出 '指定' / 如果
  });

  // ---------- 复制规则内容（纯文本，即最终结果）----------
  // 复制文本（优先 Clipboard API，失败回退 execCommand）
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(ta);
        ok ? resolve() : reject(new Error("copy failed"));
      } catch (e) { reject(e); }
    });
  }

  // 2026-09-30：「复制规则内容」按钮已从前端移除，btnCopy/copyHint 绑定一并删除
  // （copyText 保留：其他复制场景仍复用）。

  // ---------- 生成并上传到 ADP 决策库 ----------
  const uploadResult = document.getElementById("uploadResult");
  const uploadHint = document.getElementById("uploadHint");
  // 右侧"测试报文"：上传成功后由后端用生成的 ruleContent 生成 input 骨架并随响应返回
  const testMsgEl = document.getElementById("testMsg");
  const testMsgHint = document.getElementById("testMsgHint");
  // 2026-09-04：可编辑测试报文 —— null 叶子渲染成输入框，填完直接做命中测试
  const testEditEl = document.getElementById("testEdit");
  const hitResultEl = document.getElementById("hitResult");
  const testHitHint = document.getElementById("testHitHint");
  let testInputSkeleton = undefined;   // 后端返回的 input 骨架（结构基准）
  let lastRuleContent = undefined;     // 上传响应带回的 ruleContent（本地求值对照用）
  let lastThenElseRefs = [];           // 那么/否则文本引用的 BOM 参数（填空模式展示用）
  let lastPredefineRefs = [];          // 预定义'指定…'行出现的 BOM 对象（填空模式展示用）
  let showRawJson = false;             // 树视图 ↔ 原始 JSON 文本
  const inputMap = new Map();          // JSON 路径 → 输入框元素
  const filledValues = new Map();      // JSON 路径 → 用户已填文本（树/JSON 视图切换不丢值）

  // 输入框文本 → JSON 值：
  //   空 → null；"x" / [..] / {..} → 按 JSON 解析（带引号自动剥壳，残缺 JSON 报错）；
  //   true/false/null 原样；纯数字 → 数值（前导 0 编号如 0265010000000 保持字符串）；
  //   其余 → 字符串
  function parseInputVal(s) {
    const t = (s == null ? "" : String(s)).trim();
    if (!t) return { v: null };
    const c = t.charAt(0);
    if (c === "[" || c === "{" || c === '"') {
      try { return { v: JSON.parse(t) }; }
      catch (e) {
        return { err: "JSON 解析失败：" + e.message +
                 (c === '"' ? "" : "（列表请带 [ { … } ] 外壳）") };
      }
    }
    if (t === "true") return { v: true };
    if (t === "false") return { v: false };
    if (t === "null") return { v: null };
    if (/^-?\d+(\.\d+)?$/.test(t)) {
      if (/^-?0\d/.test(t)) return { v: t };   // 前导 0 的编号/机构号不转数值
      return { v: Number(t) };
    }
    return { v: t };
  }

  function pathJoin(p, k) { return p ? p + "." + String(k) : String(k); }

  // 骨架 → 可编辑树（dict/array 展开为缩进结构，null/标量叶子为输入框）
  function isContainer(v) { return v !== null && v !== undefined && typeof v === "object"; }

  function renderTestEdit() {
    // 2026-09-30：「测试报文」卡片已从前端移除，元素缺失时渲染降级为 no-op
    if (!testEditEl) return;
    inputMap.clear();
    testEditEl.innerHTML = "";
    hitResultEl.className = "hit-result";
    testHitHint.textContent = "";
    if (showRawJson) {
      let cur;
      try { cur = collectTestInput(); } catch (e) { cur = testInputSkeleton; }
      testEditEl.textContent = cur === undefined
        ? "（暂无骨架）" : JSON.stringify(cur, null, 2);
      testEditEl.className = "test-edit show";
      return;
    }
    if (!isContainer(testInputSkeleton)) {
      testEditEl.className = "test-edit";
      return;
    }
    const emptyKeys = function (v) {
      return Array.isArray(v) ? v.length === 0 : Object.keys(v).length === 0;
    };
    (function walk(v, path, indent, isLast, out) {
      const pad = "  ".repeat(indent);
      const open = Array.isArray(v) ? "[" : "{";
      const close = Array.isArray(v) ? "]" : "}";
      const keys = Array.isArray(v) ? v.map(function (_, i) { return i; })
                                    : Object.keys(v);
      out.appendChild(jline(pad + open));
      keys.forEach(function (k, i) {
        const child = v[k];
        const last = i === keys.length - 1;
        const keyTxt = Array.isArray(v) ? pad + "  " : pad + '  "' + k + '": ';
        if (isContainer(child) && !emptyKeys(child)) {
          out.appendChild(jline(keyTxt));
          walk(child, pathJoin(path, k), indent + 1, last, out);
        } else {
          out.appendChild(makeLeafLine(keyTxt, child, pathJoin(path, k), last));
        }
      });
      out.appendChild(jline(pad + close + (isLast ? "" : ",")));
    })(testInputSkeleton, "", 0, true, testEditEl);
    testEditEl.className = "test-edit show";
  }

  function jline(text) {
    const d = document.createElement("div");
    d.className = "jline";
    d.textContent = text;
    return d;
  }

  function makeLeafLine(keyTxt, val, path, isLast) {
    const d = document.createElement("div");
    d.className = "jline";
    const sp = document.createElement("span");
    sp.className = "jkey";
    sp.textContent = keyTxt;
    d.appendChild(sp);
    // 空容器（[] / {}）只展示不可填；null/标量 → 输入框
    const isEmptyArr = Array.isArray(val) && val.length === 0;
    const isEmptyObj = isContainer(val) && !Array.isArray(val)
      && Object.keys(val).length === 0;
    if (isEmptyArr || isEmptyObj) {
      d.appendChild(Object.assign(document.createElement("span"),
        { className: "jpunc", textContent: (isEmptyArr ? "[]" : "{}") + (isLast ? "" : ",") }));
      return d;
    }
    const inp = document.createElement("input");
    inp.className = "jval";
    inp.type = "text";
    inp.spellcheck = false;
    inp.dataset.path = path;
    const isNull = val === null || val === undefined;
    inp.placeholder = isNull ? "null（留空）" : String(val);
    const saved = filledValues.has(path) ? filledValues.get(path)
      : (isNull ? "" : String(val));
    if (saved) { inp.value = saved; inp.classList.add("filled"); }
    inputMap.set(path, inp);
    inp.addEventListener("input", function () {
      filledValues.set(path, inp.value);
      schedulePage();
      const t = inp.value.trim();
      inp.classList.toggle("filled", !!t);
      inp.classList.remove("badjson");
      const c0 = t.charAt(0);
      if (c0 === "[" || c0 === "{" || c0 === '"') {
        try { JSON.parse(t); } catch (e) { inp.classList.add("badjson"); }
      }
    });
    d.appendChild(inp);
    d.appendChild(Object.assign(document.createElement("span"),
      { className: "jnull", textContent: (isNull ? " ← 填值" : "") + (isLast ? "" : ",") }));
    return d;
  }

  // 从可编辑树收集 → 报文对象；有 JSON 解析错误时抛 Error
  function collectTestInput() {
    if (testInputSkeleton === undefined || testInputSkeleton === null)
      return undefined;
    return (function fill(v, path) {
      if (v !== null && typeof v === "object") {
        if (Array.isArray(v)) return v.map(function (_, i) { return fill(v[i], pathJoin(path, i)); });
        const o = {};
        Object.keys(v).forEach(function (k) { o[k] = fill(v[k], pathJoin(path, k)); });
        return o;
      }
      const el = inputMap.get(path);
      if (!el) {
        // 原始 JSON 视图（无输入框）：回退到已填文本
        if (filledValues.has(path)) {
          const r0 = parseInputVal(filledValues.get(path));
          if (r0.err) throw new Error(r0.err);
          return r0.v;
        }
        return v;
      }
      const r = parseInputVal(el.value);
      if (r.err) { el.classList.add("badjson"); throw new Error(r.err); }
      return r.v;
    })(testInputSkeleton, "");
  }

  function setTestMsg(data) {
    // data：上传响应。优先渲染可编辑树；生成失败回退只读提示。
    // 2026-09-30：卡片已移除（testMsgEl 为空）→ 整体跳过，骨架状态也不再维护
    if (!testMsgEl) return;
    if (data.testInputError) {
      testInputSkeleton = undefined;
      filledValues.clear();
      testEditEl.className = "test-edit";
      testMsgEl.style.display = "";
      testMsgEl.textContent = data.testInputError;
      testMsgEl.className = "test-msg empty";
      return;
    }
    if (data.testInput !== undefined) {
      testInputSkeleton = data.testInput;
      filledValues.clear();   // 新骨架：清空旧填值
      showRawJson = false;
      testMsgEl.className = "test-msg";
      testMsgEl.style.display = "none";
      renderTestEdit();
      // 骨架来源：优先 ADP /test/getInputByRuleId 接口，接口未取到时退化本地生成
      testMsgHint.textContent = data.testInputSource === "local"
        ? "本地生成 ✓（接口未获取到）请在输入框填值"
        : "接口获取 ✓ 请在输入框填值";
      setTimeout(function () { testMsgHint.textContent = ""; }, 2500);
    }
  }

  function val(id) { return (document.getElementById(id).value || "").trim(); }

  // ---------- 2026-09-21 生成/上传两步分离 ----------
  // 原"生成并上传"一键（后端生成+上传同时发生）拆为两个按钮：
  //   ① 生成上传参数（btnGen → /api/generate_rule）：只做行拆分、合法性校验与
  //      ruleContent/那么否则参数生成，不访问 ADP、不需要 token，结果 JSON
  //      展示在下方供核对（genPayload 缓存）；
  //   ② 上传到 ADP（btnUpload → /api/upload_generated）：直接上传 ① 的结果，
  //      所见即所传；编辑器文本或 ruleId 在生成后发生变化时拦截，要求重新生成。
  // 行拆分口径（与预览一致，沿用旧逻辑）：
  //   - 预定义行（'指定'开头）：整行原样上传 → predefineContent（PUT /api/predefine）
  //   - 那么行（'那么'开头）：去引导词 → thenText（POST /rule/method/define）
  //   - 否则行（'否则'开头）：去引导词 → elseText
  //   - 规则行（'如果'开头/纯规则）：去"如果" → ruleDesc（保存如果接口）
  // 预览区逐行渲染，\n 分隔，行序与编辑区一致；标注失败的行退化为编辑器原文。
  // 2026-09-03：入口词可独立成行，上传按"折叠后的逻辑行"拆分（与预览同口径）。
  // 2026-09-09：取渲染时缓存的规范文本数组 renderedPlainLines（preview 物理行
  // 因连接词换行不再与逻辑行对应），异常时回退旧行为。
  function collectUploadParts() {
    const renderedLines = renderedPlainLines.length
      ? renderedPlainLines
      : (preview.textContent || "").split("\n");
    const editLines = foldEntryLines(box.value || "").split("\n");
    const predefParts = [], ruleParts = [], thenParts = [], elseParts = [];
    editLines.forEach(function (ln, i) {
      const rl = renderedLines[i] != null ? renderedLines[i] : ln;
      const t = ln.trim();
      if (/^预定义\s*指定/.test(t)) {
        // 新行文：预定义 指定'别名'… → 剥离"预定义 "引导词（类似"如果"不上传）
        predefParts.push(rl.replace(/^预定义\s*/, ""));
      } else if (/^指定/.test(t)) {
        // 多预定义续行（第2…N条，折叠时不并入"预定义"行）：整行原样
        predefParts.push(rl);
      } else if (/^那么/.test(t)) {
        thenParts.push(rl.replace(/^那么\s*/, ""));
      } else if (/^否则/.test(t)) {
        elseParts.push(rl.replace(/^否则\s*/, ""));
      } else {
        ruleParts.push(rl.replace(/^如果\s*/, ""));
      }
    });
    return {
      predefineContent: predefParts.join("\n"),
      ruleDesc: ruleParts.join("\n"),
      thenText: thenParts.join("\n"),
      elseText: elseParts.join("\n"),
      // 2026-09-03：硬校验文本与预览同口径（先折叠入口词独立行），避免两行式导致行数不一致
      ruleDescRaw: foldEntryLines(box.value || "").trim(),
    };
  }

  // 生成/上传公共预检：内容非空、无爆红词、ruleId 已填。
  // 通过返回 parts（附 ruleId），失败返回 null（已提示）。
  function precheckUploadParts() {
    const parts = collectUploadParts();
    if (!parts.ruleDesc && !parts.predefineContent && !parts.thenText && !parts.elseText) {
      uploadHint.textContent = "请先在编辑器中输入规则描述（或预定义 / 那么/否则）";
      return null;
    }
    // 合法性预检：存在报错词时直接拦截，不生成不上传（后端还有硬校验兜底）
    const bad = (lastWords || []).filter(function (w) { return w.valid === false; });
    if (bad.length) {
      const msg = bad.map(function (w) { return "『" + w.text + "』" + (w.error || "不合法"); }).join("；");
      uploadHint.textContent = "规则描述不合法，已阻止生成与上传";
      uploadResult.className = "upload-result show err";
      uploadResult.textContent = msg;
      return null;
    }
    parts.ruleId = val("inRuleId");
    if (!parts.ruleId) { uploadHint.textContent = "请填写 ruleId"; return null; }
    return parts;
  }

  // 上传成功后公共处理（原一键上传成功分支原样搬入）
  function handleUploadResp(data, httpStatus) {
    uploadResult.textContent = JSON.stringify(data, null, 2);
    if (data.success) {
      uploadResult.className = "upload-result show ok";
      uploadHint.textContent = "上传成功 ✓ HTTP " + data.httpStatus;
      // 右侧"测试报文"：可编辑树（null → 输入框），并缓存 ruleContent 供命中测试本地对照
      if (data.ruleContent !== undefined) lastRuleContent = data.ruleContent;
      lastThenElseRefs = data.thenElseRefs || [];
      lastPredefineRefs = data.predefineRefs || [];
      renderBlankRule();
      setTestMsg(data);
      // 新骨架/新 ruleContent 立即写入整页快照（下方 clearDraft 只删文本草稿，
      // 快照恰恰在这之后最有价值——用户通常继续填测试报文）
      savePageNow();
      // 上传成功=内容已落库，草稿使命完成，删除避免下次误恢复旧版
      clearDraft();
    } else {
      uploadResult.className = "upload-result show err";
      uploadHint.textContent = "上传失败：" + (data.errMsg || ("HTTP " + httpStatus));
    }
  }

  let genPayload = null;   // 第一步生成结果（/api/generate_rule 的 generated）
  let genKey = "";         // 生成时编辑器内容指纹（文本/ruleId 变化即失效）
  const btnGen = document.getElementById("btnGen");
  const btnUploadEl = document.getElementById("btnUpload");

  btnGen.addEventListener("click", function () {
    const parts = precheckUploadParts();
    if (!parts) return;
    const payload = {
      ruleDesc: parts.ruleDesc,
      ruleDescRaw: parts.ruleDescRaw,
      ruleId: parts.ruleId,
      predefineContent: parts.predefineContent,
      thenText: parts.thenText,
      elseText: parts.elseText,
      bomBaseInfoId: val("inBomBase"),   // 生成阶段即检查关联 ID，避免"生成成功、上传必败"
      bomModelId: val("inBomModel"),
    };
    uploadHint.textContent = "生成中...";
    uploadResult.className = "upload-result";
    btnUploadEl.disabled = true;
    fetch("/api/generate_rule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (resp) {
      return resp.json().then(function (data) {
        if (!data.success) {
          genPayload = null; genKey = "";
          uploadResult.className = "upload-result show err";
          uploadHint.textContent = "生成失败：" + (data.errMsg || ("HTTP " + resp.status));
          uploadResult.textContent = JSON.stringify(data, null, 2);
          return;
        }
        genPayload = data.generated;
        genKey = parts.ruleDescRaw + "\u0001" + parts.ruleId;
        btnUploadEl.disabled = false;
        uploadResult.className = "upload-result show";
        uploadResult.textContent = JSON.stringify(data.generated, null, 2);
        uploadHint.textContent = "生成完成 ✓ 请核对下方上传参数，无误后点「上传到 ADP」";
        // 2026-09-30 填空模式（本地命中测试）改在生成后即渲染，不必等上传 ADP：
        // 条件树与那么/否则、预定义引用均由 /api/generate_rule 离线带回，
        // 求值仅用 ruleContent 本地进行，与是否上传无关。
        lastRuleContent = data.generated.ruleContent;
        lastThenElseRefs = data.generated.thenElseRefs || [];
        lastPredefineRefs = data.generated.predefineRefs || [];
        renderBlankRule();
        savePageNow();   // 快照带上 ruleContent+填空字段，刷新后可恢复
      });
    }).catch(function (e) {
      uploadResult.className = "upload-result show err";
      uploadResult.textContent = "请求异常：" + e.message;
      uploadHint.textContent = "网络错误";
    });
  });

  btnUploadEl.addEventListener("click", function () {
    if (!genPayload) {
      uploadHint.textContent = "请先点击「生成上传参数」";
      return;
    }
    const parts = precheckUploadParts();
    if (!parts) return;
    // 生成后内容失效检测：编辑器文本或 ruleId 变了就必须重新生成（防止传旧参数）
    if (parts.ruleDescRaw + "\u0001" + parts.ruleId !== genKey) {
      uploadHint.textContent = "生成后编辑器内容或 ruleId 已变化，请重新点击「生成上传参数」";
      return;
    }
    // 出于安全已移除账号密码直连登录：上传必须携带有效 adp-token
    if (!val("inToken")) {
      uploadHint.textContent = "缺少 token：已展开「上传参数（高级）」，请填写 adp-token 后重试";
      const advBox = document.getElementById("advParams");
      if (advBox && advBox.style.display === "none") {
        document.getElementById("btnAdvToggle").click();  // 自动展开方便补填
      }
      return;
    }
    const body = {
      generated: genPayload,
      upload: {
        ruleDesc: genPayload.ruleDesc || "",   // 文本以 generated 为准（模型必填占位）
        ruleId: parts.ruleId,
        repositoryId: val("inRepo"),
        catalogueId: val("inCat"),
        url: val("inUrl"),
        token: val("inToken"),
        ruleName: val("inRuleName") || "测试接口",
        predefineContent: genPayload.predefineContent || "",
        bomBaseInfoId: val("inBomBase"),
        bomModelId: val("inBomModel"),
      },
    };
    uploadHint.textContent = "上传中...";
    uploadResult.className = "upload-result";
    fetch("/api/upload_generated", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (resp) {
      return resp.json().then(function (data) {
        handleUploadResp(data, resp.status);
      });
    }).catch(function (e) {
      uploadResult.className = "upload-result show err";
      uploadResult.textContent = "请求异常：" + e.message;
      uploadHint.textContent = "网络错误";
    });
  });
  document.getElementById("btnUploadClear").addEventListener("click", function () {
    uploadResult.className = "upload-result";
    uploadResult.textContent = "";
    uploadHint.textContent = "";
  });

  // ---------- 上传参数（高级）：配置默认值预填 + 折叠显示 ----------
  // 表单只常显 ruleId/repositoryId/catalogueId 三项；
  // token 必填（已移除账号密码直连登录，iframe 场景由 URL ?token= 自动注入）；
  // url/ruleName/bomBaseInfoId/bomModelId 从 /api/upload_defaults（config.yaml
  // upload 节）拉取预填，默认隐藏，点"上传参数（高级）"展开后可手工覆盖。
  (function initAdvUploadParams() {
    const advBox = document.getElementById("advParams");
    const advBtn = document.getElementById("btnAdvToggle");
    const advHint = document.getElementById("advHint");
    advBtn.addEventListener("click", function () {
      const hidden = advBox.style.display === "none";
      advBox.style.display = hidden ? "" : "none";
      advBtn.textContent = hidden ? "上传参数（高级） ▾" : "上传参数（高级） ▸";
    });
    fetch("/api/upload_defaults")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        const map = { inUrl: d.url, inRuleName: d.ruleName,
                      inBomBase: d.bomBaseInfoId, inBomModel: d.bomModelId };
        Object.keys(map).forEach(function (id) {
          const el = document.getElementById(id);
          if (el && !el.value) el.value = map[id] || "";
        });
        // 2026-09-30：config.yaml upload.token 预置 adp-token。
        // 仅当输入框仍为空时填入（?token= 注入在脚本加载时已同步生效，
        // 优先于此）；填入走 applyInjectedToken 口径，_tokenAuto=true，
        // 后续父页面 postMessage 续期 token 仍可热覆盖，用户手改后不再覆盖。
        if (d.token && _tokenEl && !_tokenEl.value.trim()) applyInjectedToken(d.token);
      })
      .catch(function () {
        advHint.textContent = "默认参数加载失败，展开「上传参数（高级）」手工填写";
      });
  })();

  // ---------- 选库/选分类浮层共用：视口避障定位 ----------
  // 横向贴边收进；竖向哪侧空间大选哪侧（下沿不够向上翻），
  // 列表 max-height 按剩余空间在 [120, 280] 收缩，防止被屏幕底边遮住。
  function placePickPanel(btn, panel, listBox) {
    const rect = btn.getBoundingClientRect();
    const pw = panel.offsetWidth || 360;
    let left = rect.left;
    if (left + pw > window.innerWidth - 12) left = Math.max(12, window.innerWidth - pw - 12);
    const CHROME = 70;                 // padding + 搜索框 + 间距（面板除列表外的高度）
    const vh = window.innerHeight;
    const spaceDown = vh - rect.bottom - 16;
    const spaceUp = rect.top - 16;
    const below = spaceDown >= spaceUp;
    const avail = below ? spaceDown : spaceUp;
    const listH = Math.min(280, Math.max(120, avail - CHROME));
    listBox.style.maxHeight = listH + "px";
    panel.style.left = left + "px";
    panel.style.top = below
      ? (rect.bottom + 4) + "px"
      : Math.max(12, rect.top - 4 - (listH + CHROME)) + "px";
  }

  // ---------- 决策库选择（repositoryId ↔ 库名，数据源 /api/repositories） ----------
  // 库对照表由 新车险915/repository.txt 经 etl/repositories.py 导入 bom_catalog.sqlite；
  // 面板一次拉全量、本地过滤（库数量级很小）。选中回填 inRepo（上传仍取该输入框值），
  // 也允许手输任意 ID（手输值不在列表时 hint 提示"自定义 ID"，不拦截）。
  (function initRepoPicker() {
    const repoIn = document.getElementById("inRepo");
    const btn = document.getElementById("btnRepoPick");
    const panel = document.getElementById("repoPanel");
    const search = document.getElementById("repoSearch");
    const listBox = document.getElementById("repoList");
    const nameHint = document.getElementById("repoNameHint");
    if (!repoIn || !btn || !panel || !search || !listBox) return;
    let repos = null;   // 全量缓存（lazy 一次）
    let shown = [];     // 当前过滤结果
    let rhl = -1;       // 键盘高亮索引

    function findRepo(id) {
      const v = (id || "").trim().toLowerCase();
      if (!v || !repos) return null;
      for (let i = 0; i < repos.length; i++) {
        if ((repos[i].id || "").toLowerCase() === v) return repos[i];
      }
      return null;
    }
    function updateNameHint() {
      if (!nameHint) return;
      const v = repoIn.value.trim();
      if (!v) {
        nameHint.className = "pick-name empty";
        nameHint.textContent = "未选择";
        nameHint.title = "";
        return;
      }
      if (!repos) {
        nameHint.className = "pick-name empty";
        nameHint.textContent = "库列表加载中…";
        nameHint.title = "";
        load();
        return;
      }
      const hit = findRepo(v);
      nameHint.className = "pick-name";
      /* 框内只显示名称，编号/自定义说明收进悬浮 title */
      nameHint.textContent = hit ? (hit.name || hit.id) : v;
      nameHint.title = hit
        ? (hit.name || "") + "  " + hit.id
        : "自定义 ID（不在库列表中，仍可按原样上传）";
    }
    function load() {
      if (repos) return Promise.resolve(repos);
      return fetch("/api/repositories")
        .then(function (r) { return r.json(); })
        .then(function (d) { repos = (d && d.repositories) || []; updateNameHint(); return repos; })
        .catch(function () { repos = []; return repos; });
    }
    function esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }
    function render() {
      const w = search.value.trim().toLowerCase();
      shown = (repos || []).filter(function (r) {
        if (!w) return true;
        return (r.name || "").toLowerCase().indexOf(w) >= 0 ||
               (r.id || "").toLowerCase().indexOf(w) >= 0;
      });
      rhl = shown.length ? 0 : -1;
      let html = "";
      shown.forEach(function (r, i) {
        html += '<div class="repo-item' + (i === rhl ? " hl" : "") + '" data-i="' + i + '">' +
                '<span class="rname">' + esc(r.name) + "</span>" +
                '<span class="rid">' + esc(r.id) + "</span></div>";
      });
      if (!html) {
        html = '<div class="repo-empty">' + (repos && repos.length
          ? "无匹配库（换关键字试试）"
          : "库列表为空：请在 " + (location.host || "本地") + " 数据源目录的 repository 文件补充对照表，" +
            "并执行 python -m rule_editor.etl.repositories 导入") + "</div>";
      }
      listBox.innerHTML = html;
    }
    function markHl() {
      const items = listBox.querySelectorAll(".repo-item");
      for (let i = 0; i < items.length; i++) items[i].classList.toggle("hl", i === rhl);
      if (items[rhl] && items[rhl].scrollIntoView) items[rhl].scrollIntoView({ block: "nearest" });
    }
    function pick(i) {
      const r = shown[i];
      if (!r) return;
      repoIn.value = r.id;
      updateNameHint();
      close();
      try { repoIn.dispatchEvent(new Event("input", { bubbles: true })); } catch (e) {}
      if (typeof scheduleDraft === "function") scheduleDraft();
    }
    function open() {
      panel.classList.add("show");
      placePickPanel(btn, panel, listBox);
      search.value = "";
      render();
      load().then(function () { render(); });
      search.focus();
    }
    function close() { panel.classList.remove("show"); }
    function isOpen() { return panel.classList.contains("show"); }

    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      isOpen() ? close() : open();
    });
    search.addEventListener("input", render);
    search.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowDown") { ev.preventDefault(); if (shown.length) { rhl = (rhl + 1) % shown.length; markHl(); } }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); if (shown.length) { rhl = (rhl - 1 + shown.length) % shown.length; markHl(); } }
      else if (ev.key === "Enter") { ev.preventDefault(); if (rhl >= 0) pick(rhl); }
      else if (ev.key === "Escape") { ev.preventDefault(); close(); btn.focus(); }  // ID 输入框已藏进高级参数，焦点回“选库”按钮
    });
    listBox.addEventListener("mousedown", function (ev) {
      const item = ev.target.closest ? ev.target.closest(".repo-item") : null;
      if (!item) return;
      ev.preventDefault();   // 防 search 失焦竞态
      pick(parseInt(item.getAttribute("data-i"), 10));
    });
    repoIn.addEventListener("input", updateNameHint);
    document.addEventListener("click", function (ev) {
      if (!isOpen()) return;
      if (panel.contains(ev.target) || ev.target === btn) return;
      close();
    });
    load();   // 页面加载即拉一次：草稿恢复/手输的 ID 也能回显库名
  })();

  // ---------- 决策分类选择（repositoryId → catalogueId 列表，数据源 /api/catalogues） ----------
  // 对照表由 新车险915/respository_cata.txt 经 etl/repositories.py 导入 catalogues 表；
  // 面板按当前 inRepo 值检索该库全部分类（单库数量很小），本地过滤；
  // inRepo 未填时不检索，空态提示先选库。选中回填 inCat（上传仍取该输入框值），
  // 也允许手输任意 ID（不在该库列表时 hint 提示"自定义 ID"，不拦截）。
  (function initCatPicker() {
    const repoIn = document.getElementById("inRepo");
    const catIn = document.getElementById("inCat");
    const btn = document.getElementById("btnCatPick");
    const panel = document.getElementById("catPanel");
    const search = document.getElementById("catSearch");
    const listBox = document.getElementById("catList");
    const nameHint = document.getElementById("catNameHint");
    if (!repoIn || !catIn || !btn || !panel || !search || !listBox) return;
    let cats = null;     // 当前库的分类缓存（null=未加载）
    let catsRepo = "";   // cats 对应的 repositoryId（小写）
    let shown = [];
    let chl = -1;

    function esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }
    function fetchCats(repo) {
      // 拉取 repo 的全部分类并缓存；repo 变化即失效重取
      if (!repo) { cats = []; catsRepo = ""; return Promise.resolve(cats); }
      const key = repo.toLowerCase();
      if (cats && catsRepo === key) return Promise.resolve(cats);
      return fetch("/api/catalogues?repositoryId=" + encodeURIComponent(repo))
        .then(function (r) { return r.json(); })
        .then(function (d) { cats = (d && d.catalogues) || []; catsRepo = key; return cats; })
        .catch(function () { cats = []; catsRepo = key; return cats; });
    }
    function findCat(id) {
      const v = (id || "").trim().toLowerCase();
      if (!v || !cats) return null;
      for (let i = 0; i < cats.length; i++) {
        if ((cats[i].id || "").toLowerCase() === v) return cats[i];
      }
      return null;
    }
    function updateCatHint() {
      if (!nameHint) return;
      const v = catIn.value.trim();
      const repo = repoIn.value.trim();
      if (!v) {
        nameHint.className = "pick-name empty";
        nameHint.textContent = repo ? "未选择" : "请先选库";
        nameHint.title = "";
        return;
      }
      if (cats && catsRepo === repo.toLowerCase()) {
        const hit = findCat(v);
        nameHint.className = "pick-name";
        /* 框内只显示名称，编号/自定义说明收进悬浮 title */
        nameHint.textContent = hit ? (hit.name || hit.id) : v;
        nameHint.title = hit
          ? (hit.name || "") + "  " + hit.id
          : "自定义 ID（不在当前决策库的分类列表中，仍可按原样上传）";
      } else {
        nameHint.className = "pick-name empty";
        nameHint.textContent = "分类列表加载中…";
        nameHint.title = "";
        fetchCats(repo).then(function () { updateCatHint(); });
      }
    }
    function render() {
      const w = search.value.trim().toLowerCase();
      shown = (cats || []).filter(function (c) {
        if (!w) return true;
        return (c.name || "").toLowerCase().indexOf(w) >= 0 ||
               (c.id || "").toLowerCase().indexOf(w) >= 0;
      });
      chl = shown.length ? 0 : -1;
      let html = "";
      shown.forEach(function (c, i) {
        html += '<div class="repo-item' + (i === chl ? " hl" : "") + '" data-i="' + i + '">' +
                '<span class="rname">' + esc(c.name) + "</span>" +
                '<span class="rid">' + esc(c.id) + "</span></div>";
      });
      if (!html) {
        let msg = "加载…";
        if (!repoIn.value.trim()) msg = "请先点「选库」选择决策库（分类按 repositoryId 检索）";
        else if (cats !== null) msg = cats.length ? "无匹配分类（换关键字试试）" : "该决策库暂无分类记录";
        html = '<div class="repo-empty">' + msg + "</div>";
      }
      listBox.innerHTML = html;
    }
    function markHl() {
      const items = listBox.querySelectorAll(".repo-item");
      for (let i = 0; i < items.length; i++) items[i].classList.toggle("hl", i === chl);
      if (items[chl] && items[chl].scrollIntoView) items[chl].scrollIntoView({ block: "nearest" });
    }
    function pick(i) {
      const c = shown[i];
      if (!c) return;
      catIn.value = c.id;
      close();
      try { catIn.dispatchEvent(new Event("input", { bubbles: true })); } catch (e) {}
      if (typeof scheduleDraft === "function") scheduleDraft();
    }
    function open() {
      panel.classList.add("show");
      placePickPanel(btn, panel, listBox);
      search.value = "";
      cats = null;   // 每次打开重取，防切库后展示旧列表
      catsRepo = "";
      render();
      const repo = repoIn.value.trim();
      if (repo) fetchCats(repo).then(function () { if (isOpen()) render(); });
      search.focus();
    }
    function close() { panel.classList.remove("show"); }
    function isOpen() { return panel.classList.contains("show"); }

    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      isOpen() ? close() : open();
    });
    search.addEventListener("input", render);
    search.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowDown") { ev.preventDefault(); if (shown.length) { chl = (chl + 1) % shown.length; markHl(); } }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); if (shown.length) { chl = (chl - 1 + shown.length) % shown.length; markHl(); } }
      else if (ev.key === "Enter") { ev.preventDefault(); if (chl >= 0) pick(chl); }
      else if (ev.key === "Escape") { ev.preventDefault(); close(); btn.focus(); }  // 焦点回“选目录”按钮
    });
    listBox.addEventListener("mousedown", function (ev) {
      const item = ev.target.closest ? ev.target.closest(".repo-item") : null;
      if (!item) return;
      ev.preventDefault();   // 防 search 失焦竞态
      pick(parseInt(item.getAttribute("data-i"), 10));
    });
    catIn.addEventListener("input", updateCatHint);
    // 换库：分类缓存失效 + 重算 hint（选库面板 pick 会派发 input 触发这里）
    repoIn.addEventListener("input", function () { cats = null; catsRepo = ""; updateCatHint(); });
    document.addEventListener("click", function (ev) {
      if (!isOpen()) return;
      if (panel.contains(ev.target) || ev.target === btn) return;
      close();
    });
    updateCatHint();   // 草稿恢复时 inCat 已有值：回显分类名
  })();

  // ---------- 复制测试报文（右侧）：复制当前填值后的报文 ----------
  // 2026-09-30：「测试报文」卡片已从前端移除，以下三处按钮绑定按元素存在与否降级
  const _btnCopyTestMsg = document.getElementById("btnCopyTestMsg");
  if (_btnCopyTestMsg) _btnCopyTestMsg.addEventListener("click", function () {
    if (testInputSkeleton === undefined) {
      alert("暂无测试报文（上传成功后自动生成）");
      return;
    }
    let cur;
    try { cur = collectTestInput(); }
    catch (e) { alert("存在填写错误：\n" + e.message); return; }
    copyText(JSON.stringify(cur, null, 2)).then(function () {
      testMsgHint.textContent = "已复制 ✓";
      setTimeout(function () { testMsgHint.textContent = ""; }, 2000);
    }).catch(function () {
      alert("复制失败，请手动选择文本复制");
    });
  });

  // ---------- 树视图 ↔ 原始 JSON 文本 ----------
  const _btnShowJson = document.getElementById("btnShowJson");
  if (_btnShowJson) _btnShowJson.addEventListener("click", function () {
    if (testInputSkeleton === undefined) {
      alert("暂无测试报文（上传成功后自动生成）");
      return;
    }
    showRawJson = !showRawJson;
    renderTestEdit();
    testMsgHint.textContent = showRawJson ? "原始 JSON（只读快照，改值请切回）" : "";
    setTimeout(function () { if (showRawJson) testMsgHint.textContent = ""; }, 3000);
  });

  // ---------- 测试规则命中：填值后的报文 → /api/test_rule_hit ----------
  const _btnTestHit = document.getElementById("btnTestHit");
  if (_btnTestHit) _btnTestHit.addEventListener("click", function () {
    if (testInputSkeleton === undefined) {
      alert("暂无测试报文（上传成功后自动生成）");
      return;
    }
    const ruleId = val("inRuleId");
    if (!ruleId) { testHitHint.textContent = "请先填写 ruleId（左上方传表单）"; return; }
    const appId = (val("inAppId") || "").trim();
    if (!appId) {
      testHitHint.textContent =
        "请先填写 applicationId（规则所属应用标识，如 YAIC_LIFE_FEE_B3_AnHui）；应用不匹配时 ADP 会 success=true 但 pass=0";
      return;
    }
    let inputPayload;
    try { inputPayload = collectTestInput(); }
    catch (e) {
      testHitHint.textContent = "存在填写错误：" + e.message;
      return;
    }
    const payload = {
      ruleId: ruleId,
      input: inputPayload,
      url: val("inUrl"),
      token: val("inToken"),
      applicationId: appId,
      flag: false,
      ruleContent: lastRuleContent || undefined,
    };
    testHitHint.textContent = "命中测试中…（ADP ruleSingleTest" +
      (payload.ruleContent ? " + 本地求值对照" : "") + "）";
    hitResultEl.className = "hit-result";
    fetch("/api/test_rule_hit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (resp) {
      return resp.json().then(function (data) {
        renderHitResult(data);
      });
    }).catch(function (e) {
      hitResultEl.className = "hit-result show err";
      hitResultEl.textContent = "请求异常：" + e.message;
      testHitHint.textContent = "网络错误";
    });
  });

  // ADP 返回摘要：url/状态码/success/errCode/pass/errMsg（response 已由后端 slim）
  function apiBriefText(data) {
    var api = (data && data.api) || {};
    var j = api.response || {};
    var r = (j.resp && typeof j.resp === "object") ? j.resp : {};
    var bits = [];
    if (api.url) bits.push("POST " + api.url);
    bits.push("httpStatus=" + (api.httpStatus == null ? "?" : api.httpStatus) +
      "  success=" + (j.success === undefined ? data.success : j.success) +
      "  errCode=" + (j.errCode === undefined ? (api.errCode === undefined ? "-" : api.errCode) : j.errCode) +
      "  pass=" + JSON.stringify(r.pass === undefined ? data.pass : r.pass));
    var m = j.errMsg || j.msg || api.errMsg || data.errMsg;
    if (m) bits.push("errMsg=" + String(m).slice(0, 300));
    // resp 其余关键状态字段（log/ruleId 等非对象值）
    Object.keys(r).forEach(function (k) {
      if (k === "pass" || typeof r[k] === "object") return;
      var s = String(r[k]);
      if (s && s !== "null") bits.push(k + "=" + s.slice(0, 200));
    });
    return bits.join("\n");
  }

  function renderHitResult(data) {
    data = data || {};
    const lc = data.localCheck;
    const lcText = lc ? (function () {
      let t = "\n\n本地求值对照：" + (lc.pass === true ? "命中 ✓" : "未命中 ✗");
      if (lc.warnings && lc.warnings.length)
        t += "\n" + lc.warnings.map(function (w) { return "[诊断] " + w; }).join("\n");
      return t + "\n" + (lc.tree || "");
    })() : (data.localCheckError ? "\n\n" + data.localCheckError : "");
    // 2026-09-04：本地求值命中 → 直接整体标绿，接口结果只作附注（暂不采信）
    if (lc && lc.pass === true) {
      let apiNote = "";
      if (data.success === false) apiNote = "\n[接口测试失败：" + (data.errMsg || "未知错误") + "（暂不采信）]";
      else if (data.pass === false) apiNote = "\n[接口未命中（暂不采信）]";
      else if (data.pass === true) apiNote = "\n[接口命中 ✓]";
      hitResultEl.className = "hit-result show hit";
      hitResultEl.textContent = "本地求值命中 ✓" + apiNote + lcText +
        (data.api ? "\n\nADP 返回：\n" + apiBriefText(data) : "");
      testHitHint.textContent = "";
      return;
    }
    if (!data || data.success === false) {
      hitResultEl.className = "hit-result show err";
      hitResultEl.textContent = "接口测试失败：" + (data.errMsg || "未知错误") +
        lcText +
        (data.api ? "\n\nADP 返回：\n" + apiBriefText(data) : "");
      testHitHint.textContent = "";
      return;
    }
    const apiPass = data.pass;
    let head, cls;
    if (apiPass === true) { cls = "hit"; head = "接口命中 ✓"; }
    else if (apiPass === false) { cls = "miss"; head = "接口未命中 ✗"; }
    else { cls = "err"; head = "接口返回无 pass 字段"; }
    hitResultEl.className = "hit-result show " + cls;
    let text = head + (data.errMsg ? "  errMsg=" + data.errMsg : "") + lcText;
    // 命中/未命中也展示 ADP 具体返回（pass 原始值等），不只一个标题
    if (data.api) text += "\n\nADP 返回：\n" + apiBriefText(data);
    if (cls === "err" && data.api) {
      text += "\n[ADP 排查] url=" + (data.api.url || "-") +
        " httpStatus=" + (data.api.httpStatus == null ? "-" : data.api.httpStatus) +
        " errCode=" + (data.api.errCode == null ? "-" : data.api.errCode);
    }
    if (lc && apiPass === false && lc.pass === true)
      text += "\n>>> 接口未命中但本地命中：报文填值大概率正确，请核对 applicationId 是否为规则所属应用（应用不匹配必 pass=0）";
    hitResultEl.textContent = text;
    testHitHint.textContent = "";
  }

  // ---------- 填空模式：条件文本只读 + 字段取值表单（显示文本，不暴露路径），仅本地求值（不走 ADP） ----------
  const blankBox = document.getElementById("blankRule");
  const blankHint = document.getElementById("blankHint");
  const blankResultEl = document.getElementById("blankResult");
  const blankSync = new Map();   // fieldPath → [输入框,…]：同字段多处出现值同步
  const blankMethodKeys = new Set();
  function localFieldKey(opd) {
    const expr = opd.exprNode;
    if (!expr || !expr.paramValue || !Object.keys(expr.paramValue).length) return opd.fieldPath;
    const args = Object.keys(expr.paramValue).sort().map(function (k) {
      const v = expr.paramValue[k];
      return [k, v && v.fieldPath ? v.fieldPath : v.paramValue];
    });
    const key = opd.fieldPath + "|" + JSON.stringify(args);
    blankMethodKeys.add(key);
    return key;
  }
  function isLocalEnum(opd) {
    return /^dynamicsAttributeList\.[^.]+\.[^.]+_[^.]+$/.test(opd.fieldPath || "") &&
      !opd.exprNode && opd.dataType !== "method";
  }
  let blankFields = new Map();   // fieldPath → { label, group, rest }
  let blankAux = [];             // 那么/否则参数输入框（不进报文、不参与求值）

  function blankFieldInfo(fp, label) {
    if (blankFields.has(fp)) return blankFields.get(fp);
    let group = null, rest = fp.split(".");
    if (rest[0] === "规则预定义变量") {
      group = rest[1] || "";
      rest = rest.slice(2);
    }
    const info = { label: label || fp, group: group, rest: rest };
    blankFields.set(fp, info);
    return info;
  }

  function blankSpan(cls, text) {
    const s = document.createElement("span");
    s.className = cls;
    s.textContent = text;
    return s;
  }

  // EXPRESSION 运算符 → 中文标签（与后端 rule_evaluator.EXPR_LABELS 同口径）
  const EXPR_OP_TEXT = {
    STR_BELONG_TO: "属于", STR_NOT_BELONG_TO: "不属于",
    STR_BELONG_TO_MUL: "属于", STR_NOT_BELONG_TO_MUL: "不属于",
    BOOLEAN_BELONG_TO: "为", STR_INCLUDE: "包含", STR_NOT_INCLUDE: "不包含",
    STR_EMPTY: "为空", STR_NOT_EMPTY: "不为空", DATE_BETWEEN: "在",
    STR_START_WITH: "以…开头", STR_END_WITH: "以…结尾",
    SIZE_OF_LIST: "长度", STR_LENGTH: "长度",
    // 运算符924 档1
    STR_NE: "不是", STR_NOT_START_WITH: "不以…开头",
    STR_NOT_END_WITH: "不以…结尾",
    NUM_BETWEEN: "在", TIME_BETWEEN: "在",  // 区间系：渲染「在 起 和 止 之间」三段（见 renderBlankExpr）
    COLLECTION_IS_EMPTY: "为空", COLLECTION_NOT_EMPTY: "不为空",
    COLLECTION_ANY_STR: "包含", COLLECTION_NOT_IN_STR: "不包含",
    COLLECTION_ANY_BELONG_TO: "部分属于", COLLECTION_ALL_BELONG_TO: "全部属于",
    COLLECTION_ANY_CONTAINS: "部分包含", COLLECTION_ALL_CONTAINS: "全部包含",
    COLLECTION_NOT_IN_CONTAINS: "不包含集合",
  };

  // 条件文本只读渲染：字段显示"显示文本"（label，不暴露路径名），参数显示规则原值；
  // METHOD（自定义方法如 费用比率()）显示方法名——本地求值把它当可填字段（填方法结果值）
  function renderBlankOperand(opd) {
    if (!opd || typeof opd !== "object")
      return blankSpan("blank-lit", String(opd == null ? "" : opd));
    if (opd.nodeType === "CUSTOM") {
      const v = opd.paramValue;
      if (v && typeof v === "object")   // selector {key,label,value} → 显示 label
        return blankSpan("blank-lit", v.label || v.key || "空");
      return blankSpan("blank-lit", v === null || v === undefined ? "空" : String(v));
    }
    if (opd.nodeType === "METHOD")
      return blankSpan("blank-field", opd.methodName || opd.methodPath || "（方法）");
    // 嵌套布尔子树："（…）等于 成立/不成立"产物——操作数位置又是一棵
    // COMPARISON/LOGICAL，递归渲染（否则只剩 label、内层字段丢失）
    if (opd.nodeType === "COMPARISON" || opd.nodeType === "LOGICAL")
      return renderBlankNode(opd);
    if (opd.nodeType === "EXPRESSION")
      return renderBlankExpr(opd);
    if (opd.nodeType === "NESTING" || opd.nodeType === "TREE_EX")
      return renderBlankArith(opd);
    return blankSpan("blank-field", opd.label || opd.fieldPath || "（无字段）");
  }

  // EXPRESSION 条件（属于…中的一个 / 包含 / 为空 / 在…和…之间 / 为是否 / 长度…）
  function renderBlankExpr(node) {
    const pv = node.paramValue || {};
    const eid = node.expressionId || "";
    const opText = EXPR_OP_TEXT[eid] || eid || "表达式";
    const f = document.createDocumentFragment();
    if (eid === "SIZE_OF_LIST" || eid === "STR_LENGTH") {
      f.appendChild(blankSpan("blank-logic", opText + "（"));
      f.appendChild(renderBlankOperand(pv.param1));
      f.appendChild(blankSpan("blank-logic", "）"));
      return f;
    }
    f.appendChild(renderBlankOperand(pv.param1));
    f.appendChild(blankSpan("blank-op", " " + opText + " "));
    if (/_BETWEEN$/.test(eid)) {   // DATE/TIME/NUM_BETWEEN：在 起 和 止 之间
      f.appendChild(renderBlankOperand(pv.param2));
      f.appendChild(blankSpan("blank-logic", " 和 "));
      f.appendChild(renderBlankOperand(pv.param3));
      f.appendChild(blankSpan("blank-logic", " 之间"));
    } else if (pv.param2 !== undefined) {
      if (Array.isArray(pv.param2)) {   // 字段值列表：A, B → 逐项展示
        pv.param2.forEach(function (x, i) {
          if (x === "," || (typeof x === "string" && x.trim() === ",")) return;
          if (i) f.appendChild(blankSpan("blank-lit", " , "));
          f.appendChild(renderBlankOperand(x));
        });
      } else {
        f.appendChild(renderBlankOperand(pv.param2));
      }
    }
    // "属于 X 中的一个"后缀：生成器口径，仅字符串系 BELONG_TO；
    // COLLECTION_ANY/ALL_BELONG_TO（运算符924 档1）自带"部分/全部属于"语义，不加后缀
    if (/^STR_(NOT_)?BELONG_TO(_MUL)?$/.test(eid))
      f.appendChild(blankSpan("blank-logic", " 中的一个"));
    return f;
  }

  // 算术树（NESTING 二叉 / TREE_EX 交替数组）：渲染为 (A 加 B) 大于 … 形式
  function renderBlankArith(node) {
    const f = document.createDocumentFragment();
    if (node.nodeType === "NESTING" && node.nestingTree) {
      f.appendChild(renderCalcTree(node.nestingTree));
    } else if (node.nodeType === "TREE_EX" && Array.isArray(node.arr)) {
      node.arr.forEach(function (x) { f.appendChild(renderCalcTree(x)); });
    } else {
      f.appendChild(blankSpan("blank-lit", "（算式）"));
    }
    return f;
  }

  function renderCalcTree(t) {
    if (!t || typeof t !== "object")
      return document.createTextNode(String(t == null ? "" : t));
    if (t.nodeType === "CALCULATE") {
      const f = document.createDocumentFragment();
      f.appendChild(document.createTextNode("（ "));
      f.appendChild(renderCalcTree(t.left));
      f.appendChild(blankSpan("blank-op", " " + (t.label || t.operator || "?") + " "));
      f.appendChild(renderCalcTree(t.right));
      f.appendChild(document.createTextNode(" ）"));
      return f;
    }
    return renderBlankOperand(t);
  }

  function renderBlankNode(node) {
    if (!node || typeof node !== "object") return blankSpan("blank-lit", "（?）");
    if (node.nodeType === "LOGICAL") {
      const f = document.createDocumentFragment();
      f.appendChild(document.createTextNode("（ "));
      f.appendChild(renderBlankNode(node.left));
      f.appendChild(blankSpan("blank-logic", " " + (node.label || "并且") + " "));
      f.appendChild(renderBlankNode(node.right));
      f.appendChild(document.createTextNode(" ）"));
      return f;
    }
    if (node.nodeType === "COMPARISON") {
      const f = document.createDocumentFragment();
      f.appendChild(renderBlankOperand(node.left));
      f.appendChild(blankSpan("blank-op", " " + (node.label || node.code || "是") + " "));
      f.appendChild(renderBlankOperand(node.right));
      return f;
    }
    if (node.nodeType === "EXPRESSION")
      return renderBlankExpr(node);
    return blankSpan("blank-lit", "（未知条件节点: " + (node.nodeType || "?") + "）");
  }

  // 2026-09-07 填空口径 v3：条件文本只读展示（不挖空），下方按条件中出现的字段
  // 逐行列出"显示文本：输入框"表单；用户填各字段的模拟取值，收集后按字段路径
  // 构造最小报文做本地求值（如 A>100 填 50 → 50<100 → 未命中）。
  function renderBlankRule() {
    blankFields = new Map();
    blankSync.clear();
    blankAux = [];
    blankResultEl.className = "hit-result";
    blankResultEl.textContent = "";
    blankHint.textContent = "";
    blankBox.innerHTML = "";
    const hasCond = lastRuleContent && typeof lastRuleContent === "object"
                    && lastRuleContent.nodeType;
    const anyAux = (lastThenElseRefs || []).length || (lastPredefineRefs || []).length;
    if (!hasCond && !anyAux) {
      blankBox.className = "blank-box empty";
      blankBox.textContent = "（点击「生成上传参数」后，自动按'如果'条件列出需填字段）";
      return;
    }
    blankBox.className = "blank-box";
    const fps = [];
    if (hasCond) {
      // 1) 条件文本只读展示（不挖空）
      const cond = document.createElement("div");
      cond.className = "blank-cond";
      try { cond.appendChild(renderBlankNode(lastRuleContent)); }
      catch (e) {
        blankBox.className = "blank-box empty";
        blankBox.textContent = "条件渲染失败：" + e.message;
        return;
      }
      blankBox.appendChild(cond);
      // 2) 字段填空表单：按字段路径去重，label 用字段显示文本（不暴露路径）
      collectFieldOps(lastRuleContent, fps);
    }
    if (!fps.length && !anyAux) {
      const none = document.createElement("div");
      none.className = "blank-none";
      none.textContent = "（条件中未发现可填字段）";
      blankBox.appendChild(none);
      return;
    }
    const list = document.createElement("div");
    list.className = "blank-list";
    fps.forEach(function (item) {
      const info = blankFieldInfo(item.fp, item.label);
      const row = document.createElement("div");
      row.className = "blank-row";
      const lab = document.createElement("label");
      lab.className = "blank-label";
      lab.textContent = info.label + "：";
      lab.title = item.fp;
      const inp = document.createElement("input");
      inp.className = "blank-input";
      inp.type = "text";
      inp.spellcheck = false;
      inp.dataset.blank = item.fp;
      inp.placeholder = "填值";
      if (PAGE_BLANK && PAGE_BLANK[item.fp] != null) {
        inp.value = PAGE_BLANK[item.fp];
        if (inp.value) inp.classList.add("filled");
      }
      inp.addEventListener("input", function () {
        (blankSync.get(item.fp) || []).forEach(function (o) {
          if (o !== inp && o.value !== inp.value) o.value = inp.value;
        });
        inp.classList.toggle("filled", !!inp.value.trim());
        schedulePage();
      });
      if (!blankSync.has(item.fp)) blankSync.set(item.fp, []);
      blankSync.get(item.fp).push(inp);
      row.appendChild(lab);
      row.appendChild(inp);
      list.appendChild(row);
    });
    // 3) 引用参数展示区：预定义'指定…'行、那么/否则动作文本里出现的 BOM 对象
    //    也列成可填参数行（仅展示，不进报文、不参与本地命中判定——
    //    2026-09-10 起测试报文口径不含 then 引用；预定义定义内字段同属展示口径）
    const used = new Set(fps.map(function (x) { return x.fp; }));
    const auxGroups = [];   // [[组名, [ref,…]], …]：预定义 / 那么/否则
    function pushAux(list2, fallback) {
      (list2 || []).forEach(function (r) {
        if (!r || !r.fp || used.has(r.fp)) return;
        used.add(r.fp);
        const gname = (r.src === "预定义") ? "预定义" : "那么/否则";
        let g = auxGroups.find(function (x) { return x[0] === gname; });
        if (!g) { g = [gname, []]; auxGroups.push(g); }
        g[1].push(r);
      });
    }
    pushAux(lastPredefineRefs, "预定义");
    pushAux(lastThenElseRefs, "那么/否则");
    auxGroups.forEach(function (g) {
      const head = document.createElement("div");
      head.className = "blank-aux-head";
      head.textContent = g[0] + "引用参数（可填展示值，不参与命中判定）";
      list.appendChild(head);
      g[1].forEach(function (r) {
        const row = document.createElement("div");
        row.className = "blank-row";
        const lab = document.createElement("label");
        lab.className = "blank-label";
        lab.textContent = (r.label || r.fp) + "：";
        lab.title = (r.src || "") + " 参数 " + r.fp;
        const inp = document.createElement("input");
        inp.className = "blank-input";
        inp.type = "text";
        inp.spellcheck = false;
        inp.placeholder = "可留空";
        const ai = blankAux.length;   // aux 行无稳定 key，按 DOM 顺序回填/收集
        if (PAGE_AUX && PAGE_AUX[ai] != null) {
          inp.value = PAGE_AUX[ai];
          if (inp.value) inp.classList.add("filled");
        }
        inp.addEventListener("input", function () {
          inp.classList.toggle("filled", !!inp.value.trim());
          schedulePage();
        });
        blankAux.push(inp);
        row.appendChild(lab);
        row.appendChild(inp);
        list.appendChild(row);
      });
    });
    blankBox.appendChild(list);
  }

  // 收集条件树中的可填操作数（FIELD 字段 + METHOD 方法结果 + EXPRESSION/算术树内
  // 的字段；按路径去重，保留首个显示文本）。out 为结果数组，贯穿全部递归函数。
  function collectFieldOps(node, out) {
    if (!node || typeof node !== "object") return;
    if (node.nodeType === "LOGICAL") {
      collectFieldOps(node.left, out);
      collectFieldOps(node.right, out);
      return;
    }
    if (node.nodeType === "COMPARISON") {
      collectOperandOps(node.left, out);
      collectOperandOps(node.right, out);
      return;
    }
    if (node.nodeType === "EXPRESSION") {
      const pv = node.paramValue || {};
      Object.keys(pv).forEach(function (k) { collectOperandOps(pv[k], out); });
    }
  }

  // 操作数递归收集：FIELD/METHOD 直接收；EXPRESSION 展开 paramValue；
  // NESTING/TREE_EX 走算术树；数组（如 param2 字段值列表）逐项。
  function collectOperandOps(opd, out) {
    if (!opd || typeof opd !== "object") return;
    if (opd.nodeType === "FIELD" && opd.fieldPath) {
      if (isLocalEnum(opd)) return;  // 动态域枚举是固定值，不要求用户再次填写。
      const expr = opd.exprNode;
      let label = opd.label || "";
      if (expr && expr.paramValue) {
        const args = Object.keys(expr.paramValue).sort().map(function (k) {
          const v = expr.paramValue[k];
          return v.label || String(v.paramValue);
        });
        label = label.replace('()', '(' + args.join(', ') + ')');
      }
      pushFieldOp(out, localFieldKey(opd), label);
    } else if (opd.nodeType === "METHOD" && opd.methodPath) {
      // 本地求值不执行方法：方法结果值由用户填写，按 methodPath 挂入报文参与比较
      pushFieldOp(out, opd.methodPath,
                  (opd.methodName || opd.methodPath) + "（方法结果值）");
    } else if (opd.nodeType === "EXPRESSION") {
      const pv = opd.paramValue || {};
      Object.keys(pv).forEach(function (k) { collectOperandOps(pv[k], out); });
    } else if (opd.nodeType === "COMPARISON" || opd.nodeType === "LOGICAL") {
      // 嵌套布尔子树（"（…）等于 成立/不成立"）→ 复用节点级收集
      collectFieldOps(opd, out);
    } else if (opd.nodeType === "NESTING" && opd.nestingTree) {
      walkCalcTree(opd.nestingTree, out);
    } else if (opd.nodeType === "TREE_EX" && Array.isArray(opd.arr)) {
      opd.arr.forEach(function (t) { walkCalcTree(t, out); });
    } else if (Array.isArray(opd)) {
      opd.forEach(function (x) { collectOperandOps(x, out); });
    }
  }

  function walkCalcTree(t, out) {
    if (!t || typeof t !== "object") return;
    if (t.nodeType === "CALCULATE") {
      walkCalcTree(t.left, out);
      walkCalcTree(t.right, out);
      return;
    }
    collectOperandOps(t, out);
  }

  function pushFieldOp(out, fp, label) {
    if (fp && !out.some(function (x) { return x.fp === fp; }))
      out.push({ fp: fp, label: label });
  }

  // 深层写值：segs=["appbase","csubdpt"] → {appbase:{csubdpt:v}}
  function setDeep(obj, segs, v) {
    let cur = obj;
    for (let i = 0; i < segs.length - 1; i++) {
      if (typeof cur[segs[i]] !== "object" || cur[segs[i]] === null) cur[segs[i]] = {};
      cur = cur[segs[i]];
    }
    cur[segs[segs.length - 1]] = v;
  }

  // 填空值 → 最小 input 报文：
  //   普通字段按 fieldPath 嵌套；预定义组 → 规则预定义变量.别名.叶子（求值器 _locate a) 分支）
  function collectBlankInput() {
    const input = {};
    blankSync.forEach(function (els, fp) {
      const info = blankFields.get(fp);
      if (!info || !fp) return;
      const r = parseInputVal(els[0].value);
      if (r.err) throw new Error("『" + info.label + "』" + r.err);
      if (r.v === null) return;   // 留空不写入 → 求值时取值缺失（诊断可见）
      if (blankMethodKeys.has(fp)) {
        if (!input.__method_results__) input.__method_results__ = {};
        input.__method_results__[fp] = r.v;
      } else if (info.group) {
        if (!input["规则预定义变量"]) input["规则预定义变量"] = {};
        if (!info.rest.length) {
          // 两段引用：别名即叶子（如 指定'A费用上限'为…），值直接挂别名下
          input["规则预定义变量"][info.group] = r.v;
        } else {
          if (!input["规则预定义变量"][info.group])
            input["规则预定义变量"][info.group] = {};
          setDeep(input["规则预定义变量"][info.group], info.rest, r.v);
        }
      } else {
        setDeep(input, info.rest, r.v);
      }
    });
    return input;
  }

  document.getElementById("btnBlankTest").addEventListener("click", function () {
    if (!lastRuleContent) {
      alert("暂无规则条件（上传规则成功后自动生成填空）");
      return;
    }
    let input;
    try { input = collectBlankInput(); }
    catch (e) { blankHint.textContent = "存在填写错误：" + e.message; return; }
    if (!Object.keys(input).length) {
      blankHint.textContent = "请至少填写一个字段值";
      return;
    }
    blankHint.textContent = "本地求值中…";
    blankResultEl.className = "hit-result";
    fetch("/api/test_rule_local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ruleId: val("inRuleId"),
        ruleContent: lastRuleContent,
        input: input,
        predefineText: "",
      }),
    }).then(function (r) { return r.json(); }).then(function (data) {
      blankHint.textContent = "";
      if (!data || data.success === false) {
        blankResultEl.className = "hit-result show err";
        blankResultEl.textContent = "本地求值失败：" + (data && data.errMsg || "未知错误");
        return;
      }
      let t = "本地求值对照：" + (data.pass === true ? "命中 ✓" : "未命中 ✗");
      if (data.warnings && data.warnings.length)
        t += "\n" + data.warnings.map(function (w) { return "[诊断] " + w; }).join("\n");
      blankResultEl.className = "hit-result show " + (data.pass === true ? "hit" : "miss");
      blankResultEl.textContent = t + "\n" + (data.tree || "");
    }).catch(function (e) {
      blankHint.textContent = "";
      blankResultEl.className = "hit-result show err";
      blankResultEl.textContent = "请求异常：" + e.message;
    });
  });

  // ---------- 草稿持久化 ----------
  // 主系统导航无 keep-alive：切菜单会销毁重建本 iframe（页面必然刷新）。
  // 编辑态实时存 localStorage（对本页 origin 持久，独立于 iframe 生命周期），
  // 下次加载自动恢复，效果等同页面未被刷新。
  // 注意：token 不进草稿（敏感信息；iframe 重建时由主系统 URL 参数自动注入）。
  const DRAFT_KEY = "rule_editor_draft_v1";
  const DRAFT_FIELDS = ["inRuleId", "inRepo", "inCat", "inRuleName",
                        "inBomBase", "inBomModel", "inAppId", "inUrl"];
  let draftTimer = null;
  const draftHint = document.getElementById("draftHint");

  function collectDraft() {
    const d = { text: box.value, caret: box.selectionStart || 0, ts: Date.now() };
    DRAFT_FIELDS.forEach(function (id) {
      const el = document.getElementById(id);
      if (el) d[id] = el.value;
    });
    const adv = document.getElementById("advParams");
    if (adv) d._adv = adv.style.display !== "none";
    return d;
  }
  function saveDraftNow() {
    clearTimeout(draftTimer); draftTimer = null;
    try { localStorage.setItem(DRAFT_KEY, JSON.stringify(collectDraft())); }
    catch (e) { /* 无痕模式/配额满：静默降级为不持久化 */ }
  }
  function scheduleDraft() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(saveDraftNow, 400);
  }
  function clearDraft() {
    clearTimeout(draftTimer); draftTimer = null;
    try { localStorage.removeItem(DRAFT_KEY); } catch (e) {}
    if (draftHint) draftHint.style.display = "none";
  }
  function restoreDraft() {
    let d = null;
    try { d = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null"); } catch (e) { return; }
    // 无草稿或空文本草稿（用户主动删空后保存的）→ 保持空白
    if (!d || !d.text || !d.text.trim()) return;
    box.value = d.text;
    DRAFT_FIELDS.forEach(function (id) {
      const el = document.getElementById(id);
      // 仅填空值字段：config.yaml 预填值与 URL 参数优先于草稿
      if (el && d[id] != null && !el.value) el.value = d[id];
    });
    // 主区库名/分类名回显靠 input 事件驱动：草稿直接写 value 不触发，需补派发
    ["inRepo", "inCat"].forEach(function (id) {
      const el = document.getElementById(id);
      if (el) { try { el.dispatchEvent(new Event("input", { bubbles: true })); } catch (e) {} }
    });
    const adv = document.getElementById("advParams");
    if (adv && d._adv) adv.style.display = "";
    try { box.selectionStart = box.selectionEnd = Math.min(d.caret || 0, d.text.length); } catch (e) {}
    if (draftHint) {
      const age = document.getElementById("draftAge");
      if (age) age.textContent = d.ts ? new Date(d.ts).toLocaleString() : "上次会话";
      draftHint.style.display = "";
    }
  }
  DRAFT_FIELDS.forEach(function (id) {
    const el = document.getElementById(id);
    if (el) { el.addEventListener("input", scheduleDraft); el.addEventListener("change", scheduleDraft); }
  });
  // iframe 被销毁前（切菜单/关标签）400ms 防抖可能未触发 → 立即落盘兜底
  window.addEventListener("beforeunload", saveDraftNow);
  window.addEventListener("pagehide", saveDraftNow);
  if (draftHint) {
    const dropBtn = document.getElementById("btnDraftDrop");
    if (dropBtn) dropBtn.addEventListener("click", function () {
      clearDraft();
      resetPageState();   // 放弃草稿 = 整页从零开始（存储 + 内存派生态一并清）
      document.getElementById("btnClear").click();  // 复用清空逻辑（内部也会 clearDraft，幂等无害）
    });
  }

  // ---------- 整页状态快照（2026-09-16）----------
  // 草稿只管"编辑态"；上传成功后的派生产物——测试报文骨架、报文填值、
  // 填空模式 ruleContent/引用参数、已填的填空值、JSON 视图、滚动位置——
  // 是用户"上传后去别处查数据再回来填值"的工作现场，iframe 重载同样会丢，
  // 故单列一个快照 key（与草稿生命周期解耦：上传成功 clearDraft 删的是文本，
  // 此刻骨架刚生成、恰恰最新，必须 savePageNow 而非清除）。token 依旧不存。
  const PAGE_KEY = "rule_editor_page_v1";
  let PAGE_BLANK = null;   // 仅启动恢复时非空：填空主字段 {fp: 文本}（消费一次即置 null）
  let PAGE_AUX = null;     // 仅启动恢复时非空：aux 行按 DOM 顺序的文本数组
  let pageTimer = null;
  function collectPage() {
    const p = { ts: Date.now() };
    if (testInputSkeleton !== undefined) p.skeleton = testInputSkeleton;
    if (lastRuleContent !== undefined) p.ruleContent = lastRuleContent;
    if (lastThenElseRefs.length) p.thenRefs = lastThenElseRefs;
    if (lastPredefineRefs.length) p.pdRefs = lastPredefineRefs;
    if (showRawJson) p.rawJson = true;
    if (filledValues.size) {
      p.msgFilled = {};
      filledValues.forEach(function (v, k) { if (v !== "") p.msgFilled[k] = v; });
    }
    const bf = {};
    blankBox.querySelectorAll("input[data-blank]").forEach(function (inp) {
      if (inp.value.trim()) bf[inp.dataset.blank] = inp.value;
    });
    if (Object.keys(bf).length) p.blankFilled = bf;
    const ba = [];
    let anyBa = false;
    blankAux.forEach(function (inp) { ba.push(inp.value); if (inp.value.trim()) anyBa = true; });
    if (anyBa) p.blankAux = ba;
    p.scroll = window.scrollY || document.documentElement.scrollTop || 0;
    p.boxScroll = box.scrollTop || 0;
    // 本页 body 是 100vh flex 布局，实际滚动在 .left/.right 容器内（window 恒 0）
    const left = document.querySelector(".left"), right = document.querySelector(".right");
    p.leftScroll = left ? left.scrollTop : 0;
    p.rightScroll = right ? right.scrollTop : 0;
    return p;
  }
  function savePageNow() {
    clearTimeout(pageTimer); pageTimer = null;
    try { localStorage.setItem(PAGE_KEY, JSON.stringify(collectPage())); }
    catch (e) { /* 配额满/无痕：静默降级 */ }
  }
  function schedulePage() {
    clearTimeout(pageTimer);
    pageTimer = setTimeout(savePageNow, 400);
  }
  function clearPage() {
    clearTimeout(pageTimer); pageTimer = null;
    try { localStorage.removeItem(PAGE_KEY); } catch (e) {}
  }
  // 放弃草稿用：存储和"内存派生态"必须一起清——只 clearPage 的话，
  // 页面卸载时 unload 兜底 savePageNow 会把内存里的骨架/ruleContent 原样写回，清而不净。
  function resetPageState() {
    testInputSkeleton = undefined;
    lastRuleContent = undefined;
    lastThenElseRefs = [];
    lastPredefineRefs = [];
    filledValues.clear();
    showRawJson = false;
    renderTestEdit();     // 骨架没了 → 树视图隐藏
    renderBlankRule();    // 无 ruleContent/refs → 回到"上传成功后自动生成"空态
    if (testMsgEl) {
      testMsgEl.style.display = "";
      testMsgEl.className = "test-msg empty";
      testMsgEl.textContent = "（上传成功后自动生成）";
    }
    if (testMsgHint) testMsgHint.textContent = "";
    clearPage();
  }
  function restorePage() {
    let p = null;
    try { p = JSON.parse(localStorage.getItem(PAGE_KEY) || "null"); } catch (e) { return; }
    if (!p) return;
    if (p.skeleton !== undefined) testInputSkeleton = p.skeleton;
    if (p.ruleContent !== undefined) lastRuleContent = p.ruleContent;
    if (Array.isArray(p.thenRefs)) lastThenElseRefs = p.thenRefs;
    if (Array.isArray(p.pdRefs)) lastPredefineRefs = p.pdRefs;
    if (p.msgFilled) Object.keys(p.msgFilled).forEach(function (k) { filledValues.set(k, p.msgFilled[k]); });
    PAGE_BLANK = p.blankFilled || null;
    PAGE_AUX = Array.isArray(p.blankAux) ? p.blankAux : null;
    showRawJson = !!p.rawJson;
    if (testInputSkeleton !== undefined || (lastRuleContent && lastRuleContent.nodeType)) {
      renderTestEdit();
      renderBlankRule();
      PAGE_BLANK = null; PAGE_AUX = null;   // 一次性回填，防止污染下次新上传的重渲染
    }
    // 正文/预览是异步渲染的，页高先稳一稳再回滚（尽力而为，不被 scroll 事件反馈污染）
    setTimeout(function () {
      try {
        window.scrollTo(0, p.scroll || 0);
        box.scrollTop = p.boxScroll || 0;
        const left = document.querySelector(".left"), right = document.querySelector(".right");
        if (left) left.scrollTop = p.leftScroll || 0;
        if (right) right.scrollTop = p.rightScroll || 0;
      } catch (e) {}
    }, 250);
  }
  // 卸载兜底：与草稿同样，防切 iframe 瞬间 400ms 防抖未触发
  window.addEventListener("beforeunload", savePageNow);
  window.addEventListener("pagehide", savePageNow);

  // ---------- 服务重启 → 缓存作废（2026-09-16）----------
  // localStorage 存在浏览器磁盘，服务端进程重启清不掉它，也不该"堆积"
  // （固定就两个 key，永远是覆盖写）。但换 BOM/rule_model 后重启服务，
  // 旧快照里的骨架/填空卡片就成了陈旧现场。口径：后端每次进程启动生成
  // 随机 bootId，前端启动时比对——不一致即丢弃旧缓存；接口不可达则保留
  //（服务瞬时故障不能误删用户工作现场）。fetch 超时 1.5s 兜底照常恢复，
  // 迟到的比对结果不再回删（避免扫掉已展示的现场）。
  const BOOT_KEY = "rule_editor_boot_v1";
  function bootCheck(done) {
    let settled = false;
    const go = function () { if (!settled) { settled = true; done(); } };
    const timer = setTimeout(go, 1500);
    fetch("/api/boot_id")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        clearTimeout(timer);
        if (settled) return;
        try {
          if (d && d.bootId) {
            if (localStorage.getItem(BOOT_KEY) !== d.bootId) {
              localStorage.removeItem(DRAFT_KEY);
              try { localStorage.removeItem(PAGE_KEY); } catch (e) {}
            }
            localStorage.setItem(BOOT_KEY, d.bootId);
          }
        } catch (e) { /* 无痕/禁用存储：静默，恢复照常 */ }
        go();
      })
      .catch(function () { clearTimeout(timer); go(); });
  }

  // ---------- 初始化 ----------
  setMode("entry");
  bootCheck(function () {
    restoreDraft();   // 先恢复草稿，再由 updatePreview 按恢复后内容渲染预览
    restorePage();    // 再恢复测试报文骨架/填空卡片/滚动位置（与草稿相互独立）
    updatePreview();
  });
  // 2026-09-30：顶栏"已建立 N 条多级对象路径索引"统计已隐藏，
  // 对应的 /api/search?word=&limit=1 探活请求一并移除（status 不再显示"无法连接后端"）。

  status.textContent = "就绪 · 请输入规则";
})();
