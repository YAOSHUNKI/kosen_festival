"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const yen = (n) => "¥" + n.toLocaleString("ja-JP");

const S = {
  config: { marker_max: 999, mode: "" },
  products: [],
  cart: new Map(),      // product_id -> 個数
  status: null,
  statusKey: "",
  numTouched: false,    // スタッフが番号を手で変えたら自動入力しない
  sending: false,
};

// ---------------------------------------------------------------- API
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* 空レスポンス */ }
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = "入力内容を確認してください";
    throw new Error(msg || `エラー (${res.status})`);
  }
  return data;
}

let toastTimer;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = kind), kind === "err" ? 4500 : 2500);
}

// ---------------------------------------------------------------- 確認ダイアログ
// ブラウザ標準の確認ダイアログは「今後表示しない」設定やキオスク表示で黙ってキャンセル扱いになるため使わない
function askConfirm(message, okLabel = "OK", danger = false) {
  return new Promise((resolve) => {
    const m = $("#modal");
    $("#modal-msg").textContent = message;
    const ok = $("#modal-ok");
    ok.textContent = okLabel;
    ok.className = "btn " + (danger ? "danger-fill" : "primary");
    m.hidden = false;
    ok.focus();
    const done = (v) => {
      m.hidden = true;
      ok.onclick = $("#modal-cancel").onclick = m.onclick = null;
      resolve(v);
    };
    ok.onclick = () => done(true);
    $("#modal-cancel").onclick = () => done(false);
    m.onclick = (e) => { if (e.target === m) done(false); };
  });
}

// ---------------------------------------------------------------- タブ
document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("on", x === b));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("on", v.id === "view-" + b.dataset.view));
  })
);

// ---------------------------------------------------------------- 注文
function renderProducts() {
  const g = $("#pgrid");
  if (!S.products.length) {
    g.innerHTML = `<div class="empty" style="grid-column:1/-1">商品がまだありません。「商品」タブから追加してください。</div>`;
    return;
  }
  g.innerHTML = S.products.map((p) => {
    const n = S.cart.get(p.id) || 0;
    return `<button class="pbtn ${n ? "sel" : ""}" data-pid="${p.id}">
      ${n ? `<span class="pcount">${n}</span>` : ""}
      <span class="pname">${esc(p.name)}</span>
      <span class="pmeta"><span>ID ${p.id}</span><span>${p.price ? yen(p.price) : ""}</span></span>
    </button>`;
  }).join("");
}

function renderCart() {
  const lines = $("#lines");
  const byId = new Map(S.products.map((p) => [p.id, p]));
  // 削除された商品はカートからも外す
  for (const pid of [...S.cart.keys()]) if (!byId.has(pid)) S.cart.delete(pid);

  if (!S.cart.size) {
    lines.innerHTML = `<div class="hint" style="justify-content:center;padding:14px 0">左の商品をタップして追加</div>`;
  } else {
    lines.innerHTML = [...S.cart].map(([pid, q]) => {
      const p = byId.get(pid);
      return `<div class="line">
        <div><div class="lname">${esc(p.name)}</div><div class="lsub">ID ${pid}${p.price ? " ・ " + yen(p.price * q) : ""}</div></div>
        <div class="stepper">
          <button data-dec="${pid}" aria-label="減らす">−</button><span>${q}</span><button data-inc="${pid}" aria-label="増やす">＋</button>
        </div></div>`;
    }).join("");
  }
  const total = [...S.cart].reduce((s, [pid, q]) => s + (byId.get(pid).price || 0) * q, 0);
  $("#totalrow").hidden = total === 0;
  $("#total").textContent = yen(total);
  updateSendButton();
  renderProducts();
}

function currentNumber() {
  const v = $("#num").value.trim();
  return /^\d+$/.test(v) ? parseInt(v, 10) : null;
}

function updateSendButton() {
  const n = currentNumber();
  const ok = S.cart.size > 0 && n !== null && n <= S.config.marker_max && !S.sending;
  $("#send").disabled = !ok;
  $("#send").textContent = S.sending ? "送信中…" : n !== null ? `${n}番で送信` : "送信";
}

