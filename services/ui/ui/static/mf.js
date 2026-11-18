/* MediaFarm · глобальное поведение консоли: часы, WS-LED, flash, таблицы, drawer, тосты. */
(() => {
    "use strict";
    const $  = (s, r = document) => r.querySelector(s);
    const $$ = (s, r = document) => [...r.querySelectorAll(s)];
    const esc = (s) => String(s).replace(/[&<>"']/g,
        c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    /* ── тосты ── */
    function toast(kind, text) {
        let box = $("#mf-toasts");
        if (!box) { box = document.createElement("div"); box.id = "mf-toasts"; document.body.appendChild(box); }
        const t = document.createElement("div");
        t.className = "mf-toast " + kind;
        t.textContent = text;
        box.appendChild(t);
        setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .4s"; }, 3000);
        setTimeout(() => t.remove(), 3500);
    }

    /* ── часы пульта ── */
    function tickClock() {
        const el = $("#mf-clock");
        if (el) el.textContent = new Date().toISOString().slice(11, 19) + " UTC";
    }

    /* ── WS-индикатор ── */
    function wsLed(state, label) {
        const led = $("#mf-ws-led"), txt = $("#mf-ws-txt");
        if (led) led.className = "mf-led " + state;
        if (txt) txt.textContent = label;
    }
    document.body.addEventListener("htmx:wsConnecting", () => wsLed("warn pulse", "WS…"));
    document.body.addEventListener("htmx:wsOpen",       () => wsLed("ok", "WS live"));
    document.body.addEventListener("htmx:wsClose",      () => wsLed("err", "WS off"));
    document.body.addEventListener("htmx:wsError",      () => wsLed("err", "WS err"));

    /* ── flash ── */
    let flashTimer = 0;
    function armFlash() {
        const f = $("main .flash");
        clearTimeout(flashTimer);
        if (!f) return;
        f.classList.remove("mf-hide");
        flashTimer = setTimeout(() => f.classList.add("mf-hide"), 4500);
    }

    /* ── относительное время и размеры ── */
    function humanBytes(n) {
        n = Number(n);
        if (!isFinite(n) || n <= 0) return "—";
        const u = ["B", "KiB", "MiB", "GiB", "TiB"];
        let i = 0;
        while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
        return (i === 0 ? n : n.toFixed(1)) + " " + u[i];
    }
    function relTime(iso) {
        const t = Date.parse(iso);
        if (isNaN(t)) return "";
        const s = Math.max(0, (Date.now() - t) / 1000);
        if (s < 8) return "только что";
        if (s < 60) return Math.floor(s) + " с назад";
        if (s < 3600) return Math.floor(s / 60) + " мин назад";
        if (s < 86400) return Math.floor(s / 3600) + " ч назад";
        return Math.floor(s / 86400) + " дн назад";
    }
    function refreshLive() {
        $$("[data-ts]").forEach(el => {
            const iso = el.getAttribute("data-ts");
            if (!iso) return;
            el.textContent = relTime(iso);
            el.title = new Date(iso).toLocaleString();
            if (el.hasAttribute("data-stale")) {
                const age = (Date.now() - Date.parse(iso)) / 1000;
                el.classList.toggle("mf-bad", age > 90);
                const led = el.parentElement && el.parentElement.querySelector(".mf-led");
                if (led && el.hasAttribute("data-led")) {
                    led.classList.toggle("ok", age <= 90);
                    led.classList.toggle("err", age > 90);
                }
            }
        });
        $$("[data-bytes]").forEach(el => { el.textContent = humanBytes(el.getAttribute("data-bytes")); });
    }

    /* ── drawer ── */
    function openDrawer() {
        const d = $("#mf-drawer");
        if (d) { d.classList.add("open"); d.setAttribute("aria-hidden", "false"); }
    }
    function closeDrawer() {
        const d = $("#mf-drawer");
        if (d) { d.classList.remove("open"); d.setAttribute("aria-hidden", "true"); }
    }
    document.body.addEventListener("click", (e) => {
        if (e.target.closest && e.target.closest("[data-drawer-close]")) closeDrawer();
        const cp = e.target.closest && e.target.closest("[data-copy]");
        if (cp) {
            const txt = cp.getAttribute("data-copy") || cp.textContent;
            navigator.clipboard.writeText(txt.trim())
                .then(() => toast("info", "Скопировано в буфер"))
                .catch(() => toast("err", "Не удалось скопировать"));
        }
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { closeDrawer(); const f = document.activeElement; if (f && f.type === "search") { f.value = ""; f.dispatchEvent(new Event("input")); } }
        if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
            const f = $(".mf-toolbar input[type=search]");
            if (f) { e.preventDefault(); f.focus(); }
        }
    });
    document.body.addEventListener("htmx:before-request", (e) => {
        const elt = e.detail.elt;
        if (elt.hasAttribute && elt.hasAttribute("data-drawer")) {
            const body = $("#mf-drawer-body");
            if (body) body.innerHTML = '<div class="mf-drawer-load"><span class="mf-spin"></span>Загрузка деталей…</div>';
            openDrawer();
        }
    });
    document.body.addEventListener("htmx:after-request", (e) => {
        const elt = e.detail.elt;
        if (elt.hasAttribute && elt.hasAttribute("data-drawer")) {
            const ok = e.detail.successful ?? (e.detail.xhr && e.detail.xhr.status < 400);
            if (!ok) {
                const body = $("#mf-drawer-body");
                if (body) body.innerHTML = '<div class="mf-result err"><div class="ico">✕</div><div class="bd"><div class="tt">Не удалось загрузить детали</div></div></div>';
            }
        }
    });

    /* ── энхансер таблиц: фильтр, сортировка, сохранённые виды ── */
    const VKEY = (id) => "mf-views:" + id;
    function loadViews(id) { try { return JSON.parse(localStorage.getItem(VKEY(id)) || "{}"); } catch { return {}; } }
    function saveViews(id, v) { try { localStorage.setItem(VKEY(id), JSON.stringify(v)); } catch {} }

    function enhance(wrap) {
        if (wrap.dataset.mfReady) return;
        const table = wrap.querySelector("table");
        const tbody = table && table.querySelector("tbody");
        if (!tbody) return;
        wrap.dataset.mfReady = "1";
        const id = wrap.id || ("t" + Math.random().toString(36).slice(2, 8));
        wrap.id = id;

        const bar = document.createElement("div");
        bar.className = "mf-toolbar";
        bar.innerHTML =
            '<input type="search" placeholder="Фильтр по таблице…  ( / )" autocomplete="off">' +
            '<select class="mf-sort" aria-label="Сортировка"><option value="">без сортировки</option></select>' +
            '<span class="mf-views"><select class="mf-view-sel" aria-label="Сохранённые виды"><option value="">виды…</option></select>' +
            '<button type="button" class="mf-btn mf-btn-sm mf-view-save" title="Сохранить текущий фильтр и сортировку">＋ вид</button></span>' +
            '<span class="mf-count">показано: <b>0</b> / <span class="tot">0</span></span>';
        wrap.parentNode.insertBefore(bar, wrap);

        const ths = $$("thead th", table);
        const sortSel = $(".mf-sort", bar);
        ths.forEach((th, i) => {
            if (th.classList.contains("mf-act")) return;
            const o = document.createElement("option");
            o.value = i; o.textContent = "по: " + th.textContent.trim();
            sortSel.appendChild(o);
        });

        const filterInp = $("input[type=search]", bar);
        const viewSel = $(".mf-view-sel", bar);
        const countB = $(".mf-count b", bar), countT = $(".mf-count .tot", bar);

        const pairs = () => {
            const out = [];
            $$("tr", tbody).forEach(tr => {
                if (tr.classList.contains("mf-detail")) return;
                const nx = tr.nextElementSibling;
                out.push([tr, nx && nx.classList.contains("mf-detail") ? nx : null]);
            });
            return out;
        };
        const cellText = (tr, i) => (tr.children[i] ? tr.children[i].textContent : "").trim().toLowerCase();

        function applyFilter() {
            const q = filterInp.value.trim().toLowerCase();
            let shown = 0, total = 0;
            pairs().forEach(([tr, det]) => {
                total++;
                const hit = !q || tr.textContent.toLowerCase().includes(q);
                tr.classList.toggle("mf-hidden", !hit);
                if (det) det.classList.toggle("mf-hidden", !hit);
                if (hit) shown++;
            });
            countB.textContent = shown; countT.textContent = total;
        }
        function applySort() {
            const i = sortSel.value;
            if (i === "") return;
            const idx = Number(i);
            const num = ths[idx] && ths[idx].classList.contains("mf-num");
            pairs().sort(([a], [b]) => {
                const x = cellText(a, idx), y = cellText(b, idx);
                if (num) return (parseFloat(x) || 0) - (parseFloat(y) || 0);
                return x.localeCompare(y, "ru");
            }).forEach(([tr, det]) => { tbody.appendChild(tr); if (det) tbody.appendChild(det); });
        }
        function refreshViewSel() {
            const views = loadViews(id);
            viewSel.innerHTML = '<option value="">виды…</option>' +
                Object.keys(views).map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
        }

        filterInp.addEventListener("input", applyFilter);
        sortSel.addEventListener("change", applySort);
        $(".mf-view-save", bar).addEventListener("click", () => {
            const name = (prompt("Имя вида (фильтр + сортировка):") || "").trim();
            if (!name) return;
            const views = loadViews(id);
            views[name] = { q: filterInp.value, sort: sortSel.value };
            saveViews(id, views); refreshViewSel(); viewSel.value = name;
            toast("ok", "Вид «" + name + "» сохранён");
        });
        viewSel.addEventListener("change", () => {
            const v = loadViews(id)[viewSel.value];
            if (!v) return;
            filterInp.value = v.q || ""; sortSel.value = v.sort || "";
            applySort(); applyFilter();
            toast("info", "Вид «" + viewSel.value + "» применён");
        });

        wrap._mfApply = applyFilter;
        refreshViewSel(); applyFilter();
    }
    function enhanceAll() { $$(".mf-enhance").forEach(enhance); }
    function reapplyFilters() { $$(".mf-enhance").forEach(w => w._mfApply && w._mfApply()); }

    /* ── массовая отмена видимых задач ── */
    document.body.addEventListener("click", (e) => {
        const btn = e.target.closest && e.target.closest("#mf-cancel-visible");
        if (!btn || btn.disabled) return;
        const targets = $$("#jobs-body tr[data-id]").filter(tr =>
            !tr.classList.contains("mf-hidden") &&
            ["queued", "running", "retry_scheduled"].includes(tr.dataset.state));
        if (!targets.length) { toast("info", "Нет активных задач для отмены"); return; }
        if (!confirm("Запросить отмену " + targets.length + " задач(и)?")) return;
        btn.disabled = true;
        targets.forEach((tr, i) => {
            tr.classList.add("mf-cancelling");
            setTimeout(() => {
                if (window.htmx) htmx.ajax("POST", "/jobs/" + tr.dataset.id + "/cancel", { swap: "none" });
            }, i * 120);
        });
        toast("ok", "Отмена запрошена: " + targets.length);
        setTimeout(() => { btn.disabled = false; }, targets.length * 120 + 800);
    });

    /* ── глобальные HTMX-события ── */
    document.body.addEventListener("htmx:after-settle", () => {
        armFlash(); refreshLive(); enhanceAll(); reapplyFilters();
    });
    document.body.addEventListener("htmx:responseError", (e) => {
        const elt = e.detail.elt;
        if (elt.hasAttribute && elt.hasAttribute("data-drawer")) return;
        const code = e.detail.xhr ? e.detail.xhr.status : "?";
        toast("err", "HTTP " + code + " — действие не выполнено");
    });
    document.body.addEventListener("htmx:sendError", () => toast("err", "Сеть недоступна — запрос не отправлен"));

    /* ── старт ── */
    function init() {
        tickClock(); setInterval(tickClock, 1000);
        refreshLive(); setInterval(refreshLive, 5000);
        armFlash(); enhanceAll();
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
})();