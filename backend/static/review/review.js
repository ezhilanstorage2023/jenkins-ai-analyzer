// Review UI for shadow-mode events. Everything from the server is rendered
// with textContent / createElement, never as markup.

const FILTERS = {
    triage: "?category=needs_triage&unlabeled=true",
    all: "",
    infra: "?category=infra",
    devops: "?category=devops",
    compilation: "?category=compilation",
    needs_triage: "?category=needs_triage",
};

const $ = (id) => document.getElementById(id);

const POLL_MS = 3000;
const TERMINAL = new Set(["done", "failed"]);

let me = { username: "", role: "viewer" };
let controls = {
    kill_switch: false, kill_switch_from_settings: false,
    mode_infra: "shadow", mode_devops: "shadow",
};
let failedRows = [];          // current page of the live failing-jobs table
let totalJobs = 0;
let jobsError = "";           // why the live table is empty, if loading failed
let offset = 0;
let selected = new Set();     // rowKey(row) of ticked jobs on the current page
let tracked = new Set();      // event ids still moving through the worker
let pollTimer = null;
let openId = null;            // event shown in the drawer
let searchTimer = null;
const lastSeen = new Map();   // event id -> status/rebuild signature, to spot changes
let loadSeq = 0;              // newest loadJobs call; older responses are dropped
const knownEvents = new Map();  // rowKey -> {event_id, status} queued from this page


// ---- helpers ------------------------------------------------------------

function el(tag, text, className) {
    const node = document.createElement(tag);

    if (text !== undefined && text !== null) {
        node.textContent = String(text);
    }

    if (className) {
        node.className = className;
    }

    return node;
}


async function request(path, options) {
    // Relative, same-origin: the browser reuses the Basic credentials.
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
        const detail = typeof data.detail === "string" ? data.detail : "";
        throw new Error(detail || ("HTTP " + response.status));
    }

    return { data, headers: response.headers };
}


async function api(path, options) {
    return (await request(path, options)).data;
}


function post(path, body) {
    return api(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
}


let toastTimer = null;

function toast(message, isError) {
    const node = $("toast");
    node.textContent = message;
    node.className = "toast" + (isError ? " error" : "");
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 8000 : 4000);
}


function rowKey(row) {
    return row.job + "#" + row.build_number;
}


function fmtTime(ms) {
    // Jenkins reports epoch milliseconds; show UTC, matching the events table.
    if (!ms) {
        return "—";
    }

    return new Date(ms).toISOString().replace("T", " ").slice(0, 19);
}


function ago(ms) {
    if (!ms) {
        return "unknown";
    }

    const minutes = Math.max(0, Math.round((Date.now() - ms) / 60000));

    if (minutes < 1) { return "just now"; }
    if (minutes < 60) { return minutes + " min ago"; }
    if (minutes < 1440) { return Math.round(minutes / 60) + " h ago"; }

    return Math.round(minutes / 1440) + " d ago";
}


function buildLink(url, label) {
    // Only http(s) links; the URL was already scoped to Jenkins at intake.
    if (!/^https?:\/\//i.test(url)) {
        return el("span", label || url);
    }

    const a = el("a", label || url);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
}


function badge(text, kind) {
    return el("span", text, "badge st-" + kind);
}


// How the build an automatic rebuild queued is doing, as one badge.
function rebuildBadge(rebuild) {
    if (!rebuild) {
        return null;
    }

    const label = rebuild.build_number ? "rebuild #" + rebuild.build_number : "rebuild";

    switch (rebuild.state) {
        case "finished": {
            const kind = rebuild.result === "SUCCESS" ? "sent"
                : (rebuild.result === "FAILURE" ? "failed" : "uncertain");
            return badge(`${label} ${rebuild.result || "finished"}`, kind);
        }
        case "running":
            return badge(`${label} running`, "processing");
        case "cancelled":
            return badge(`${label} cancelled`, "skipped");
        case "unknown":
            return badge(`${label} status unknown`, "uncertain");
        default:
            return badge(`${label} waiting in Jenkins queue`, "queued");
    }
}


function rebuildCell(rebuild) {
    const cell = el("td");
    cell.append(rebuildBadge(rebuild) || document.createTextNode("—"));
    return cell;
}


function rebuildPending(rebuild) {
    return Boolean(rebuild) && (rebuild.state === "queued" || rebuild.state === "running");
}


