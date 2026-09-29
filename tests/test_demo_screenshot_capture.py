"""Service-free contract checks for the local screenshot capture script."""

# --- Importing Libraries
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


# --- Defining Script Location
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capture_demo_screenshots.cjs"


# --- Testing Pure Helpers And CLI Contracts
@unittest.skipUnless(shutil.which("node"), "Node.js is required for capture helper tests")
class DemoScreenshotCaptureTests(unittest.TestCase):
    def node_eval(self, source: str) -> dict:
        result = subprocess.run(
            ["node", "-e", f"const m=require({json.dumps(str(SCRIPT))}); {source}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return json.loads(result.stdout)

    def test_required_surfaces_are_explicit(self) -> None:
        result = self.node_eval("console.log(JSON.stringify({surfaces:m.SURFACES}))")
        self.assertEqual(result["surfaces"], [
            "next-overview", "next-triage", "streamlit-alerts", "airflow-graph",
            "ch-ui", "s3-artifacts", "discord-output", "mcp-tools",
        ])

    def test_nonlocal_and_credentialed_urls_are_rejected(self) -> None:
        for value in [
            "https://example.com", "file:///etc/passwd", "http://192.168.1.1:3000",
            "http://user:password@localhost:3000", "javascript:alert(1)",
        ]:
            with self.subTest(value=value):
                result = self.node_eval(
                    f"let accepted=true; try {{ m.localUrl({json.dumps(value)}); }} "
                    "catch { accepted=false; } console.log(JSON.stringify({accepted}))"
                )
                self.assertEqual(result, {"accepted": False})

    def test_explicit_local_http_url_is_accepted(self) -> None:
        result = self.node_eval(
            "console.log(JSON.stringify({url:m.localUrl('http://127.0.0.1:8501/').href}))"
        )
        self.assertEqual(result, {"url": "http://127.0.0.1:8501/"})

    def test_invalid_ports_are_rejected(self) -> None:
        result = self.node_eval(
            "let accepted=true; try { m.portUrl('3000.evil.com'); } "
            "catch { accepted=false; } console.log(JSON.stringify({accepted}))"
        )
        self.assertEqual(result, {"accepted": False})

    def test_inventory_escapes_markup_and_redacts_token_like_values(self) -> None:
        result = self.node_eval(
            "console.log(JSON.stringify({html:m.inventoryHtml('Local', 'read-only', "
            "[{name:'<script>alert(1)</script>',detail:'token=secretvalue'}])}))"
        )
        self.assertNotIn("<script>", result["html"])
        self.assertIn("&lt;script&gt;", result["html"])
        self.assertNotIn("secretvalue", result["html"])
        self.assertIn("[redacted]", result["html"])
        self.assertIn("Read-only local inventory", result["html"])

    def test_redaction_covers_provider_and_transport_secret_formats(self) -> None:
        secrets = {
            "json_api_key": '"api_key": "super-secret-json-value"',
            "json_password": "'password': 'quoted-password-value'",
            "bearer": "Authorization: Bearer eyJhbGciOiJIUzI1Ni.secret.signature",
            "github_classic": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
            "github_fine_grained": "github_pat_11AA0_exampleTokenValue123456789",
            "discord_webhook": "https://discord.com/api/webhooks/123456789012345678/abc_DEF-token.value",
            "aws": "AKIAABCDEFGHIJKLMNOP",
            "google_aiza": "AIzaSyA1234567890abcdefghijklmnopqrstuvwxyz",
            "google_aq": "AQ.SYNTHETIC_GOOGLE_API_KEY_1234567890abcd",
        }
        secret_values = {
            "json_api_key": "super-secret-json-value",
            "json_password": "quoted-password-value",
            "bearer": "eyJhbGciOiJIUzI1Ni.secret.signature",
            "github_classic": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
            "github_fine_grained": "github_pat_11AA0_exampleTokenValue123456789",
            "discord_webhook": "abc_DEF-token.value",
            "aws": "AKIAABCDEFGHIJKLMNOP",
            "google_aiza": "AIzaSyA1234567890abcdefghijklmnopqrstuvwxyz",
            "google_aq": "AQ.SYNTHETIC_GOOGLE_API_KEY_1234567890abcd",
        }
        result = self.node_eval(
            "const values=" + json.dumps(secrets) + "; "
            "console.log(JSON.stringify(Object.fromEntries(Object.entries(values).map("
            "([key,value])=>[key,m.redactSensitive(value)]))))"
        )
        for name, secret in secret_values.items():
            with self.subTest(name=name):
                self.assertNotIn(secret, result[name])
                self.assertIn("[redacted", result[name].lower())

    def test_discord_markdown_is_rendered_and_sanitized(self) -> None:
        result = self.node_eval(
            "console.log(JSON.stringify({html:m.discordMarkdownHtml("
            "'# Alert\\n\\n1. **Issue** `DQ-1`\\n  token=secretvalue')}))"
        )
        self.assertIn("<h1>Alert</h1>", result["html"])
        self.assertIn("<strong>Issue</strong>", result["html"])
        self.assertIn("<code>DQ-1</code>", result["html"])
        self.assertNotIn("secretvalue", result["html"])

    def test_env_parser_can_report_presence_without_printing_value(self) -> None:
        result = self.node_eval(
            "const env=m.readEnv('WEB_PORT=3000\\nAIRFLOW_WWW_USER_PASSWORD=\"not-for-output\"'); "
            "console.log(JSON.stringify({port:env.WEB_PORT,passwordSet:!!env.AIRFLOW_WWW_USER_PASSWORD}))"
        )
        self.assertEqual(result, {"port": "3000", "passwordSet": True})

    def test_help_is_service_free_and_invalid_selection_fails_closed(self) -> None:
        help_result = subprocess.run(
            ["node", str(SCRIPT), "--help"], cwd=ROOT, capture_output=True,
            text=True, check=True, timeout=10,
        )
        self.assertIn("--only=surface", help_result.stdout)
        self.assertIn("--strict", help_result.stdout)
        self.assertIn("docs/images", help_result.stdout)

        invalid = subprocess.run(
            ["node", str(SCRIPT), "--only=discord"], cwd=ROOT, capture_output=True,
            text=True, timeout=10,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("Capture failed", invalid.stderr)
        self.assertNotIn("secret", invalid.stderr.lower())

    def test_atomic_capture_replaces_target_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "demo.png"
            target.write_text("old-image", encoding="utf-8")
            result = self.node_eval(
                "const fs=require('node:fs/promises');"
                f"const target={json.dumps(str(target))};"
                "const page={screenshot:async options=>fs.writeFile(options.path,'new-image')};"
                "(async()=>{await m.writeScreenshotAtomically(page,target);"
                "const content=await fs.readFile(target,'utf8');"
                "const files=await fs.readdir(require('node:path').dirname(target));"
                "console.log(JSON.stringify({content,tempFiles:files.filter(name=>name.includes('.tmp.'))}));})()"
            )
            self.assertEqual(result, {"content": "new-image", "tempFiles": []})

    def test_failed_capture_preserves_existing_target_and_cleans_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "demo.png"
            target.write_text("known-good-image", encoding="utf-8")
            result = self.node_eval(
                "const fs=require('node:fs/promises');"
                f"const target={json.dumps(str(target))};"
                "const page={screenshot:async options=>{await fs.writeFile(options.path,'partial');throw new Error('capture failed')}};"
                "(async()=>{let failed=false;try{await m.writeScreenshotAtomically(page,target)}catch{failed=true}"
                "const content=await fs.readFile(target,'utf8');"
                "const files=await fs.readdir(require('node:path').dirname(target));"
                "console.log(JSON.stringify({failed,content,tempFiles:files.filter(name=>name.includes('.tmp.'))}));})()"
            )
            self.assertEqual(result, {
                "failed": True,
                "content": "known-good-image",
                "tempFiles": [],
            })

    def test_strict_mode_fails_after_emitting_skipped_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_playwright = Path(directory) / "fake-playwright.cjs"
            fake_playwright.write_text(
                "module.exports={chromium:{launch:async()=>{throw new Error('not installed')}}};",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PLAYWRIGHT_MODULE"] = str(fake_playwright)

            exploratory = subprocess.run(
                ["node", str(SCRIPT), "--only=next-overview"],
                cwd=ROOT, capture_output=True, text=True, timeout=10, env=environment,
            )
            strict = subprocess.run(
                ["node", str(SCRIPT), "--only=next-overview", "--strict"],
                cwd=ROOT, capture_output=True, text=True, timeout=10, env=environment,
            )

            self.assertEqual(exploratory.returncode, 0)
            self.assertNotEqual(strict.returncode, 0)
            self.assertEqual(json.loads(exploratory.stdout)["results"][0]["status"], "skipped")
            self.assertEqual(json.loads(strict.stdout)["results"][0]["status"], "skipped")


class DemoScreenshotSourceTests(unittest.TestCase):
    def test_source_has_no_discord_send_or_external_url(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("discord.com/api", source)
        self.assertNotIn("discordapp.com/api", source)
        self.assertNotIn("fetch(webhook", source.lower())
        self.assertNotIn("requests.post", source.lower())
        self.assertIn("Only credential-free local HTTP URLs are allowed", source)
        self.assertIn("list_objects_v2", source)
        self.assertIn("socket.connectToServer()", source)
        self.assertNotIn("socket.connect()", source)
        self.assertIn("SELECT dt, count() AS alert_count FROM dq.alerts", source)
        self.assertIn("LIMIT 10", source)
        self.assertNotIn("await fs.rm(path.join(OUTPUT, `demo-${surface}.png`)", source)
        self.assertIn("writeScreenshotAtomically", source)


if __name__ == "__main__":
    unittest.main()
