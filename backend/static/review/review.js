// Review UI for shadow-mode events. All data is rendered with textContent
// or createElement; never as HTML markup.

const FILTERS = {
    triage: "?category=needs_triage&unlabeled=true",
    all: "",
    infra: "?category=infra",
    devops: "?category=devops",
    compilation: "?category=compilation",
    needs_triage: "?category=needs_triage",
};

const filter = document.getElementById("filter");
const eventsBody = document.getElementById("events");
const buildsBody = document.getElementById("failedBuilds");
const buildsStatus = document.getElementById("buildsStatus");
const buildsCount = document.getElementById("buildsCount");
const listStatus = document.getElementById("listStatus");
const detail = document.getElementById("detail");
const labelForm = document.getElementById("labelForm");
const correctCategory = document.getElementById("correctCategory");
const labelStatus = document.getElementById("labelStatus");

let selectedId = null;


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


async function api(path, options) {
    // Relative, same-origin: the browser reuses the Basic credentials.
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
        throw new Error(data.detail || ("HTTP " + response.status));
    }

    return data;
}


async function loadAccuracy() {
    const target = document.getElementById("accuracy");
    target.replaceChildren();

    const metrics = await api("/api/metrics/accuracy");
    const names = Object.keys(metrics);

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


async function loadEvents() {
    listStatus.textContent = "Loading…";

    const rows = await api("/api/events" + (FILTERS[filter.value] || ""));
    eventsBody.replaceChildren();

    for (const row of rows) {
        const tr = document.createElement("tr");
        tr.dataset.id = row.id;

        if (row.id === selectedId) {
            tr.classList.add("selected");
        }

        tr.append(
            el("td", row.job),
            el("td", "#" + row.build_number),
            el("td", (row.received_at || "").replace("T", " ").slice(0, 19)),
            el("td", row.final_category || row.status, "cat cat-" + row.final_category),
            el("td", row.llm_category || "—"),
            el("td", row.validated === null ? "—" : (row.validated ? "yes" : "no")),
            el("td", (row.reason_codes || []).join(", ") || "—"),
            el("td", row.label_verdict || "—"),
        );

        tr.addEventListener("click", () => showDetail(row.id));
        eventsBody.append(tr);
    }

    listStatus.textContent = rows.length ? `${rows.length} event(s)` : "No events.";
}


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


function buildLink(url) {
    // Only http(s) links; the URL was already scoped to Jenkins at intake.
    if (!/^https?:\/\//i.test(url)) {
        return url;
    }

    const a = el("a", url);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
}


async function showDetail(id) {
    selectedId = id;
    const data = await api("/api/events/" + id);
    const { event, enrichment, analysis, action, label } = data;

    for (const tr of eventsBody.children) {
        tr.classList.toggle("selected", Number(tr.dataset.id) === id);
    }

    detail.classList.remove("hidden");
    document.getElementById("detailTitle").textContent =
        `${event.job} #${event.build_number}`;

    const body = document.getElementById("detailBody");
    body.replaceChildren();

    body.append(definitionList([
        ["Build", buildLink(event.build_url)],
        ["Status", event.status + (event.last_error ? ` (${event.last_error})` : "")],
        ["Failing stage", enrichment && enrichment.failing_stage],
        ["Final category", analysis && analysis.final_category],
        ["Reason codes", analysis && analysis.reason_codes.join(", ")],
        ["LLM category", analysis && analysis.response.llm_category],
        ["Validated", analysis && String(analysis.response.validated)],
        ["Confidence", analysis && analysis.response.confidence],
        ["Root cause", analysis && analysis.response.root_cause],
        ["Recommended fix", analysis && analysis.response.recommended_fix],
        ["Gerrit scope", enrichment && JSON.stringify(enrichment.gerrit_scope)],
        ["Artifactory links", enrichment && enrichment.artifactory_links.join("\n")],
        ["Log truncated", enrichment && String(enrichment.log_truncated)],
    ]));

    if (analysis && analysis.response.evidence && analysis.response.evidence.length) {
        body.append(el("h3", "Evidence"));
        body.append(el("pre", analysis.response.evidence.join("\n")));
    }

    if (enrichment && enrichment.log_excerpt) {
        body.append(el("h3", "Redacted log excerpt"));
        body.append(el("pre", enrichment.log_excerpt));
    }

    const actionBox = document.getElementById("detailAction");
    actionBox.replaceChildren();

    if (!action) {
        actionBox.append(el("p", "No action (needs triage or not processed yet).", "muted"));
    } else {
        actionBox.append(definitionList([
            ["Action", action.action_type],
            ["Would be allowed", el("span", action.allowed ? "yes" : "no",
                action.allowed ? "allowed" : "denied")],
            ["Reason codes", action.reason_codes.join(", ")],
        ]));
        actionBox.append(el("pre", JSON.stringify(action.target, null, 2)));
    }

    labelStatus.textContent = label
        ? `Current label: ${label.verdict}` +
          (label.correct_category ? ` → ${label.correct_category}` : "") +
          ` by ${label.reviewer}`
        : (analysis ? "Not labeled yet." : "Not analyzed yet; cannot label.");
}


labelForm.addEventListener("change", () => {
    const verdict = labelForm.querySelector("input[name=verdict]:checked").value;
    correctCategory.disabled = verdict !== "incorrect";
});


labelForm.addEventListener("submit", async (submitEvent) => {
    submitEvent.preventDefault();

    if (selectedId === null) {
        return;
    }

    const verdict = labelForm.querySelector("input[name=verdict]:checked").value;
    const body = {
        verdict,
        note: document.getElementById("note").value || null,
    };

    if (verdict === "incorrect") {
        if (!correctCategory.value) {
            labelStatus.textContent = "Choose the correct category.";
            return;
        }

        body.correct_category = correctCategory.value;
    }

    try {
        await api(`/api/events/${selectedId}/label`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        await Promise.all([showDetail(selectedId), loadEvents(), loadAccuracy()]);
    } catch (error) {
        labelStatus.textContent = "Error: " + error.message;
    }
});


function fmtTime(ms) {
    // Jenkins reports epoch milliseconds; show UTC, matching the events table.
    if (!ms) {
        return "—";
    }

    return new Date(ms).toISOString().replace("T", " ").slice(0, 19);
}


function stateLabel(row) {
    if (!row.event_id) {
        return "—";
    }

    if (row.event_status === "done" && row.final_category) {
        return "done · " + row.final_category;
    }

    return row.event_status || "queued";
}


function pollEvent(id, onUpdate) {
    // Poll the worker's progress until the event settles, then refresh views.
    const timer = setInterval(async () => {
        try {
            const data = await api("/api/events/" + id);
            const status = data.event.status;
            onUpdate(status, data.analysis && data.analysis.final_category);

            if (status === "done" || status === "failed") {
                clearInterval(timer);
                await Promise.all([showDetail(id), loadEvents()]);
            }
        } catch (error) {
            clearInterval(timer);
            buildsStatus.textContent = "Error: " + error.message;
        }
    }, 2000);
}


async function classify(row, button, stateCell) {
    button.disabled = true;
    stateCell.textContent = "queued";

    try {
        const result = await api("/api/jenkins/analyze", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                job: row.job,
                build_number: row.build_number,
            }),
        });

        pollEvent(result.event_id, (status, category) => {
            stateCell.textContent =
                status === "done" && category ? "done · " + category : status;
        });
    } catch (error) {
        button.disabled = false;
        buildsStatus.textContent = "Error: " + error.message;
    }
}