function rebuildSignature(event) {
    const r = event.rebuild;
    return event.status + "|" + (r ? `${r.state}|${r.result}|${r.build_number}` : "");
}


function stateBadge(row) {
    if (!row.event_id) {
        return badge("not classified", "none");
    }

    if (row.event_status === "done" && row.final_category) {
        const node = badge("done · " + row.final_category, "done");
        return node;
    }

    return badge(row.event_status || "queued", row.event_status || "queued");
}


function canOperate() { return me.role === "operator"; }
function canLabel() { return me.role === "operator" || me.role === "labeler"; }


// ---- execution controls -------------------------------------------------

function renderControls() {
    const box = $("controls");
    box.replaceChildren();

    const infra = controls.mode_infra;
    const devops = controls.mode_devops;
    const anyAuto = infra === "auto" || devops === "auto";

    box.append(el("span", "infra re-run: " + infra,
        "badge " + (infra === "auto" ? "st-triggered" : "st-none")));
    box.append(el("span", "devops email: " + devops,
        "badge " + (devops === "auto" ? "st-triggered" : "st-none")));

    // The banner must not claim "nothing is executed" while something is.
    $("modeBanner").textContent = anyAuto
        ? "AUTOMATIC MODE: " + [
            infra === "auto" ? "failed infra builds are re-run in Jenkins" : null,
            devops === "auto" ? "devops failures are emailed to the owner" : null,
        ].filter(Boolean).join("; ") + ". Everything is recorded and can be stopped with the kill switch."
        : "Shadow mode: classifications and proposed actions are recorded only. " +
          "Nothing is executed against Jenkins, Gerrit or email.";

    if (controls.kill_switch) {
        box.append(el("span", "KILL SWITCH ON", "badge st-failed"));
    }

    // Only matters when something can execute.
    if (!anyAuto || !canOperate()) {
        return;
    }

    const button = el("button",
        controls.kill_switch ? "Resume executions" : "Stop all executions",
        controls.kill_switch ? "" : "danger");
    button.type = "button";
    button.disabled = controls.kill_switch && controls.kill_switch_from_settings;
    button.title = button.disabled ? "Set in server settings; cannot be cleared here." : "";
    button.addEventListener("click", toggleKillSwitch);
    box.append(button);
}


async function toggleKillSwitch() {
    const enable = !controls.kill_switch;

    // Stopping is always one click; resuming asks first.
    if (!enable && !window.confirm("Resume automatic executions?")) {
        return;
    }

    try {
        controls = await post("/api/controls/kill-switch", { enabled: enable });
        renderControls();
        toast(enable ? "All executions stopped." : "Executions resumed.");
    } catch (error) {
        toast("Could not change the kill switch: " + error.message, true);
    }
}


async function loadControls() {
    try {
        controls = await api("/api/controls");
    } catch (error) {
        // Non-fatal: the page works without it.
    }

    renderControls();
}


// ---- tabs ---------------------------------------------------------------

function showTab(name) {
    for (const tab of ["jobs", "events"]) {
        const active = tab === name;
        $("tab-" + tab).setAttribute("aria-selected", String(active));
        $("panel-" + tab).hidden = !active;
    }

    if (name === "events") {
        loadEvents().catch((error) => { $("listStatus").textContent = "Error: " + error.message; });
    }
}


// ---- live failing jobs --------------------------------------------------

function jobsQuery() {
    const params = new URLSearchParams({
        count: $("jobPageSize").value,
        offset: String(offset),
        max_age_days: $("jobAge").value,
        sort: $("jobSort").value,
        include_disabled: String($("jobDisabled").checked),
    });
    const q = $("jobQuery").value.trim();

    if (q) {
        params.set("q", q);
    }

    return "/api/jenkins/failed-builds?" + params.toString();
}


