/**
 * Local-only README rendering evidence; complementary to Airflow acceptance.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */

// --- Importing Libraries
const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const fs = require("node:fs/promises");
const http = require("node:http");
const path = require("node:path");
const { marked } = require("marked");
const { chromium } = require("playwright");

// --- Defining Local Paths
const root = path.resolve(__dirname, "..");
const output = path.join(root, "data/acceptance/readme-browser");
const mermaidDist = path.dirname(require.resolve("mermaid"));

// --- Building A Local Preview
function preview(markdown, theme) {
  const content = marked.parse(markdown);
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Local README acceptance</title><style>
    *{box-sizing:border-box}body{margin:0;background:${theme === "dark" ? "#15191d" : "#fff"};color:${theme === "dark" ? "#e5e9ed" : "#202830"};font:16px/1.6 sans-serif}
    main{max-width:1060px;margin:auto;padding:24px;overflow-wrap:anywhere}
    a{color:${theme === "dark" ? "#8ec9ff" : "#005ea8"}}
    img{max-width:100%;height:auto}pre,table,.mermaid{display:block;max-width:100%;overflow-x:auto}
    pre{padding:16px;background:${theme === "dark" ? "#222b33" : "#f1f4f6"};font-size:13px}
    table{border-collapse:collapse}td,th{border:1px solid #75818a;padding:8px}
    .mermaid{background:transparent}h1,h2,h3{line-height:1.25}h2{margin-top:40px}
    </style></head><body><main>${content}</main>
    <script type="module">
    import mermaid from '/mermaid/mermaid.esm.min.mjs';
    document.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(el=>{
      el.id=el.textContent.toLowerCase().replace(/[^\\p{L}\\p{N}_ -]/gu,'').replace(/ /g,'-');
    });
    document.querySelectorAll('code.language-mermaid').forEach(el=>{
      const pre=el.parentElement;pre.className='mermaid';pre.textContent=el.textContent;
    });
    mermaid.initialize({startOnLoad:false,securityLevel:'strict',theme:'${theme === "dark" ? "dark" : "default"}'});
    try{await mermaid.run();window.renderComplete=true;}
    catch(error){window.renderFailure=String(error);}
    </script></body></html>`;
}

// --- Serving Only Reviewed Asset Roots
async function serveAsset(response, directory, relative) {
  const candidate = path.resolve(directory, relative);
  if (!candidate.startsWith(path.resolve(directory) + path.sep)) {
    response.writeHead(403).end();
    return;
  }
  const mime = { ".mjs": "text/javascript", ".js": "text/javascript", ".png": "image/png", ".svg": "image/svg+xml" };
  response.setHeader("Content-Type", mime[path.extname(candidate)] || "application/octet-stream");
  response.end(await fs.readFile(candidate));
}

// --- Inspecting Desktop And Mobile Rendering
async function main() {
  const markdown = await fs.readFile(path.join(root, "README.md"), "utf8");
  const expectedDiagrams = (markdown.match(/^```mermaid\s*$/gm) || []).length;
  assert.ok(expectedDiagrams > 0, "No Mermaid diagrams found");
  await fs.mkdir(output, { recursive: true });
  await fs.rm(path.join(output, "results.json"), { force: true });
  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, "http://localhost");
      if (request.method !== "GET") return response.writeHead(405).end();
      if (url.pathname === "/") {
        response.setHeader("Content-Type", "text/html; charset=utf-8");
        return response.end(preview(markdown, url.searchParams.get("theme")));
      }
      if (url.pathname.startsWith("/mermaid/")) {
        return await serveAsset(response, mermaidDist, decodeURIComponent(url.pathname.slice(9)));
      }
      if (url.pathname.startsWith("/docs/images/")) {
        return await serveAsset(response, path.join(root, "docs/images"), decodeURIComponent(url.pathname.slice(13)));
      }
      response.writeHead(404).end();
    } catch {
      response.writeHead(404).end();
    }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  const results = [];
  let blockedExternalRequests = 0;
  try {
    browser = await chromium.launch({ headless: true });
    for (const width of [1440, 390]) {
      for (const theme of ["light", "dark"]) {
        const page = await browser.newPage({ viewport: { width, height: 1000 } });
        const errors = [];
        page.on("pageerror", error => errors.push(error.message));
        await page.route("**/*", route => {
          if (new URL(route.request().url()).origin === origin) return route.continue();
          blockedExternalRequests++;
          // External badges are intentionally not fetched and are not validated.
          return route.fulfill({ status: 200, contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>' });
        });
        await page.goto(`${origin}/?theme=${theme}`);
        await page.waitForFunction(() => window.renderComplete || window.renderFailure, null, { timeout: 30000 });
        const facts = await page.evaluate(() => ({
          failure: window.renderFailure || null,
          svgCount: document.querySelectorAll(".mermaid svg").length,
          internalLinks: [...document.querySelectorAll('a[href^="#"]')].map(a => ({ href: a.getAttribute("href"), resolves: !!document.getElementById(decodeURIComponent(a.hash.slice(1))) })),
          brokenLocalImages: [...document.images].filter(i => i.getAttribute("src").startsWith("docs/") && (!i.complete || !i.naturalWidth)).length,
          overflow: document.documentElement.scrollWidth > innerWidth,
        }));
        assert.equal(facts.failure, null);
        assert.equal(facts.svgCount, expectedDiagrams);
        assert.ok(facts.internalLinks.length > 0 && facts.internalLinks.every(link => link.resolves));
        assert.equal(facts.brokenLocalImages, 0);
        assert.equal(facts.overflow, false);
        assert.deepEqual(errors, []);
        await page.screenshot({ path: path.join(output, `readme-${width}-${theme}.png`) });
        for (let index = 0; index < expectedDiagrams; index++) {
          await page.locator(".mermaid").nth(index).screenshot({ path: path.join(output, `diagram-${index + 1}-${width}-${theme}.png`) });
        }
        results.push({ width, theme, ...facts, pageErrors: errors });
        await page.close();
      }
    }
    const versions = {
      node: process.versions.node,
      marked: require("marked/package.json").version,
      playwright: require("playwright/package.json").version,
      mermaid: JSON.parse(await fs.readFile(path.join(mermaidDist, "../package.json"), "utf8")).version,
    };
    await fs.writeFile(path.join(output, "results.json"), JSON.stringify({
      status: "pass", scope: "local_marked_mermaid_not_github_renderer",
      completedAt: new Date().toISOString(),
      readmeSha256: createHash("sha256").update(markdown).digest("hex"),
      versions, externalRequests: 0, blockedExternalRequests, results,
    }, null, 2));
    console.log(JSON.stringify({ status: "pass", cases: results.length, expectedDiagrams, output }));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

// --- Running CLI Entrypoint
main().catch(error => { console.error(error); process.exitCode = 1; });