async function loadFailedBuilds() {
    buildsStatus.textContent = "Fetching live failed builds from Jenkins…";
    buildsBody.replaceChildren();

    let rows;

    try {
        const count = Number(buildsCount.value) || 20;
        rows = await api("/api/jenkins/failed-builds?count=" + count);
    } catch (error) {
        buildsStatus.textContent = "Error: " + error.message;
        return;
    }

    for (const row of rows) {
        const tr = document.createElement("tr");
        const stateCell = el("td", stateLabel(row));

        const actionCell = document.createElement("td");

        if (row.event_id) {
            actionCell.append(el("span", "enqueued", "muted"));
        } else {
            const button = el("button", "Classify");
            button.type = "button";
            button.addEventListener("click", () =>
                classify(row, button, stateCell));
            actionCell.append(button);
        }

        const buildCell = document.createElement("td");
        buildCell.append(buildLink(row.build_url));

        tr.append(
            el("td", row.job),
            buildCell,
            el("td", row.result),
            el("td", fmtTime(row.timestamp)),
            stateCell,
            actionCell,
        );
        buildsBody.append(tr);
    }

    buildsStatus.textContent = rows.length
        ? `${rows.length} failed build(s)`
        : "No failed builds.";
}


function refresh() {
    Promise.all([loadEvents(), loadAccuracy()]).catch((error) => {
        listStatus.textContent = "Error: " + error.message;
    });
}


filter.addEventListener("change", refresh);
document.getElementById("refresh").addEventListener("click", refresh);
document.getElementById("loadBuilds").addEventListener("click", loadFailedBuilds);
refresh();