async function loadJobs(resetOffset) {
    if (resetOffset) {
        offset = 0;
    }

    const seq = ++loadSeq;
    jobsError = "";

    $("buildsStatus").textContent = "Fetching live failing jobs from Jenkins…";
    $("jobRefresh").disabled = true;

    try {
        const { data, headers } = await request(jobsQuery());

        if (seq !== loadSeq) {
            return;     // a newer load is in flight; this answer is stale
        }

        failedRows = data;
        totalJobs = Number(headers.get("X-Total-Count")) || data.length;
    } catch (error) {
        if (seq !== loadSeq) {
            return;
        }

        failedRows = [];
        totalJobs = 0;
        jobsError = error.message;
        $("buildsStatus").textContent = "";
        renderJobs();
        return;
    } finally {
        if (seq === loadSeq) {
            $("jobRefresh").disabled = false;
        }
    }

    // A response that started before a classify must not erase it.
    for (const row of failedRows) {
        const known = knownEvents.get(rowKey(row));

        if (!row.event_id && known) {
            row.event_id = known.event_id;
            row.event_status = known.status;
        }
    }

    // Selection only makes sense for builds that are not queued yet.
    const open = new Set(failedRows.filter((row) => !row.event_id).map(rowKey));
    selected = new Set([...selected].filter((key) => open.has(key)));

    for (const row of failedRows) {
        if (row.event_id && (!TERMINAL.has(row.event_status) || rebuildPending(row.rebuild))) {
            track(row.event_id);
        }
    }

    renderJobs();
}


function renderJobs() {
    const body = $("failedBuilds");
    body.replaceChildren();

    if (failedRows.length === 0) {
        const tr = document.createElement("tr");
        const td = jobsError
            ? el("td", "Could not load failing jobs from Jenkins: " + jobsError +
                ". If this keeps happening, check your VPN / network access to Jenkins, then press Refresh.",
                "empty error")
            : el("td", "No failing jobs match these filters.", "empty");
        td.colSpan = 6;
        tr.append(td);
        body.append(tr);
    }

    for (const row of failedRows) {
        const tr = document.createElement("tr");
        const key = rowKey(row);
        const selectable = canOperate() && !row.event_id;

        const box = document.createElement("input");
        box.type = "checkbox";
        box.disabled = !selectable;
        box.checked = selected.has(key);
        box.setAttribute("aria-label", "Select " + key);
        box.addEventListener("change", () => {
            if (box.checked) { selected.add(key); } else { selected.delete(key); }
            renderBulk();
        });
        const boxCell = el("td", null, "narrow");
        boxCell.append(box);

        const buildCell = el("td");
        buildCell.append(buildLink(row.build_url, "#" + row.build_number));

        const timeCell = el("td", ago(row.timestamp));
        timeCell.title = fmtTime(row.timestamp) + " UTC";

        const stateCell = el("td");
        stateCell.append(stateBadge(row));

        const rebuilt = rebuildBadge(row.rebuild);

        if (rebuilt) {
            stateCell.append(document.createTextNode(" "), rebuilt);
        }

        const actionCell = el("td", null, "narrow");

        if (row.event_id) {
            const button = el("button", "Details");
            button.type = "button";
            button.addEventListener("click", () => openDetail(row.event_id));
            actionCell.append(button);
        } else if (canOperate()) {
            const button = el("button", "Classify");
            button.type = "button";
            button.addEventListener("click", () => classifyOne(row, button));
            actionCell.append(button);
        }

        tr.append(boxCell, el("td", row.job, "job"), buildCell, timeCell, stateCell, actionCell);
        body.append(tr);
    }

    renderBulk();
    renderPager();
}


function renderBulk() {
    const count = selected.size;
    $("bulkBar").hidden = count === 0;
    $("bulkCount").textContent = count + " selected";

    const selectable = failedRows.filter((row) => canOperate() && !row.event_id);
    const all = $("selectAll");
    all.disabled = selectable.length === 0;
    all.checked = selectable.length > 0 && selectable.every((row) => selected.has(rowKey(row)));
}


function renderPager() {
    const size = Number($("jobPageSize").value);
    const first = totalJobs === 0 ? 0 : offset + 1;
    const last = Math.min(offset + size, totalJobs);

    $("pageInfo").textContent = totalJobs === 0
        ? (jobsError ? "" : "0 failing jobs")
        : `${first}–${last} of ${totalJobs} failing jobs`;
    $("prevPage").disabled = offset === 0;
    $("nextPage").disabled = offset + size >= totalJobs;

    $("buildsStatus").textContent = totalJobs === 0 && failedRows.length === 0
        ? ""
        : "Failure times are UTC on hover. Newest first unless sorted by name.";
}


function applyResult(row, result) {
    row.event_id = result.event_id;
    row.event_status = result.status;
    knownEvents.set(rowKey(row), { event_id: result.event_id, status: result.status });
    selected.delete(rowKey(row));
    track(result.event_id);
}


