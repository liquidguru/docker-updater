"""Run the reclaim panel's real rendering code under node.

Extracts the functions between `// <prune-ui>` markers in templates/index.html
and executes them with the real English catalog, so a mismatched `{placeholder}`
shows up as a leftover brace. What matters: images a backup still holds are shown
but can never be selected, and the job view says plainly that leaving the page is
safe and what happened to anything that was skipped.
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
Date.now = () => Date.parse('2026-11-01T12:00:00Z');
%(helpers)s
%(block)s

const GB = 1024 ** 3;
const img = (id, size, extra) => Object.assign({
  id: 'sha256:' + id, short_id: id.slice(0, 12), repo: 'ghcr.io/x/' + id, size: size,
  size_human: (size / GB).toFixed(2) + 'GB', created: '2026-10-20T00:00:00Z', held_by: []}, extra || {});
const backup = (name, expires) => ({name, state: 'exited', backup: true, expires_at: expires || null});

const out = {};
out.free_only = pruneListHtml([img('aaaaaaaaaaaaaa', 2 * GB), img('bbbbbbbbbbbbbb', GB)]);
out.mixed = pruneListHtml([
  img('heldheldheld', 4 * GB, {held_by: [backup('openwebui_old', '2026-11-02T00:00:00Z')]}),
  img('freefreefree', 2 * GB),
  img('free2free2free', GB),
]);
out.all_held = pruneListHtml([img('heldheldheld', 4 * GB, {held_by: [backup('web_old')]})]);
out.none = pruneListHtml([]);
out.one_free = pruneListHtml([img('onlyoneonly', GB)]);
out.held_running = pruneListHtml([img('runrunrun', GB, {held_by: [{name: 'web', state: 'running', backup: false}]}), img('freefreefree', GB)]);
out.held_stopped = pruneListHtml([img('stopstopstop', GB, {held_by: [{name: 'tool', state: 'exited', backup: false}]}), img('freefreefree', GB)]);
out.held_many = pruneListHtml([img('manymanymany', GB, {held_by: [backup('a_old'), backup('b_old'), backup('c_old')]}), img('freefreefree', GB)]);
out.hostile = pruneListHtml([img('hostilehostile', GB, {held_by: [{name: '<img src=x onerror=alert(1)>', state: 'exited', backup: false}]}), img('freefreefree', GB)]);

const job = (o) => Object.assign({status: 'done', total: 3, done: 3, count: 2, freed: '8.63GB', skipped: [],
  finished_at: '2026-11-01T11:00:00Z', job_id: 'j1'}, o);
out.running = pruneJobView({status: 'running', total: 30, done: 4, current_size: '4.24GB'});
out.running_no_current = pruneJobView({status: 'running', total: 30, done: 0});
out.done_clean = pruneJobView(job({}));
out.done_none = pruneJobView(job({count: 0, freed: '0B'}));
out.done_skipped = pruneJobView(job({skipped: [
  {id: 'sha256:aaaaaaaaaaaa', kind: 'container', state: 'stopped', name: 'openwebui_old', backup: true, reason: 'x'},
  {id: 'sha256:bbbbbbbbbbbb', kind: 'container', state: 'running', name: 'web', backup: false, reason: 'x'},
  {id: 'sha256:cccccccccccc', kind: 'container', state: 'stopped', name: 'tool', backup: false, reason: 'x'},
  {id: 'sha256:dddddddddddd', kind: 'dependents', reason: 'x'},
  {id: 'sha256:eeeeeeeeeeee', kind: 'slow', reason: 'x'},
  {id: 'sha256:ffffffffffff', kind: 'other', reason: 'raw docker words'},
]}));
out.lost = pruneJobView({status: 'lost'});
out.error = pruneJobView({status: 'error', error: 'Job not found.'});
out.error_plain = pruneJobView({status: 'error'});
out.hostile_job = pruneJobView(job({skipped: [{id: 'x', kind: 'container', state: 'stopped', name: '<b>boom</b>', backup: false}]}));
out.when = pruneWhen('2026-11-02T00:00:00Z');
out.when_bad = pruneWhen('not a date');
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ReclaimUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = HARNESS % {
            "catalog": json.dumps(CATALOG["en"]),
            "helpers": "\n".join([
                _extract(r"  function esc\(s\) \{.*?\n  \}\n"),
                _extract(r"  function timeAgo\(iso\) \{.*?\n  \}\n"),
                _extract(r"  function _human_size_js\(n\) \{.*?\n  \}\n"),
            ]),
            "block": _extract(r"// <prune-ui>.*?// </prune-ui>"),
        }
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

    # -- the list ------------------------------------------------------------

    def test_with_nothing_held_the_list_is_as_it_was(self):
        html = self.out["free_only"]
        self.assertIn("Select all (2 images)", html)
        self.assertEqual(html.count('class="prune-check"'), 2)
        self.assertNotIn("can be removed now", html)
        self.assertNotIn('type="checkbox" disabled', html, "nothing is held, so no row is locked")

    def test_a_held_image_can_never_be_selected(self):
        html = self.out["mixed"]
        held_row = next(r for r in html.split("<label") if "heldheldheld" in r)
        self.assertIn("disabled", held_row)
        self.assertNotIn("prune-check", held_row, "select-all would pick it up")
        self.assertNotIn("value=", held_row)

    def test_select_all_counts_and_sizes_only_what_can_be_removed(self):
        html = self.out["mixed"]
        self.assertIn("Select all (2 images)", html)
        self.assertEqual(html.count('class="prune-check"'), 2)
        self.assertIn("3.00GB", html)          # 2 + 1, not 7
        self.assertNotIn("7.00GB", html)

    def test_the_summary_separates_removable_from_held(self):
        html = self.out["mixed"]
        self.assertIn("2 can be removed now (3.00GB)", html)
        self.assertIn("1 held by backups or containers (4.00GB)", html)

    def test_removable_images_are_listed_before_held_ones(self):
        html = self.out["mixed"]
        self.assertLess(html.index("freefreefree"), html.index("heldheldheld"))

    def test_a_backup_holder_says_which_backup_and_until_when(self):
        html = self.out["mixed"]
        self.assertIn("Kept by backup openwebui_old until", html)
        self.assertNotIn("{", re.sub(r"[^{]*style=\"[^\"]*\"", "", html).replace("${", ""))

    def test_a_backup_without_an_expiry_is_still_named(self):
        self.assertIn("Kept by backup web_old", self.out["all_held"])
        self.assertNotIn("until", self.out["all_held"])

    def test_when_everything_is_held_there_is_nothing_to_select_or_delete(self):
        html = self.out["all_held"]
        self.assertIn("Nothing can be removed right now", html)
        self.assertIn("1 held by backups or containers (4.00GB)", html)
        self.assertNotIn("prune-select-all", html)
        self.assertNotIn("prune-delete-btn", html)
        self.assertNotIn("Delete selected", html)

    def test_a_running_container_and_a_stopped_one_are_described_differently(self):
        self.assertIn("In use by running container web", self.out["held_running"])
        self.assertIn("Used by stopped container tool", self.out["held_stopped"])

    def test_extra_holders_are_counted(self):
        html = self.out["held_many"]
        self.assertIn("Kept by backup a_old (+2 more)", html)

    def test_an_empty_list_says_so(self):
        self.assertIn("No reclaimable dangling images found.", self.out["none"])
        self.assertNotIn("prune-delete-btn", self.out["none"])

    def test_one_removable_image_uses_the_singular(self):
        self.assertIn("Select all (1 image)", self.out["one_free"])

    def test_hostile_container_names_are_escaped(self):
        html = self.out["hostile"]
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)

    # -- the job view --------------------------------------------------------

    def test_a_running_job_says_leaving_the_page_is_safe(self):
        view = self.out["running"]
        self.assertIn("Working — 4/30", view["html"])
        self.assertIn("Removing a 4.24GB image", view["html"])
        self.assertIn("Safe to leave this page", view["html"])
        self.assertNotIn("Dismiss", view["html"], "a running job must not be dismissable")

    def test_a_running_job_without_a_current_image_still_renders(self):
        html = self.out["running_no_current"]["html"]
        self.assertIn("Working — 0/30", html)
        self.assertNotIn("Removing a", html)

    def test_a_finished_job_reports_what_was_freed_and_when(self):
        view = self.out["done_clean"]
        self.assertIn("Freed 8.63GB across 2 image(s).", view["html"])
        self.assertIn("Finished", view["html"])
        self.assertEqual(view["color"], "var(--green)")
        self.assertIn("Dismiss", view["html"])

    def test_a_job_that_removed_nothing_does_not_claim_success(self):
        view = self.out["done_none"]
        self.assertIn("Nothing was removed.", view["html"])
        self.assertNotIn("Freed", view["html"])
        self.assertEqual(view["color"], "var(--muted)")

    def test_every_kind_of_skip_has_its_own_plain_wording(self):
        html = self.out["done_skipped"]["html"]
        self.assertIn("6 skipped:", html)
        self.assertIn("kept by backup openwebui_old", html)
        self.assertIn("used by running container web", html)
        self.assertIn("used by stopped container tool", html)
        self.assertIn("other images depend on it", html)
        self.assertIn("Docker was still removing it — check again shortly", html)
        self.assertIn("raw docker words", html)

    def test_no_view_leaves_a_placeholder_unfilled(self):
        for name in ("running", "done_clean", "done_none", "done_skipped", "lost", "error"):
            html = self.out[name]["html"]
            self.assertNotRegex(html, r"\{\w+\}", f"{name} has an unfilled placeholder")
        for name in ("mixed", "all_held", "held_many", "held_running", "held_stopped"):
            self.assertNotRegex(self.out[name], r"\{(name|n|when|size|held|heldsize)\}", name)

    def test_a_lost_job_and_a_failed_poll_are_reported_in_red(self):
        self.assertIn("Lost track of the job", self.out["lost"]["html"])
        self.assertEqual(self.out["lost"]["color"], "var(--red)")
        self.assertIn("Job not found.", self.out["error"]["html"])
        self.assertIn("Could not check progress.", self.out["error_plain"]["html"])

    def test_hostile_names_in_a_finished_job_are_escaped(self):
        html = self.out["hostile_job"]["html"]
        self.assertNotIn("<b>boom</b>", html)
        self.assertIn("&lt;b&gt;boom&lt;/b&gt;", html)

    def test_dates_that_cannot_be_read_do_not_break_the_row(self):
        self.assertEqual(self.out["when_bad"], "")
        self.assertTrue(self.out["when"])


class ReclaimWiringTests(unittest.TestCase):
    """The panel is rebuilt every time Settings is opened (that is what left it
    blank); these pin that it restores the list and the job."""

    def test_opening_settings_restores_the_list_and_the_job(self):
        body = _extract(r"    applyScheduleVisibility\(\);\n    renderNextCheck\(_settings\.next_check\);.*?\n  \}\n")
        self.assertIn("renderDanglingList()", body)
        self.assertIn("resumePruneView()", body)

    def test_the_list_is_remembered_between_visits(self):
        self.assertIn("let _pruneLoaded = false;", INDEX)
        self.assertIn("_pruneLoaded = true;", INDEX)

    def test_the_poller_does_not_pop_or_depend_on_the_panel_that_started_it(self):
        poll = _extract(r"  async function pollPruneJob\(jobId\) \{.*?\n  \}\n")
        self.assertIn("_prunePolling === jobId", poll, "a second poller could start")
        self.assertIn("paintPruneJob()", poll)
        self.assertNotIn("getElementById", poll,
                         "polling must repaint from state, not from elements that may be gone")

    def test_a_409_attaches_to_the_running_job(self):
        delete = _extract(r"  async function deleteSelectedImages\(\) \{.*?\n  \}\n")
        self.assertIn("r.status === 409", delete)
        self.assertIn("pollPruneJob(d.job_id)", delete)

    def test_the_catalogs_have_every_new_key_in_both_languages(self):
        for key in ("prune.held_backup", "prune.held_backup_until", "prune.held_running",
                    "prune.held_stopped", "prune.held_more", "prune.summary",
                    "prune.nothing_now", "prune.running_note", "prune.working_current",
                    "prune.freed_none", "prune.finished_ago", "prune.skip_backup",
                    "prune.skip_running", "prune.skip_stopped", "prune.skip_dependents",
                    "prune.skip_slow", "prune.dismiss", "prune.already_running"):
            self.assertIn(key, CATALOG["en"], key)
            self.assertIn(key, CATALOG["zh-CN"], key)


if __name__ == "__main__":
    unittest.main()
