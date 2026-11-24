"""Groundwork for a German translation (issue #40 follow-up).

The "de" block of static/i18n_messages.json starts out empty. A language is only
offered once its block has strings, and any string it is missing falls back to
English. These tests pin that behaviour, and validate the German block as soon as
a translator has filled it in (same keys as English, same {placeholders}).
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_safety import SafetyTestBase

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "static" / "i18n_messages.json").read_text(encoding="utf-8"))
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

PLACEHOLDER = re.compile(r"\{(\w+)\}")


class GermanCatalogTests(unittest.TestCase):
    def test_a_german_block_exists(self):
        self.assertIn("de", CATALOG)

    def test_german_only_uses_known_keys(self):
        extra = set(CATALOG["de"]) - set(CATALOG["en"])
        self.assertFalse(extra, f"keys in 'de' that English does not have: {sorted(extra)}")

    def test_german_placeholders_match_english(self):
        for key, value in CATALOG["de"].items():
            with self.subTest(key=key):
                self.assertEqual(set(PLACEHOLDER.findall(value)),
                                 set(PLACEHOLDER.findall(CATALOG["en"][key])))

    def test_german_has_no_empty_strings(self):
        # An empty string would show as a blank label instead of falling back to English.
        for key, value in CATALOG["de"].items():
            with self.subTest(key=key):
                self.assertTrue(value.strip())

    def test_the_language_name_is_in_every_populated_block(self):
        for lang in ("en", "zh-CN"):
            self.assertEqual(CATALOG[lang]["settings.lang_de"], "Deutsch")


class GermanServerTests(SafetyTestBase):
    def setUp(self):
        super().setUp()
        mod = self.mod
        for p in (mock.patch.object(mod, "load_state", side_effect=lambda: self.state),
                  mock.patch.object(mod, "save_state",
                                    side_effect=lambda s: self.state.update(s)),
                  mock.patch.object(mod, "AUTH_ENABLED", False),
                  mock.patch.object(mod, "_I18N_MESSAGES", None)):
            p.start()
            self.addCleanup(p.stop)

    def _catalog(self, de):
        self.mod._I18N_MESSAGES = {"en": {"a": "English {n}", "b": "Only English"},
                                   "zh-CN": {}, "de": de}

    def test_an_empty_german_block_means_english(self):
        self._catalog({})
        self.state["ui_language"] = "de"
        self.assertEqual(self.mod.get_ui_lang(), "en")

    def test_german_is_used_once_it_has_strings(self):
        self._catalog({"a": "Deutsch {n}"})
        self.state["ui_language"] = "de"
        self.assertEqual(self.mod.get_ui_lang(), "de")
        self.assertEqual(self.mod.ui_t("a", n=3), "Deutsch 3")

    def test_a_missing_german_string_falls_back_to_english(self):
        self._catalog({"a": "Deutsch {n}"})
        self.state["ui_language"] = "de"
        self.assertEqual(self.mod.ui_t("b"), "Only English")

    def test_browser_hint_de_is_followed_too(self):
        self._catalog({"a": "Deutsch {n}"})
        self.state.pop("ui_language", None)
        self.state["client_lang"] = "de-AT"
        self.assertEqual(self.mod.get_ui_lang(), "de")

    def test_settings_accept_german(self):
        self._catalog({"a": "Deutsch {n}"})
        r = self.mod.app.test_client().post("/api/settings",
                                            json={"ui_language": "de", "client_lang": "de"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.state["ui_language"], "de")
        self.assertEqual(self.state["client_lang"], "de")

    def test_settings_still_reject_unknown_languages(self):
        self.mod.app.test_client().post("/api/settings", json={"ui_language": "xx"})
        self.assertNotIn("ui_language", self.state)


NODE = r"""
const src = %s;
function load(messages, navLang, stored) {
  const store = stored ? {du_lang_mode: stored} : {};
  const win = {I18N_MESSAGES: messages};
  const doc = {documentElement: {}, querySelector: () => null, querySelectorAll: () => [],
               getElementById: () => null, addEventListener: () => {},
               createElement: () => ({style: {}, classList: {add() {}, remove() {}}, appendChild() {},
                                      setAttribute() {}, addEventListener() {}})};
  new Function('window', 'document', 'navigator', 'localStorage', src)(
    win, doc, {language: navLang},
    {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; }});
  return {I18N: win.I18N, doc};
}
const en = {'a': 'English', 'b': 'Only English'};
const empty = load({en, 'zh-CN': {}, de: {}}, 'de-DE');
const full = load({en, 'zh-CN': {}, de: {a: 'Deutsch'}}, 'de-DE');
const stored = load({en, 'zh-CN': {}, de: {}}, 'en-US', 'de');
console.log(JSON.stringify({
  empty_lang: empty.I18N.currentLang, empty_avail: empty.I18N.availableLangs(),
  full_lang: full.I18N.currentLang, full_avail: full.I18N.availableLangs(),
  full_html_lang: full.doc.documentElement.lang,
  full_a: full.I18N.t('a'), full_b: full.I18N.t('b'),
  stored_mode: stored.I18N.getLangMode(), stored_lang: stored.I18N.currentLang,
}));
"""


class GermanBrowserTests(unittest.TestCase):
    def test_language_choice_and_fallback_in_the_browser_code(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(NODE % json.dumps(I18N_JS))
            path = f.name
        try:
            r = subprocess.run(["node", path], capture_output=True, text=True,
                               encoding="utf-8", timeout=30)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        # Empty German block: not offered, a German browser still gets English.
        self.assertEqual(out["empty_lang"], "en")
        self.assertEqual(out["empty_avail"], ["en"])
        self.assertNotIn("de", out["empty_avail"])
        # A saved choice of German is ignored while there is nothing to show.
        self.assertEqual(out["stored_mode"], "auto")
        self.assertEqual(out["stored_lang"], "en")
        # Filled in: offered, followed from the browser language, English fills the gaps.
        self.assertEqual(out["full_lang"], "de")
        self.assertIn("de", out["full_avail"])
        self.assertEqual(out["full_html_lang"], "de")
        self.assertEqual(out["full_a"], "Deutsch")
        self.assertEqual(out["full_b"], "Only English")


if __name__ == "__main__":
    unittest.main()