async function classifyOne(row, button) {
    button.disabled = true;

    try {
        applyResult(row, await post("/api/jenkins/analyze", {
            job: row.job,
            build_number: row.build_number,
        }));
        renderJobs();
        toast("Queued " + rowKey(row) + " for classification.");
    } catch (error) {
        button.disabled = false;
        toast("Could not queue " + rowKey(row) + ": " + error.message, true);
    }
}


async function classifySelected() {
    const rows = failedRows.filter((row) => selected.has(rowKey(row)));

    if (rows.length === 0) {
        return;
    }

    const builds = rows.map((row) => ({ job: row.job, build_number: row.build_number }));

    $("classifySelected").disabled = true;

    try {
        const results = await post("/api/jenkins/analyze-batch", { builds });
        let queued = 0;
        const errors = [];

        results.forEach((result, index) => {
            if (result.event_id) {
                applyResult(rows[index], result);
                queued += 1;
            } else {
                errors.push(`${result.job} #${result.build_number}: ${result.error}`);
            }
        });

        renderJobs();

        if (errors.length) {
            toast(`${queued} queued, ${errors.length} failed. ${errors[0]}`, true);
        } else {
            toast(`Queued ${queued} build(s) for classification.`);
        }
    } catch (error) {
        toast("Batch failed: " + error.message, true);
    } finally {
        $("classifySelected").disabled = false;
    }
}


// ---- live status of queued builds ---------------------------------------

function track(eventId) {
    tracked.add(eventId);

    if (pollTimer === null) {
        pollTimer = setInterval(pollTracked, POLL_MS);
    }
}


function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = null;
}


async function pollTracked() {
    const ids = [...tracked];

    if (ids.length === 0) {
        stopPolling();
        return;
    }

    let events;

    try {
        events = await api("/api/events?ids=" + ids.join(","));
    } catch (error) {
        return;     // transient; try again on the next tick
    }

    const seen = new Set();
    let finished = false;
    let changed = false;

    for (const event of events) {
        seen.add(event.id);
        const row = failedRows.find((r) => r.event_id === event.id);

        if (row) {
            row.event_status = event.status;
            row.final_category = event.final_category;
            row.rebuild = event.rebuild;
            knownEvents.set(rowKey(row), { event_id: event.id, status: event.status });
        }

        const signature = rebuildSignature(event);

        if (lastSeen.get(event.id) !== signature) {
            lastSeen.set(event.id, signature);
            changed = true;
        }

        // Done only when the event is processed AND any rebuild it triggered
        // has ended: the rebuild result must show up without a page refresh.
        if (TERMINAL.has(event.status) && !rebuildPending(event.rebuild)) {
            tracked.delete(event.id);
            finished = true;
        }
    }

    for (const id of ids) {
        if (!seen.has(id)) {
            tracked.delete(id);
        }
    }

    renderJobs();

    if (finished) {
        loadAccuracy().catch(() => {});
        loadEmailStats().catch(() => {});
    }

    if (finished || changed) {
        if (!$("panel-events").hidden) {
            loadEvents().catch(() => {});
        }

        if (openId !== null && ids.includes(openId)) {
            openDetail(openId).catch(() => {});
        }
    }

    if (tracked.size === 0) {
        stopPolling();
    }
}


// ---- classified events --------------------------------------------------

async function loadAccuracy() {
    const target = $("accuracy");
    const metrics = await api("/api/metrics/accuracy");
    const names = Object.keys(metrics);

    target.replaceChildren();

    if (names.length === 0) {
        target.append(el("span", "No labels yet.", "muted"));
        return;
    }

    for (const name of names) {
        const m = metrics[name];
        target.append(el(
            "span",
            `${name}: ${Math.round(m.accuracy * 100)}% (${m.correct}/${m.labeled})`,
            "chip"
        ));
    }
}


async function loadEmailStats() {
    const target = $("emailStats");
    target.replaceChildren();

    const stats = await api("/api/metrics/notifications");

    if (stats.total === 0) {
        return;
    }

    const d = stats.by_decision;
    const f = stats.recipient_feedback;
    const sent = stats.by_delivery || {};
    const delivered = Object.keys(sent).length
        ? ` · emails: ${sent.sent || 0} sent, ${sent.failed || 0} failed, ` +
          `${sent.uncertain || 0} uncertain, ${sent.pending || 0} pending`
        : " (not sent: shadow mode)";
    target.append(el(
        "span",
        `email previews${delivered}: ${d.would_send || 0} would send · ` +
        `${d.suppressed || 0} suppressed · ${d.blocked || 0} blocked · ` +
        `recipients ${f.ok} right / ${f.wrong} wrong`,
        "chip"
    ));
}


