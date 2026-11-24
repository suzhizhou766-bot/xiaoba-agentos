/* =========================================================
   7.9 Key 池：多个中转站合并成一个 Key 的实时拓扑（模块 keypool）
   写操作令牌在这个闭包里先取一份（下一段脚本会把页面上的令牌移除，
   内嵌的导演台与本页同源，也读不到这个闭包）。
   ========================================================= */
(() => {
  const meta = document.querySelector('meta[name="xiaoba-page-token"]');
  const TOKEN = meta ? meta.content : "";
  const NS = "http://www.w3.org/2000/svg";
  let W = 1030;   // 画布宽度：跟着拓扑面板的实际宽度走（不小于 1030），面板变宽时节点之间的空间跟着变大，而不是整体放大
  const NODE_W = 188, NODE_H = 60, GAP = 16, SIDE_W = 132;   // SIDE_W：节点右侧“首字 / 余额”读数的宽度
  const HUB = {w:196, h:150}, CLIENT = {w:148, h:76};
  const STATUS = {ok:["ok","正常"], idle:["ok","空闲"], busy:["busy","转发中"], cooling:["warn","冷却"],
                  down:["down","故障"], disabled:["disabled","停用"], unknown:["idle","未连接"],
                  quarantined:["quar","已隔离"], reauth:["warn","等待重新授权"]};
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const clamp = (v,a,b) => Math.max(a, Math.min(b, v));
  const clip = (s,n) => { s = String(s || ""); return s.length > n ? s.slice(0, n-1) + "…" : s; };
  const clock = sec => new Date(sec*1000).toLocaleTimeString("zh-CN", {hour12:false});

  let data = null, seq = 0, proxyStarted = null, first = true, busy = false, timer = null;
  let mode = "idle", selected = null, dragging = null, geo = null, layers = null, recent = [], secRecent = [];
  const nodeEls = new Map(), edgeEls = new Map(), packets = [], dots = [];
  let raf = 0, flashTimer = 0;

  async function api(path, body, raw=false){
    const init = {cache:"no-store"};
    if(body){ init.method = "POST"; init.headers = {"Content-Type":"application/json", "X-Xiaoba-Token":TOKEN}; init.body = JSON.stringify(body); }
    const res = await fetch(path, init);
    let payload = {};
    try{ payload = await res.json(); }catch(e){}
    if(!res.ok || (!raw && payload.ok === false)) throw new Error(payload.error || ("HTTP " + res.status));
    return payload;
  }

  // ---- 状态文字 ----
  function nodeStatus(n){
    if(!n.enabled) return "disabled";
    if(!data || !data.online) return "unknown";
    return (n.live && n.live.status) || "idle";
  }
  const errCode = e => String(e || "").split("：")[0] || "出错";
  const errText = e => { const c = errCode(e); return c === "network" ? "连接失败" : (c === "401" || c === "403") ? "Key 无效或无额度" : c; };
  const left = s => s >= 60 ? Math.ceil(s/60) + " 分" : (s || 0) + " s";
  function nodeSub(n){
    const s = nodeStatus(n), L = n.live || {};
    if(s === "disabled") return "已停用";
    if(s === "reauth") return "地址换了站点 · 旧 Key 已停用 · 重新填 Key 恢复";
    if(s === "quarantined") return `安全拦截 · ${L.quarantineReason || "危险操作"}`;
    if(s === "unknown") return "代理未连接";
    if(s === "cooling") return `${errCode(L.lastError)} · 冷却 ${left(L.cooldownLeft)}`;
    if(s === "down") return `${errText(L.lastError)} · ${left(L.cooldownLeft)}`;
    const conc = `${L.inflight || 0}/${n.maxConcurrency || "∞"}`;
    if(L.breaker === "half") return `${conc} · 试探恢复中`;
    if(L.failStreak) return `${conc} · 连续失败 ${L.failStreak} 次`;
    if(L.ttfbMs != null) return `${conc} · 首字 ${dur(L.ttfbMs)}${L.lastBuffered ? " 整段" : ""}`;
    return `${conc} · ${L.rpm || 0} 次/分`;
  }
  // 毫秒 → 简短时长：820ms / 3.4s / 287s / 12m
  const dur = ms => ms == null ? "—" : ms < 1000 ? ms + "ms" : ms < 10000 ? (ms/1000).toFixed(1) + "s"
                  : ms < 600000 ? Math.round(ms/1000) + "s" : Math.round(ms/60000) + "m";

  // ---- 几何：格位。列从右往左数、行从上往下数，节点只落在格位上（磁性排布），
  //      所以位置不随面板宽度、节点数量变化而乱动；存的是第几列第几行，不是像素 ----
  const ROW_H = NODE_H + GAP, COL_W = NODE_W + SIDE_W + GAP;
  const slotOf = n => { const v = n && n.slot; return v && Number.isInteger(v.col) && Number.isInteger(v.row) && v.col >= 0 && v.row >= 0 ? v : null; };
  function assignSlots(nodes, maxCol){
    const taken = new Set(), out = {};
    const put = (id, col, row) => { col = clamp(col, 0, maxCol); while(taken.has(col + "," + row)) row++; taken.add(col + "," + row); out[id] = {col, row}; };
    nodes.forEach(n => { const v = slotOf(n); if(v) put(n.id, v.col, v.row); });                       // 摆过的：原位
    nodes.forEach(n => { if(!out[n.id] && n.pos) put(n.id, 0, Math.max(0, Math.round(((+n.pos.y || 0) - 44) / ROW_H))); });   // 旧的像素位置：归到最近的一行
    nodes.forEach(n => { if(!out[n.id]) put(n.id, 0, 0); });                                             // 没摆过的：自上而下填空位
    return out;
  }
  function computeGeo(){
    const nodes = data ? data.nodes : [];
    const hubX = Math.round(W * 0.29), col0 = W - NODE_W - SIDE_W - 12;
    const maxCol = Math.max(0, Math.floor((col0 - (hubX + HUB.w + 48)) / COL_W));   // 面板够宽时才有第二、第三列
    const slots = assignSlots(nodes, maxCol);
    let last = -1; nodes.forEach(n => { last = Math.max(last, slots[n.id].row); });
    const rows = last + 2;                                                          // 末尾留一行给「添加节点」，也是可以拖过去的空位
    const H = Math.max(480, 72 + rows * ROW_H), top0 = (H - rows * ROW_H + GAP) / 2;
    const at = v => ({x:col0 - v.col * COL_W, y:top0 + v.row * ROW_H});
    const g = {H, rows, maxCol, top0, col0, slots, at, client:{x:20, y:H/2 - CLIENT.h/2, ...CLIENT}, hub:{x:hubX, y:H/2 - HUB.h/2, ...HUB},
               nodes:{}, ghost:at({col:0, row:last + 1})};
    nodes.forEach(n => { g.nodes[n.id] = dragging && dragging.id === n.id && dragging.moved ? dragging.pos : at(slots[n.id]); });
    return g;
  }
  const slotAt = p => ({col:clamp(Math.round((geo.col0 - p.x) / COL_W), 0, geo.maxCol), row:clamp(Math.round((p.y - geo.top0) / ROW_H), 0, geo.rows - 1)});
  // 刚拖好、服务器还没确认的格位：这期间的刷新不许把节点拉回旧位置
  const pending = new Map();
  function applyPending(){
    for(const [id, p] of pending){
      const n = data.nodes.find(x => x.id === id), v = slotOf(n);
      if(!n || Date.now() > p.until || (v && v.col === p.slot.col && v.row === p.slot.row)) pending.delete(id);
      else n.slot = p.slot;
    }
  }
  function curve(x0, y0, x1, y1){ const dx = Math.max(40, Math.abs(x1 - x0) / 2); return [x0, y0, x0 + dx, y0, x1 - dx, y1, x1, y1]; }
  function at(c, t){ const u = 1 - t, a = u*u*u, b = 3*u*u*t, d = 3*u*t*t, e = t*t*t;
    return [a*c[0] + b*c[2] + d*c[4] + e*c[6], a*c[1] + b*c[3] + d*c[5] + e*c[7]]; }
  const dOf = c => `M${c[0]} ${c[1]} C${c[2]} ${c[3]} ${c[4]} ${c[5]} ${c[6]} ${c[7]}`;
  const edgeOf = id => { const p = geo.nodes[id], h = geo.hub; return curve(h.x + h.w, h.y + h.h/2, p.x, p.y + NODE_H/2); };
  const clientEdge = () => { const c = geo.client, h = geo.hub; return curve(c.x + c.w, c.y + c.h/2, h.x, h.y + h.h/2); };
  const strokeW = (inflight, rpm, base) => Math.min(8, base + (inflight || 0) * 0.9 + (rpm || 0) / 12).toFixed(1);

  // ---- 绘制（按编号增量更新，拖动、点击、键盘焦点都不会被刷新打断）----
  function svgEl(tag, attrs, parent){ const e = document.createElementNS(NS, tag); for(const k in attrs) e.setAttribute(k, attrs[k]); if(parent) parent.appendChild(e); return e; }
  function label(parent, x, y, cls, str){ const t = svgEl("text", {x, y, class:cls}, parent); t.textContent = str || ""; return t; }
  function ensureLayers(){
    if(layers) return layers;
    const svg = el("kpSvg");
    layers = {edges:svgEl("g", {}, svg), pkts:svgEl("g", {"aria-hidden":"true"}, svg), nodes:svgEl("g", {}, svg)};
    layers.drop = svgEl("rect", {class:"kp-drop", width:NODE_W, height:NODE_H, rx:12, display:"none", "aria-hidden":"true"}, layers.nodes);   // 拖动时的落点轮廓
    const client = svgEl("g", {class:"kp-client"}, layers.nodes);
    svgEl("rect", {class:"box", width:CLIENT.w, height:CLIENT.h, rx:14}, client);
    label(client, 16, 33, "name", "客户端"); label(client, 16, 53, "sub", "本机应用");
    const hub = svgEl("g", {class:"hub"}, layers.nodes);
    svgEl("rect", {width:HUB.w, height:HUB.h, rx:22}, hub);
    label(hub, 20, 34, "eyebrow", "KEY POOL"); label(hub, 20, 62, "big", "合并入口");
    layers.hubPort = label(hub, 20, 86, "mono", ""); layers.hubStat = label(hub, 20, 120, "stat", "");
    const ghost = svgEl("g", {class:"ghost", tabindex:"0", role:"button", "aria-label":"添加节点"}, layers.nodes);
    svgEl("rect", {width:NODE_W, height:NODE_H, rx:12}, ghost);
    const gt = label(ghost, NODE_W/2, NODE_H/2 + 5, "", "＋ 添加节点"); gt.setAttribute("text-anchor", "middle");
    ghost.addEventListener("click", openAdd);
    ghost.addEventListener("keydown", ev => { if(ev.key === "Enter" || ev.key === " "){ ev.preventDefault(); openAdd(); } });
    Object.assign(layers, {client, hub, ghost, clientEdge:svgEl("path", {class:"edge"}, layers.edges)});
    return layers;
  }
  let stageSeen = 0;
  new ResizeObserver(() => { const w = el("kpSvg").parentNode.clientWidth; if(w !== stageSeen){ stageSeen = w; requestAnimationFrame(render); } }).observe(el("kpSvg").parentNode);
  function render(){
    if(!data) return;
    W = Math.max(1030, Math.round(el("kpSvg").parentNode.clientWidth - 20));
    geo = computeGeo();
    const L = ensureLayers(), t = data.totals || {};
    el("kpSvg").setAttribute("viewBox", `0 0 ${W} ${geo.H}`);
    L.client.setAttribute("transform", `translate(${geo.client.x} ${geo.client.y})`);
    L.hub.setAttribute("transform", `translate(${geo.hub.x} ${geo.hub.y})`);
    L.ghost.setAttribute("transform", `translate(${geo.ghost.x} ${geo.ghost.y})`);
    L.hubPort.textContent = ":" + (data.endpoint || "").split(":").pop() + " · 最空闲优先";
    L.hubStat.textContent = `${t.inflight || 0} 并发 · ${t.rpm || 0} 次/分`;
    L.clientEdge.setAttribute("d", dOf(clientEdge()));
    L.clientEdge.setAttribute("class", "edge" + (t.inflight || t.rpm ? " hot" : ""));
    L.clientEdge.setAttribute("stroke-width", strokeW(t.inflight, t.rpm, 2));
    const alive = new Set();
    data.nodes.forEach(n => {
      alive.add(n.id);
      const s = nodeStatus(n), live = n.live || {};
      let edge = edgeEls.get(n.id);
      if(!edge){ edge = svgEl("path", {}, L.edges); edgeEls.set(n.id, edge); }
      edge.setAttribute("d", dOf(edgeOf(n.id)));
      edge.setAttribute("class", "edge " + ({ok:"hot", busy:"hot", idle:"", cooling:"warn", down:"down", disabled:"off", unknown:"off"}[s] || ""));
      edge.setAttribute("stroke-width", ["ok", "busy", "idle"].includes(s) ? strokeW(live.inflight, live.rpm, 1.5) : 1.4);
      let rec = nodeEls.get(n.id);
      if(!rec){
        const g = svgEl("g", {class:"kp-node", tabindex:"0", role:"button", "data-id":n.id}, L.nodes);
        svgEl("rect", {class:"box", width:NODE_W, height:NODE_H, rx:12}, g);
        svgEl("circle", {class:"sdot", cx:18, cy:NODE_H/2, r:4.5}, g);
        rec = {g, name:label(g, 32, 26, "name", ""), sub:label(g, 32, 45, "sub", "")};
        // 节点右侧的两行读数：首字响应、账户余额。各自悬停显示详情。
        [["speed", 0, "首字"], ["money", NODE_H/2, "余额"]].forEach(([kind, y, cap]) => {
          const side = svgEl("g", {class:"kp-side " + kind}, g);
          svgEl("rect", {x:NODE_W + 4, y:y + 2, width:SIDE_W - 8, height:NODE_H/2 - 4, rx:8}, side);
          rec[kind + "Cap"] = label(side, NODE_W + 12, y + 20, "cap", cap);
          rec[kind] = label(side, NODE_W + SIDE_W - 12, y + 20, "val", "—");
          rec[kind].setAttribute("text-anchor", "end");
          side.addEventListener("pointerenter", ev => showTip(n.id, kind, ev));
          side.addEventListener("pointermove", ev => moveTip(ev));
          side.addEventListener("pointerleave", hideTip);
        });
        bindNode(g, n.id);
        nodeEls.set(n.id, rec);
        L.nodes.insertBefore(g, L.ghost);
      }
      const on = mode === "edit" && selected === n.id;
      rec.g.setAttribute("class", `kp-node ${STATUS[s][0]}${on ? " sel" : ""}${dragging && dragging.id === n.id && dragging.moved ? " dragging" : ""}`);
      const p = geo.nodes[n.id];
      rec.g.setAttribute("transform", `translate(${p.x} ${p.y})`);
      rec.name.textContent = clip(n.name, 11) + (Number(n.weight) !== 1 ? `  ×${n.weight}` : "");
      rec.sub.textContent = nodeSub(n);
      rec.moneyCap.textContent = routeNow(n) ? "走" + ROUTE[routeNow(n)] : "余额";
      const speed = live.ttfbMs != null ? dur(live.ttfbMs) : "—", bal = balanceShort(n);
      if(rec.speed.textContent !== speed) rec.speed.textContent = speed;
      if(rec.money.textContent !== bal.text) rec.money.textContent = bal.text;
      rec.money.setAttribute("class", "val " + bal.cls);
      rec.g.setAttribute("aria-label", `${n.name}：${STATUS[s][1]}，${nodeSub(n)}。回车打开编辑`);
      rec.g.setAttribute("aria-pressed", String(on));
    });
    for(const [id, rec] of nodeEls) if(!alive.has(id)){ rec.g.remove(); nodeEls.delete(id); }
    for(const [id, edge] of edgeEls) if(!alive.has(id)){ edge.remove(); edgeEls.delete(id); }
    refreshTip();
    loop();
  }

  // ---- 拖动与点选 ----
  function toSvg(ev){ const svg = el("kpSvg"), pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
    const r = pt.matrixTransform(svg.getScreenCTM().inverse()); return {x:r.x, y:r.y}; }
  function bindNode(g, id){
    g.addEventListener("pointerdown", ev => {
      if(ev.button !== 0 || !geo) return;
      const p = geo.nodes[id];
      dragging = {id, start:toSvg(ev), origin:{...p}, pos:{...p}, moved:false};
      g.setPointerCapture(ev.pointerId);
    });
    g.addEventListener("pointermove", ev => {
      if(!dragging || dragging.id !== id) return;
      const pt = toSvg(ev), dx = pt.x - dragging.start.x, dy = pt.y - dragging.start.y;
      if(!dragging.moved && Math.hypot(dx, dy) < 4) return;
      dragging.moved = true;
      dragging.pos = {x:clamp(dragging.origin.x + dx, 0, W - NODE_W - SIDE_W), y:clamp(dragging.origin.y + dy, 0, geo.H - NODE_H)};
      geo.nodes[id] = dragging.pos;
      g.setAttribute("transform", `translate(${dragging.pos.x} ${dragging.pos.y})`);
      g.classList.add("dragging");
      const spot = geo.at(slotAt(dragging.pos));
      layers.drop.setAttribute("transform", `translate(${spot.x} ${spot.y})`); layers.drop.removeAttribute("display");
      const edge = edgeEls.get(id); if(edge) edge.setAttribute("d", dOf(edgeOf(id)));
    });
    g.addEventListener("pointerup", async () => {
      if(!dragging || dragging.id !== id) return;
      const d = dragging; dragging = null; g.classList.remove("dragging"); layers.drop.setAttribute("display", "none");
      if(!d.moved){ select(id); return; }
      // 吸附到最近的格位；那里已有节点就和它对调。第一次拖动时把所有节点现在的格位一起存下，之后互不挤动
      const target = slotAt(d.pos), from = geo.slots[id], next = {}, changed = {};
      data.nodes.forEach(n => { next[n.id] = {...geo.slots[n.id]}; });
      const other = data.nodes.find(n => n.id !== id && next[n.id].col === target.col && next[n.id].row === target.row);
      if(other) next[other.id] = {...from};
      next[id] = target;
      data.nodes.forEach(n => {
        const v = slotOf(n), t = next[n.id];
        if(!v || v.col !== t.col || v.row !== t.row){ changed[n.id] = t; pending.set(n.id, {slot:t, until:Date.now() + 8000}); }
        n.slot = t;
      });
      render();
      if(!Object.keys(changed).length) return;
      if(!data.slotLayout){ Object.keys(changed).forEach(k => pending.delete(k)); flash("位置没保存：重启监看中心后才支持格位排布", true); return; }
      try{ await api("api/keypool/layout", {positions:changed}); }
      catch(e){ Object.keys(changed).forEach(k => pending.delete(k)); flash("位置没保存：" + e.message, true); }
    });
    g.addEventListener("pointercancel", () => { dragging = null; g.classList.remove("dragging"); layers.drop.setAttribute("display", "none"); render(); });
    g.addEventListener("keydown", ev => { if(ev.key === "Enter" || ev.key === " "){ ev.preventDefault(); select(id); } });
  }

  // ---- 请求流动的小圆点 ----
  function spawn(id, err){ if(reduce.matches) return; packets.push({id, born:performance.now(), dur:900, err}); if(packets.length > 60) packets.shift(); loop(); }
  function loop(){ if(!raf && !reduce.matches) raf = requestAnimationFrame(frame); }
  function frame(now){
    raf = 0;
    if(!geo || !layers || !data || Tabs.current !== "keypool" || document.hidden) return;
    const want = [];
    for(let i = packets.length - 1; i >= 0; i--){
      const p = packets[i], t = (now - p.born) / p.dur;
      if(t >= 1 || !geo.nodes[p.id]){ packets.splice(i, 1); continue; }
      want.push([edgeOf(p.id), t, p.err]);
    }
    data.nodes.forEach(n => { const k = Math.min(3, (n.live && n.live.inflight) || 0);
      for(let j = 0; j < k; j++) want.push([edgeOf(n.id), (now/1600 + j/k) % 1, false]); });
    const k = Math.min(3, (data.totals && data.totals.inflight) || 0);
    for(let j = 0; j < k; j++) want.push([clientEdge(), (now/900 + j/k) % 1, false]);
    while(dots.length < want.length) dots.push(svgEl("circle", {r:4, class:"pkt"}, layers.pkts));
    dots.forEach((d, i) => {
      const w = want[i];
      if(!w){ d.setAttribute("visibility", "hidden"); return; }
      const [x, y] = at(w[0], w[1]);
      d.setAttribute("cx", x.toFixed(1)); d.setAttribute("cy", y.toFixed(1));
      d.setAttribute("class", w[2] ? "pkt err" : "pkt"); d.setAttribute("visibility", "visible");
    });
    if(want.length) loop();
  }

  // ---- 顶部、指标、提示 ----
  function flash(text, err=false){
    const m = el("kpTopMsg"); m.textContent = text; m.className = "kp-msg kp-top-msg " + (err ? "err" : "ok");
    clearTimeout(flashTimer); flashTimer = setTimeout(() => { m.textContent = ""; }, 5000);
  }
  function renderTop(){
    const h = data.health || {}, online = data.online;
    const cls = !online ? (h.status === "failed" ? "down" : "warn") : "ok";
    el("kpProxyDot").className = "dot " + cls + (online ? " live" : "");
    el("kpProxyText").textContent = online ? "代理运行中" : (h.status === "starting" ? "代理启动中" : "代理未运行");
    el("kpEndpoint").textContent = (data.endpoint || "").replace("http://", "");
    el("kpKeyMasked").textContent = data.masterKeyMasked || "—";
    let text = "", info = false, restart = false;
    if(!online){ text = "Key 池代理没有在运行：" + (h.detail || "未知原因") + "。请求暂时转发不了。"; restart = h.status !== "starting"; }
    else if(data.configError){ text = data.configError; }
    else if(!data.nodes.length){ text = "还没有节点。点「＋ 添加节点」接入第一个中转站，填好后建议先测试连通。"; info = true; }
    else if(!data.nodes.some(n => ["ok", "idle", "busy"].includes(nodeStatus(n)))){ text = "现在没有可用节点：请求会返回错误。检查节点是否被停用、Key 是否有效。"; }
    el("kpBanner").hidden = !text;
    el("kpBanner").classList.toggle("info", info);
    el("kpBannerText").textContent = text;
    el("kpRestart").hidden = !restart;
  }
  function renderKpis(){
    const t = data.totals || {}, nodes = data.nodes, enabled = nodes.filter(n => n.enabled);
    const usable = nodes.filter(n => ["ok", "idle", "busy"].includes(nodeStatus(n))).length;
    const trouble = nodes.filter(n => ["cooling", "down"].includes(nodeStatus(n))).length;
    const capped = enabled.length && enabled.every(n => n.maxConcurrency);
    const cap = capped ? enabled.reduce((s, n) => s + n.maxConcurrency, 0) : 0;
    const rate = t.successRate;
    const rows = [
      ["当前并发", data.online ? (t.inflight || 0) : "—", cap ? "/ " + cap + " 上限" : "", ""],
      ["请求 / 分钟", data.online ? (t.rpm || 0) : "—", "", ""],
      ["成功率 · 近 10 分钟", rate == null ? "—" : rate, rate == null ? "" : "%", rate != null && rate < 90 ? "warn" : ""],
      ["可用节点", usable, "/ " + nodes.length + (trouble ? ` · ${trouble} 冷却或故障` : ""), nodes.length && !usable ? "crit" : trouble ? "warn" : ""]];
    // 卡片只建一次，之后原地改文字：整块重建会让卡片的入场动画每秒重放一次（看起来在闪）
    const box = el("kpKpis");
    if(box.children.length !== rows.length){
      box.innerHTML = rows.map(() => `<div class="kpi"><span class="label"></span><span class="val num"><span></span><small></small></span></div>`).join("");
    }
    rows.forEach(([labelText, val, small, cls], i) => {
      const card = box.children[i], parts = card.querySelector(".val").children;
      const set = (node, text) => { text = String(text); if(node.textContent !== text) node.textContent = text; };
      set(card.querySelector(".label"), labelText); set(parts[0], val); set(parts[1], small);
      parts[1].hidden = !small;
      const want = "kpi" + (cls ? " " + cls : "");
      if(card.className !== want) card.className = want;
    });
  }
  function renderEvents(){
    const list = recent.slice(-10).reverse();
    el("kpEvents").innerHTML = list.length ? list.map(e => `<li><span class="t">${clock(e.t)}</span>
      <span class="nm" title="${esc(e.asked && e.asked !== e.model ? "客户端要的是 " + e.asked + "，实际发的是 " + e.model : e.model || "")}">${esc(e.nodeName || "无可用节点")}${e.model ? ` · ${esc(e.model)}${e.asked && e.asked !== e.model ? "（要的是 " + esc(e.asked) + "）" : ""}` : ""}${e.route ? ` · ${ROUTE[e.route]}` : ""}${e.attempt > 1 ? ` · 第${e.attempt}次` : ""}</span>
      <span class="st${e.ok ? "" : " err"}">${esc(e.status === "network" ? "断开" : e.status)}</span>
      <span class="ms${e.buffered ? " slow" : ""}" title="首字 / 总耗时${e.buffered ? "（整段返回）" : ""}">${e.total != null ? dur(e.ttfb) + " / " + dur(e.total) : dur(e.ms)}</span></li>`).join("")
      : `<li class="empty">还没有请求。客户端接上合并 Key 后，这里会实时滚动。</li>`;
  }

  // ---- 安全检测面板 ----
  const SEC_TAG = {blocked:["blocked","拦截"], warned:["warned","告警"], masked:["masked","打码"], checked:["", "检查"]};
  function initSecurity(){
    el("kpSec").innerHTML = `
      <div class="kp-sec-head">
        <span class="kp-shield" aria-hidden="true"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z"/><path d="M9 12l2 2 4-4"/></svg></span>
        <div><b>安全检测</b><div class="sub" id="kpSecSub">逐条检查回复里的工具调用，拦住中转站塞进来的危险操作</div></div>
        <div class="kp-sec-counts" id="kpSecCounts"></div>
        <div class="kp-toggles">
          <label class="kp-switch"><input type="checkbox" id="kpGuard">启用拦截</label>
          <label class="kp-switch"><input type="checkbox" id="kpMask">请求密钥自动打码</label>
        </div>
      </div>
      <div class="kp-quar" id="kpQuar" hidden></div>
      <div class="kp-quar" id="kpReauth" hidden></div>
      <ul class="kp-seclist" id="kpSecList"></ul>`;
    el("kpGuard").addEventListener("change", async e => {
      try{ await api("api/keypool/settings", {guardEnabled:e.target.checked}); flash(e.target.checked?"已开启安全检测":"已关闭安全检测（回复不再检查）", !e.target.checked); refresh(); }
      catch(err){ flash("设置失败："+err.message, true); e.target.checked=!e.target.checked; }
    });
    el("kpMask").addEventListener("change", async e => {
      try{ await api("api/keypool/settings", {maskSecrets:e.target.checked}); flash(e.target.checked?"请求里的密钥将自动打码":"改为只告警、不打码"); refresh(); }
      catch(err){ flash("设置失败："+err.message, true); e.target.checked=!e.target.checked; }
    });
    el("kpQuar").addEventListener("click", async e => {
      const btn = e.target.closest("button[data-id]"); if(!btn) return;
      btn.disabled = true;
      try{ await api("api/keypool/release", {id:btn.dataset.id}); flash("已放行，节点恢复参与分流"); refresh(); }
      catch(err){ flash("放行失败："+err.message, true); btn.disabled=false; }
    });
  }
  function renderSecurity(){
    if(!el("kpSecCounts")) return;
    const on = data.guardEnabled !== false, sec = data.security || {counts:{}, events:[]}, c = sec.counts || {};
    el("kpSecSub").innerHTML = on ? "逐条检查回复里的工具调用，拦住中转站塞进来的危险操作"
      : `<span class="kp-off">安全检测已关闭，回复不再检查。</span>`;
    const g = el("kpGuard"), m = el("kpMask");
    if(document.activeElement !== g) g.checked = on;
    if(document.activeElement !== m) m.checked = (data.settings||{}).maskSecrets !== false;
    const count = (key, cls) => { const [c2, label] = SEC_TAG[key]; return `<span class="kp-count ${cls||""}"><b>${c[key]||0}</b>${label}</span>`; };
    el("kpSecCounts").innerHTML = count("checked") + count("blocked","block") + count("warned","warn") + count("masked","mask");
    // 拦截、告警涉及的工具名和请求接口：用来分辨是真篡改还是工具声明没认全
    const secExtra = e => {
      const tools = [...new Set((e.findings||[]).map(f => f.tool).filter(Boolean))];
      const bits = [];
      if(tools.length) bits.push("工具：" + tools.slice(0,4).join("、") + (tools.length > 4 ? " 等 " + tools.length + " 个" : ""));
      if(e.path) bits.push("接口：" + String(e.path).split("?")[0]);
      const snip = (e.findings||[]).map(f => f.snippet).find(Boolean);
      if(snip) bits.push("命中：" + snip);
      return bits.length ? ` <span class="muted">（${esc(bits.join("；"))}）</span>` : "";
    };
    const quar = data.nodes.filter(n => nodeStatus(n) === "quarantined");
    const qbox = el("kpQuar");
    qbox.hidden = !quar.length;
    if(quar.length) qbox.innerHTML = quar.map(n =>
      `<span>⚠️ <b>${esc(n.name)}</b> 因 ${esc((n.live&&n.live.quarantineReason)||"危险操作")} 被隔离，已停止分流。确认无害后可放行。</span><button class="btn" type="button" data-id="${esc(n.id)}">放行</button>`).join("");
    // 地址换到别的站点、Key 还没重填：旧 Key 停用，不发请求（只影响这一个节点）
    const hold = data.nodes.filter(n => nodeStatus(n) === "reauth");
    const hbox = el("kpReauth");
    if(hbox){
      hbox.hidden = !hold.length;
      if(hold.length) hbox.innerHTML = hold.map(n =>
        `<span>🔒 <b>${esc(n.name)}</b> ${esc((n.live&&n.live.reauthReason)||"地址换了站点")}；重新填写 Key 才会恢复发请求。</span>`).join("");
    }
    const list = secRecent.slice(-12).reverse();
    el("kpSecList").innerHTML = list.length ? list.map(e => { const [cls] = SEC_TAG[e.kind] || ["",""];
      return `<li><span class="t">${clock(e.t)}</span><span class="tag ${e.kind}">${(SEC_TAG[e.kind]||["","事件"])[1]}</span>
      <span class="d">${e.nodeName?esc(e.nodeName)+" · ":""}${esc(e.detail||"")}${secExtra(e)}</span></li>`; }).join("")
      : `<li class="empty">暂无安全事件。拦截、告警、打码都会记在这里。</li>`;
  }

  // ---- 右侧面板：使用说明 / 编辑节点 / 添加节点 ----
  const FS_KEY = "xiaoba.keypool.sections";   // 记住抽屉里哪些分组是展开的
  function renderPanel(){
    const n = mode === "edit" && data ? data.nodes.find(x => x.id === selected) : null;
    const open = mode === "add" || !!n, drawer = el("kpDrawer");
    drawer.hidden = el("kpBack").hidden = !open;
    if(open){
      drawer.innerHTML = formHtml(n); bindForm(n); renderLive();
      let saved = {}; try{ saved = JSON.parse(localStorage.getItem(FS_KEY) || "{}"); }catch(e){}
      drawer.querySelectorAll("details[data-fs]").forEach(d => {
        if(d.dataset.fs in saved) d.open = !!saved[d.dataset.fs];
        d.addEventListener("toggle", () => { saved[d.dataset.fs] = d.open; try{ localStorage.setItem(FS_KEY, JSON.stringify(saved)); }catch(e){} });
      });
    }else{ mode = "idle"; selected = null; drawer.innerHTML = ""; }
    renderNodes(); renderBand();
  }
  function closeDrawer(){ mode = "idle"; selected = null; renderPanel(); render(); }
  function renderBand(){
    const box = el("kpPanel");
    const ep = data ? data.endpoint : "http://127.0.0.1:8780";
    const html = `<div class="kp-idle"><div><h3>怎么用</h3>
      <p class="kp-howto">客户端里的 API 地址填 <code>${esc(ep)}</code>（要求带 /v1 的填 <code>${esc(ep)}/v1</code>），
      API Key 填上方的<b>合并 Key</b>（点「复制」）。OpenAI 格式与 Anthropic 格式都可以。</p>
      <p class="kp-howto">每个请求会交给当前最空闲的节点；某个节点限流、出错或连不上时，自动换下一个，出错的节点先冷却一会儿。</p>
      <p class="kp-howto">启用、停用直接点上方「节点」列表里的开关；点节点名或拓扑里的节点打开设置；拖动拓扑里的节点可以调整位置。</p>` + stationsHtml() + `</div><div>` + publishedHtml() + `</div></div>`;
    // 站点的实时情况（在途数、限流暂停倒计时）单独更新，不触发重画
    const live = () => ((data && data.stations) || []).forEach(st => {
      const e = [...box.querySelectorAll("[data-st-live]")].find(x => x.dataset.stLive === st.name); if(!e) return;
      e.textContent = `${st.nodes} 个节点 · ${st.inflight || 0} 个在途` + (st.holdLeft ? ` · 被对方按 IP 限流，整个站暂停中，还有 ${st.holdLeft} 秒` : "");
      e.classList.toggle("hold", !!st.holdLeft);
    });
    if(html === idleHtml){ live(); return; }      // 内容没变就不重画：正在勾选或填数字时不会被每秒的刷新打断
    idleHtml = box.innerHTML = html; live();
    box.querySelectorAll(".kp-st button").forEach(b => b.addEventListener("click", async () => {
      const row = b.closest(".kp-st"), val = f => row.querySelector(`[data-f="${f}"]`).value || 0;
      try{ await api("api/keypool/stations", {station:row.dataset.st, maxConcurrency:val("cap"), minIntervalSeconds:val("gap")}); flash(`已保存 ${row.dataset.st} 的限速，代理已自动应用`); }
      catch(e){ flash(e.message, true); }
      idleHtml = ""; refresh();
    }));
    box.querySelectorAll("[data-pub]").forEach(c => c.addEventListener("change", () =>
      savePublished([...box.querySelectorAll("[data-pub]")].filter(x => x.checked).map(x => x.dataset.pub))));
    const all = el("kpPubAll"); if(all) all.addEventListener("click", () => savePublished([]));
    const reread = el("kpPubRead"); if(reread) reread.addEventListener("click", async () => {
      reread.disabled = true;
      try{ const r = await api("api/keypool/models/refresh", {}); flash(`已重读模型清单：${r.refreshed} 路成功${r.failed ? "，" + r.failed + " 路失败（多半是限流，稍后会自动再读）" : ""}`, !!r.failed); refresh(); }
      catch(e){ flash(e.message, true); reread.disabled = false; }
    });
  }
  // 站点限速：对方按客户端 IP 限流，所以是整个站一起算，和这个站有几个节点无关
  function stationsHtml(){
    const list = (data && data.stations) || [];
    if(!list.length) return "";
    return `<h3 style="margin-top:16px">站点限速</h3>
      <p class="kp-howto" title="超出的请求先交给别的站，没有别的站能接就排队">对方按 IP 限流，所以整个站一起算，和节点数无关。0 表示不限。</p>`
      + list.map(st => `<div class="kp-st" data-st="${esc(st.name)}"><div class="kp-st-head"><b>${esc(st.name)}</b><span class="kp-st-live" data-st-live="${esc(st.name)}"></span></div>
        <label>同时最多<input type="number" data-f="cap" min="0" max="1000" step="1" value="${esc(st.maxConcurrency || 0)}"></label>
        <label>间隔（秒）<input type="number" data-f="gap" min="0" max="600" step="0.5" value="${esc(st.minIntervalSeconds || 0)}"></label>
        <button class="kp-chipbtn" type="button">保存</button></div>`).join("");
  }
  // 对外公布的模型：客户端向 Key 池读模型列表时看到的清单
  let idleHtml = "";
  function publishedHtml(){
    if(!data) return "";
    const pub = data.publishedModels || [], cat = data.modelCatalog || [], known = new Set(cat.map(m => m.id));
    const rows = cat.concat(pub.filter(m => !known.has(m)).map(id => ({id, routes:0})));
    return `<h3>对外公布的模型</h3>
      <p class="kp-howto">客户端读模型列表时只看到勾选的这些。客户端选哪个模型，请求就只分给有这个模型的线路，模型名不改。${pub.length ? "" : "<b>现在公布全部。</b>"}</p>
      ${rows.length ? `<div class="kp-pub">` + rows.map(m => `<label class="kp-switch" title="${esc(m.id)}"><input type="checkbox" data-pub="${esc(m.id)}"${!pub.length || pub.includes(m.id) ? " checked" : ""}><span class="mono">${esc(m.id)}</span><span class="kp-hint" style="margin:0">${m.routes ? m.routes + " 路" : "无线路"}</span></label>`).join("") + `</div>`
        : `<p class="kp-howto">还没有读到任何线路的模型清单。</p>`}
      <div class="kp-actions" style="margin-top:8px"><button class="btn" type="button" id="kpPubRead">重读模型清单</button>${pub.length ? `<button class="btn" type="button" id="kpPubAll">公布全部</button>` : ""}</div>`;
  }
  async function savePublished(models){
    try{ await api("api/keypool/published-models", {models}); flash(models.length ? `已公布 ${models.length} 个模型` : "已改为公布全部模型"); }
    catch(e){ flash(e.message, true); }
    idleHtml = ""; refresh();
  }
  function formHtml(n){
    const st = n ? STATUS[nodeStatus(n)] : null;
    const sum = {
      routes: n ? [n.hasKey && "套餐 Key", n.hasPayKey && "余额 Key"].filter(Boolean).join(" + ") + (n.hasKey && n.hasPayKey ? (n.payFirst ? " · 先余额" : " · 先套餐") : "") : "可选",
      tune: n ? `权重 ${n.weight} · 并发 ${n.maxConcurrency || "不限"}` : "权重 1 · 并发不限",
      bal: n && n.hasBalance ? "已填写" : "未填写",
    };
    return `<form class="kp-dform" id="kpForm" novalidate>
      <div class="kp-dhead">
        <h3><span class="nm">${n ? esc(n.name) : "添加节点"}</span>${n ? ` <span class="pill ${st[0]}">${st[1]}</span>` : ""}</h3>
        <label class="kp-switch" title="${n ? "立即生效，不用点保存" : "添加后是否马上参与分流"}"><input type="checkbox" id="kpfEnabled"${!n || n.enabled ? " checked" : ""}>启用</label>
        <button class="kp-dclose" type="button" id="kpCancel" aria-label="关闭" title="关闭（Esc）">✕</button>
      </div>
      <div class="kp-dbody">
        <details class="kp-fs" open><summary>基本信息</summary><div class="kp-fsb">
        <label class="field">名称<input id="kpfName" maxlength="40" autocomplete="off" value="${esc(n ? n.name : "")}" placeholder="例如：中转A"></label>
        <label class="field">地址<input id="kpfUrl" class="mono" inputmode="url" autocomplete="off" spellcheck="false" value="${esc(n ? n.baseUrl : "")}" placeholder="https://api.example.com/v1"></label>
        <label class="field">套餐 Key（只有一个 Key 就填这里）<input id="kpfKey" class="mono" type="password" autocomplete="new-password" spellcheck="false" placeholder="${n && n.hasKey ? "已保存 " + esc(n.keyMasked) + "，留空不修改" : "sk-…"}"></label>
        <label class="field">余额 Key（可选，按量结算的那一个）<input id="kpfPayKey" class="mono" type="password" autocomplete="new-password" spellcheck="false" placeholder="${n && n.hasPayKey ? "已保存 " + esc(n.payKeyMasked) + "，留空不修改" : "可选"}"></label>
        </div></details>
        <details class="kp-fs" data-fs="routes"><summary>两路与模型<span class="sum">${esc(sum.routes)}</span></summary><div class="kp-fsb">
        <div class="kp-two">
          <label class="field">两路的先后<select id="kpfPayFirst"><option value="">先套餐，后余额</option><option value="1"${n && n.payFirst ? " selected" : ""}>先余额，后套餐</option></select></label>
          <label class="field">余额底线（元）<input id="kpfPayFloor" type="number" min="0" step="1" value="${esc(n && Number(n.payFloor) > 0 ? n.payFloor : "")}" placeholder="不设"></label>
        </div>
        <p class="kp-hint">两个 Key 都填就是双路：前一路额度用完或被拒时，同一个节点自动换后一路重发。余额底线：按量余额降到这个数就不再走余额路，需要下面填了余额查询会话才能判断。${n && n.hasKey && n.hasPayKey ? ` <button class="kp-chipbtn" type="button" id="kpPayClear">去掉余额 Key</button>` : ""}</p>
        <div class="tr-row" style="display:flex;gap:8px;align-items:end">
          <label class="field" style="flex:1;min-width:0">缺模型时改用（套餐路）<select id="kpfModel">${modelOptions("原样发出（不改模型名）", n && n.model, n && n.models)}</select></label>
          <button class="btn" type="button" id="kpModels">读取模型</button>
        </div>
        <label class="field">缺模型时改用（余额路）<select id="kpfPayModel">${modelOptions("和套餐路相同", n && n.payModel, n && n.payModels)}</select></label>
        <p class="kp-hint">客户端要的模型，哪一路有就原样发给哪一路。只有整个 Key 池没有任何线路有这个模型时，才改用这里选的模型。</p>
        ${n ? `<p class="kp-hint">${n.hasKey ? "套餐路的模型：" + esc((n.models || []).join("、") || "还没读到") : ""}${n.hasKey && n.hasPayKey ? "<br>" : ""}${n.hasPayKey ? "余额路的模型：" + esc((n.payModels || []).join("、") || "还没读到") : ""}</p>` : ""}
        <label class="field">模型别名（每行一条：客户端要的模型名 = 这个节点上的模型名）<textarea id="kpfAliases" class="mono" rows="2" spellcheck="false" placeholder="Qwen3.8-Flash = Qwen3.8-Flash-Next">${esc(Object.entries((n && n.aliases) || {}).map(([a, b]) => a + " = " + b).join("\n"))}</textarea></label>
        <p class="kp-hint">同一个模型在这个中转站名字不一样时用：客户端要左边的名字，发给这个节点时换成右边的。两边其实是不同模型的话，回答质量会随线路变化。</p>
        </div></details>
        <details class="kp-fs" data-fs="tune"><summary>调度与兼容<span class="sum">${esc(sum.tune)}</span></summary><div class="kp-fsb">
        <div class="kp-two">
          <label class="field">权重<input id="kpfWeight" type="number" min="0.1" max="100" step="0.1" value="${esc(n ? n.weight : 1)}"></label>
          <label class="field">最大并发<input id="kpfMax" type="number" min="0" max="1000" step="1" value="${esc(n && n.maxConcurrency ? n.maxConcurrency : "")}" placeholder="不限"></label>
        </div>
        <p class="kp-hint">权重越大分到的请求越多；最大并发是这个中转站同时能处理的请求数，满了就交给别的节点。</p>
        <label class="field">客户端标识（可选）<input id="kpfAgent" type="text" maxlength="200" autocomplete="off" spellcheck="false" value="${esc(n && n.userAgent || "")}" placeholder="留空：客户端带什么就转发什么"></label>
        <p class="kp-hint">个别中转站前面的防护只认特定的客户端标识（User-Agent）。测试连通提示“被对方站点的防护拦截”时再填。</p>
        </div></details>
        <details class="kp-fs" data-fs="bal"><summary>余额查询<span class="sum">${sum.bal}</span></summary><div class="kp-fsb">
        <div class="kp-two">
          <label class="field">余额查询 · Session Cookie<input id="kpfBalCookie" class="mono" type="password" autocomplete="new-password" spellcheck="false" placeholder="${n && n.hasBalance ? "已保存，重新登录后在这里填新的" : "可选"}"></label>
          <label class="field">余额查询 · TF-User<input id="kpfBalUser" class="mono" type="password" autocomplete="new-password" spellcheck="false" placeholder="${n && n.hasBalance ? "已保存，留空不修改" : "可选"}"></label>
        </div>
        <p class="kp-hint">可选。填站点网页登录后的 Session Cookie 值和 Session Token（TF-User）值，两个都要填。只保存在本机，只发往这个节点自己的站点。${n && n.hasBalance ? ` <button class="kp-chipbtn" type="button" id="kpBalClear">清除已保存的会话</button>` : ""}</p>
        </div></details>
        ${n ? `<details class="kp-fs" data-fs="live" open><summary>实时数据</summary><div class="kp-fsb"><div id="kpLive"></div></div></details>` : ""}
      </div>
      <div class="kp-dfoot">
        <p class="kp-msg" id="kpMsg" role="status" aria-live="polite"></p>
        ${n ? `<button class="btn danger" type="button" id="kpDelete">删除</button>` : ""}
        <span class="grow"></span>
        ${n && n.hasBalance ? `<button class="btn" type="button" id="kpBalOne">查询余额</button>` : ""}
        <button class="btn" type="button" id="kpTest">测试连通</button>
        <button class="btn primary" type="submit" id="kpSave">${n ? "保存修改" : "添加节点"}</button>
      </div>
    </form>`;
  }
  function modelOptions(blank, current, list){
    const names = list || [];
    return `<option value="">${blank}</option>` + names.map(m => `<option value="${esc(m)}"${m === current ? " selected" : ""}>${esc(m)}</option>`).join("")
      + (current && !names.includes(current) ? `<option value="${esc(current)}" selected>${esc(current)}${names.length ? "（不在清单里）" : ""}</option>` : "");
  }
  function msg(text, kind=""){ const m = el("kpMsg"); if(m){ m.textContent = text; m.className = "kp-msg " + kind; } }
  function bindForm(n){
    const values = () => {
      const body = {name:el("kpfName").value.trim(), baseUrl:el("kpfUrl").value.trim(), weight:el("kpfWeight").value || 1,
                    maxConcurrency:el("kpfMax").value || 0, enabled:el("kpfEnabled").checked};
      const key = el("kpfKey").value.trim(); if(key) body.key = key;
      const payKey = el("kpfPayKey").value.trim(); if(payKey) body.payKey = payKey;
      body.payFirst = !!el("kpfPayFirst").value;
      body.payFloor = el("kpfPayFloor").value || 0;
      body.model = el("kpfModel").value;
      body.payModel = el("kpfPayModel").value;
      body.userAgent = el("kpfAgent").value.trim();
      body.aliases = {};
      el("kpfAliases").value.split("\n").map(x => x.trim()).filter(Boolean).forEach(line => {
        const at = line.indexOf("=");
        body.aliases[at < 0 ? line : line.slice(0, at).trim()] = at < 0 ? "" : line.slice(at + 1).trim();
      });
      const balCookie = el("kpfBalCookie").value.trim(), balUser = el("kpfBalUser").value.trim();
      if(balCookie) body.balanceCookie = balCookie;
      if(balUser) body.balanceUser = balUser;
      if(n) body.id = n.id;
      return body;
    };
    el("kpForm").addEventListener("submit", async ev => {
      ev.preventDefault();
      el("kpSave").disabled = true; msg("保存中…");
      try{
        const sent = values();
        const r = await api("api/keypool/nodes", {node:sent});
        mode = "edit"; selected = r.node.id;
        await refresh(); renderPanel();
        msg(n ? "已保存，代理已自动应用" : "已添加，开始参与分流", "ok");
        // 刚填了余额查询会话：顺手查一次，马上能看到填得对不对
        if(sent.balanceCookie || sent.balanceUser){
          msg("已保存，正在查询余额…");
          try{
            const b = (await api("api/keypool/balance", {id:r.node.id})).balances[r.node.id];
            await refresh();
            msg(b.ok ? `已保存 · 余额 ¥${money(b.total)}${b.note ? "（" + b.note + "）" : ""}` : "已保存，但余额查询失败：" + b.error, b.ok ? "ok" : "err");
          }catch(e){ msg("已保存，但余额查询失败：" + e.message, "err"); }
        }
      }catch(e){ msg(e.message, "err"); const b = el("kpSave"); if(b) b.disabled = false; }
    });
    el("kpModels").addEventListener("click", async () => {
      const b = values(), button = el("kpModels"); button.disabled = true; msg("正在读取这个节点的模型…");
      try{
        // 两个 Key 能用的模型不一样：各读各的，分别填进各自的下拉框
        const jobs = [["key", "套餐 Key", "kpfModel", "原样发出（不改模型名）", b.key || (n && n.hasKey)],
                      ["pay", "余额 Key", "kpfPayModel", "和套餐路相同", b.payKey || (n && n.hasPayKey)]].filter(x => x[4]);
        const out = [];
        for(const [which, name, id, blank] of jobs){
          const r = await api("api/keypool/nodes/test", {id:n ? n.id : undefined, baseUrl:b.baseUrl, key:b.key, payKey:b.payKey, userAgent:b.userAgent, which}, true);
          const head = jobs.length > 1 ? name + "：" : "";
          if(!r.ok){ out.push({ok:false, text:head + `${r.detail}${r.status ? `（HTTP ${r.status}）` : ""}`}); continue; }
          if(!(r.modelList || []).length){ out.push({ok:false, text:head + "没有返回模型清单"}); continue; }
          const select = el(id), current = select.value;
          select.replaceChildren(new Option(blank, ""));
          r.modelList.forEach(m => select.add(new Option(m, m)));
          if(current && !r.modelList.includes(current)) select.add(new Option(current + "（不在清单里）", current));
          select.value = current;
          out.push({ok:true, text:head + `${r.modelList.length} 个模型`});
        }
        if(!out.length) throw new Error("先填一个 Key");
        msg(out.map(x => x.text).join("；") + (out.some(x => x.ok) ? "，选好后点保存" : ""), out.every(x => x.ok) ? "ok" : "err");
      }catch(e){ msg(e.message, "err"); }
      button.disabled = false;
    });
    el("kpTest").addEventListener("click", async () => {
      const b = values(); msg("测试中…");
      try{
        // 两个 Key 分别测一次
        const which = [["key", "套餐 Key", b.key || (n && n.hasKey)], ["pay", "余额 Key", b.payKey || (n && n.hasPayKey)]].filter(x => x[2]);
        const out = [];
        for(const [w, name] of which){
          const r = await api("api/keypool/nodes/test", {id:n ? n.id : undefined, baseUrl:b.baseUrl, key:b.key, payKey:b.payKey, userAgent:b.userAgent, which:w}, true);
          out.push({ok:r.ok, text:(which.length > 1 ? name + "：" : "") + (r.ok ? `${r.detail} · ${r.ms} ms` : `${r.detail}${r.status ? `（HTTP ${r.status}）` : ""}`)});
        }
        if(!out.length) throw new Error("先填一个 Key");
        msg(out.map(x => x.text).join("；"), out.every(x => x.ok) ? "ok" : "err");
      }catch(e){ msg(e.message, "err"); }
    });
    const balOne = el("kpBalOne");
    if(balOne) balOne.addEventListener("click", async () => {
      // 输入框里有新填的会话：先保存再查（保存后会自动查一次），否则查到的还是旧会话
      if(el("kpfBalCookie").value.trim() || el("kpfBalUser").value.trim()){ el("kpForm").requestSubmit(); return; }
      balOne.disabled = true; msg("正在查询余额…");
      try{
        const r = await api("api/keypool/balance", {id:n.id}), b = r.balances[n.id];
        await refresh();
        msg(b.ok ? `余额 ¥${money(b.total)}${b.note ? "（" + b.note + "）" : ""}` : "余额查询失败：" + b.error, b.ok ? "ok" : "err");
      }catch(e){ msg(e.message, "err"); }
      const again = el("kpBalOne"); if(again) again.disabled = false;
    });
    const payClear = el("kpPayClear");
    if(payClear) payClear.addEventListener("click", async () => {
      if(!confirm("去掉这个节点的余额 Key？之后它只走套餐 Key。")) return;
      try{ await api("api/keypool/nodes", {node:{id:n.id, payKeyClear:true}}); await refresh(); renderPanel(); msg("已去掉余额 Key", "ok"); }
      catch(e){ msg(e.message, "err"); }
    });
    const balClear = el("kpBalClear");
    if(balClear) armed(balClear, "再点一次清除", async () => {
      try{ await api("api/keypool/nodes", {node:{id:n.id, balanceClear:true}}); await refresh(); renderPanel(); msg("已清除余额查询会话", "ok"); }
      catch(e){ msg(e.message, "err"); }
    });
    el("kpCancel").addEventListener("click", closeDrawer);
    // 已有节点：抽屉头部的开关立即生效，不用再点保存
    if(n) el("kpfEnabled").addEventListener("change", ev => toggleNode(n.id, ev.target.checked, ev.target));
    const del = el("kpDelete");
    if(del) armed(del, "再点一次删除", async () => {
      try{ await api("api/keypool/nodes/delete", {id:n.id}); mode = "idle"; selected = null; renderPanel(); flash(`已删除「${n.name}」`); refresh(); }
      catch(e){ msg(e.message, "err"); }
    });
  }
  // ---- 余额 ----
  const money = v => Number(v).toFixed(4);
  // 双路节点：套餐 Key 与余额 Key
  const ROUTE = {plan:"套餐", pay:"余额"};
  const routeNow = n => n.live && n.live.dual ? n.live.route : null;
  function routeLines(n){
    const L = n.live || {};
    if(!L.dual) return [];
    const lines = [`双路　　　${n.payFirst ? "先余额后套餐" : "先套餐后余额"} · 现在走${L.route ? ROUTE[L.route] : "不了（两路都停用）"}`];
    (L.routes || []).forEach(r => { if(r.blocked) lines.push(`${ROUTE[r.id]}路停用　${clip(r.blocked, 60)}${r.downLeft ? " · " + left(r.downLeft) + "后再试" : ""}`); });
    if(Number(n.payFloor) > 0) lines.push(`余额底线　¥${n.payFloor}`);
    return lines;
  }
  function balanceText(b){
    if(!b) return "还没查询";
    if(!b.ok) return `<span style="color:var(--crit)">${esc(b.error)}</span> · ${clock(b.at)}`;
    return `<span class="num">¥${money(b.total)}</span> · 按量 ${money(b.payAsYouGo)} + 套餐 ${money(b.plan)}`
      + (b.basicTokens ? ` · 基础 token 剩余 ${Number(b.basicTokens).toLocaleString("zh-CN")}` : "")
      + (b.note ? ` · <span style="color:var(--warn)">${esc(b.note)}</span>` : "") + ` · ${clock(b.at)}`;
  }
  function balanceShort(n){
    if(!n.hasBalance) return {text:"未填", cls:"dim"};
    const b = n.balance;
    if(!b) return {text:"…", cls:"dim"};
    if(b.ok) return {text:"¥" + Number(b.total).toFixed(2), cls:b.degraded ? "warn" : ""};
    return b.last ? {text:"¥" + Number(b.last.total).toFixed(2), cls:"warn"} : {text:"失败", cls:"crit"};
  }
  function balanceLines(n){
    if(!n.hasBalance) return ["还没有填写余额查询会话", "点这个节点，在编辑框里填 Session Cookie 和 TF-User"];
    const b = n.balance;
    if(!b) return ["正在查询余额…"];
    const good = b.ok ? b : b.last, lines = [];
    if(good){
      lines.push(`总余额　¥${money(good.total)}`, `按量　　¥${money(good.payAsYouGo)}`,
                 `套餐　　¥${money(good.plan)}（${good.activePlans} 个生效）`);
      if(good.basicTokens) lines.push(`基础 token 剩余　${Number(good.basicTokens).toLocaleString("zh-CN")}（不折算金额）`);
      if(good.note) lines.push("⚠ " + good.note);
      lines.push(`更新于 ${clock(good.at)}`);
    }
    if(!b.ok) lines.push(`⚠ ${clock(b.at)} 查询失败：${b.error}` + (good ? "（上面是上一次的结果）" : ""));
    return lines.concat(routeLines(n));
  }
  function statusLines(n){
    const L = n.live || {}, s = nodeStatus(n);
    const lists = [[n.hasKey, n.hasPayKey ? "套餐模型" : "模型　　", n.models], [n.hasPayKey, "余额模型", n.payModels]].filter(x => x[0])
      .map(x => `${x[1]}　${(x[2] || []).length ? x[2].join("、") : "还没读到"}`);
    const alias = Object.entries(n.aliases || {}).map(([a, b]) => `别名　　　${a} → ${b}`);
    return [...lists, ...alias, ...routeLines(n), `状态　　　${STATUS[s][1]}${L.cooldownLeft ? " · 还要冷却 " + left(L.cooldownLeft) : ""}`,
      `当前并发　${L.inflight || 0} / ${n.maxConcurrency || "不限"}`, `近 1 分钟　${L.rpm || 0} 次`,
      `成功/失败　${L.ok || 0} / ${L.fail || 0}`, `响应头　　${dur(L.latencyMs)}`, `首字　　　${dur(L.ttfbMs)}`,
      `总耗时　　${dur(L.totalMs)}`,
      `流式输出　${!L.streams ? "还没有流式请求" : L.buffered ? `${L.buffered}/${L.streams} 次整段返回` : `正常（${L.streams} 次）`}`,
      `最近错误　${L.lastError ? (L.lastErrorAt ? clock(L.lastErrorAt) + " · " : "") + clip(L.lastError, 60) : "无"}`];
  }
  // ---- 悬停详情 ----
  let tip = null;   // {id, kind}
  function tipBox(){
    let box = el("kpTip");
    if(!box){ box = document.createElement("div"); box.id = "kpTip"; box.className = "kp-tip"; box.setAttribute("role", "tooltip"); box.hidden = true; document.body.append(box); }
    return box;
  }
  function refreshTip(){
    if(!tip) return;
    const n = data && data.nodes.find(x => x.id === tip.id);
    if(!n || dragging){ hideTip(); return; }
    const text = [n.name + (tip.kind === "money" ? " · 账户余额" : " · 状态")].concat(tip.kind === "money" ? balanceLines(n) : statusLines(n)).join("\n");
    const box = tipBox(); if(box.textContent !== text) box.textContent = text;
  }
  function moveTip(ev){
    if(!tip) return;
    const box = tipBox(), w = box.offsetWidth, h = box.offsetHeight;
    box.style.left = Math.max(8, Math.min(innerWidth - w - 8, ev.clientX - w - 14)) + "px";
    box.style.top = Math.max(8, Math.min(innerHeight - h - 8, ev.clientY + 14)) + "px";
  }
  function showTip(id, kind, ev){ if(dragging) return; tip = {id, kind}; tipBox().hidden = false; refreshTip(); moveTip(ev); }
  function hideTip(){ tip = null; const box = el("kpTip"); if(box) box.hidden = true; }
  function renderBalance(){
    const withCreds = data.nodes.filter(n => n.hasBalance);
    const totals = withCreds.map(n => n.balance && (n.balance.ok ? n.balance : n.balance.last)).filter(Boolean);
    const failed = withCreds.filter(n => n.balance && !n.balance.ok).length;
    el("kpBalanceChip").hidden = !withCreds.length;
    const text = !totals.length ? "—" : "¥" + money(totals.reduce((sum, b) => sum + b.total, 0)) + (failed ? ` · ${failed} 个失败` : "");
    if(el("kpBalanceTotal").textContent !== text) el("kpBalanceTotal").textContent = text;
  }
  function armed(btn, prompt, action){   // 两步确认：先点一下变红，4 秒内再点才执行
    const original = btn.textContent; let on = false, t = 0;
    btn.addEventListener("click", () => {
      if(!on){ on = true; btn.classList.add("armed"); btn.textContent = prompt;
        t = setTimeout(() => { on = false; btn.classList.remove("armed"); btn.textContent = original; }, 4000); return; }
      clearTimeout(t); on = false; btn.classList.remove("armed"); btn.textContent = original; action();
    });
  }
  function renderLive(){
    const box = el("kpLive"); if(!box || !data) return;
    const n = data.nodes.find(x => x.id === selected); if(!n) return;
    const L = n.live || {}, s = nodeStatus(n);
    box.innerHTML = `<dl class="kv">
      <dt>状态</dt><dd>${STATUS[s][1]}${L.cooldownLeft ? ` · 还要冷却 ${left(L.cooldownLeft)}` : ""}</dd>
      <dt>当前并发</dt><dd class="num">${L.inflight || 0} / ${n.maxConcurrency || "不限"}</dd>
      <dt>近 1 分钟</dt><dd class="num">${L.rpm || 0} 次</dd>
      <dt>成功 / 失败</dt><dd class="num">${L.ok || 0} / ${L.fail || 0}</dd>
      <dt>响应头 · 平均</dt><dd class="num">${dur(L.latencyMs)}</dd>
      <dt>首字 · 平均</dt><dd class="num">${dur(L.ttfbMs)}</dd>
      <dt>总耗时 · 平均</dt><dd class="num">${dur(L.totalMs)}</dd>
      <dt>流式输出</dt><dd>${!L.streams ? "还没有流式请求" : L.buffered
        ? `<span style="color:var(--warn)">${L.buffered}/${L.streams} 次整段返回</span>` : `正常（${L.streams} 次）`}</dd>
      ${n.hasBalance ? `<dt>账户余额</dt><dd>${balanceText(n.balance)}</dd>` : ""}
      <dt>最近错误</dt><dd>${L.lastError ? esc((L.lastErrorAt ? clock(L.lastErrorAt) + " · " : "") + clip(L.lastError, 60)) : "无"}</dd>
    </dl>${s === "quarantined" ? `<div class="kp-quar" style="margin-top:10px"><span>⚠️ 因 ${esc(L.quarantineReason||"危险操作")} 被安全检测隔离，已停止分流。</span><button class="btn" type="button" id="kpRelease">放行</button></div>` : ""}${s === "reauth" ? `<div class="kp-quar" style="margin-top:10px"><span>🔒 ${esc(L.reauthReason||"地址换了站点")}；重新填写 Key 才会恢复发请求。</span></div>` : ""}
    <p class="kp-hint" style="margin-top:8px">计数从代理本次启动开始。“整段返回”指流式请求的首字几乎等于总耗时：中转站等全部生成完才一次性发回，首字会很慢。</p>`;
    const rel = el("kpRelease");
    if(rel) rel.addEventListener("click", async () => {
      rel.disabled = true;
      try{ await api("api/keypool/release", {id:n.id}); flash("已放行，节点恢复参与分流"); refresh(); }
      catch(e){ flash("放行失败："+e.message, true); rel.disabled=false; }
    });
  }
  // ---- 节点列表：开关、测试、编辑都在一行里，不用进编辑页 ----
  let nodeKey = "", nodeFilter = "all";
  const testNote = {};    // 节点编号 → {text, cls, until}：测试结果在这一行里显示一会儿
  const stationOf = n => { try{ return new URL(n.baseUrl).host; }catch(e){ return n.baseUrl || "未填地址"; } };
  function renderNodes(){
    const body = el("kpnBody"); if(!body || !data) return;
    const groups = [];
    data.nodes.forEach(n => { const s = stationOf(n); let g = groups.find(x => x.name === s); if(!g) groups.push(g = {name:s, nodes:[]}); g.nodes.push(n); });
    const key = JSON.stringify(groups.map(g => [g.name, g.nodes.map(n => [n.id, n.name, n.hasKey, n.hasPayKey])]));
    if(key !== nodeKey){          // 节点有增删改名才重画结构；数字和开关原地更新，点到一半不会被刷新打断
      nodeKey = key;
      body.innerHTML = groups.length ? groups.map(g => `<tr class="kpn-st" data-st="${esc(g.name)}"><td colspan="9"><div class="row"><b>${esc(g.name)}</b>
          <span data-stinfo></span><span class="spacer"></span>
          <button class="kp-chipbtn" type="button" data-bulk="on">全部启用</button><button class="kp-chipbtn" type="button" data-bulk="off">全部停用</button></div></td></tr>`
        + g.nodes.map(n => `<tr data-id="${esc(n.id)}" data-st="${esc(g.name)}">
          <td><label class="kp-switch" title="启用 / 停用，立即生效"><input type="checkbox" data-toggle aria-label="启用 ${esc(n.name)}"></label></td>
          <td><button class="kpn-name" type="button" data-edit title="打开设置"><span class="dot" data-c="dot"></span>${esc(n.name)}</button><div class="kpn-sub" data-c="sub"></div></td>
          <td data-c="routes"></td><td class="num" data-c="inflight"></td><td class="num" data-c="rpm"></td><td class="num" data-c="ttfb"></td>
          <td class="num" data-c="okfail"></td><td class="num" data-c="bal"></td>
          <td class="kpn-act"><button class="kp-chipbtn" type="button" data-test>测试</button><button class="kp-chipbtn" type="button" data-edit>设置</button></td></tr>`).join("")).join("")
        : `<tr class="kpn-empty"><td colspan="9">还没有节点。点右上角「＋ 添加节点」，填上中转站的地址和 Key。</td></tr>`;
    }
    const now = Date.now(), stations = (data.stations || []);
    let shown = 0;
    data.nodes.forEach(n => {
      const row = [...body.querySelectorAll("tr[data-id]")].find(r => r.dataset.id === n.id); if(!row) return;
      const L = n.live || {}, s = nodeStatus(n), cell = c => row.querySelector(`[data-c="${c}"]`);
      const box = row.querySelector("[data-toggle]"); if(!box.disabled) box.checked = !!n.enabled;
      row.classList.toggle("off", !n.enabled); row.classList.toggle("sel", mode === "edit" && selected === n.id);
      row.hidden = nodeFilter === "on" ? !n.enabled : nodeFilter === "off" ? n.enabled : false; if(!row.hidden) shown++;
      cell("dot").className = "dot " + STATUS[s][0];
      const note = testNote[n.id] && testNote[n.id].until > now ? testNote[n.id] : null, sub = cell("sub");
      const text = note ? note.text : STATUS[s][1] + (L.cooldownLeft ? ` · 还要冷却 ${left(L.cooldownLeft)}` : "")
        + (s === "reauth" && L.reauthReason ? " · " + clip(L.reauthReason, 44) : "")
        + (["cooling","down","quarantined"].includes(s) && L.lastError ? " · " + clip(L.lastError, 48) : "");
      if(sub.textContent !== text) sub.textContent = text;
      sub.className = "kpn-sub " + (note ? note.cls : ["cooling","quarantined","reauth"].includes(s) ? "warn" : s === "down" ? "err" : "");
      sub.title = (s === "reauth" ? L.reauthReason : L.lastError) || "";
      const cur = routeNow(n);
      const routes = [n.hasKey && ["plan", n.hasPayKey ? "套餐" : "单 Key"], n.hasPayKey && ["pay", "余额"]].filter(Boolean)
        .map(([id, name]) => `<span class="kpn-badge${cur === id ? " now" : ""}"${cur === id ? ` title="现在走这一路"` : ""}>${name}</span>`).join("");
      if(cell("routes").innerHTML !== routes) cell("routes").innerHTML = routes;
      cell("inflight").textContent = `${L.inflight || 0} / ${n.maxConcurrency || "∞"}`;
      cell("rpm").textContent = L.rpm || 0;
      cell("ttfb").textContent = dur(L.ttfbMs);
      cell("okfail").textContent = `${L.ok || 0} / ${L.fail || 0}`;
      const b = balanceShort(n); cell("bal").textContent = b.text; cell("bal").style.color = b.cls === "warn" ? "var(--warn)" : b.cls === "crit" ? "var(--crit)" : b.cls === "dim" ? "var(--muted)" : "";
    });
    body.querySelectorAll("tr.kpn-st").forEach(r => {
      const members = data.nodes.filter(n => stationOf(n) === r.dataset.st), on = members.filter(n => n.enabled).length;
      const st = stations.find(x => x.name === r.dataset.st) || {};
      const info = r.querySelector("[data-stinfo]");
      const limit = st.maxConcurrency || st.minIntervalSeconds ? `限速：${st.maxConcurrency ? "同时最多 " + st.maxConcurrency : "并发不限"}${st.minIntervalSeconds ? " · 间隔 " + st.minIntervalSeconds + " 秒" : ""}` : "未限速";
      info.textContent = `启用 ${on} / ${members.length} · ${limit} · ${st.inflight || 0} 个在途` + (st.holdLeft ? ` · 被对方按 IP 限流，暂停中，还有 ${st.holdLeft} 秒` : "");
      info.classList.toggle("hold", !!st.holdLeft);
      r.hidden = !members.some(n => nodeFilter === "on" ? n.enabled : nodeFilter === "off" ? !n.enabled : true);
      r.querySelector('[data-bulk="on"]').hidden = on === members.length; r.querySelector('[data-bulk="off"]').hidden = on === 0;
    });
    const total = data.nodes.length, enabled = data.nodes.filter(n => n.enabled).length;
    el("kpnSummary").textContent = total ? `启用 ${enabled} / 共 ${total}` + (nodeFilter !== "all" ? ` · 当前显示 ${shown} 个` : "") : "";
  }
  async function toggleNode(id, on, box){
    const n = data && data.nodes.find(x => x.id === id); if(!n) return;
    if(box) box.disabled = true;
    try{ await api("api/keypool/nodes", {node:{id, enabled:on}}); n.enabled = on; flash(`「${n.name}」已${on ? "启用，开始参与分流" : "停用，不再分到请求"}`); }
    catch(e){ flash(`「${n.name}」${on ? "启用" : "停用"}失败：${e.message}`, true); }
    if(box) box.disabled = false;
    await refresh();
    const head = el("kpfEnabled"); if(head && mode === "edit" && selected === id && head !== box) head.checked = !!n.enabled;
  }
  async function testNode(n, btn){
    btn.disabled = true; testNote[n.id] = {text:"测试中…", cls:"", until:Date.now() + 60000}; renderNodes();
    const out = [];
    try{
      for(const [w, name] of [["key", "套餐"], ["pay", "余额"]].filter(x => x[0] === "key" ? n.hasKey : n.hasPayKey)){
        const r = await api("api/keypool/nodes/test", {id:n.id, which:w}, true);
        out.push({ok:r.ok, text:(n.hasKey && n.hasPayKey ? name + "：" : "") + (r.ok ? `${r.detail} · ${r.ms} ms` : `${r.detail}${r.status ? `（HTTP ${r.status}）` : ""}`)});
      }
      testNote[n.id] = {text:out.map(x => x.text).join("；"), cls:out.every(x => x.ok) ? "ok" : "err", until:Date.now() + 12000};
    }catch(e){ testNote[n.id] = {text:"测试失败：" + e.message, cls:"err", until:Date.now() + 12000}; }
    btn.disabled = false; renderNodes();
  }
  function initNodes(){
    const body = el("kpnBody");
    body.addEventListener("change", ev => { const box = ev.target.closest("[data-toggle]"); if(box) toggleNode(box.closest("tr").dataset.id, box.checked, box); });
    body.addEventListener("click", async ev => {
      const row = ev.target.closest("tr"); if(!row || !data) return;
      if(ev.target.closest("[data-edit]")) return select(row.dataset.id);
      const test = ev.target.closest("[data-test]");
      if(test){ const n = data.nodes.find(x => x.id === row.dataset.id); if(n) testNode(n, test); return; }
      const bulk = ev.target.closest("[data-bulk]");
      if(bulk){
        const on = bulk.dataset.bulk === "on", members = data.nodes.filter(n => stationOf(n) === row.dataset.st && n.enabled !== on);
        bulk.disabled = true;
        try{ for(const n of members) await api("api/keypool/nodes", {node:{id:n.id, enabled:on}}); flash(`${row.dataset.st} 的 ${members.length} 个节点已${on ? "启用" : "停用"}`); }
        catch(e){ flash(e.message, true); }
        bulk.disabled = false; refresh();
      }
    });
    el("kpnFilter").addEventListener("click", ev => {
      const b = ev.target.closest("[data-f]"); if(!b) return;
      nodeFilter = b.dataset.f; el("kpnFilter").querySelectorAll("[data-f]").forEach(x => x.setAttribute("aria-pressed", String(x === b))); renderNodes();
    });
    // 抽屉挂到页面最外层：标签页容器有模糊和变换效果，留在里面的话“固定在窗口右侧”会失效
    document.body.append(el("kpBack"), el("kpDrawer"));
    el("kpBack").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", ev => { if(ev.key === "Escape" && !el("kpDrawer").hidden) closeDrawer(); });
  }
  function select(id){
    mode = "edit"; selected = id; renderPanel(); render();
  }
  function openAdd(){
    mode = "add"; selected = null; renderPanel(); render();
    el("kpfName").focus();
  }

  // ---- 轮询 ----
  async function refresh(){
    if(busy) return; busy = true;
    try{
      const res = await fetch("api/keypool/state?balance=1&since=" + seq, {cache:"no-store"});
      const next = await res.json();
      if(!res.ok || !next.ok) throw new Error(next.error || ("HTTP " + res.status));
      const restarted = proxyStarted && next.proxyStartedAt && next.proxyStartedAt !== proxyStarted;
      const events = restarted ? [] : (next.events || []);
      recent = recent.concat(events).slice(-40);
      const secEv = restarted ? [] : ((next.security && next.security.events) || []);
      if(restarted) secRecent = [];
      secRecent = secRecent.concat(secEv).slice(-60);
      if(!first) events.forEach(e => { if(e.node) spawn(e.node, !e.ok); });
      first = false;
      seq = restarted ? (next.seq || 0) : Math.max(seq, next.seq || 0);
      if(next.proxyStartedAt) proxyStarted = next.proxyStartedAt;
      data = next;
      applyPending();
      if(mode === "edit" && !data.nodes.some(n => n.id === selected)){ mode = "idle"; selected = null; renderPanel(); }
      render(); renderTop(); renderKpis(); renderEvents(); renderSecurity(); renderLive(); renderBalance();
      renderNodes(); renderBand();
    }catch(e){
      el("kpBanner").hidden = false; el("kpBanner").classList.remove("info");
      el("kpBannerText").textContent = "读不到 Key 池状态：" + e.message;
    }finally{ busy = false; }
  }
  // ---- 用量统计 ----
  const Stats = (() => {
    let mode = "requests", page = 0, last = null, timer2 = null, loading = false;
    const SIZE = 20, ids = {range:"kpsRange", bucket:"kpsBucket", node:"kpsNode", model:"kpsModel", route:"kpsRoute", result:"kpsResult"};
    const big = n => n == null ? "—" : n >= 1e8 ? (n/1e8).toFixed(2) + " 亿" : n >= 1e4 ? (n/1e4).toFixed(n >= 1e6 ? 0 : 1) + " 万" : String(n);
    const query = (extra={}) => {
      const q = new URLSearchParams();
      Object.entries(ids).forEach(([k, id]) => { const v = el(id).value; if(v) q.set(k, v); });
      Object.entries(extra).forEach(([k, v]) => q.set(k, v));
      return q.toString();
    };
    function options(id, items, blank){
      const select = el(id), current = select.value, have = [...select.options].map(o => o.value).join("\n");
      const want = [""].concat(items.map(x => x.key)).concat(current && !items.some(x => x.key === current) ? [current] : []);
      if(have === want.join("\n")) return;
      select.replaceChildren(new Option(blank, ""));
      items.forEach(x => select.add(new Option(x.label, x.key)));
      if(current && !items.some(x => x.key === current)) select.add(new Option(current, current));
      select.value = current;
    }
    function bars(id, items, unit){
      const box = el(id), key = unit || mode, top = Math.max(1, ...items.map(x => x[key] || 0));
      const shown = items.filter(x => (x[key] || 0) > 0 || key === "requests").slice(0, 8);
      box.innerHTML = shown.length ? shown.map(x => `<div class="kps-row"><span class="nm" title="${esc(x.label)}">${esc(x.label)}</span>
        <span class="v">${key === "tokens" ? big(x.tokens) + " token" : x.requests + " 次" + (x.requests - x.ok ? ` · ${x.requests - x.ok} 失败` : "")}</span>
        <span class="bar"><i style="width:${Math.max(2, (x[key] || 0) / top * 100)}%"></i></span></div>`).join("") : `<div class="kps-empty">暂无数据</div>`;
    }
    function cards(d){
      const tokens = d.tokensIn + d.tokensOut, rate = d.requests ? Math.round(d.succeeded * 1000 / d.requests) / 10 : null;
      const rows = [
        ["总请求数", d.calls, d.requests > d.calls ? `含重试共 ${d.requests} 次尝试` : "所选范围内"],
        ["成功率", rate == null ? "—" : rate + "%", d.fail ? `${d.fail} 次失败` : "没有失败"],
        ["总 Token", big(tokens), `输入 ${big(d.tokensIn)} / 输出 ${big(d.tokensOut)} / 缓存 ${big(d.tokensCache)}`],
        ["花费（估算）", "¥" + Number(d.spend.total).toFixed(4), d.spend.readings ? `按量 ${Number(d.spend.pay).toFixed(2)} + 套餐 ${Number(d.spend.plan).toFixed(2)}` : "还没有余额读数"],
        ["平均耗时", d.avgMs == null ? "—" : dur(d.avgMs), d.avgTtfbMs == null ? "" : "首字 " + dur(d.avgTtfbMs)]];
      el("kpsCards").innerHTML = rows.map(([a, b, c]) => `<div class="kpi"><span class="label">${a}</span><span class="val num">${esc(b)}</span><span class="label">${esc(c)}</span></div>`).join("");
    }
    function trend(d){
      const cells = d.trend, byDay = d.bucket >= 86400, unit = mode === "tokens" ? "tokens" : "requests";
      const height = c => unit === "tokens" ? c.in + c.out : c.requests;
      const top = Math.max(1, ...cells.map(height));
      const stamp = t => { const x = new Date(t * 1000), p = n => String(n).padStart(2, "0"); return byDay ? `${x.getMonth()+1}/${x.getDate()}` : `${x.getMonth()+1}/${x.getDate()} ${p(x.getHours())}:00`; };
      el("kpsLegend").innerHTML = unit === "tokens" ? `<span><b style="background:var(--accent)"></b>输入</span><span><b style="background:color-mix(in srgb,var(--accent) 45%,var(--ok))"></b>输出</span>`
        : `<span><b style="background:var(--accent)"></b>成功</span><span><b style="background:var(--crit)"></b>失败</span>`;
      if(!cells.some(height)){ el("kpsTrend").innerHTML = `<div class="kps-empty">暂无数据</div>`; return; }
      el("kpsTrend").innerHTML = `<div class="kps-trend">${cells.map(c => {
        const a = unit === "tokens" ? c.in : c.requests - c.fail, b = unit === "tokens" ? c.out : c.fail;
        const tip = `${stamp(c.t)}　${c.requests} 次请求${c.fail ? `（${c.fail} 失败）` : ""}　输入 ${big(c.in)} / 输出 ${big(c.out)} token`;
        return `<div class="col" title="${tip}"><i class="${unit === "tokens" ? "out" : "fail"}" style="height:${b / top * 100}%"></i><i class="in" style="height:${a / top * 100}%"></i></div>`;
      }).join("")}</div><div class="kps-axis"><span>${stamp(cells[0].t)}</span><span>最高 ${unit === "tokens" ? big(top) + " token" : top + " 次"} / ${byDay ? "天" : "小时"}</span><span>${stamp(cells[cells.length - 1].t)}</span></div>`;
    }
    function spendBox(d){
      const m = d.spend;
      if(!m.readings){ el("kpsSpend").innerHTML = `<div class="kps-empty">这段时间还没有余额读数。<br>填了余额查询会话的节点才有。</div>`; return; }
      const top = Math.max(0.0001, ...m.nodes.map(x => x.amount));
      el("kpsSpend").innerHTML = (m.nodes.length ? m.nodes.map(x => `<div class="kps-row"><span class="nm">${esc(x.label)}</span><span class="v">¥${Number(x.amount).toFixed(4)}</span>
        <span class="bar"><i style="width:${Math.max(2, x.amount / top * 100)}%"></i></span></div>`).join("") : `<div class="kps-empty">余额没有下降</div>`)
        + `<p class="kp-hint" style="margin:6px 0 0">把每个节点余额“变少的部分”加起来；充值和套餐续期不算。不是逐笔账单，也分不到单次请求。</p>`;
    }
    async function table(){
      const r = await api("api/keypool/usage?" + query({limit:SIZE, offset:page * SIZE}));
      const pages = Math.max(1, Math.ceil(r.total / SIZE));
      if(page >= pages){ page = pages - 1; return table(); }
      const when = t => { const x = new Date(t * 1000), p = n => String(n).padStart(2, "0"); return `${x.getMonth()+1}/${x.getDate()} ${p(x.getHours())}:${p(x.getMinutes())}:${p(x.getSeconds())}`; };
      const tok = v => v == null ? "?" : big(v);
      el("kpsRows").innerHTML = r.rows.length ? r.rows.map(x => `<tr><td class="num">${when(x.t)}</td><td>${esc(x.name || "")}</td><td>${x.route ? ROUTE[x.route] : ""}</td>
        <td title="${esc(x.asked && x.asked !== x.model ? "客户端要的是 " + x.asked : "")}">${esc(x.model || "")}</td><td class="num">${esc(x.path || "")}</td>
        <td class="num${x.ok ? "" : " bad"}">${esc(x.status)}${x.attempt > 1 ? ` · 第${x.attempt}次` : ""}</td>
        <td class="num">${x.ok ? `${tok(x.in)} / ${tok(x.out)} / ${tok(x.cache)}` : ""}</td><td class="num">${x.ttfb != null ? dur(x.ttfb) : ""}</td><td class="num">${x.total != null ? dur(x.total) : ""}</td></tr>`).join("")
        : `<tr><td colspan="9"><div class="kps-empty">暂无数据</div></td></tr>`;
      el("kpsPage").textContent = `第 ${page + 1} / ${pages} 页 · 共 ${r.total} 条`;
      el("kpsPrev").disabled = page <= 0; el("kpsNext").disabled = page >= pages - 1;
    }
    async function load(){
      if(loading) return; loading = true;
      try{
        const d = await api("api/keypool/stats?" + query());
        last = d;
        cards(d); bars("kpsByModel", d.byModel); bars("kpsByNode", d.byNode, "requests"); bars("kpsByPath", d.byPath, "requests");
        spendBox(d); trend(d);
        options("kpsNode", d.byNode, "全部节点"); options("kpsModel", d.byModel, "全部模型");
        el("kpsCsv").href = "api/keypool/usage.csv?" + query();
        el("kpsBase").hidden = !d.baseline;
        if(d.baseline){ const x = new Date(d.baseline * 1000), p = n => String(n).padStart(2, "0");
          el("kpsBaseText").textContent = `只统计 ${x.getMonth()+1}/${x.getDate()} ${p(x.getHours())}:${p(x.getMinutes())}:${p(x.getSeconds())} 之后的请求，更早的记录还在。`; }
        el("kpsUpdated").textContent = "更新于 " + clock(Date.now() / 1000) + (d.tokenCounted < d.succeeded ? ` · ${d.succeeded - d.tokenCounted} 次成功请求上游没给 token 数` : "");
        await table();
      }catch(e){ el("kpsUpdated").textContent = "读不到统计：" + e.message; }
      finally{ loading = false; }
    }
    function wire(){
      Object.values(ids).forEach(id => el(id).addEventListener("change", () => { page = 0; load(); }));
      el("kpsRefresh").addEventListener("click", load);
      el("kpsReset").addEventListener("click", () => { Object.entries(ids).forEach(([k, id]) => el(id).value = k === "range" ? "24h" : ""); page = 0; load(); });
      const baseline = async (action, done) => {
        try{ await api("api/keypool/stats/baseline", {action}); page = 0; while(loading) await new Promise(r => setTimeout(r, 50)); await load(); flash(done); }
        catch(e){ flash("没改成：" + e.message, true); }
      };
      el("kpsZero").addEventListener("click", () => baseline("set", "已从现在起重新统计，之前的记录没有删除"));
      el("kpsAll").addEventListener("click", () => baseline("clear", "已恢复，统计包含全部记录"));
      el("kpsPrev").addEventListener("click", () => { if(page > 0){ page--; table(); } });
      el("kpsNext").addEventListener("click", () => { page++; table(); });
      document.querySelectorAll(".kps-mode").forEach(b => b.addEventListener("click", () => {
        mode = b.dataset.mode;
        document.querySelectorAll(".kps-mode").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
        if(last){ bars("kpsByModel", last.byModel); trend(last); }
      }));
    }
    return {wire, start(){ load(); if(!timer2) timer2 = setInterval(() => { if(!document.hidden) load(); }, 30000); }, stop(){ clearInterval(timer2); timer2 = null; }};
  })();
  const startPolling = () => { if(timer) return; refresh(); timer = setInterval(() => { if(!document.hidden) refresh(); }, 1000); Stats.start(); };
  const stopPolling = () => { clearInterval(timer); timer = null; Stats.stop(); };

  document.addEventListener("DOMContentLoaded", () => {
    if(!meta){
      el("kpPanel").innerHTML = `<p class="kp-howto">Key 池只能在监看中心里使用：请打开 <code>http://127.0.0.1:8799/#keypool</code>。</p>`;
      return;
    }
    el("kpAddBtn").addEventListener("click", openAdd);
    el("kpBalanceBtn").addEventListener("click", async () => {
      const button = el("kpBalanceBtn"); button.disabled = true; button.textContent = "查询中…";
      try{
        const r = await api("api/keypool/balance", {});
        await refresh();
        flash(`余额合计 ¥${money(r.total)}` + (r.failed ? `，${r.failed} 个节点查询失败（点节点看原因）` : ""), !!r.failed);
      }catch(e){ flash("余额查询失败：" + e.message, true); }
      button.disabled = false; button.textContent = "立即查询";
    });
    el("kpCopy").addEventListener("click", async () => {
      try{ const r = await api("api/keypool/master-key", {}); await navigator.clipboard.writeText(r.key); flash("合并 Key 已复制，粘贴到客户端的 API Key 里"); }
      catch(e){ flash("复制失败：" + e.message, true); }
    });
    armed(el("kpRotate"), "确认更换？", async () => {
      try{ await api("api/keypool/master-key/rotate", {}); flash("已更换合并 Key，旧的立即失效：记得在客户端里换成新的（点复制）"); refresh(); }
      catch(e){ flash("更换失败：" + e.message, true); }
    });
    el("kpRestart").addEventListener("click", async () => {
      try{ await api("api/keypool/restart", {}); flash("已请求重启代理"); setTimeout(refresh, 1500); }
      catch(e){ flash("重启失败：" + e.message, true); }
    });
    initSecurity(); initNodes();
    renderPanel();
    Stats.wire();
    Tabs.onChange(name => { if(name === "keypool") startPolling(); else { stopPolling(); if(!el("kpDrawer").hidden) closeDrawer(); } });
    if(Tabs.current === "keypool") startPolling();
  });
})();
