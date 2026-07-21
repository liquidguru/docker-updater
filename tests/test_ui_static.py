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

    def test_embedded_client_catalog_matches_shared_json(self):
        line = next(line for line in self.i18n_js.splitlines() if line.startswith("  const MESSAGES = "))
        embedded = json.loads(line.removeprefix("  const MESSAGES = ").removesuffix(";"))
        self.assertEqual(embedded, self.messages)

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