async function loadEvents() {
    $("listStatus").textContent = "Loading…";

    const rows = await api("/api/events" + (FILTERS[$("filter").value] || ""));
    const body = $("events");
    body.replaceChildren();

    for (const row of rows) {
        const tr = document.createElement("tr");
        tr.className = "clickable" + (row.id === openId ? " selected" : "");
        tr.dataset.id = row.id;
        tr.tabIndex = 0;

        const finalCell = el("td", row.final_category || row.status,
            "cat cat-" + row.final_category);

        tr.append(
            el("td", row.job, "job"),
            el("td", "#" + row.build_number),
            el("td", (row.received_at || "").replace("T", " ").slice(0, 19)),
            finalCell,
            el("td", row.llm_category || "—"),
            el("td", row.validated === null ? "—" : (row.validated ? "yes" : "no")),
            el("td", (row.reason_codes || []).join(", ") || "—"),
            el("td", row.label_verdict || "—"),
            rebuildCell(row.rebuild),
        );

        if (rebuildPending(row.rebuild)) {
            track(row.id);
        }

        tr.addEventListener("click", () => openDetail(row.id));
        tr.addEventListener("keydown", (event) => {
            if (event.key === "Enter") { openDetail(row.id); }
        });
        body.append(tr);
    }

    if (rows.length === 0) {
        const tr = document.createElement("tr");
        const td = el("td", $("filter").value === "all"
            ? "No events yet. Classify a failing job from the other tab."
            : "No events match this filter. Choose “All” to see every event " +
              "(the default shows only needs_triage events nobody has labeled yet).",
            "empty");
        td.colSpan = 9;
        tr.append(td);
        body.append(tr);
    }

    $("listStatus").textContent = rows.length ? `${rows.length} event(s)` : "";
}


// ---- detail drawer ------------------------------------------------------

function definitionList(pairs) {
    const dl = document.createElement("dl");

    for (const [term, value] of pairs) {
        dl.append(el("dt", term));

        const dd = document.createElement("dd");

        if (value instanceof Node) {
            dd.append(value);
        } else {
            dd.textContent = value === null || value === undefined || value === ""
                ? "—"
                : String(value);
        }

        dl.append(dd);
    }

    return dl;
}


function chips(items) {
    const box = el("div", null, "chips");

    if (!items || items.length === 0) {
        return el("span", "—", "muted");
    }

    for (const item of items) {
        box.append(el("span", item, "badge"));
    }

    return box;
}


function section(title, node) {
    const wrap = document.createElement("div");
    wrap.append(el("h3", title), node);
    return wrap;
}


function categoryNode(category) {
    return el("span", category || "—", "cat cat-" + category);
}


