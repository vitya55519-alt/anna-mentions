const tg = window.Telegram?.WebApp;

const state = {
  q: "",
  network: "",
  rubric: "",
  verdict: "",
  sort: "relevance",
  offset: 0,
  limit: 20,
  matched: 0,
  rubrics: [],
  caps: [],
  botUsername: "",
  isAdmin: false,
  accLoginId: "",
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
  sortBtn: $("btn-sort"),
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
  lookupInput: $("lookup-input"),
  lookupBtn: $("btn-lookup"),
  lookupStatus: $("lookup-status"),
  lookupResults: $("lookup-results"),
  joinInput: $("join-input"),
  joinBtn: $("btn-join-import"),
  joinStatus: $("join-status"),
  joinList: $("join-list"),
  joinSummary: $("join-panel")?.querySelector("summary"),
  publicBtn: $("btn-public"),
  publicStatus: $("public-status"),
  publicResults: $("public-results"),
  accountsList: $("accounts-list"),
  accountsSummary: $("accounts-panel")?.querySelector("summary"),
  accountSheet: $("account-sheet"),
  accStepPhone: $("acc-step-phone"),
  accStepCode: $("acc-step-code"),
  accStepPassword: $("acc-step-password"),
  accPhone: $("acc-phone"),
  accCode: $("acc-code"),
  accPassword: $("acc-password"),
  accStatus: $("acc-status"),
};

const KINDS = {
  username: "юзернейм",
  id: "id",
  phone: "телефон",
};

const VERDICTS = {
  likely: "вероятно",
  borderline: "пограничное",
  clean: "не попадает",
  pending: "не проверено",
};

