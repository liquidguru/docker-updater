import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "templates" / "index.html"
LOGIN = ROOT / "templates" / "login.html"
CATALOG = ROOT / "static" / "i18n_messages.json"


class UiStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = INDEX.read_text(encoding="utf-8")
        cls.login = LOGIN.read_text(encoding="utf-8")
        cls.i18n_js = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
        cls.messages = json.loads(CATALOG.read_text(encoding="utf-8"))

    def test_javascript_parses(self):
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", self.index, re.S)
        inline = [script for script in scripts if script.strip()]
        self.assertTrue(inline)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(inline[-1])
            inline_path = f.name
        inline_result = subprocess.run(["node", "--check", inline_path], capture_output=True, text=True)
        Path(inline_path).unlink(missing_ok=True)
        static_result = subprocess.run(
            ["node", "--check", str(ROOT / "static" / "i18n.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(inline_result.returncode, 0, inline_result.stderr)
        self.assertEqual(static_result.returncode, 0, static_result.stderr)

    def test_no_native_browser_dialogs(self):
        self.assertNotRegex(self.index, r"(?<!show)\balert\s*\(")
        self.assertNotRegex(self.index, r"(?<!show)\bconfirm\s*\(")

    def test_no_digest_containers_have_a_reason_and_no_update_action_in_both_languages(self):
        def extract(name):
            match = re.search(rf"  function {name}\([^)]*\) \{{.*?\n  \}}\n", self.index, re.S)
            self.assertIsNotNone(match, name)
            return match.group(0)

        script = """
const catalogs = %s;
let messages;
const t = (key, params) => messages[key].replace(/\\{(\\w+)\\}/g,
  (m, k) => params && k in params ? params[k] : m);
const selectedNames = new Set(), multiHost = true;
const timeAgo = () => 'earlier';
const list = {};
const document = {getElementById: () => list};
%s
const out = {};
for (const language of ['en', 'zh-CN']) {
  messages = catalogs[language];
  for (const host of ['local', 'nas']) {
    const c = {name: 'offline-app', image: 'app:latest', host_id: host,
      host_name: 'NAS', status: 'unknown', unchecked_reason: 'no_registry_digest',
      checked_at: '2026-11-18T00:00:00Z'};
    renderContainerList([c, {...c, name: 'checked-app', status: 'ok'}], 'unknown');
    out[language + '_' + host] = list.innerHTML;
  }
  out[language + '_ordinary'] = cardHTML({
    name: 'ordinary', image: 'nginx:latest', host_id: 'local', status: 'unknown'
  });
  out[language + '_update'] = cardHTML({
    name: 'update-app', image: 'nginx:latest', host_id: 'local', status: 'update'
  });
}
console.log(JSON.stringify(out));
""" % (json.dumps(self.messages), "\n".join(
            extract(name) for name in ("esc", "cardHTML", "renderContainerList")))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(script)
            path = f.name
        try:
            result = subprocess.run(["node", path], capture_output=True, text=True,
                                    encoding="utf-8", timeout=30)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        for language in ("en", "zh-CN"):
            for host in ("local", "nas"):
                with self.subTest(language=language, host=host):
                    html = out[f"{language}_{host}"]
                    self.assertIn(self.messages[language]["card.no_registry_digest"], html)
                    self.assertIn("status-unknown", html)
                    self.assertNotIn("checked-app", html)
                    self.assertNotIn("earlier", html)
                    self.assertNotIn("confirmUpdate(", html)
                    self.assertNotIn("toggleSelect(", html)
                    self.assertNotIn(self.messages[language]["empty.unknown_desc"], html)
            self.assertIn(self.messages[language]["card.not_checked"], out[f"{language}_ordinary"])
            self.assertIn("confirmUpdate(", out[f"{language}_update"])

    def test_no_known_corruption_markers(self):
        for marker in ("statusOkstatusOk", "'?? '", ">??<", " e:'??'", " e:'?'", "refreshStatus()"):
            self.assertNotIn(marker, self.index)

    def test_translation_catalogs_have_identical_keys_and_no_corruption(self):
        self.assertEqual(set(self.messages["en"]), set(self.messages["zh-CN"]))
        for key, value in self.messages["zh-CN"].items():
            self.assertNotIn("?", value, f"ASCII question mark/corruption in zh-CN {key}: {value}")
        for language in ("en", "zh-CN"):
            for key, value in self.messages[language].items():
                self.assertNotIn("??", value, f"Corrupt translation {language}.{key}: {value}")

    def test_templates_inject_shared_catalog_before_runtime(self):
        marker = "window.I18N_MESSAGES = {{ i18n_messages | tojson }};"
        runtime = '<script src="/static/i18n.js"></script>'
        for name, template in (("index", self.index), ("login", self.login)):
            self.assertIn(marker, template, f"Missing shared catalog injection in {name} template")
            self.assertLess(
                template.index(marker),
                template.index(runtime),
                f"Catalog must be injected before i18n.js in {name} template",
            )
        self.assertIn(
            'const MESSAGES = global.I18N_MESSAGES || { en: {}, "zh-CN": {} };',
            self.i18n_js,
        )
        self.assertNotRegex(self.i18n_js, r'const MESSAGES = \{"en":')

    def test_all_referenced_translation_keys_exist(self):
        combined = self.index + self.login + self.i18n_js
        referenced = set(re.findall(
            r"""(?:data-i18n(?:-html|-placeholder|-title|-aria-label)?="|\bt\(['"])([\w.-]+)""",
            combined,
        ))
        missing = referenced - set(self.messages["en"])
        self.assertEqual(missing, set(), f"Missing i18n keys: {sorted(missing)}")

    def test_required_auth_and_language_controls_exist(self):
        self.assertIn('id="lang-select"', self.index)
        self.assertIn('id="logout-form"', self.index)
        self.assertIn('name="username"', self.login)
        self.assertIn('name="password"', self.login)
        self.assertIn('role="alert"', self.login)
        self.assertIn('aria-describedby="login-error"', self.login)
        self.assertIn('data-i18n="btn.refresh"', self.index)
        self.assertIn('data-i18n="modal.changelog_src_hint"', self.index)
        self.assertIn(":focus-visible", self.index)
        self.assertIn(":focus-visible", self.login)

    def test_custom_dialog_has_accessible_focus_management(self):
        self.assertIn('aria-labelledby="app-dialog-title"', self.i18n_js)
        self.assertIn('aria-describedby="app-dialog-message"', self.i18n_js)
        self.assertIn("_dlgPreviousFocus", self.i18n_js)
        self.assertIn("trapDialogFocus", self.i18n_js)
        self.assertIn('active.matches("button, a, input, select, textarea")', self.i18n_js)
        self.assertIn("data-i18n-aria-label", self.index)

    def test_auto_language_sync_clears_server_override(self):
        self.assertGreaterEqual(self.index.count("ui_language: null, client_lang: effectiveLang"), 2)
        self.assertIn("body: JSON.stringify(payload)", self.index)
        self.assertNotIn("body: JSON.stringify(_settings)", self.index)
        self.assertIn("toLocaleString(uiLocale())", self.index)


if __name__ == "__main__":
    unittest.main()