async function openDetail(id) {
    openId = id;
    const data = await api("/api/events/" + id);
    const { event, enrichment, analysis, action, label, execution, notification } = data;
    const response = analysis ? analysis.response : null;

    for (const tr of $("events").children) {
        tr.classList.toggle("selected", Number(tr.dataset.id) === id);
    }

    $("detail").hidden = false;
    $("detailTitle").textContent = `${event.job} #${event.build_number}`;

    const link = $("detailLink");
    link.hidden = !/^https?:\/\//i.test(event.build_url);
    link.href = link.hidden ? "#" : event.build_url;

    const body = $("detailBody");
    body.replaceChildren();

    body.append(definitionList([
        ["Status", badge(event.status, event.status)],
        ["Attempts", event.attempts],
        ["Error", event.last_error],
        ["Final category", analysis ? categoryNode(analysis.final_category) : null],
        ["LLM category", response && response.llm_category],
        ["Validated", response && (response.validated ? "yes" : "no")],
        ["Confidence", response && response.confidence],
        ["Model", analysis && analysis.model],
        ["Failing stage", enrichment && enrichment.failing_stage],
        ["Source", event.source],
    ]));

    if (analysis) {
        body.append(section("Reason codes", chips(analysis.reason_codes)));
        body.append(section("Root cause", el("p", response.root_cause)));
        body.append(section("Recommended fix (advisory)", el("p", response.recommended_fix)));

        if (response.evidence && response.evidence.length) {
            body.append(section("Evidence (verbatim from the log)",
                el("pre", response.evidence.join("\n"))));
        }

        if (analysis.rule_matches && analysis.rule_matches.length) {
            const list = el("ul", null, "rules");

            for (const match of analysis.rule_matches) {
                list.append(el("li",
                    `${match.rule_id} → ${match.category} (line ${match.line_number})`));
            }

            body.append(section("Rule matches", list));
        }
    } else {
        body.append(el("p",
            event.status === "failed"
                ? "Processing failed; see the error above."
                : "Not analyzed yet. The worker will pick this up shortly.",
            "muted"));
    }

    if (enrichment && enrichment.log_excerpt) {
        const details = document.createElement("details");
        details.append(el("summary", "Redacted log excerpt"));
        details.append(el("pre", enrichment.log_excerpt));
        body.append(details);
    }

    const actionBox = $("detailAction");
    actionBox.replaceChildren();

    // The heading must not claim "not executed" once something was executed.
    $("actionHeading").textContent = !execution
        ? "Proposed (shadow — not executed)"
        : (action && action.executed
            ? "Executed"
            : `Proposed (execution ${execution.status})`);

    if (!action) {
        actionBox.append(el("p", "No action (needs triage or not processed yet).", "muted"));
    } else {
        actionBox.append(definitionList([
            ["Action", action.action_type],
            ["Would be allowed", el("span", action.allowed ? "yes" : "no",
                action.allowed ? "allowed" : "denied")],
            ["Reason codes", (action.reason_codes || []).join(", ")],
        ]));
        actionBox.append(el("pre", JSON.stringify(action.target, null, 2)));
    }

    if (execution) {
        actionBox.append(el("h3", "Execution"));
        actionBox.append(definitionList([
            ["Status", badge(execution.status, execution.status)],
            ["Why", (execution.reason_codes || []).join(", ")],
            ["New build", execution.new_build_url
                ? buildLink(execution.new_build_url, "#" + execution.new_build_number)
                : (execution.new_build_number ? "#" + execution.new_build_number : null)],
            ["Rebuild result", execution.rebuild ? rebuildBadge(execution.rebuild) : null],
            ["Rebuild took", execution.rebuild && execution.rebuild.duration_ms
                ? Math.round(execution.rebuild.duration_ms / 1000) + " s" : null],
            ["Rebuild checked", execution.rebuild && execution.rebuild.checked_at
                ? execution.rebuild.checked_at.replace("T", " ").slice(0, 19) + " UTC" : null],
            ["Error", execution.error],
        ]));

        if (execution.status === "uncertain") {
            actionBox.append(el("p",
                "The rebuild request may or may not have been applied. Check Jenkins " +
                "before doing anything; it is never retried automatically.", "muted"));
        }
    }

    renderEmail(event.id, notification);
    renderLabelSection(analysis, label);
}


const DECISION_KIND = { would_send: "triggered", suppressed: "skipped", blocked: "failed" };

function renderEmail(eventId, notification) {
    const box = $("emailPreview");
    box.replaceChildren();
    $("emailSection").hidden = !notification;

    if (!notification) {
        return;
    }

    const delivery = notification.delivery;
    $("emailHeading").textContent = !delivery
        ? "Email preview (shadow — not sent)"
        : (delivery.status === "sent" ? "Email (sent)" : `Email (delivery ${delivery.status})`);

    box.append(definitionList([
        ["Decision", badge(notification.decision.replace("_", " "),
            DECISION_KIND[notification.decision] || "none")],
        ["Delivery", delivery ? badge(delivery.status, delivery.status) : "not sent (shadow mode)"],
        ["Sent at (UTC)", delivery && delivery.sent_at ? delivery.sent_at.replace("T", " ").slice(0, 19) : null],
        ["Delivery error", delivery && delivery.error],
        ["To", (notification.recipients || []).join(", ")],
        ["Recipient from", notification.recipient_source],
        ["Why", (notification.reason_codes || []).join(", ")],
        ["Subject", notification.subject],
    ]));

    const details = document.createElement("details");
    details.open = true;
    details.append(el("summary", delivery && delivery.status === "sent"
        ? "Message body (as sent)"
        : "Message body"));
    details.append(el("pre", notification.body));
    box.append(details);

    if (notification.decision !== "would_send") {
        return;
    }

    if (notification.recipient_ok !== null && notification.recipient_ok !== undefined) {
        box.append(el("p",
            `Recipient rated ${notification.recipient_ok ? "correct" : "WRONG"}` +
            (notification.reviewed_by ? ` by ${notification.reviewed_by}` : "") + ".",
            "muted"));
    }

    if (!canLabel()) {
        return;
    }

    const row = el("div", null, "chips");

    for (const [text, ok] of [["Recipient is right", true], ["Wrong recipient", false]]) {
        const button = el("button", text);
        button.type = "button";
        button.addEventListener("click", async () => {
            try {
                await post(`/api/events/${eventId}/notification-label`, { recipient_ok: ok });
                await Promise.all([openDetail(eventId), loadEmailStats()]);
                toast("Recipient feedback saved.");
            } catch (error) {
                toast("Could not save feedback: " + error.message, true);
            }
        });
        row.append(button);
    }

    box.append(row);
}