$("#pgrid").addEventListener("click", (e) => {
  const b = e.target.closest("[data-pid]");
  if (!b) return;
  const pid = +b.dataset.pid;
  S.cart.set(pid, Math.min(99, (S.cart.get(pid) || 0) + 1));
  renderCart();
});
$("#lines").addEventListener("click", (e) => {
  const inc = e.target.closest("[data-inc]"), dec = e.target.closest("[data-dec]");
  if (inc) S.cart.set(+inc.dataset.inc, Math.min(99, S.cart.get(+inc.dataset.inc) + 1));
  if (dec) {
    const pid = +dec.dataset.dec, q = S.cart.get(pid) - 1;
    q > 0 ? S.cart.set(pid, q) : S.cart.delete(pid);
  }
  if (inc || dec) renderCart();
});
$("#num").addEventListener("input", () => { S.numTouched = true; updateSendButton(); });
function bump(d) {
  const n = currentNumber() ?? 0, m = S.config.marker_max + 1;
  $("#num").value = (((n + d) % m) + m) % m;
  S.numTouched = true;
  updateSendButton();
}
$("#numup").addEventListener("click", () => bump(1));
$("#numdown").addEventListener("click", () => bump(-1));
$("#suggest").addEventListener("click", () => {
  if (!S.status) return;
  $("#num").value = S.status.next_number;
  S.numTouched = false;
  updateSendButton();
});
$("#clear").addEventListener("click", () => { S.cart.clear(); renderCart(); });

$("#send").addEventListener("click", async () => {
  const n = currentNumber();
  if (n === null || !S.cart.size) return;
  S.sending = true; updateSendButton();
  try {
    const r = await api("/api/orders", {
      method: "POST",
      body: { order_number: n, items: [...S.cart].map(([product_id, quantity]) => ({ product_id, quantity })) },
    });
    if (r.delivered) toast(`${n}番 ロボットに送信しました ✓`, "ok");
    else toast(`${n}番を送信しましたが、ロボット側の受付を確認できません。待ち行列を確認してください`, "err");
    S.cart.clear();
    S.numTouched = false;
    $("#num").value = "";
    renderCart();
    await pollStatus();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    S.sending = false; updateSendButton();
  }
});

// ---------------------------------------------------------------- 状態表示
function ago(stamp) {
  const s = Math.max(0, Date.now() / 1000 - stamp);
  if (s < 60) return "たった今";
  return `${Math.floor(s / 60)}分前`;
}
const pname = (i) => esc(i.product_name || "ID" + i.product_id);
const summary = (o) => o.items.map((i) => `${pname(i)} ×${i.quantity}`).join("　");

function renderStatus() {
  const st = S.status;
  const conn = $("#conn");
  conn.className = "conn " + (st && st.connected ? "ok" : "ng");
  $("#conntext").textContent = st && st.connected ? "ロボット接続中" : "ロボット未接続";

  // 番号の自動入力
  if (st && !S.numTouched && !S.sending) $("#num").value = st.next_number;
  $("#suggest").textContent = st ? `候補: ${st.next_number}番` : "";
  $("#nexthint").textContent = st ? `次の番号札: ${st.next_number}番` : "";
  updateSendButton();

  renderCamera(!!(st && st.camera));

  const q = st && st.queue;
  $("#qcount").textContent = q ? q.pending.length + q.ready.length + (q.active ? 1 : 0) : "–";

  const key = JSON.stringify(q) + Math.floor(Date.now() / 30000); // 30秒ごとに経過時間も更新
  if (key === S.statusKey) return;
  S.statusKey = key;

  const now = $("#now");
  if (!q) {
    now.innerHTML = `<h2>ロボット</h2><p>ロボット側（キューノード）からの状態が届いていません。<br>ノードが起動しているか、同じネットワーク・ROS_DOMAIN_ID か確認してください。</p>`;
    ["#plist", "#rlist", "#done", "#pcount", "#rcount"].forEach((s) => ($(s).innerHTML = ""));
    return;
  }

  // 準備中（スタッフの作業リスト）
  $("#pcount").textContent = q.pending.length || "";
  $("#plist").innerHTML = q.pending.length
    ? q.pending.map((o) => `<div class="qrow">
        <div class="qnum">${o.order_number}</div>
        <div><div class="qitems">${o.items.map((i) => `<span class="it">${pname(i)}<b>×${i.quantity}</b></span>`).join("")}</div>
          <div class="qmeta">${ago(o.stamp)}に受付</div></div>
        <div class="qbtns">
          <button class="btn danger small" data-cmd="cancel" data-n="${o.order_number}">取消</button>
          <button class="btn done" data-cmd="ready" data-n="${o.order_number}">完成</button>
        </div></div>`).join("")
    : `<div class="empty">準備中の注文はありません</div>`;

  // 呼び出し中
  $("#rcount").textContent = q.ready.length || "";
  $("#rlist").innerHTML = q.ready.length
    ? q.ready.map((o) => `<div class="qrow">
        <div class="qnum">${o.order_number}</div>
        <div><div class="qitems">${summary(o)}</div><div class="qmeta">お客様のマーカー提示待ち</div></div>
        <div class="qbtns">
          ${q.active ? "" : `<button class="btn small indigo" data-cmd="force_start" data-n="${o.order_number}">マーカーなしで開始</button>`}
          <button class="btn small ghost" data-cmd="unready" data-n="${o.order_number}">準備中に戻す</button>
          <button class="btn small danger" data-cmd="cancel" data-n="${o.order_number}">取消</button>
        </div></div>`).join("")
    : `<div class="hint" style="justify-content:flex-start">呼び出し中の番号はありません</div>`;

  // ロボット
  const body = q.active
    ? `<span class="state busy">動作中</span>
      <div class="bignum">${q.active.order_number}<small>番</small></div>
      <div class="items">${summary(q.active)}</div>
      <div class="progress"><div style="width:${Math.round(q.progress * 100)}%"></div></div>
      <div class="ptext">${esc(q.progress_text)}</div>
      <div style="margin-top:12px"><button class="btn small danger" data-cmd="cancel" data-n="${q.active.order_number}">ロボット動作を中止</button></div>`
    : `<span class="state">待機中</span>`;
  now.innerHTML = `<h2>ロボット</h2>${body}${q.last_event ? `<div class="event">${esc(q.last_event)}</div>` : ""}`;

  $("#done").innerHTML = q.recently_completed.length
    ? q.recently_completed.map((n) => `<span class="chip">${n}</span>`).join("")
    : `<span class="hint">まだありません</span>`;
}

