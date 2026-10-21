const tg = window.Telegram?.WebApp;

const state = {
  q: "",
  network: "",
  rubric: "",
  verdict: "",
  sort: "date",
  offset: 0,
  limit: 20,
  matched: 0,
  rubrics: [],
  caps: [],
  botUsername: "",
  loading: false,
};

const $ = (id) => document.getElementById(id);
const el = {
  q: $("q"),
  form: $("search-form"),
  status: $("search-status"),
  results: $("results"),
  empty: $("results-empty"),
  more: $("btn-more"),
  actions: $("search-actions"),
  csv: $("btn-csv"),
  rubricChips: $("rubric-chips"),
  networkChips: $("network-chips"),
  sources: $("sources-list"),
  caps: $("caps-list"),
  watches: $("watches-list"),
  watchForm: $("watch-form"),
  watchInput: $("watch-input"),
  sheet: $("sheet"),
  sheetInput: $("source-ref"),
  addToChat: $("btn-add-to-chat"),
  toast: $("toast"),
  brandSub: $("brand-sub"),
};

const VERDICTS = {
  likely: "вероятно",
  borderline: "пограничное",
  clean: "не попадает",
  pending: "не проверено",
};

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );

const marked = (value) =>
  esc(value).replace(/&lt;mark&gt;/g, "<mark>").replace(/&lt;\/mark&gt;/g, "</mark>");

const fmtDate = (iso) => {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
};