function renderLabelSection(analysis, label) {
    const status = $("labelStatus");
    $("labelForm").hidden = !(canLabel() && analysis);

    if (label) {
        status.textContent = `Current label: ${label.verdict}` +
            (label.correct_category ? ` → ${label.correct_category}` : "") +
            ` by ${label.reviewer}`;
    } else if (!analysis) {
        status.textContent = "Not analyzed yet; cannot label.";
    } else if (!canLabel()) {
        status.textContent = `Not labeled yet. Your role (${me.role}) is read-only.`;
    } else {
        status.textContent = "Not labeled yet.";
    }
}


function closeDetail() {
    openId = null;
    $("detail").hidden = true;

    for (const tr of $("events").children) {
        tr.classList.remove("selected");
    }
}


// ---- label form ---------------------------------------------------------

$("labelForm").addEventListener("change", () => {
    const verdict = $("labelForm").querySelector("input[name=verdict]:checked").value;
    $("correctCategory").disabled = verdict !== "incorrect";
});


$("labelForm").addEventListener("submit", async (submitEvent) => {
    submitEvent.preventDefault();

    if (openId === null) {
        return;
    }

    const verdict = $("labelForm").querySelector("input[name=verdict]:checked").value;
    const body = { verdict, note: $("note").value || null };

    if (verdict === "incorrect") {
        if (!$("correctCategory").value) {
            $("labelStatus").textContent = "Choose the correct category.";
            return;
        }

        body.correct_category = $("correctCategory").value;
    }

    try {
        await post(`/api/events/${openId}/label`, body);
        await Promise.all([openDetail(openId), loadEvents(), loadAccuracy()]);
        toast("Label saved.");
    } catch (error) {
        $("labelStatus").textContent = "Error: " + error.message;
    }
});


// ---- wiring -------------------------------------------------------------

$("tab-jobs").addEventListener("click", () => showTab("jobs"));
$("tab-events").addEventListener("click", () => showTab("events"));

$("jobRefresh").addEventListener("click", () => loadJobs(true));

for (const id of ["jobAge", "jobSort", "jobPageSize", "jobDisabled"]) {
    $(id).addEventListener("change", () => loadJobs(true));
}

$("jobQuery").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadJobs(true), 350);
});

$("prevPage").addEventListener("click", () => {
    offset = Math.max(0, offset - Number($("jobPageSize").value));
    loadJobs(false);
});

$("nextPage").addEventListener("click", () => {
    offset += Number($("jobPageSize").value);
    loadJobs(false);
});

$("selectAll").addEventListener("change", (event) => {
    for (const row of failedRows) {
        if (canOperate() && !row.event_id) {
            if (event.target.checked) {
                selected.add(rowKey(row));
            } else {
                selected.delete(rowKey(row));
            }
        }
    }

    renderJobs();
});

$("classifySelected").addEventListener("click", classifySelected);
$("clearSelection").addEventListener("click", () => { selected.clear(); renderJobs(); });

$("filter").addEventListener("change", () => loadEvents());
$("refresh").addEventListener("click", () => {
    Promise.all([loadEvents(), loadAccuracy()]).catch((error) => {
        $("listStatus").textContent = "Error: " + error.message;
    });
});

$("closeDetail").addEventListener("click", closeDetail);
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("detail").hidden) { closeDetail(); }
});


async function start() {
    try {
        me = await api("/api/me");
    } catch (error) {
        toast("Could not load your account: " + error.message, true);
    }

    $("who").textContent = `${me.username} · ${me.role}`;

    if (!canOperate()) {
        $("classifySelected").hidden = true;
    }

    loadAccuracy().catch(() => {});
    loadEmailStats().catch(() => {});
    loadControls();
    loadJobs(true);
}


start();