const CONFIRM = {
  cancel: (n) => (S.status?.queue?.active?.order_number === n
    ? `${n}番のロボット動作を中止して注文を取り消しますか？` : `${n}番の注文を取り消しますか？`),
  force_start: (n) => `${n}番をマーカーなしでロボット動作開始しますか？`,
};
async function control(cmd, n) {
  if (CONFIRM[cmd] && !(await askConfirm(CONFIRM[cmd](n), "はい", cmd === "cancel"))) return;
  try {
    const r = await api(`/api/queue/${cmd}`, { method: "POST", body: { order_number: n } });
    if (r.message) toast(r.message, "ok");
  } catch (err) {
    toast(err.message, "err");
  }
  pollStatus();
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-cmd]");
  if (b) control(b.dataset.cmd, +b.dataset.n);
});
// ---------------------------------------------------------------- カメラ
let camShown = true;
try { camShown = localStorage.getItem("camShown") !== "0"; } catch { /* 保存できなくても動く */ }
let camStreaming = false;
function renderCamera(available) {
  const img = $("#camimg");
  $("#camview").classList.toggle("hidden", !camShown);
  $("#camtoggle").textContent = camShown ? "非表示にする" : "表示する";
  const want = camShown && available;
  if (want && !camStreaming) img.src = "/api/camera.mjpg?t=" + Date.now();
  if (!want && camStreaming) img.removeAttribute("src");   // 接続を切って帯域を使わない
  camStreaming = want;
  img.hidden = !available;
  $("#camoff").hidden = available;
}
$("#camtoggle").addEventListener("click", () => {
  camShown = !camShown;
  try { localStorage.setItem("camShown", camShown ? "1" : "0"); } catch { /* 無視 */ }
  renderCamera(!!(S.status && S.status.camera));
});
// 映像が途切れたら（サーバ再起動など）次の状態更新でつなぎ直す
$("#camimg").addEventListener("error", () => { camStreaming = false; });

