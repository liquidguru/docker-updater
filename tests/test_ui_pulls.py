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

const E = (phase, done, total, pct, est, layersDone, layersTotal) => ({
  host_id: 'local', container: 'web', phase,
  progress_bytes: done, total_bytes: total, percent_complete: pct, total_estimated: est,
  layers_done: layersDone, layers_total: layersTotal,
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
run('layers_mid',        {[K]: E('extracting', 398351173, 398351173, 100.0, false, 3, 8)}, []);
run('layers_none_done',  {[K]: E('extracting', 398351173, 398351173, 100.0, false, 0, 8)}, []);
run('layers_all_done',   {[K]: E('extracting', 398351173, 398351173, 100.0, false, 8, 8)}, []);
run('layers_overcount',  {[K]: E('extracting', 398351173, 398351173, 100.0, false, 9, 8)}, []);
run('layers_single',     {[K]: E('extracting', 398351173, 398351173, 100.0, false, 0, 1)}, []);
run('layers_zero_total', {[K]: E('extracting', 398351173, 398351173, 100.0, false, 0, 0)}, []);
run('layers_in_download', {[K]: E('downloading', 100, 400, 25.0, false, 2, 8)},           []);
run('no_phase_field',    {[K]: {host_id: 'local', container: 'web', progress_bytes: 5242880,
                               total_bytes: 10485760, percent_complete: 50.0, total_estimated: false}}, []);
out.escaped_key = pullProgressHtml('a"b<c');   // no entry + not seen -> empty, so seed one
activePulls = {'a"b<c': E('downloading', 1, 2, 50.0, false)};
out.escaped_key = pullProgressHtml('a"b<c');

// --- syncPullProgress against a minimal DOM: how an existing card's bar changes
const mkBar = kind => ({
  dataset: {kind}, firstElementChild: {style: {}}, attrs: {},
  setAttribute(k, v) { this.attrs[k] = v; },
  set outerHTML(v) { this.rebuilt = v; },
});
function sync(barKind, pulls) {
  const bar = mkBar(barKind), text = {textContent: ''};
  const el = {querySelector: sel => sel === '.pull-text' ? text : bar, remove() {}};
  const card = {dataset: {key: K},
    querySelector: sel => sel === '.pull-progress' ? el : {insertAdjacentHTML() {}}};
  globalThis.document = {querySelectorAll: () => [card]};
  activePulls = pulls; pullSeen = new Set([K]);
  syncPullProgress();
  return {rebuilt: bar.rebuilt || null, width: bar.firstElementChild.style.width || null,
          now: bar.attrs['aria-valuenow'] ?? null, text: text.textContent};
}
const DL  = {[K]: E('downloading', 100, 400, 25.0, false)};
const UNP = {[K]: E('extracting', 400, 400, 100.0, false, 3, 8)};
const ONE = {[K]: E('extracting', 400, 400, 100.0, false, 0, 1)};
out.sync = {
  dl_to_dl:      sync('download', DL),
  dl_to_unpack:  sync('download', UNP),
  unpack_stays:  sync('unpack', UNP),
  unpack_to_wait: sync('unpack', ONE),
  wait_stays:    sync('wait', ONE),
  wait_to_dl:    sync('wait', DL),
};

// --- the waiting bar's animation phase follows the wall clock
const realNow = Date.now;
const waitAt = ms => { Date.now = () => ms; return pullBarHtml({pct: null}); };
out.clock = {
  at_1000:  waitAt(1000),
  at_3600:  waitAt(3600),     // one period (2600ms) later: same phase
  at_2000:  waitAt(2000),     // a different phase
  at_0:     waitAt(0),
  at_2599:  waitAt(2599),
  download: (Date.now = () => 1234, pullBarHtml({pct: 50})),
};
Date.now = realNow;
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
            "block": _extract(r"// <pull-status>.*?// </pull-status>") + "\n"
                     + _extract(r"  function syncPullProgress\(\) \{.*?\n  \}\n"),
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

    def test_unpacking_counts_finished_layers_on_a_determinate_bar(self):
        s = self.status("layers_mid")
        self.assertEqual(s["text"], "Unpacking the image · 3 of 8 layers done")
        self.assertEqual(s["pct"], 37.5)
        html = self.out["layers_mid"]["html"]
        self.assertIn("pull-bar unpack", html)
        self.assertIn("width:37.5%", html)
        self.assertNotIn("indeterminate", html)

    def test_unpacking_with_nothing_finished_yet_is_an_empty_bar_not_a_waiting_one(self):
        s = self.status("layers_none_done")
        self.assertEqual(s["pct"], 0)
        self.assertIn("0 of 8", s["text"])
        self.assertIn("pull-bar unpack", self.out["layers_none_done"]["html"])

    def test_layer_count_never_runs_past_the_total(self):
        self.assertEqual(self.status("layers_overcount")["pct"], 100)
        self.assertIn("8 of 8", self.status("layers_overcount")["text"])
        self.assertEqual(self.status("layers_all_done")["pct"], 100)

    def test_a_single_layer_or_missing_counts_fall_back_to_a_calm_wait(self):
        """One layer has nothing to count, and an older server sends no counts."""
        for name in ("layers_single", "layers_zero_total", "extracting"):
            with self.subTest(name):
                self.assertIsNone(self.status(name)["pct"])
                self.assertIn("Unpacking the image", self.status(name)["text"])
                self.assertNotIn(" of ", self.status(name)["text"])
                self.assertIn("indeterminate", self.out[name]["html"])

    def test_layer_counts_only_matter_while_unpacking(self):
        self.assertTrue(self.status("layers_in_download")["text"].startswith("Downloading 25%"))
        self.assertNotIn("unpack", self.out["layers_in_download"]["html"])

    def test_the_download_bar_and_the_unpack_bar_are_different_kinds(self):
        """Same blue bar going from full to empty would visibly run backwards."""
        self.assertIn('data-kind="download"', self.out["downloading"]["html"])
        self.assertIn('data-kind="unpack"', self.out["layers_mid"]["html"])
        self.assertIn('data-kind="wait"', self.out["extracting"]["html"])

    def test_a_card_whose_bar_changes_kind_gets_a_fresh_bar(self):
        """Moving from download to unpack must rebuild the bar (no animated trip
        from 100% back to 0%); staying the same kind updates it in place."""
        sync = self.out["sync"]
        self.assertIn("pull-bar unpack", sync["dl_to_unpack"]["rebuilt"])
        self.assertIsNone(sync["dl_to_unpack"]["width"])
        self.assertIn("indeterminate", sync["unpack_to_wait"]["rebuilt"])
        self.assertIn("pull-bar", sync["wait_to_dl"]["rebuilt"])

    def test_a_card_whose_bar_keeps_its_kind_is_updated_in_place(self):
        sync = self.out["sync"]
        self.assertIsNone(sync["dl_to_dl"]["rebuilt"])
        self.assertEqual(sync["dl_to_dl"]["width"], "25%")
        self.assertIsNone(sync["unpack_stays"]["rebuilt"])
        self.assertEqual(sync["unpack_stays"]["width"], "37.5%")
        self.assertEqual(sync["unpack_stays"]["now"], 38)
        self.assertIn("3 of 8", sync["unpack_stays"]["text"])
        self.assertIsNone(sync["wait_stays"]["rebuilt"])

    @staticmethod
    def _delay(html):
        match = re.search(r"animation-delay:-(\d+)ms", html)
        assert match, html
        return int(match.group(1))

    def test_a_rebuilt_waiting_bar_carries_on_from_the_same_point_in_the_fade(self):
        """The status poll rebuilds every card each 2s while anything updates, and
        a rebuilt element restarts its animation — so without this the fade would
        jerk back to its start every couple of seconds."""
        clock = self.out["clock"]
        self.assertEqual(self._delay(clock["at_1000"]), 1000)
        self.assertEqual(self._delay(clock["at_3600"]), 1000,
                         "one full period later should be the same phase")
        self.assertNotEqual(self._delay(clock["at_2000"]), self._delay(clock["at_1000"]))
        self.assertEqual(self._delay(clock["at_0"]), 0)
        self.assertEqual(self._delay(clock["at_2599"]), 2599)

    def test_the_phase_period_matches_the_stylesheets_animation_length(self):
        css = re.search(r"animation:pull-pulse ([\d.]+)s", INDEX)
        self.assertIsNotNone(css)
        period_ms = round(float(css.group(1) if css else 0) * 1000)
        self.assertIn(f"const PULL_PULSE_MS = {period_ms};", INDEX)

    def test_only_the_waiting_bar_carries_an_animation_delay(self):
        self.assertNotIn("animation-delay", self.out["clock"]["download"])
        self.assertNotIn("animation-delay", self.out["layers_mid"]["html"])

    def test_the_sliding_stripe_is_gone_from_the_stylesheet(self):
        """It snapped back to the start every cycle; two at once were distracting."""
        self.assertNotIn("pull-slide", INDEX)
        self.assertNotIn("margin-left:-35%", INDEX)
        self.assertIn("pull-pulse", INDEX)

    def test_the_calm_wait_still_respects_reduced_motion(self):
        match = re.search(r"prefers-reduced-motion: reduce\) \{ \.pull-bar\.indeterminate[^}]*\}", INDEX)
        self.assertIsNotNone(match)
        self.assertIn("animation:none", match.group(0) if match else "")

    def test_the_key_is_escaped_in_the_markup(self):
        html = self.out["escaped_key"]
        self.assertIn("a&quot;b&lt;c", html)
        self.assertNotIn('a"b<c', html)


if __name__ == "__main__":
    unittest.main()
