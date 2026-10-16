"""Run the dashboard's real pull-status code under node.

test_ui_static only proves the template's JavaScript *parses*. What an updating
card says about its download is logic — which phase gets which wording, when the
bar is determinate, what happens when the entry disappears — so this extracts the
actual functions from templates/index.html and executes them against a table of
/api/pulls entries, using the real English catalog (so a mismatched `{placeholder}`
shows up as a leftover brace instead of passing silently).
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
let activePulls = {};
let pullSeen = new Set();
%(human)s
%(esc)s
%(block)s

const E = (phase, done, total, pct, est) => ({
  host_id: 'local', container: 'web', phase,
  progress_bytes: done, total_bytes: total, percent_complete: pct, total_estimated: est,
});
const out = {};
function run(name, pulls, seen) {
  activePulls = pulls; pullSeen = new Set(seen || []);
  out[name] = {status: pullStatus('local:web'), html: pullProgressHtml('local:web')};
}
const K = 'local:web';
run('absent',            {},                                                   []);
run('absent_but_seen',   {},                                                   [K]);
run('starting_estimate', {[K]: E('starting', 0, 398351173, 0.0, true)},        []);
run('starting_bare',     {[K]: E('starting', 0, null, null, false)},           []);
run('downloading',       {[K]: E('downloading', 247100000, 398351173, 62.04, false)}, []);
run('downloading_est',   {[K]: E('downloading', 100000000, 398351173, 25.1, true)},   []);
run('downloading_nopct', {[K]: E('downloading', 52428800, null, null, false)}, []);
run('floors_not_rounds', {[K]: E('downloading', 1, 2, 99.99, false)},          []);
run('extracting',        {[K]: E('extracting', 398351173, 398351173, 100.0, false)},  []);
run('finishing',         {[K]: E('finishing', 398351173, 398351173, 100.0, false)},   []);
run('no_phase_field',    {[K]: {host_id: 'local', container: 'web', progress_bytes: 5242880,
                               total_bytes: 10485760, percent_complete: 50.0, total_estimated: false}}, []);
out.escaped_key = pullProgressHtml('a"b<c');   // no entry + not seen -> empty, so seed one
activePulls = {'a"b<c': E('downloading', 1, 2, 50.0, false)};
out.escaped_key = pullProgressHtml('a"b<c');
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PullStatusUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = HARNESS % {
            "catalog": json.dumps(CATALOG["en"]),
            "human": _extract(r"  function _human_size_js\(n\) \{.*?\n  \}\n"),
            "esc": _extract(r"  function esc\(s\) \{.*?\n  \}\n"),
            "block": _extract(r"// <pull-status>.*?// </pull-status>"),
        }
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(script)
            path = f.name
        try:
            # node writes UTF-8; without this, Python on Windows decodes the output
            # with the console code page and turns "…" and "·" into mojibake
            result = subprocess.run(["node", path], capture_output=True, text=True,
                                    encoding="utf-8", timeout=30)
        finally:
            Path(path).unlink(missing_ok=True)
        assert result.returncode == 0, result.stderr
        cls.out = json.loads(result.stdout)

    def status(self, name):
        return self.out[name]["status"]

    def test_no_pull_and_never_seen_says_nothing(self):
        self.assertIsNone(self.status("absent"))
        self.assertEqual(self.out["absent"]["html"], "")

    def test_after_the_pull_ends_the_card_says_the_container_is_being_replaced(self):
        """The entry vanishes when the stream ends but the update carries on
        (stop, rename, recreate, verify) — going quiet there looks like a hang."""
        self.assertEqual(self.status("absent_but_seen")["text"], "Replacing the container…")

    def test_each_phase_has_its_own_wording(self):
        self.assertEqual(self.status("starting_estimate")["text"], "Starting the download…")
        self.assertIn("Unpacking the image", self.status("extracting")["text"])
        self.assertEqual(self.status("finishing")["text"], "Finishing the download…")

    def test_unpacking_and_finishing_are_not_shown_as_a_stuck_100_percent(self):
        """The whole reason `phase` exists: after the download the byte counts sit
        at 100% for minutes while the image is unpacked."""
        for name in ("extracting", "finishing"):
            with self.subTest(name):
                self.assertIsNone(self.status(name)["pct"])
                self.assertIn("indeterminate", self.out[name]["html"])
                self.assertNotIn("100%", self.status(name)["text"])

    def test_a_real_total_reads_plainly_and_an_estimate_is_marked_with_a_tilde(self):
        self.assertTrue(self.status("downloading")["text"].startswith("Downloading 62% · "))
        self.assertNotIn("~", self.status("downloading")["text"])
        self.assertIn(" of ~", self.status("downloading_est")["text"])

    def test_percentage_is_floored_so_it_never_claims_100_early(self):
        self.assertTrue(self.status("floors_not_rounds")["text"].startswith("Downloading 99%"))

    def test_unknown_total_shows_bytes_so_far_with_an_indeterminate_bar(self):
        s = self.status("downloading_nopct")
        self.assertEqual(s["text"], "Downloading… 50.00MB so far")
        self.assertIsNone(s["pct"])

    def test_a_pull_without_a_phase_field_is_treated_as_downloading(self):
        """An older server doesn't send one."""
        self.assertTrue(self.status("no_phase_field")["text"].startswith("Downloading 50%"))

    def test_determinate_bar_carries_the_percentage_and_is_accessible(self):
        html = self.out["downloading"]["html"]
        self.assertIn('role="progressbar"', html)
        self.assertIn('aria-valuenow="62"', html)
        self.assertIn("width:62.04%", html)

    def test_no_placeholder_is_left_unfilled_in_any_message(self):
        """A parameter named differently in the code and the catalog would
        otherwise render a literal {pct} to users."""
        for name, entry in self.out.items():
            status = entry["status"] if isinstance(entry, dict) and "status" in entry else None
            if status:
                with self.subTest(name):
                    self.assertNotRegex(status["text"], r"\{\w+\}")

    def test_the_key_is_escaped_in_the_markup(self):
        html = self.out["escaped_key"]
        self.assertIn("a&quot;b&lt;c", html)
        self.assertNotIn('a"b<c', html)


if __name__ == "__main__":
    unittest.main()