// ---------------------------------------------------------------- 番号札のリセット
async function setNumbering(n) {
  try {
    const r = await api("/api/numbering", { method: "POST", body: { next_number: n } });
    S.numTouched = false;   // 注文画面の番号欄に新しい候補を入れる
    toast(`次の番号を ${r.next_number}番 にしました`, "ok");
  } catch (err) { toast(err.message, "err"); }
  pollStatus();
}
$("#reset1").addEventListener("click", async () => {
  if (await askConfirm("番号を1番から振り直しますか？\n（受付中の番号は自動で飛ばします。注文は消えません）", "振り直す")) setNumbering(1);
});
$("#setnext").addEventListener("click", () => {
  const v = $("#nextnum").value.trim();
  if (!/^\d+$/.test(v)) return toast("番号を数字で入力してください", "err");
  setNumbering(+v);
  $("#nextnum").value = "";
});
$("#clearall").addEventListener("click", async () => {
  const q = S.status && S.status.queue;
  const n = q ? q.pending.length + q.ready.length + (q.active ? 1 : 0) : 0;
  if (!(await askConfirm(`全注文（${n}件）と完了履歴を消して、番号を1番に戻しますか？\nこの操作は元に戻せません。`, "全注文をクリア", true))) return;
  try {
    await api("/api/queue/clear_all", { method: "POST", body: { order_number: 0 } });
    await setNumbering(1);
    toast("全注文をクリアしました", "ok");
  } catch (err) { toast(err.message, "err"); }
  pollStatus();
});

$("#inject").addEventListener("click", () => {
  const v = $("#injnum").value.trim();
  if (/^\d+$/.test(v)) control("inject_marker", +v);
});

async function pollStatus() {
  try {
    S.status = await api("/api/status");
  } catch {
    S.status = null; // Webサーバ自体に届かない
  }
  renderStatus();
}

// ---------------------------------------------------------------- 商品管理
function renderProductTable() {
  $("#ptbody").innerHTML = S.products.length
    ? S.products.map((p) => `<tr data-row="${p.id}">
        <td class="id">${p.id}</td>
        <td><input class="name" value="${esc(p.name)}"></td>
        <td><input class="price" inputmode="numeric" value="${p.price || ""}" placeholder="0"></td>
        <td class="ops"><button class="btn small ghost" data-save="${p.id}">保存</button>
          <button class="btn small danger" data-del="${p.id}">削除</button></td></tr>`).join("")
    : `<tr><td colspan="4" class="hint" style="padding:16px">商品がありません</td></tr>`;
}

async function loadProducts(onlyIfChanged = false) {
  const list = await api("/api/products");
  if (onlyIfChanged && JSON.stringify(list) === JSON.stringify(S.products)) return;
  S.products = list;
  // 編集中の行は上書きしない（他の端末で商品が変わったときの自動更新用）
  if (!(onlyIfChanged && document.activeElement && document.activeElement.closest("#ptbody"))) renderProductTable();
  renderCart();
}

$("#addform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const id = String(f.get("id")).trim(), price = String(f.get("price")).trim();
  if (!/^\d+$/.test(id)) return toast("商品IDは数字で入力してください", "err");
  if (price && !/^\d+$/.test(price)) return toast("価格は数字で入力してください", "err");
  try {
    await api("/api/products", { method: "POST", body: { id: +id, name: f.get("name"), price: +(price || 0) } });
    e.target.reset();
    toast("追加しました", "ok");
    loadProducts();
  } catch (err) { toast(err.message, "err"); }
});

$("#ptbody").addEventListener("click", async (e) => {
  const save = e.target.closest("[data-save]"), del = e.target.closest("[data-del]");
  try {
    if (save) {
      const id = +save.dataset.save, row = save.closest("tr");
      const price = row.querySelector(".price").value.trim();
      if (price && !/^\d+$/.test(price)) return toast("価格は数字で入力してください", "err");
      await api(`/api/products/${id}`, { method: "PUT", body: { id, name: row.querySelector(".name").value, price: +(price || 0) } });
      toast("保存しました", "ok");
    } else if (del) {
      const id = +del.dataset.del;
      if (!(await askConfirm(`商品ID ${id} を削除しますか？`, "削除", true))) return;
      await api(`/api/products/${id}`, { method: "DELETE" });
      toast("削除しました", "ok");
    } else return;
    loadProducts();
  } catch (err) { toast(err.message, "err"); }
});

// ---------------------------------------------------------------- 起動
(async () => {
  try {
    S.config = await api("/api/config");
    $("#demo").hidden = S.config.mode !== "demo";
    $("#numrange").textContent = `0〜${S.config.marker_max}`;
    await loadProducts();
  } catch (err) { toast(err.message, "err"); }
  pollStatus();
  setInterval(pollStatus, 1000);
  setInterval(() => loadProducts(true).catch(() => {}), 10000);
})();
