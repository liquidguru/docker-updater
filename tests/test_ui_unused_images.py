"""Run the Images tab's real rendering code under node (issue #31).

The tab must be informational only — there is no button anywhere in it that pulls
anything — and it must not exist at all while the option is off. This extracts the
actual functions from templates/index.html and executes them, using the real
English catalog so a mismatched `{placeholder}` shows up as a leftover brace.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
CATALOG = json.loads((ROOT / "static" / "i18n_messages.json").read_text(encoding="utf-8"))


def _extract(pattern):
    match = re.search(pattern, INDEX, re.S)
    assert match, f"could not find {pattern!r} in templates/index.html"
    return match.group(0)


HARNESS = """
const EN = %(catalog)s;
function t(key, params) {
  return (EN[key] ?? key).replace(/\\{(\\w+)\\}/g, (m, k) => (params && k in params ? params[k] : m));
}
Date.now = () => Date.parse('2026-10-21T12:00:00Z');
%(helpers)s
%(block)s
%(tabcounts)s

const img = (o) => Object.assign({image: 'postgres:13', host_id: 'local', host_name: 'Local',
  status: 'update', checked_at: '2026-10-21T10:00:00Z', size: 412345678}, o);
const out = {};
out.empty        = imagesPanelHtml([], false);
out.update       = imageCardHtml(img({}), false);
out.ok           = imageCardHtml(img({status: 'ok', image: 'redis:7'}), false);
out.unknown      = imageCardHtml(img({status: 'unknown', image: 'odd:1', checked_at: null, size: null}), false);
out.single_host  = imageCardHtml(img({host_id: 'vps', host_name: 'VPS'}), false);
out.multi_local  = imageCardHtml(img({}), true);
out.multi_remote = imageCardHtml(img({host_id: 'vps', host_name: 'VPS <1>'}), true);
out.hostile      = imageCardHtml(img({image: 'x"><img src=x onerror=alert(1)>:1'}), false);
out.panel        = imagesPanelHtml([img({}), img({status: 'ok', image: 'redis:7'})], false);

