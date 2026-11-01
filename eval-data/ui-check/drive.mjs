// Drives headless Edge over the DevTools protocol against the review UI.
// node drive.mjs <url> <screenshotPrefix> <scenario: operator|viewer>
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [url, prefix, scenario] = process.argv.slice(2);
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const port = 9333 + (scenario === "viewer" ? 1 : 0);
const profile = mkdtempSync(join(tmpdir(), "jai-edge-"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const edge = spawn(EDGE, [
    "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    "--window-size=1400,1000", "--no-first-run", "--disable-gpu", "about:blank",
], { stdio: "ignore" });

async function wsUrl() {
    for (let i = 0; i < 50; i++) {
        try {
            const tabs = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
            const page = tabs.find((t) => t.type === "page");
            if (page) return page.webSocketDebuggerUrl;
        } catch {}
        await sleep(200);
    }
    throw new Error("no devtools endpoint");
}

const ws = new WebSocket(await wsUrl());
await new Promise((r) => ws.addEventListener("open", r));
let id = 0;
const pending = new Map();
const problems = [];

ws.addEventListener("message", (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    if (msg.method === "Runtime.exceptionThrown") problems.push("exception: " + msg.params.exceptionDetails.text + " " + (msg.params.exceptionDetails.exception?.description || ""));
    if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") problems.push("console.error: " + msg.params.args.map((a) => a.value ?? a.description).join(" "));
});

const send = (method, params = {}) => new Promise((resolve) => {
    const n = ++id; pending.set(n, resolve); ws.send(JSON.stringify({ id: n, method, params }));
});
const run = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || "eval failed");
    return r.result.result.value;
};
const until = async (expression, label, ms = 30000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) { if (await run(expression)) return true; await sleep(250); }
    throw new Error("timeout waiting for: " + label);
};
const shot = async (name) => {
    const r = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(`${prefix}-${name}.png`, Buffer.from(r.result.data, "base64"));
};
const out = {};

try {
    await send("Page.enable"); await send("Runtime.enable");
    await send("Page.navigate", { url });
    await until(`document.querySelectorAll('#failedBuilds tr').length > 1 && !document.getElementById('jobRefresh').disabled`, "jobs table");

    out.who = await run(`document.getElementById('who').textContent`);
    out.rows = await run(`document.querySelectorAll('#failedBuilds tr').length`);
    out.pageInfo = await run(`document.getElementById('pageInfo').textContent`);
    out.classifyButtons = await run(`[...document.querySelectorAll('#failedBuilds button')].filter(b => b.textContent === 'Classify').length`);
    out.selectAllDisabled = await run(`document.getElementById('selectAll').disabled`);
    out.firstRow = await run(`document.querySelector('#failedBuilds tr').innerText.replace(/\\s+/g,' ')`);
    await shot("1-jobs");

    // search narrows the server-side result set
    const before = out.pageInfo;
    await run(`(() => { const q = document.getElementById('jobQuery'); q.value = 'gen3plus'; q.dispatchEvent(new Event('input')); })()`);
    await until(`document.getElementById('pageInfo').textContent !== ${JSON.stringify(before)} && !document.getElementById('jobRefresh').disabled`, "search result");
    out.pageInfoAfterSearch = await run(`document.getElementById('pageInfo').textContent`);
    out.allMatchSearch = await run(`[...document.querySelectorAll('#failedBuilds td.job')].every(td => td.textContent.toLowerCase().includes('gen3plus'))`);

    // widen age window -> total can only grow or stay
    await run(`(() => { const a = document.getElementById('jobAge'); a.value = '0'; a.dispatchEvent(new Event('change')); })()`);
    await sleep(2500);
    await until(`!document.getElementById('jobRefresh').disabled`, "age reload");
    out.pageInfoAnyAge = await run(`document.getElementById('pageInfo').textContent`);

    // clear search for the rest
    await run(`(() => { const q = document.getElementById('jobQuery'); q.value = ''; q.dispatchEvent(new Event('input')); const a = document.getElementById('jobAge'); a.value = '30'; a.dispatchEvent(new Event('change')); })()`);
    await sleep(2500);
    await until(`!document.getElementById('jobRefresh').disabled && document.querySelectorAll('#failedBuilds tr').length > 1`, "reset");

    if (scenario === "operator") {
        // tick two rows, queue them as a batch
        out.queuedBefore = await run(`document.querySelectorAll('#failedBuilds .st-queued, #failedBuilds .st-processing').length`);
        await run(`(() => { const boxes = [...document.querySelectorAll('#failedBuilds input[type=checkbox]:not(:disabled)')].slice(0, 2); boxes.forEach(b => { b.checked = true; b.dispatchEvent(new Event('change')); }); })()`);
        out.bulkText = await run(`document.getElementById('bulkBar').hidden ? 'hidden' : document.getElementById('bulkCount').textContent`);
        await run(`document.getElementById('classifySelected').click()`);
        await until(`document.querySelectorAll('#failedBuilds .st-queued, #failedBuilds .st-processing').length >= ${out.queuedBefore + 2}`, "queued badges", 15000);
        out.queuedBadges = await run(`document.querySelectorAll('#failedBuilds .st-queued, #failedBuilds .st-processing').length`);
        out.toast = await run(`document.getElementById('toast').textContent`);
        out.bulkAfter = await run(`document.getElementById('bulkBar').hidden`);
        await shot("2-queued");

        // open the detail drawer from a queued row
        await run(`[...document.querySelectorAll('#failedBuilds button')].find(b => b.textContent === 'Details').click()`);
        await until(`!document.getElementById('detail').hidden && document.getElementById('detailTitle').textContent.length > 0`, "drawer");
        out.drawerTitle = await run(`document.getElementById('detailTitle').textContent`);
        out.drawerHasShadowLabel = await run(`document.getElementById('detail').innerText.toLowerCase().includes('proposed (shadow — not executed)')`);
        out.drawerStatus = await run(`document.querySelector('#detailBody dl dd').innerText`);
        await shot("3-drawer");
        await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
        await sleep(300);
        out.drawerClosedByEsc = await run(`document.getElementById('detail').hidden`);

        // events tab lists them (default filter shows only analysed events)
        await run(`document.getElementById('tab-events').click()`);
        await run(`(() => { const f = document.getElementById('filter'); f.value = 'all'; f.dispatchEvent(new Event('change')); })()`);
        await until(`document.getElementById('listStatus').textContent.length > 0 && document.getElementById('listStatus').textContent !== 'Loading…'`, "events");
        out.eventsStatus = await run(`document.getElementById('listStatus').textContent`);
        out.eventsTabVisible = await run(`!document.getElementById('panel-events').hidden && document.getElementById('panel-jobs').hidden`);
        await shot("4-events");
    }
} catch (error) {
    out.ERROR = String(error);
    out.diag = await run(`JSON.stringify({
        toast: document.getElementById('toast').textContent,
        toastHidden: document.getElementById('toast').hidden,
        bulk: document.getElementById('bulkBar').hidden,
        rows: [...document.querySelectorAll('#failedBuilds tr')].slice(0, 4).map(r => r.innerText.replace(/\\s+/g, ' ')),
        badges: [...document.querySelectorAll('#failedBuilds .badge')].slice(0, 4).map(b => b.className + ':' + b.textContent),
    })`).catch((e) => "diag failed: " + e);
    await shot("error").catch(() => {});
}

out.pageProblems = problems;
console.log(JSON.stringify(out, null, 2));
ws.close(); edge.kill();
process.exit(out.ERROR ? 1 : 0);