const JOIN_STATUSES = {
  pending: "в очереди",
  joined: "подключён",
  exists: "уже был",
  error: "ошибка",
  cancelled: "отменён",
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
  const source = item.source || {};
  const chatUrl = safeUrl(source.url);
  const chatTitle = source.title || source.handle || "";
  const chat = chatUrl
    ? `<a class="chat-link" href="${esc(chatUrl)}" title="${esc(chatTitle)}">чат: ${esc(
        chatTitle || shortUrl(chatUrl),
      )}</a>`
    : `<span>${esc(chatTitle)}</span>`;
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
      <a href="${esc(url)}" data-link>${esc(shortUrl(url) || "сообщение")}</a>
      <span class="spacer"></span>
      ${chat}
    </div>
  </article>`;
}

function renderStatus(data) {
  const seconds = (data.took_ms / 1000).toFixed(1).replace(".", ",");
  const note = data.fallback ? " · точных нет, показаны по отдельным словам" : "";
  el.status.textContent = `Готово за ${seconds} с · проверено ${data.checked} · совпадений ${data.matched}${note}`;
}

function renderSortLabel() {
  el.sortBtn.textContent =
    state.sort === "relevance" ? "сортировка: по точности" : "сортировка: по дате";
}

function lookupGroup(group, index) {
  const remain = group.total - group.items.length;
  // Длинный список не раскрываем целиком: первые десять — открыты, остальные сворачиваются.
  return `<details class="lookup-group" ${index < 10 ? "open" : ""}>
    <summary class="lookup-head">
      <b>${esc(group.raw)}</b>
      <span class="muted">${esc(KINDS[group.kind] || group.kind)} · ${group.total}</span>
    </summary>
    ${group.items.map(cardHtml).join("")}
    ${remain > 0 ? `<p class="muted">ещё ${remain} — ищите через поиск</p>` : ""}
  </details>`;
}

function lookupHtml(data) {
  const found = data.groups.filter((group) => group.total > 0);
  const empty = data.groups.filter((group) => !group.total);
  const blocks = found.map(lookupGroup);

  if (empty.length) {
    blocks.push(`<div class="lookup-group">
      <div class="lookup-head">
        <b>без совпадений</b>
        <span class="muted">${empty.length}</span>
      </div>
      <p class="muted">${empty.map((group) => esc(group.raw)).join(", ")}</p>
    </div>`);
  }

  if (data.unknown.length) {
    blocks.push(`<div class="lookup-group">
      <div class="lookup-head"><b>не разобрал</b></div>
      <p class="muted">${data.unknown.map(esc).join(", ")}</p>
    </div>`);
  }

  return blocks.join("") || `<p class="muted">Список пуст.</p>`;
}

async function runLookup() {
  const raw = el.lookupInput.value.trim();
  if (!raw) {
    toast("Вставьте список");
    return;
  }

  el.lookupBtn.disabled = true;
  el.lookupStatus.textContent = "Проверяю…";
  el.lookupResults.innerHTML = "";
  try {
    // Разбирает сервер: телефон с пробелами и дефисами — одна запись, а не три.
    const data = await api("/api/lookup", {
      method: "POST",
      body: JSON.stringify({ identifiers: [raw] }),
    });
    const seconds = (data.took_ms / 1000).toFixed(1).replace(".", ",");
    const count = data.groups.length + data.unknown.length;
    const limit = data.dropped ? ` · не влезло ${data.dropped} (лимит 100)` : "";
    el.lookupStatus.textContent = `Готово за ${seconds} с · записей ${count} · сообщений ${data.total}${limit}`;
    el.lookupResults.innerHTML = lookupHtml(data);
    haptic("medium");
  } catch (error) {
    el.lookupStatus.textContent = "";
    toast(error.message);
  } finally {
    el.lookupBtn.disabled = false;
  }
}

function renderChips(data) {
  const rubricItems = state.rubrics.slice(0, 16);
  el.rubricChips.innerHTML =
    `<button class="chip ${state.rubric ? "" : "active"}" data-rubric="">все статьи</button>` +
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
    state.isAdmin = Boolean(me.is_admin);
  } catch {
    state.botUsername = "";
    state.isAdmin = false;
  }
  applyAdminVisibility();
}

function applyAdminVisibility() {
  // Вкладку «Источники» с аккаунтами и группами видит только владелец.
  const tab = document.querySelector('.tab[data-view="sources"]');
  if (tab) tab.classList.toggle("hidden", !state.isAdmin);
  if (!state.isAdmin) {
    document.querySelectorAll(".view").forEach((view) => {
      if (view.id === "view-sources" && !view.classList.contains("hidden")) {
        switchView("search");
      }
    });
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
  const scan = source.mode === "scan";
  const badge = live
    ? "live"
    : scan
      ? "скан"
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
      <span>${
        live
          ? "сообщения в реальном времени"
          : scan
            ? `читает аккаунт · обход: ${last}`
            : `обход: ${last}`
      }</span>
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

function joinTaskHtml(task) {
  const label = JOIN_STATUSES[task.status] || task.status;
  const tone = task.status === "joined" || task.status === "exists" ? "clean" : task.status === "error" ? "likely" : "";
  const account = task.account_index ? `аккаунт ${task.account_index}` : "ждёт аккаунт";
  const group = task.group_joined ? " · с группой обсуждения" : "";
  const meta = task.error
    ? esc(task.error)
    : `${account}${group}${task.attempts > 1 ? ` · попыток ${task.attempts}` : ""}`;
  const cancel = task.status === "pending"
    ? `<button class="link-btn" data-join-cancel="${task.id}">отменить</button>`
    : "";
  return `<div class="join-task">
    <span class="ref" title="${esc(task.ref)}">${esc(task.ref)}</span>
    <span class="badge ${tone}">${esc(label)}</span>
    <span class="meta" title="${meta}">${meta}</span>
    ${cancel}
  </div>`;
}

async function loadJoins() {
  try {
    const data = await api("/api/sources/import");
    const pending = data.counts.pending || 0;
    const joined = (data.counts.joined || 0) + (data.counts.exists || 0);
    const today = Object.values(data.joined_today || {}).reduce((sum, value) => sum + value, 0);
    if (!data.accounts) {
      el.joinStatus.textContent = "Аккаунты-сборщики не настроены: нужны TG_SESSION_1..3";
    } else {
      const limit = data.daily_limit * data.accounts;
      el.joinStatus.textContent = `В очереди: ${pending} · подключено: ${joined} · сегодня: ${today} из ${limit} слотов`;
    }
    if (el.joinSummary) {
      let label = "Массовое подключение";
      if (pending) label += ` · ${pending} в очереди`;
      else if (data.counts.error) label += ` · ошибок: ${data.counts.error}`;
      el.joinSummary.textContent = label;
    }
    el.joinList.innerHTML = data.tasks.length
      ? data.tasks.slice(0, 20).map(joinTaskHtml).join("")
      : `<p class="muted">Очередь пуста.</p>`;
  } catch (error) {
    el.joinStatus.textContent = error.message;
  }
}

async function importJoins() {
  const text = el.joinInput.value.trim();
  if (!text) {
    toast("Вставьте хотя бы одну ссылку");
    return;
  }

  el.joinBtn.disabled = true;
  el.joinBtn.textContent = "Ставлю в очередь…";
  try {
    const result = await api("/api/sources/import", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    el.joinInput.value = "";
    const parts = [`В очереди: ${result.queued}`];
    if (result.skipped) parts.push(`пропущено: ${result.skipped}`);
    if (result.invalid?.length) parts.push(`не разобрал: ${result.invalid.length}`);
    toast(parts.join(" · "));
    await loadJoins();
    haptic("medium");
  } catch (error) {
    toast(error.message);
  } finally {
    el.joinBtn.disabled = false;
    el.joinBtn.textContent = "Поставить в очередь";
  }
}

async function handleJoinAction(event) {
  const button = event.target.closest("[data-join-cancel]");
  if (!button) return;
  try {
    await api(`/api/sources/import/${button.dataset.joinCancel}`, { method: "DELETE" });
    await loadJoins();
    haptic();
  } catch (error) {
    toast(error.message);
  }
}

// --- Админка: аккаунты -------------------------------------------------------

function accountHtml(account) {
  const name = [account.first_name, account.username ? `@${account.username}` : ""]
    .filter(Boolean)
    .join(" ");
  const label = name || `id ${account.tg_id || "—"}`;
  const error = account.last_error
    ? `<div class="source-error">${esc(account.last_error)}</div>`
    : "";
  return `<div class="source">
    <div class="source-top">
      <div class="avatar">TG</div>
      <div class="source-title">
        <strong>${esc(label)}</strong>
        <span>${esc(account.phone)} · добавлен ${fmtDate(account.created_at)}</span>
      </div>
      <span class="badge ${account.is_active ? "" : "clean"}">${
        account.is_active ? "работает" : "выключен"
      }</span>
    </div>
    ${error}
    <div class="source-actions">
      <button class="danger" data-acc-delete="${account.id}">Удалить</button>
    </div>
  </div>`;
}

async function loadAccounts() {
  if (!state.isAdmin) return;
  try {
    const accounts = await api("/api/admin/tg");
    if (el.accountsSummary) {
      el.accountsSummary.textContent = `Аккаунты · ${accounts.length}`;
    }
    el.accountsList.innerHTML = accounts.length
      ? accounts.map(accountHtml).join("")
      : `<p class="muted">Пока ни одного аккаунта — добавьте первый ниже.</p>`;
  } catch (error) {
    el.accountsList.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  }
}

function openAccountSheet() {
  state.accLoginId = "";
  el.accPhone.value = "";
  el.accCode.value = "";
  el.accPassword.value = "";
  el.accStatus.textContent = "";
  el.accStepPhone.classList.remove("hidden");
  el.accStepCode.classList.add("hidden");
  el.accStepPassword.classList.add("hidden");
  el.accountSheet.classList.remove("hidden");
  el.accPhone.focus({ preventScroll: true });
}

function closeAccountSheet() {
  el.accountSheet.classList.add("hidden");
}

async function accSendCode() {
  const phone = el.accPhone.value.trim();
  if (!phone) {
    toast("Введите номер телефона");
    return;
  }
  const button = $("btn-acc-phone");
  button.disabled = true;
  el.accStatus.textContent = "Отправляю код…";
  try {
    const result = await api("/api/admin/tg/start", {
      method: "POST",
      body: JSON.stringify({ phone }),
    });
    state.accLoginId = result.login_id;
    el.accStepPhone.classList.add("hidden");
    el.accStepCode.classList.remove("hidden");
    el.accStatus.textContent = result.message || "Код отправлен";
    el.accCode.focus({ preventScroll: true });
    haptic("medium");
  } catch (error) {
    el.accStatus.textContent = "";
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function accSubmitCode() {
  const code = el.accCode.value.trim();
  if (!code) {
    toast("Введите код из Telegram");
    return;
  }
  const button = $("btn-acc-code");
  button.disabled = true;
  el.accStatus.textContent = "Проверяю код…";
  try {
    const result = await api("/api/admin/tg/code", {
      method: "POST",
      body: JSON.stringify({ login_id: state.accLoginId, code }),
    });
    if (result.status === "password") {
      el.accStepCode.classList.add("hidden");
      el.accStepPassword.classList.remove("hidden");
      el.accStatus.textContent = result.message;
      el.accPassword.focus({ preventScroll: true });
      return;
    }
    accDone(result);
  } catch (error) {
    el.accStatus.textContent = "";
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function accSubmitPassword() {
  const password = el.accPassword.value;
  if (!password) {
    toast("Введите пароль");
    return;
  }
  const button = $("btn-acc-password");
  button.disabled = true;
  el.accStatus.textContent = "Проверяю пароль…";
  try {
    const result = await api("/api/admin/tg/password", {
      method: "POST",
      body: JSON.stringify({ login_id: state.accLoginId, password }),
    });
    accDone(result);
  } catch (error) {
    el.accStatus.textContent = "";
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function accDone(result) {
  const who = result.account
    ? [result.account.first_name, result.account.username ? `@${result.account.username}` : ""]
        .filter(Boolean)
        .join(" ")
    : "аккаунт";
  el.accStatus.textContent = result.message || "Аккаунт подключён";
  toast(`Подключён: ${who}`);
  haptic("medium");
  setTimeout(closeAccountSheet, 900);
  await loadAccounts();
}

async function handleAccountAction(event) {
  const button = event.target.closest("[data-acc-delete]");
  if (!button) return;
  try {
    await api(`/api/admin/tg/${button.dataset.accDelete}`, { method: "DELETE" });
    toast("Аккаунт удалён");
    await loadAccounts();
    haptic();
  } catch (error) {
    toast(error.message);
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
    const isTelegram = /t\.me|telegram\.me|^@/i.test(ref);
    if (isTelegram) {
      // Telegram-ссылки вступают в очередь: аккаунт сам зайдёт в чат.
      const result = await api("/api/sources/import", {
        method: "POST",
        body: JSON.stringify({ text: ref }),
      });
      el.sheet.classList.add("hidden");
      el.sheetInput.value = "";
      const parts = [`В очереди: ${result.queued}`];
      if (result.skipped) parts.push(`пропущено: ${result.skipped}`);
      toast(parts.join(" · ") || "Поставлено в очередь");
      await loadJoins();
    } else {
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
    }
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

function publicItemHtml(item) {
  const text = item.text.length > 360 ? `${item.text.slice(0, 360)}…` : item.text;
  const channel = safeUrl(item.channel_url || item.url);
  const date = item.date ? String(item.date).slice(0, 10) : "";
  return `<article class="card">
    <div class="card-head">
      <span class="badge pending">канал</span>
      <span class="verdict pending">${esc(item.channel)}</span>
      <span class="spacer"></span>
      <span class="date">${esc(date)}</span>
    </div>
    <p class="quote">${esc(text)}</p>
    <div class="card-foot">
      <a href="${esc(safeUrl(item.url))}" data-link>открыть пост</a>
      <span class="spacer"></span>
      <a class="chat-link" href="${esc(channel)}" data-link>канал: ${esc(item.channel)}</a>
    </div>
  </article>`;
}

async function runPublicSearch() {
  const query = (el.q.value || state.q || "").trim();
  if (query.length < 2) {
    toast("Сначала напишите ник или текст в строке поиска");
    return;
  }
  el.publicBtn.disabled = true;
  el.publicStatus.textContent = "Ищу в публичных каналах…";
  el.publicResults.innerHTML = "";
  try {
    const data = await api("/api/public-search", {
      method: "POST",
      body: JSON.stringify({ query, limit: 10 }),
    });
    if (data.error && !data.items.length) {
      el.publicStatus.textContent = data.error;
      return;
    }
    el.publicStatus.textContent = `Проверено постов: ${data.checked} · найдено: ${data.items.length}`;
    el.publicResults.innerHTML = data.items.length
      ? data.items.map(publicItemHtml).join("")
      : `<p class="muted">В публичных каналах ничего не нашлось.</p>`;
  } catch (error) {
    el.publicStatus.textContent = "";
    el.publicResults.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  } finally {
    el.publicBtn.disabled = false;
  }
}

function switchView(name) {
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("hidden", view.id !== `view-${name}`);
  });
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === name);
  });
  if (name === "sources") {
    loadJoins();
    loadAccounts();
  }
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
  el.sortBtn.addEventListener("click", () => {
    state.sort = state.sort === "relevance" ? "date" : "relevance";
    renderSortLabel();
    runSearch(true);
    haptic();
  });
  el.csv.addEventListener("click", downloadCsv);
  el.lookupBtn.addEventListener("click", runLookup);
  el.lookupInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) runLookup();
  });
  $("btn-refresh").addEventListener("click", async () => {
    await Promise.all([loadStats(), loadSources(), loadJoins(), loadAccounts()]);
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
  el.joinBtn.addEventListener("click", importJoins);
  el.publicBtn.addEventListener("click", runPublicSearch);
  el.joinList.addEventListener("click", handleJoinAction);
  el.addToChat.addEventListener("click", addBotToChat);
  el.sources.addEventListener("click", handleSourceAction);
  el.accountsList.addEventListener("click", handleAccountAction);
  $("btn-add-account").addEventListener("click", openAccountSheet);
  $("btn-acc-phone").addEventListener("click", accSendCode);
  $("btn-acc-code").addEventListener("click", accSubmitCode);
  $("btn-acc-password").addEventListener("click", accSubmitPassword);
  el.accCode.addEventListener("keydown", (event) => {
    if (event.key === "Enter") accSubmitCode();
  });
  el.accPassword.addEventListener("keydown", (event) => {
    if (event.key === "Enter") accSubmitPassword();
  });
  el.accountSheet.addEventListener("click", (event) => {
    if (event.target.closest("[data-close-sheet]")) closeAccountSheet();
  });
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
  renderSortLabel();
  await Promise.all([
    loadStats(),
    runSearch(true),
    loadSources(),
    loadWatches(),
    loadJoins(),
    loadAccounts(),
  ]);
}

boot();
