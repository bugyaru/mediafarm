/* MediaFarm · хранилища: подсветка строк по результату проверки, фильтры, «Проверить все». */
(() => {
    "use strict";
    const $  = (s, r = document) => r.querySelector(s);
    const $$ = (s, r = document) => [...r.querySelectorAll(s)];
    const esc = (s) => String(s).replace(/[&<>"']/g,
        c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    const rows = () => $$("#st-tbody .st-row");
    const pair = (tr) => {
        const n = tr.nextElementSibling;
        return n && n.classList.contains("st-detail") ? n : null;
    };

    /* ---------- flash ---------- */
    let flashTimer = 0;
    function armFlash() {
        const f = $("#st-flash");
        clearTimeout(flashTimer);
        if (!f || !f.textContent.trim()) return;
        f.classList.remove("st-hide");
        flashTimer = setTimeout(() => f.classList.add("st-hide"), 4500);
    }
    function showFlash(kind, text) {
        const f = $("#st-flash");
        if (!f) return;
        f.className = "st-flash " + kind;
        f.textContent = (kind === "ok" ? "✓ " : "✕ ") + text;
        armFlash();
    }

    /* ---------- сводка ---------- */
    function recount() {
        const r = rows();
        const n = { ok: 0, err: 0, idle: 0 };
        r.forEach(tr => n[tr.dataset.status || "idle"]++);
        const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
        set("#st-n-all", r.length); set("#st-n-ok", n.ok);
        set("#st-n-err", n.err);    set("#st-n-idle", n.idle);
        const all = $("#st-test-all");
        if (all && !all.classList.contains("busy")) all.disabled = r.length === 0;
    }

    /* ---------- фильтр + сортировка (пары строк: основная + детальная) ---------- */
    function applyFilters() {
        const tbody = $("#st-tbody");
        if (!tbody) return;
        const q = ($("#st-filter")?.value || "").trim().toLowerCase();
        const st = $("#st-status")?.value || "";
        const sort = $("#st-sort")?.value || "name";
        const rank = { err: 0, idle: 1, ok: 2 };
        const byName = (a, b) => (a.dataset.name || "").localeCompare(b.dataset.name || "");
        const list = rows();
        list.sort((a, b) => {
            if (sort === "kind")
                return (a.dataset.kind || "").localeCompare(b.dataset.kind || "") || byName(a, b);
            if (sort === "status")
                return (rank[a.dataset.status || "idle"] - rank[b.dataset.status || "idle"]) || byName(a, b);
            return byName(a, b);
        });
        let shown = 0;
        list.forEach(tr => {
            const hay = (tr.dataset.name || "") + " " + (tr.dataset.uri || "") + " " + (tr.dataset.kind || "");
            const show = (!q || hay.includes(q)) && (!st || (tr.dataset.status || "idle") === st);
            tr.classList.toggle("st-hidden", !show);
            const p = pair(tr);
            if (p) p.classList.toggle("st-hidden", !show);
            tbody.appendChild(tr);
            if (p) tbody.appendChild(p);
            if (show) shown++;
        });
        const empty = $("#st-empty");
        if (empty) empty.classList.toggle("st-hidden", list.length > 0);
        const s = $("#st-n-shown");
        if (s) s.textContent = shown;
    }

    /* ---------- подсветка строки ---------- */
    function setBusy(tr, on) {
        tr.classList.toggle("is-busy", on);
        const b = $(".st-btn-test", tr);
        if (b) b.disabled = on;
    }
    function badge(tr, status, d) {
        const b = $(".st-badge", tr);
        if (!b) return;
        if (status === "ok") {
            const lat = d && d.latency_ms != null ? " · " + Math.round(d.latency_ms) + " ms" : "";
            b.textContent = "OK" + lat;
        } else if (status === "err") b.textContent = "FAIL";
        else b.textContent = "";
        b.title = d && d.checked_at ? "Проверено: " + d.checked_at : "";
    }
    function applyStatus(tr, status, d) {
        tr.classList.remove("is-ok", "is-err", "is-idle", "st-land");
        tr.classList.add(status === "ok" ? "is-ok" : "is-err");
        tr.dataset.status = status;
        void tr.offsetWidth;               // перезапуск анимации «вспышки»
        tr.classList.add("st-land");
        badge(tr, status, d);
        recount();
        applyFilters();
    }
    function errPanel(msg, hint) {
        return `<div class="st-result err st-in">
      <div class="st-ico" aria-hidden="true">✕</div>
      <div class="st-body">
        <div class="st-title">Проверка не выполнена</div>
        <pre class="st-err">${esc(msg)}</pre>
        ${hint ? `<div class="st-hint">→ ${esc(hint)}</div>` : ""}
      </div></div>`;
    }
    function failLocal(btn, msg, hint) {
        const tr = btn.closest(".st-row");
        if (!tr) return;
        setBusy(tr, false);
        applyStatus(tr, "err", {});
        const sel = btn.getAttribute("hx-target");
        const cell = sel && $(sel);
        if (cell) cell.innerHTML = errPanel(msg, hint);
    }

    /* ---------- «Проверить все» ---------- */
    const pending = new Set();
    function syncAll() {
        const all = $("#st-test-all");
        if (!all) return;
        all.classList.toggle("busy", pending.size > 0);
        all.disabled = pending.size > 0 || rows().length === 0;
    }

    /* ---------- делегированные HTMX-события (переживают OOB-замены таблицы) ---------- */
    document.body.addEventListener("htmx:before-request", (e) => {
        const elt = e.detail.elt;
        if (elt.classList && elt.classList.contains("st-btn-test")) {
            const tr = elt.closest(".st-row");
            if (!tr || tr.classList.contains("is-busy")) { e.preventDefault(); return; }
            setBusy(tr, true);
            pending.add(elt);
            syncAll();
        }
        if (elt.id === "st-add-form") {
            const ta = elt.querySelector('[name="options"]');
            const raw = ta ? ta.value.trim() : "";
            if (raw) {
                try { JSON.parse(raw); ta.classList.remove("st-invalid"); }
                catch {
                    e.preventDefault();
                    ta.classList.add("st-invalid");
                    showFlash("err", "options: некорректный JSON — нужны двойные кавычки у ключей и значений");
                    ta.focus();
                }
            }
        }
    });

    function done(elt) {
        if (elt.classList && elt.classList.contains("st-btn-test")) {
            const tr = elt.closest(".st-row");
            if (tr) setBusy(tr, false);
            pending.delete(elt);
            syncAll();
        }
    }

    document.body.addEventListener("htmx:after-request", (e) => {
        const elt = e.detail.elt;
        done(elt);
        const ok = e.detail.successful ?? (e.detail.xhr && e.detail.xhr.status < 400);
        if (elt.id === "st-add-form" && ok) {
            elt.reset();
            const ta = elt.querySelector('[name="options"]');
            if (ta) ta.classList.remove("st-invalid");
            const name = elt.querySelector('[name="name"]');
            if (name) name.focus();
        }
    });

    document.body.addEventListener("htmx:responseError", (e) => {
        const elt = e.detail.elt;
        if (elt.classList && elt.classList.contains("st-btn-test")) {
            const code = e.detail.xhr ? e.detail.xhr.status : "?";
            failLocal(elt, "HTTP " + code + " — сервис вернул ошибку вместо результата проверки",
                code === 404 ? "хранилище удалено — обновите страницу" : "смотрите лог ui-сервиса");
        }
    });
    document.body.addEventListener("htmx:sendError", (e) => {
        const elt = e.detail.elt;
        if (elt.classList && elt.classList.contains("st-btn-test"))
            failLocal(elt, "сеть недоступна: запрос не отправлен", "проверьте соединение с ui-сервисом");
    });
    document.body.addEventListener("htmx:timeout", (e) => {
        const elt = e.detail.elt;
        if (elt.classList && elt.classList.contains("st-btn-test"))
            failLocal(elt, "таймаут запроса", "хранилище отвечает слишком долго");
    });

    /* серверная команда HX-Trigger: подсветить строку результатом проверки */
    document.body.addEventListener("storageTested", (e) => {
        const d = e.detail || {};
        const tr = document.getElementById("row-" + d.id);
        if (tr) applyStatus(tr, d.ok ? "ok" : "err", d);
    });

    document.body.addEventListener("htmx:after-settle", () => {
        recount(); applyFilters(); armFlash();
    });

    /* ---------- локальные контролы ---------- */
    const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
    on("#st-filter", "input", applyFilters);
    on("#st-status", "change", applyFilters);
    on("#st-sort", "change", applyFilters);

    document.body.addEventListener("click", (e) => {
        const all = e.target.closest && e.target.closest("#st-test-all");
        if (!all || all.disabled) return;
        const btns = rows()
            .filter(tr => !tr.classList.contains("st-hidden"))
            .map(tr => $(".st-btn-test", tr))
            .filter(b => b && !b.disabled);
        btns.forEach((b, i) => setTimeout(() => {
            if (b.isConnected && !b.disabled) b.click();
        }, i * 150));
    });

    /* ---------- инициализация ---------- */
    recount();
    applyFilters();
    armFlash();
})();