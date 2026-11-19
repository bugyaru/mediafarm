/* MediaFarm · полный лог инструментов в деталях задачи: WS-live + REST-догон. */
(() => {
    "use strict";
    const root = document.getElementById("mf-logs");
    if (!root) return;

    const $ = (sel) => root.querySelector(sel);
    const view = $("#mf-log-view");
    const led = $("#mf-log-led");
    const status = $("#mf-log-status");
    const counter = $("#mf-log-count");
    const dl = $("#mf-log-download");
    const older = $("#mf-log-older");
    const API = root.dataset.api;
    const WS = root.dataset.ws;
    const PAGE = Number(root.dataset.page || 500);
    const MAX_ROWS = 6000;

    let cursor = 0, firstId = 0, ws = null, pollTimer = 0, retry = 0;
    let finished = false, manualClose = false;

    const query = () => $("#mf-log-q").value.trim().toLowerCase();
    const follow = () => $("#mf-log-follow").checked;

    function params() {
        const p = new URLSearchParams();
        const step = $("#mf-log-step").value;
        const tool = $("#mf-log-tool").value;
        const streams = ["stdout", "stderr", "meta", "progress"].filter((k) => $("#mf-log-" + k).checked);
        if (step !== "") p.set("step", step);
        if (tool) p.set("tool", tool);
        if (streams.length && streams.length < 4) p.set("stream", streams.join(","));
        const search = $("#mf-log-q").value.trim();
        if (search) p.set("q", search);
        return p;
    }

    function setLed(cls, text) { led.className = "mf-led " + cls; status.textContent = text; }

    function setState(s) {
        if (!s) return;
        root.dataset.state = s;
        const badge = $("#mf-log-state");
        if (badge) { badge.textContent = s; badge.className = "mf-st s-" + s; }
    }

    function updateLinks() {
        const p = params();
        p.set("download", "1");
        dl.href = API + "/raw?" + p.toString();
    }

    function makeRow(r) {
        const el = document.createElement("div");
        el.className = "mf-logline lv-" + r.level + " sm-" + r.stream;
        el.dataset.id = r.id;
        el.dataset.text = (r.line || "").toLowerCase();
        const ts = document.createElement("span");
        ts.className = "ts";
        ts.textContent = (r.ts || "").slice(11, 23);
        ts.title = r.ts || "";
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = (r.step_idx >= 0 ? r.step_idx + "·" : "") + r.tool + "/" + r.stream;
        const tx = document.createElement("span");
        tx.className = "tx";
        tx.textContent = r.line;
        el.append(ts, tag, tx);
        if (query() && !el.dataset.text.includes(query())) el.classList.add("mf-hidden");
        return el;
    }

    function trim() { while (view.childElementCount > MAX_ROWS) view.removeChild(view.firstElementChild); }

    function append(lines) {
        if (!lines || !lines.length) return;
        const frag = document.createDocumentFragment();
        lines.forEach((r) => {
            if (r.id > cursor) cursor = r.id;
            if (!firstId || r.id < firstId) firstId = r.id;
            frag.appendChild(makeRow(r));
        });
        view.appendChild(frag);
        trim();
        counter.textContent = String(view.childElementCount);
        if (follow()) view.scrollTop = view.scrollHeight;
    }

    function prepend(lines) {
        if (!lines || !lines.length) { older.disabled = true; return; }
        const frag = document.createDocumentFragment();
        lines.forEach((r) => {
            if (!firstId || r.id < firstId) firstId = r.id;
            frag.appendChild(makeRow(r));
        });
        const h = view.scrollHeight;
        view.insertBefore(frag, view.firstChild);
        view.scrollTop = view.scrollHeight - h;
        counter.textContent = String(view.childElementCount);
        older.disabled = lines.length < PAGE;
    }

    async function fetchJSON(url) {
        const r = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
    }

    async function pull() {
        const p = params();
        p.set("after", String(cursor));
        p.set("limit", "1000");
        try {
            const data = await fetchJSON(API + "?" + p.toString());
            append(data.lines);
            setState(data.state);
        } catch (e) { console.debug("log pull failed", e); }
    }

    async function loadOlder() {
        if (!firstId) return;
        const p = params();
        p.set("before", String(firstId));
        p.set("limit", String(PAGE));
        older.disabled = true;
        try {
            const data = await fetchJSON(API + "?" + p.toString());
            prepend(data.lines);
        } catch (e) { older.disabled = false; }
    }

    function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = 0; } }
    function startPoll() { if (!pollTimer) pollTimer = setInterval(pull, 3000); }

    function stopWS() {
        manualClose = true;
        if (ws) { try { ws.close(); } catch (e) { console.debug(e); } ws = null; }
    }

    function startWS() {
        if (finished) { setLed("ok", "задача завершена"); return; }
        const p = params();
        p.set("after", String(cursor));
        const proto = location.protocol === "https:" ? "wss://" : "ws://";
        manualClose = false;
        try { ws = new WebSocket(proto + location.host + WS + "?" + p.toString()); }
        catch (e) { setLed("err", "WS недоступен — опрос"); startPoll(); return; }
        setLed("warn pulse", "подключение…");
        ws.onopen = () => { retry = 0; stopPoll(); setLed("ok", "live"); };
        ws.onmessage = (ev) => {
            let msg;
            try { msg = JSON.parse(ev.data); } catch (e) { return; }
            if (msg.type === "lines") { append(msg.lines); setState(msg.state); }
            else if (msg.type === "state") { setState(msg.state); }
            else if (msg.type === "eof") { setState(msg.state); finished = true; setLed("ok", "задача завершена"); }
            else if (msg.type === "error") { setLed("err", msg.message || "ошибка"); }
        };
        ws.onclose = () => {
            ws = null;
            if (manualClose || finished) return;
            setLed("err", "WS off — опрос каждые 3 с");
            startPoll();
            retry = Math.min(retry + 1, 6);
            setTimeout(startWS, 1000 * Math.pow(2, retry));
        };
    }

    async function reset() {
        stopWS();
        stopPoll();
        view.innerHTML = "";
        cursor = 0; firstId = 0; finished = false;
        counter.textContent = "0";
        updateLinks();
        const p = params();
        p.set("tail", "true");
        p.set("limit", String(PAGE));
        setLed("warn pulse", "загрузка…");
        try {
            const data = await fetchJSON(API + "?" + p.toString());
            append(data.lines);
            older.disabled = !data.has_more;
            setState(data.state);
        } catch (e) {
            setLed("err", "лог недоступен: " + e.message);
            return;
        }
        startWS();
    }

    $("#mf-log-step").addEventListener("change", reset);
    $("#mf-log-tool").addEventListener("change", reset);
    ["stdout", "stderr", "meta", "progress"].forEach((k) =>
        $("#mf-log-" + k).addEventListener("change", reset));
    let qt = 0;
    $("#mf-log-q").addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(reset, 400); });
    $("#mf-log-reload").addEventListener("click", reset);
    $("#mf-log-clear").addEventListener("click", () => { view.innerHTML = ""; counter.textContent = "0"; });
    $("#mf-log-wrap").addEventListener("change", (e) => view.classList.toggle("wrap", e.target.checked));
    $("#mf-log-follow").addEventListener("change", (e) => { if (e.target.checked) view.scrollTop = view.scrollHeight; });
    older.addEventListener("click", loadOlder);
    view.addEventListener("scroll", () => {
        $("#mf-log-follow").checked = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
    });
    window.addEventListener("beforeunload", stopWS);

    updateLinks();
    reset();
})();