// --- updateTabCounts against a minimal DOM
function tabState(images, current) {
  const els = {};
  const el = id => (els[id] = els[id] || {style: {}, textContent: ''});
  globalThis.document = {getElementById: el};
  allImages = images; currentTab = current;
  switched = [];
  updateTabCounts([], []);
  return {display: el('tab-images').style.display, count: String(el('tab-count-images').textContent), switched};
}
let allImages = null, currentTab = 'update', switched = [];
function switchTab(tab) { switched.push(tab); }
out.tabs = {
  off:            tabState(null, 'update'),
  off_on_images:  tabState(null, 'images'),
  on_two_updates: tabState([img({}), img({image: 'mysql:8'}), img({status: 'ok', image: 'redis:7'})], 'update'),
  on_none:        tabState([], 'update'),
  on_viewing:     tabState([img({})], 'images'),
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class UnusedImagesUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = HARNESS % {
            "catalog": json.dumps(CATALOG["en"]),
            "helpers": "\n".join([
                _extract(r"  function fmtBytes\(n\) \{.*?\n  \}\n"),
                _extract(r"  function timeAgo\(iso\) \{.*?\n  \}\n"),
                _extract(r"  function esc\(s\) \{.*?\n  \}\n"),
            ]),
            "block": _extract(r"// <unused-images>.*?// </unused-images>"),
            "tabcounts": _extract(r"  function updateTabCounts\(containers, hosts\) \{.*?\n  \}\n"),
        }
        # updateTabCounts reads the other tab badges too; give it a DOM that
        # accepts them (the stub in HARNESS creates elements on demand).
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(script)
            path = f.name
        try:
            result = subprocess.run(["node", path], capture_output=True, text=True,
                                    encoding="utf-8", timeout=30)
        finally:
            Path(path).unlink(missing_ok=True)
        assert result.returncode == 0, result.stderr
        cls.out = json.loads(result.stdout)

    def test_an_update_shows_the_image_a_badge_and_the_command_to_run_yourself(self):
        html = self.out["update"]
        self.assertIn("postgres:13", html)
        self.assertIn("Update Available", html)
        self.assertIn("status-update", html)
        self.assertIn("To update it yourself, run:", html)
        self.assertIn("<code>docker pull postgres:13</code>", html)

    def test_nothing_in_the_tab_can_pull_anything(self):
        """The whole point: an old image may be a database, and a newer version
        can refuse the data the old one wrote. Telling you is the job."""
        for name in ("update", "ok", "unknown", "panel", "empty"):
            with self.subTest(name):
                self.assertNotIn("<button", self.out[name])
                self.assertNotIn("onclick", self.out[name])
        block = re.search(r"// <unused-images>.*?// </unused-images>", INDEX, re.S)
        assert block
        self.assertNotIn("/api/pull", block.group(0))
        self.assertNotIn("fetch(", block.group(0))

    def test_only_an_image_with_an_update_gets_the_command(self):
        self.assertNotIn("docker pull", self.out["ok"])
        self.assertNotIn("docker pull", self.out["unknown"])

    def test_each_status_has_its_own_badge(self):
        self.assertIn("Up to Date", self.out["ok"])
        self.assertIn("status-ok", self.out["ok"])
        self.assertIn("Not Checked", self.out["unknown"])
        self.assertIn("status-unknown", self.out["unknown"])

    def test_size_and_age_are_shown_when_known(self):
        self.assertIn("2h ago", self.out["update"])
        self.assertIn("393.2 MB", self.out["update"])
        self.assertIn(CATALOG["en"]["card.not_checked"], self.out["unknown"])

    def test_the_host_is_named_only_when_there_is_more_than_one(self):
        self.assertNotIn("host-chip", self.out["single_host"])
        self.assertIn("host-chip", self.out["multi_local"])
        self.assertIn(">Local<", self.out["multi_local"])
        self.assertIn("VPS &lt;1&gt;", self.out["multi_remote"])

    def test_an_image_name_cannot_inject_markup(self):
        html = self.out["hostile"]
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_the_panel_leads_with_the_warning_and_lists_every_image(self):
        html = self.out["panel"]
        self.assertTrue(html.startswith('<div class="images-note">'))
        self.assertIn("Nothing here is ever pulled for you", html)
        self.assertIn("databases especially", html)
        self.assertIn("postgres:13", html)
        self.assertIn("redis:7", html)

    def test_an_empty_list_says_to_run_a_check(self):
        html = self.out["empty"]
        self.assertIn("No unused images checked yet", html)
        self.assertIn("Check Now", html)
        self.assertIn("images-note", html)

    def test_no_placeholder_is_left_unfilled(self):
        for name in ("update", "ok", "unknown", "panel", "empty", "multi_remote"):
            with self.subTest(name):
                self.assertNotRegex(self.out[name], r"\{\w+\}")

    def test_the_tab_is_hidden_with_no_count_while_the_option_is_off(self):
        off = self.out["tabs"]["off"]
        self.assertEqual((off["display"], off["count"]), ("none", "0"))

    def test_the_tab_appears_with_the_number_of_updates_once_it_is_on(self):
        on = self.out["tabs"]["on_two_updates"]
        self.assertEqual((on["display"], on["count"]), ("", "2"))
        self.assertEqual(self.out["tabs"]["on_none"]["display"], "")

    def test_turning_it_off_while_viewing_the_tab_returns_to_updates(self):
        self.assertEqual(self.out["tabs"]["off_on_images"]["switched"], ["update"])
        self.assertEqual(self.out["tabs"]["off"]["switched"], [])
        self.assertEqual(self.out["tabs"]["on_viewing"]["switched"], [])

    def test_the_page_starts_with_the_feature_absent(self):
        self.assertIn("let allImages = null;", INDEX)
        self.assertRegex(INDEX, r'id="tab-images"[^>]*style="display:none"')
        self.assertIn("allImages     = Array.isArray(data.unused_images) ? data.unused_images : null;", INDEX)

    def test_the_setting_is_a_toggle_that_is_saved_and_refreshes_the_tab(self):
        self.assertIn('id="check-unused"', INDEX)
        payload = re.search(r"const payload = \{[^}]*\}", INDEX)
        assert payload
        self.assertIn("check_unused_images: checkUnused", payload.group(0),
                      "the toggle is read but never sent to the server")
        self.assertIn("if (unusedChanged) fetchStatus();", INDEX)

    def test_the_off_switch_defaults_off_in_the_pages_own_settings(self):
        self.assertIn("check_unused_images: false", INDEX)


if __name__ == "__main__":
    unittest.main()
