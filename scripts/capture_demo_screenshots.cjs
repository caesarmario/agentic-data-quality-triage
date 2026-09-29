/**
 * Capture real local operator surfaces for README/demo review.
 * Install Playwright locally with:
 *   npm install --prefix data/acceptance/readme-render --no-save playwright
 *   npx --prefix data/acceptance/readme-render playwright install chromium
 * Run: node scripts/capture_demo_screenshots.cjs
 * No external URLs, writes, or notification commands are used.
 */

// --- Importing Libraries
const fs = require("node:fs/promises");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { spawnSync } = require("node:child_process");

// --- Defining Capture Targets
const ROOT = path.resolve(__dirname, "..");
const OUTPUT = path.join(ROOT, "docs", "images");
const ENV_PATH = path.join(ROOT, "infra", ".env");
const AUTH_PATH = path.join(ROOT, "infra", "airflow", "auth", "simple_auth_manager_passwords.json");
const COMPOSE_PATH = path.join(ROOT, "infra", "docker-compose.yml");
const SURFACES = Object.freeze([
  "next-overview", "next-triage", "streamlit-alerts", "airflow-graph",
  "ch-ui", "s3-artifacts", "discord-output", "mcp-tools",
]);

function localUrl(value) {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) ||
      !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname) ||
      url.username || url.password) {
    throw new Error("Only credential-free local HTTP URLs are allowed");
  }
  return url;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function safeLabel(value) {
  return redactSensitive(value)
    .slice(0, 120);
}

function redactSensitive(value) {
  return String(value)
    .replace(
      /https?:\/\/(?:canary\.|ptb\.)?discord(?:app)?\.com\/api\/webhooks\/\d+\/[A-Za-z0-9._-]+/gi,
      "[redacted-discord-webhook]",
    )
    .replace(
      /(["'])(api[_-]?key|access[_-]?token|auth(?:orization)?|token|password|secret|client[_-]?secret|private[_-]?key|webhook(?:[_-]?url)?)\1(\s*:\s*)(["'])[^\r\n]*?\4/gi,
      "$1$2$1$3$4[redacted]$4",
    )
    .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi, "Bearer [redacted]")
    .replace(/\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b/g, "[redacted-github-token]")
    .replace(/\bAKIA[0-9A-Z]{16}\b/g, "[redacted-aws-access-key]")
    .replace(/\bAIza[0-9A-Za-z_-]{20,}\b/g, "[redacted-google-api-key]")
    .replace(/\bAQ\.[0-9A-Za-z_-]{16,}/g, "[redacted-google-api-key]")
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/gi, "[redacted-api-key]")
    .replace(
      /(\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|token|password|secret|client[_-]?secret|private[_-]?key|webhook(?:[_-]?url)?)\b\s*[=:]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)/gi,
      "$1[redacted]",
    )
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, " ");
}

function temporaryScreenshotPath(targetPath) {
  const extension = path.extname(targetPath) || ".png";
  const basename = path.basename(targetPath, extension);
  return path.join(
    path.dirname(targetPath),
    `.${basename}.${process.pid}.${randomUUID()}.tmp${extension}`,
  );
}

async function writeScreenshotAtomically(page, targetPath, options = {}) {
  const temporaryPath = temporaryScreenshotPath(targetPath);
  try {
    await page.screenshot({ ...options, path: temporaryPath });
    await fs.rename(temporaryPath, targetPath);
  } finally {
    await fs.rm(temporaryPath, { force: true }).catch(() => {});
  }
}

function strictExitCode(results, strict) {
  return strict && results.some(result => result.status !== "captured") ? 1 : 0;
}

function emitSummary(results, strict) {
  console.log(JSON.stringify({ results }, null, 2));
  process.exitCode = strictExitCode(results, strict);
}

function readEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z][A-Z0-9_]*)=(.*)\s*$/);
    if (!match) continue;
    let value = match[2].trim();
    if ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    values[match[1]] = value;
  }
  return values;
}

function portUrl(port) {
  if (!/^\d{2,5}$/.test(String(port)) || Number(port) > 65535) {
    throw new Error("Invalid local port");
  }
  return localUrl(`http://127.0.0.1:${port}`);
}

function playwrightModule() {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    "playwright",
    path.join(ROOT, "data", "acceptance", "readme-render", "node_modules", "playwright"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try { return require(candidate); } catch (error) {
      if (error.code !== "MODULE_NOT_FOUND") throw error;
    }
  }
  return null;
}