const shortUrl = (url) => String(url || "").replace(/^https?:\/\//, "");

const safeUrl = (value) => {
  try {
    const url = new URL(String(value || ""), location.origin);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
};

function haptic(type = "light") {
  try {
    tg?.HapticFeedback?.impactOccurred(type);
  } catch {
    /* haptics недоступны */
  }
}

let toastTimer;
function toast(message) {
  el.toast.textContent = message;
  el.toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.toast.classList.add("hidden"), 2600);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (tg?.initData) headers.Authorization = `tma ${tg.initData}`;
  if (options.body) headers["Content-Type"] = "application/json";

  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail = `Ошибка ${response.status}`;
    try {
      const payload = await response.json();
      if (payload?.detail) {
        detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
      }
    } catch {
      /* тело не JSON */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

function openLink(url) {
  const safe = safeUrl(url);
  if (!safe) return;
  if (tg?.openLink) tg.openLink(safe);
  else window.open(safe, "_blank", "noopener");
}

function cardHtml(item) {
  const verdict = item.verdict || "pending";
  const url = safeUrl(item.url || item.post?.url);
  const why = item.rationale
    ? `<div class="why"><b>почему:</b> ${marked(item.rationale)}</div>`
    : "";

  return `<article class="card">
    <div class="card-head">
      <span class="badge ${verdict}">${esc(item.rubric || "—")}</span>
      <span class="verdict ${verdict}">${esc(VERDICTS[verdict] || verdict)}</span>
      <span class="spacer"></span>
      <span class="score">${Number(item.score || 0).toFixed(2)}</span>
      <span class="date">${fmtDate(item.published_at)}</span>
    </div>
    <p class="quote">«${marked(item.snippet || item.text)}»</p>
    ${why}
    <div class="card-foot">
      <a href="${esc(url)}" data-link>${esc(shortUrl(url) || "источник")}</a>
      <span class="spacer"></span>
      <span>${esc(item.source?.title || "")}</span>
    </div>
  </article>`;
}

function renderStatus(data) {
  const seconds = (data.took_ms / 1000).toFixed(1).replace(".", ",");
  el.status.textContent =
    `Готово за ${seconds} с · проверено ${data.checked} · совпадений ${data.matched}`;
}

function renderChips(data) {
  const rubricItems = state.rubrics.slice(0, 12);
  el.rubricChips.innerHTML =
    `<button class="chip ${state.rubric ? "" : "active"}" data-rubric="">все рубрики</button>` +
    rubricItems
      .map((rubric) => {
        const count = data?.rubrics?.[rubric.code];
        return `<button class="chip ${state.rubric === rubric.code ? "active" : ""}" data-rubric="${esc(
          rubric.code,
        )}" title="${esc(rubric.title)}">${esc(rubric.code)}${count ? ` · ${count}` : ""}</button>`;
      })
      .join("");

  el.networkChips.innerHTML =
    `<button class="chip ${state.network ? "" : "active"}" data-network="">все сети</button>` +
    state.caps
      .filter((cap) => cap.mode !== "disabled")
      .map((cap) => {
        const count = data?.facets?.[cap.network];
        return `<button class="chip ${state.network === cap.network ? "active" : ""}" data-network="${esc(
          cap.network,
        )}">${esc(cap.title)}${count ? ` · ${count}` : ""}</button>`;
      })
      .join("");
}

async function runSearch(reset = true) {
  if (state.loading) return;
  state.loading = true;

  if (reset) {
    state.offset = 0;
    el.results.innerHTML = "";
  }
  el.status.textContent = "Проверяю сообщения…";

  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.network) params.set("network", state.network);
  if (state.rubric) params.set("rubric", state.rubric);
  if (state.verdict) params.set("verdict", state.verdict);
  params.set("sort", state.sort);
  params.set("limit", String(state.limit));
  params.set("offset", String(state.offset));

  try {
    const data = await api(`/api/search?${params}`);
    state.matched = data.matched;
    el.results.insertAdjacentHTML("beforeend", data.items.map(cardHtml).join(""));
    renderStatus(data);
    renderChips(data);

    el.empty.classList.toggle("hidden", state.matched > 0);
    el.more.classList.toggle("hidden", state.matched <= state.offset + data.items.length);
    el.actions.classList.toggle("hidden", state.matched === 0);
  } catch (error) {
    el.status.textContent = "";
    el.empty.classList.remove("hidden");
    el.empty.innerHTML = `<p>Не удалось выполнить поиск.</p><p class="muted">${esc(error.message)}</p>`;
  } finally {
    state.loading = false;
  }
}

async function loadMore() {
  state.offset += state.limit;
  await runSearch(false);
  haptic();
}

async function loadStats() {
  try {
    const stats = await api("/api/stats");
    el.brandSub.textContent = `проверено ${stats.checked_total} · с отметкой ${stats.flagged_total}`;
    if (state.matched === 0 && stats.comments_total === 0) {
      el.status.textContent = "База пока пустая — добавьте бота в чат и напишите сообщение.";
    }
  } catch {
    el.brandSub.textContent = "нет связи с сервером";
  }
}

async function loadMe() {
  try {
    const me = await api("/api/me");
    state.botUsername = me.bot_username || "";
  } catch {
    state.botUsername = "";
  }
}

function addBotToChat() {
  if (!state.botUsername) {
    toast("Добавьте бота в чат вручную через его профиль");
    return;
  }
  const url = `https://t.me/${state.botUsername}?startgroup=true&admin=post_messages`;
  if (tg?.openTelegramLink) tg.openTelegramLink(url);
  else openLink(url);
  haptic("medium");
}

async function loadRubrics() {
  try {
    state.rubrics = await api("/api/rubrics");
  } catch {
    state.rubrics = [];
  }
}

async function loadCaps() {
  try {
    state.caps = await api("/api/capabilities");
  } catch {
    state.caps = [];
  }
  el.caps.innerHTML = state.caps
    .map((cap) => {
      const status = cap.ready ? "подключено" : cap.mode === "disabled" ? "недоступно" : "нужна настройка";
      return `<div class="cap"><strong>${esc(cap.title)}</strong> — ${status}. ${esc(cap.hint)}</div>`;
    })
    .join("");
}

function sourceHtml(source) {
  const initials = (source.title || source.handle || "?")
    .split(/\s+/)
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase();

  const avatar = source.avatar_url
    ? `<img class="avatar" src="${esc(safeUrl(source.avatar_url))}" alt="" loading="lazy" />`
    : `<div class="avatar">${esc(initials)}</div>`;

  const last = source.last_crawl_at ? fmtDate(source.last_crawl_at) : "ещё не обходился";
  const live = source.mode === "live";
  const badge = live
    ? "live"
    : source.mode === "experimental"
      ? "бета"
      : source.is_active
        ? "активен"
        : "пауза";
  const error = source.last_error
    ? `<div class="source-error">${esc(source.last_error)}</div>`
    : "";

  return `<div class="source">
    <div class="source-top">
      ${avatar}
      <div class="source-title">
        <strong>${esc(source.title)}</strong>
        <span>${esc(source.network_title || source.network)} · ${esc(source.handle)}</span>
      </div>
      <span class="badge ${source.is_active ? "" : "clean"}">${badge}</span>
    </div>
    <div class="source-stats">
      <span>постов: ${source.posts_count}</span>
      <span>комментариев: ${source.comments_count}</span>
      <span>${live ? "сообщения в реальном времени" : `обход: ${last}`}</span>
    </div>
    ${error}
    <div class="source-actions">
      ${
        live
          ? ""
          : `<button data-action="crawl" data-id="${source.id}">Обновить</button>`
      }
      <button data-action="toggle" data-id="${source.id}" data-active="${source.is_active}">${
        source.is_active ? "Пауза" : "Включить"
      }</button>
      <button class="danger" data-action="delete" data-id="${source.id}">Удалить</button>
    </div>
  </div>`;
}

async function loadSources() {
  try {
    const sources = await api("/api/sources");
    el.sources.innerHTML = sources.length
      ? sources.map(sourceHtml).join("")
      : `<p class="muted">Пока ни одного источника.</p>`;
  } catch (error) {
    el.sources.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  }
}

async function handleSourceAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;

  const id = button.dataset.id;
  const action = button.dataset.action;
  haptic();

  try {
    if (action === "crawl") {
      await api(`/api/sources/${id}/crawl`, { method: "POST" });
      toast("Поставлено в очередь — обход начнётся в ближайшие секунды");
    }
    if (action === "toggle") {
      const active = button.dataset.active === "true";
      await api(`/api/sources/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_active: !active }),
      });
      toast(active ? "Источник на паузе" : "Источник включён");
    }
    if (action === "delete") {
      await api(`/api/sources/${id}`, { method: "DELETE" });
      toast("Источник удалён");
    }
    await loadSources();
  } catch (error) {
    toast(error.message);
  }
}

async function addSource() {
  const ref = el.sheetInput.value.trim();
  if (!ref) {
    toast("Укажите ссылку на источник");
    return;
  }

  const button = $("btn-source-save");
  button.disabled = true;
  button.textContent = "Проверяю…";

  try {
    const resolved = await api("/api/sources/resolve", {
      method: "POST",
      body: JSON.stringify({ ref }),
    });
    await api("/api/sources", {
      method: "POST",
      body: JSON.stringify({ network: resolved.network, ref }),
    });
    el.sheet.classList.add("hidden");
    el.sheetInput.value = "";
    toast(`Добавлено: ${resolved.title}`);
    await loadSources();
    haptic("medium");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Добавить";
  }
}

function watchHtml(watch) {
  const target = watch.source_id ? "источник" : watch.network ? watch.network : "все сети";
  return `<div class="watch">
    <strong>${esc(watch.keyword)}</strong>
    <span class="badge">${esc(target)}</span>
    <span class="muted">${watch.hits_count} срабатываний</span>
    <button class="link-btn" data-watch="${watch.id}">удалить</button>
  </div>`;
}

async function loadWatches() {
  try {
    const watches = await api("/api/watches");
    el.watches.innerHTML = watches.length
      ? watches.map(watchHtml).join("")
      : `<p class="muted">Слов пока нет.</p>`;
  } catch (error) {
    el.watches.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  }
}

async function addWatch(event) {
  event.preventDefault();
  const keyword = el.watchInput.value.trim();
  if (keyword.length < 2) {
    toast("Слишком короткое слово");
    return;
  }
  try {
    await api("/api/watches", { method: "POST", body: JSON.stringify({ keyword }) });
    el.watchInput.value = "";
    toast("Слово добавлено");
    await loadWatches();
    haptic("medium");
  } catch (error) {
    toast(error.message);
  }
}

async function downloadCsv() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.network) params.set("network", state.network);
  if (state.rubric) params.set("rubric", state.rubric);
  params.set("limit", "2000");

  try {
    const response = await fetch(`/api/search.csv?${params}`, {
      headers: tg?.initData ? { Authorization: `tma ${tg.initData}` } : {},
    });
    if (!response.ok) throw new Error("Не удалось выгрузить CSV");

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "comments.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast("Выгрузка готова");
  } catch (error) {
    toast(error.message);
  }
}

function switchView(name) {
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("hidden", view.id !== `view-${name}`);
  });
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === name);
  });
  haptic();
}

function bindUi() {
  el.form.addEventListener("submit", (event) => {
    event.preventDefault();
    state.q = el.q.value.trim();
    runSearch(true);
  });

  el.rubricChips.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-rubric]");
    if (!chip) return;
    state.rubric = chip.dataset.rubric;
    runSearch(true);
    haptic();
  });

  el.networkChips.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-network]");
    if (!chip) return;
    state.network = chip.dataset.network;
    runSearch(true);
    haptic();
  });

  el.more.addEventListener("click", loadMore);
  el.csv.addEventListener("click", downloadCsv);
  $("btn-refresh").addEventListener("click", async () => {
    await Promise.all([loadStats(), loadSources()]);
    await runSearch(true);
    toast("Обновлено");
  });

  document.getElementById("tabbar").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-view]");
    if (tab) switchView(tab.dataset.view);
  });

  $("btn-add-source").addEventListener("click", () => {
    el.sheet.classList.remove("hidden");
    el.sheetInput.focus({ preventScroll: true });
  });

  el.sheet.addEventListener("click", (event) => {
    if (event.target.closest("[data-close]")) el.sheet.classList.add("hidden");
  });

  $("btn-source-save").addEventListener("click", addSource);
  el.addToChat.addEventListener("click", addBotToChat);
  el.sources.addEventListener("click", handleSourceAction);
  el.watchForm.addEventListener("submit", addWatch);

  el.watches.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-watch]");
    if (!button) return;
    try {
      await api(`/api/watches/${button.dataset.watch}`, { method: "DELETE" });
      await loadWatches();
      haptic();
    } catch (error) {
      toast(error.message);
    }
  });

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-link]");
    if (!link) return;
    event.preventDefault();
    openLink(link.getAttribute("href"));
  });
}

async function boot() {
  if (tg) {
    tg.ready();
    tg.expand();
    tg.setHeaderColor?.("#0d1016");
    tg.setBackgroundColor?.("#0d1016");
  }

  bindUi();

  const params = new URLSearchParams(location.search);
  if (params.get("q")) {
    state.q = params.get("q");
    el.q.value = state.q;
  }
  if (params.get("rubric")) state.rubric = params.get("rubric");

  await Promise.all([loadRubrics(), loadCaps(), loadMe()]);
  renderChips(null);
  await Promise.all([loadStats(), runSearch(true), loadSources(), loadWatches()]);
}

boot();