function dockerReadOnly(pythonCode, timeout = 20000) {
  const result = spawnSync("docker", [
    "compose", "--env-file", ENV_PATH, "-f", COMPOSE_PATH,
    "exec", "-T", "dq-runner", "python", "-c", pythonCode,
  ], { cwd: ROOT, encoding: "utf8", timeout, maxBuffer: 1024 * 1024, windowsHide: true });
  if (result.status !== 0 || result.error) throw new Error("local_runner_unavailable");
  try { return JSON.parse(result.stdout.trim().split(/\r?\n/).at(-1)); }
  catch { throw new Error("invalid_local_runner_output"); }
}

function inventoryHtml(title, source, entries) {
  const rows = entries.map(({ name, detail }) =>
    `<tr><td>${escapeHtml(safeLabel(name))}</td><td>${escapeHtml(safeLabel(detail))}</td></tr>`
  ).join("");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${escapeHtml(title)}</title>
    <style>body{font:16px/1.5 ui-monospace,Consolas,monospace;color:#17251e;background:#eef3ec;margin:0;padding:48px}
    main{max-width:1120px;margin:auto;background:white;border:1px solid #bacabc;padding:34px 40px}
    h1{font:700 28px/1.2 Georgia,serif;margin:0 0 12px}.source{color:#50645a;margin-bottom:26px}
    table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{padding:10px;text-align:left;border-bottom:1px solid #dbe4dc;overflow-wrap:anywhere}
    th{background:#e7efe7}td:first-child{width:55%;font-weight:600}footer{margin-top:28px;color:#50645a;font-size:13px}
    </style></head><body><main><h1>${escapeHtml(title)}</h1><div class="source">${escapeHtml(source)}</div>
    <table><thead><tr><th>Asset / tool</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table>
    <footer>Read-only local inventory. This is a rendered listing, not a screenshot of the storage or MCP UI.</footer></main></body></html>`;
}

function s3Inventory() {
  const code = [
    "import json, os, boto3",
    "client = boto3.client('s3', endpoint_url=os.environ['S3_ENDPOINT_URL'])",
    "buckets = [os.environ.get(name) for name in ('ARTIFACTS_BUCKET','DQREPORTS_BUCKET','DQFAILURES_BUCKET','AUDIT_BUCKET')]",
    "rows = []",
    "for bucket in filter(None, buckets):",
    "  response = client.list_objects_v2(Bucket=bucket, MaxKeys=8)",
    "  rows.append({'bucket': bucket, 'keys': [obj['Key'] for obj in response.get('Contents', [])], 'truncated': response.get('IsTruncated', False)})",
    "print(json.dumps(rows))",
  ].join("\n");
  const data = dockerReadOnly(code);
  if (!Array.isArray(data) || !data.length) throw new Error("empty_s3_inventory");
  const entries = data.flatMap(row => row.keys.length
    ? row.keys.map(key => ({ name: `${row.bucket}/${key}`, detail: row.truncated ? "sample (first 8)" : "object" }))
    : [{ name: row.bucket, detail: "bucket empty" }]);
  return inventoryHtml("SeaweedFS artifact inventory", "Source: read-only S3 ListObjectsV2, local dq-runner", entries);
}

function mcpInventory() {
  const data = dockerReadOnly([
    "import json",
    "from agent.mcp.server import SERVER_NAME, tool_registry_as_dicts",
    "print(json.dumps({'server': SERVER_NAME, 'tools': tool_registry_as_dicts()}))",
  ].join("\n"));
  if (!Array.isArray(data.tools) || !data.tools.length) throw new Error("empty_mcp_inventory");
  return inventoryHtml("MCP tool inventory", `Source: local registry ${safeLabel(data.server)}`,
    data.tools.map(tool => ({ name: tool.name, detail: tool.purpose || tool.risk_level || "registered" })));
}

function discordMarkdownHtml(value) {
  const inline = line => escapeHtml(redactSensitive(line))
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return String(value).split(/\r?\n/).map(line => {
    if (!line.trim()) return '<div class="gap"></div>';
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) return `<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`;
    const item = line.match(/^(\s*)(\d+)\.\s+(.+)$/);
    if (item) return `<div class="item"><span>${item[2]}.</span><div>${inline(item[3])}</div></div>`;
    if (/^\s{2,}/.test(line)) return `<div class="indent">${inline(line.trim())}</div>`;
    return `<p>${inline(line)}</p>`;
  }).join("");
}

function discordOutputHtml() {
  const code = [
    "import json",
    "from apps.discord_bot.formatters import format_alert_list",
    "from apps.discord_bot.service import fetch_discord_alerts",
    "status = 'triaged'",
    "payload, transport = fetch_discord_alerts(status=status, dt=None, limit=5, api_base_url='')",
    "alerts = payload.get('alerts', [])",
    "if not alerts:",
    "  status = 'open'",
    "  payload, transport = fetch_discord_alerts(status=status, dt=None, limit=5, api_base_url='')",
    "  alerts = payload.get('alerts', [])",
    "message = format_alert_list(alerts=alerts, status=status, dt=None, data_transport=transport)",
    "print(json.dumps({'message': message, 'status': status, 'count': len(alerts)}))",
  ].join("\n");
  const data = dockerReadOnly(code);
  if (!data.message || !data.count) throw new Error("empty_discord_output");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Discord command output</title>
    <style>body{font:16px/1.55 ui-sans-serif,Segoe UI,sans-serif;color:#dbdee1;background:#1e1f22;margin:0;padding:48px}
    main{max-width:920px;margin:auto}.channel{color:#949ba4;font-weight:700;margin-bottom:14px}.message{display:grid;grid-template-columns:48px 1fr;gap:14px}
    .avatar{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;background:#3ba55c;color:white;font-weight:800}
    .author{font-weight:700;color:#f2f3f5;margin-bottom:6px}.bot{font-size:11px;background:#5865f2;padding:2px 5px;border-radius:3px;margin-left:6px}
    .content{overflow-wrap:anywhere;background:#2b2d31;border:1px solid #3f4147;border-radius:8px;padding:22px;color:#dbdee1}
    h1,h2,h3,p{margin:0}h1{font-size:23px;color:#f2f3f5}h2{font-size:18px;margin-top:2px}h3{font-size:16px;margin-top:4px;color:#f2f3f5}
    .gap{height:14px}.item{display:grid;grid-template-columns:24px 1fr;gap:4px;margin:5px 0}.indent{margin:3px 0 3px 28px;color:#b5bac1}
    code{font:14px/1.45 ui-monospace,Consolas,monospace;background:#1e1f22;border-radius:4px;padding:2px 5px;color:#f2f3f5}strong{color:#f2f3f5}
    footer{margin:22px 0 0 62px;color:#949ba4;font-size:13px}</style></head><body><main>
    <div class="channel"># dq-alerts</div><div class="message"><div class="avatar">DQ</div><div><div class="author">Data Reliability Copilot<span class="bot">APP</span></div>
    <div class="content">${discordMarkdownHtml(data.message)}</div></div></div>
    <footer>Rendered from the real local Discord formatter and alert data. This is not a Discord client screenshot.</footer>
    </main></body></html>`;
}

async function airflowCredentials(env) {
  const username = process.env.AIRFLOW_WWW_USER_USERNAME || env.AIRFLOW_WWW_USER_USERNAME || "";
  let password = process.env.AIRFLOW_WWW_USER_PASSWORD || env.AIRFLOW_WWW_USER_PASSWORD || "";
  if (!password) {
    try {
      const passwords = JSON.parse(await fs.readFile(AUTH_PATH, "utf8"));
      password = passwords[username] || "";
    } catch { /* Missing local credentials result in a skip. */ }
  }
  return username && password ? { username, password } : null;
}

async function newLocalPage(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: "block" });
  await context.route("**/*", route => {
    try { localUrl(route.request().url()); return route.continue(); }
    catch { return route.abort(); }
  });
  if (typeof context.routeWebSocket === "function") {
    await context.routeWebSocket("**/*", socket => {
      try { localUrl(socket.url().replace(/^ws/, "http")); socket.connectToServer(); }
      catch { socket.close(); }
    });
  }
  const page = await context.newPage();
  page.on("popup", popup => popup.close());
  return { page, context };
}

async function captureBrowser(browser, name, base, suffix, locator, options = {}) {
  const { page, context } = await newLocalPage(browser);
  try {
    await page.goto(new URL(suffix, base).href, { waitUntil: "domcontentloaded", timeout: 15000 });
    if (options.prepare) await options.prepare(page);
    await page.locator(locator).first().waitFor({ state: "visible", timeout: 12000 });
    if (await page.locator('input[type="password"]:visible').count()) {
      throw new Error("credential_form_visible");
    }
    await writeScreenshotAtomically(page, path.join(OUTPUT, `${name}.png`), {
      fullPage: options.fullPage !== false,
      animations: "disabled",
    });
    return { surface: name, status: "captured", file: `docs/images/${name}.png` };
  } catch (error) {
    const allowed = new Set(["credential_form_visible", "login_unavailable", "graph_not_visible", "alert_table_unavailable"]);
    const reason = allowed.has(error.message) ? error.message
      : error.message.startsWith("page.screenshot") ? "screenshot_failed"
        : error.name === "TimeoutError" ? "view_timeout" : "service_or_view_unavailable";
    if (process.env.DEMO_CAPTURE_DIAGNOSTICS === "1") {
      console.error(`${name}: ${error.name}: ${safeLabel(error.message.split("\n")[0])}`);
    }
    return { surface: name, status: "skipped", reason };
  } finally { await context.close(); }
}

async function captureInventory(browser, name, buildHtml) {
  let html;
  try { html = buildHtml(); }
  catch (error) { return { surface: name, status: "skipped", reason: error.message }; }
  const { page, context } = await newLocalPage(browser);
  try {
    await page.setContent(html);
    await writeScreenshotAtomically(page, path.join(OUTPUT, `${name}.png`), { fullPage: true });
    return { surface: name, status: "captured", file: `docs/images/${name}.png` };
  } finally { await context.close(); }
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes("--help")) {
    console.log("Usage: node scripts/capture_demo_screenshots.cjs [--only=surface,...] [--strict]\nSurfaces: " + SURFACES.join(", ") +
      "\nInstall: npm install --prefix data/acceptance/readme-render --no-save playwright && npx --prefix data/acceptance/readme-render playwright install chromium\nOutput: docs/images. Missing views are reported as skipped.");
    return;
  }
  const strict = args.includes("--strict");
  const only = args.find(arg => arg.startsWith("--only="));
  if (args.some(arg => arg !== "--strict" && !arg.startsWith("--only=")) ||
      args.filter(arg => arg === "--strict").length > 1 ||
      args.filter(arg => arg.startsWith("--only=")).length > 1) throw new Error("invalid_arguments");
  const selected = only ? only.slice(7).split(",") : SURFACES;
  if (!selected.length || selected.some(name => !SURFACES.includes(name)) || new Set(selected).size !== selected.length) throw new Error("invalid_surface_selection");
  await fs.mkdir(OUTPUT, { recursive: true });
  const playwright = playwrightModule();
  if (!playwright) {
    emitSummary(selected.map(surface => ({ surface, status: "skipped", reason: "playwright_not_installed" })), strict);
    return;
  }
  let browser;
  try { browser = await playwright.chromium.launch({ headless: true }); }
  catch {
    emitSummary(selected.map(surface => ({ surface, status: "skipped", reason: "chromium_not_installed" })), strict);
    return;
  }
  let env = {};
  try { env = readEnv(await fs.readFile(ENV_PATH, "utf8")); } catch { /* Defaults remain local. */ }
  const results = [];
  try {
    for (const name of selected) {
      const port = {
        "next-overview": env.WEB_PORT || 3000,
        "next-triage": env.WEB_PORT || 3000,
        "streamlit-alerts": env.STREAMLIT_PORT || 8501,
        "airflow-graph": env.AIRFLOW_PORT || 8080,
        "ch-ui": env.CH_UI_PORT || 3488,
      }[name];
      try {
        if (name === "s3-artifacts" || name === "discord-output" || name === "mcp-tools") {
          const buildHtml = name === "s3-artifacts" ? s3Inventory
            : name === "discord-output" ? discordOutputHtml : mcpInventory;
          results.push(await captureInventory(browser, `demo-${name}`, buildHtml));
          continue;
        }
        const base = portUrl(port);
        const config = {
          "next-overview": ["/", "h1"],
          "next-triage": ["/triage", "h1"],
          "streamlit-alerts": ["/", "text=Alert Filters"],
          "airflow-graph": ["/dags/00_dag_dq_platform_daily_orchestrator", "text=t10_trigger_landing"],
          "ch-ui": ["/", "text=ClickHouse"],
        }[name];
        const options = {};
        if (name === "streamlit-alerts") {
          options.fullPage = false;
          options.prepare = async page => {
            await page.locator("[data-testid=stSelectbox]").first()
              .waitFor({ state: "visible", timeout: 12000 });
            let found = false;
            for (const status of ["triaged", "open", "acknowledged", "resolved"]) {
              await page.locator("[data-testid=stSelectbox]").first().click();
              await page.getByRole("option", { name: status, exact: true }).click();
              found = await page.getByText("Selected Alert", { exact: true })
                .waitFor({ state: "visible", timeout: 4500 }).then(() => true, () => false);
              if (found) break;
            }
            if (!found) throw new Error("alert_table_unavailable");
            const headingY = await page.getByText("Selected Alert", { exact: true })
              .evaluate(element => element.getBoundingClientRect().y);
            const frameLocations = await page.locator("[data-testid=stDataFrame]")
              .evaluateAll(elements => elements.map(element => element.getBoundingClientRect().y));
            const index = frameLocations.findLastIndex(y => y < headingY);
            if (index < 0) throw new Error("alert_table_unavailable");
            await page.locator("[data-testid=stDataFrame]").nth(index).scrollIntoViewIfNeeded();
            await page.waitForTimeout(300);
          };
        }
        if (name === "airflow-graph") {
          options.prepare = async page => {
            await page.waitForTimeout(1000);
            if (page.url().includes("/auth/login")) {
              const credentials = await airflowCredentials(env);
              if (!credentials) throw new Error("login_unavailable");
              await page.locator('input[name="username"]').fill(credentials.username);
              await page.locator('input[name="password"]').fill(credentials.password);
              await page.locator('button[type="submit"]').click();
              await page.waitForURL(url => !url.pathname.includes("/auth/login"), { timeout: 12000 }).catch(() => { throw new Error("login_unavailable"); });
            }
            const button = page.getByRole("button", { name: /show graph/i });
            await button.first().waitFor({ state: "visible", timeout: 12000 }).catch(() => {});
            const graph = page.getByRole("tab", { name: /graph/i });
            if (await button.count()) await button.first().click();
            else if (await graph.count()) await graph.first().click();
            else {
              const link = page.getByRole("link", { name: /graph/i });
              if (await link.count()) await link.first().click();
              else throw new Error("graph_not_visible");
            }
            await page.getByText("t10_trigger_landing", { exact: true }).first()
              .waitFor({ state: "visible", timeout: 12000 });
          };
        }
        if (name === "ch-ui") {
          options.prepare = async page => {
            await page.getByRole("heading", { name: /sign in|welcome back/i }).first()
              .waitFor({ state: "visible", timeout: 10000 });
            if (await page.locator('input[type="password"]:visible').count()) {
              await page.locator('input[type="text"]').first().fill(env.CLICKHOUSE_USER || "default");
              await page.locator('input[type="password"]').first().fill(env.CLICKHOUSE_PASSWORD || "");
              await page.getByRole("button", { name: /connect/i }).click();
              await page.getByRole("heading", { name: /welcome back/i }).waitFor({ state: "visible", timeout: 10000 });
            }
            await page.getByRole("button", { name: "Run Query", exact: true }).click();
            await page.locator("[contenteditable=true]").first().fill(
              "SELECT dt, count() AS alert_count FROM dq.alerts " +
              "WHERE dt >= today() - 365 GROUP BY dt ORDER BY dt DESC LIMIT 10"
            );
            await page.getByRole("button", { name: "Run", exact: true }).click();
            await page.getByText("alert_count", { exact: true })
              .waitFor({ state: "visible", timeout: 10000 });
            await page.locator("table tbody tr").first()
              .waitFor({ state: "visible", timeout: 10000 });
          };
          config[1] = "text=alert_count";
        }
        results.push(await captureBrowser(browser, `demo-${name}`, base, config[0], config[1], options));
      } catch { results.push({ surface: `demo-${name}`, status: "skipped", reason: "invalid_local_configuration" }); }
    }
  } finally { await browser.close(); }
  emitSummary(results, strict);
}

// --- Exposing Pure Helpers For Focused Tests
module.exports = {
  localUrl, escapeHtml, safeLabel, redactSensitive, discordMarkdownHtml,
  readEnv, portUrl, inventoryHtml, temporaryScreenshotPath,
  writeScreenshotAtomically, strictExitCode, SURFACES,
};
if (require.main === module) main().catch(() => { console.error("Capture failed before surface diagnostics"); process.exitCode = 1; });
