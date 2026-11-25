"""Run the System tab's real rendering code under node.

The tab must not exist while the option is off, must never offer an action that the server
would refuse (installing something apt is holding back, rebooting mid-update), must say plainly
what a machine reported, and must not let a hostile name or error text inject markup.
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
Date.now = () => Date.parse('2026-11-24T12:00:00Z');
%(helpers)s
%(block)s
%(tabcounts)s

const pkg = (o) => Object.assign({name: 'tzdata', from: '2026e', to: '2026f', suites: ['x'],
  security: false, held_back: false, reboot_hint: false}, o);
const machine = (o, info) => Object.assign({id: 'dock', name: 'myserver', address: 'u@h', kind: 'apt',
  status: 'online', last_check: '2026-11-24T10:00:00Z', last_error: null, updating: false,
  info: Object.assign({os: 'Ubuntu 26.04', kernel: '7.0.0-44', reboot_required: false, reboot_reasons: [],
    services_need_restart: [], unattended_upgrades: true, packages: []}, info || {})}, o);

const out = {};
out.empty    = systemPanelHtml([], {});
out.upToDate = sysMachineHtml(machine({}), null);
out.pending  = sysMachineHtml(machine({}, {packages: [
  pkg({name: 'python3-oauthlib', security: true}), pkg({name: 'linux-image-generic', reboot_hint: true}),
  pkg({name: 'libheld', held_back: true})]}), null);
out.updating = sysMachineHtml(machine({updating: true}, {packages: [pkg({})]}), ['Setting up tzdata', '✔ finished']);
out.finished = sysMachineHtml(machine({}, {packages: [pkg({})]}), ['Setting up tzdata', '✔ finished']);
out.reboot   = sysMachineHtml(machine({}, {reboot_required: true, reboot_reasons: ['linux-image-7.0.0-45']}), null);
out.rebootBusy = sysMachineHtml(machine({updating: true}, {reboot_required: true}), null);
out.restart  = sysMachineHtml(machine({}, {services_need_restart: ['cron.service', 'ssh.service']}), null);
out.offline  = sysMachineHtml(machine({status: 'offline', last_error: 'Permission denied (publickey).'}), null);
out.rebooting = sysMachineHtml(machine({status: 'rebooting'}), null);
out.unattendedOff = sysMachineHtml(machine({}, {unattended_upgrades: false}), null);
out.hostile  = sysMachineHtml(machine({name: '<img src=x onerror=alert(1)>', last_error: '<b>x</b>', status: 'offline'},
  {packages: [pkg({name: 'a"><script>alert(1)</script>'})]}), ['<script>alert(2)</script>']);
out.panel    = systemPanelHtml([machine({}, {packages: [pkg({})]}), machine({id: 'nas', name: 'nas'})], {});
out.form     = sysFormHtml([machine({})]);
out.quotes   = sysMachineHtml(machine({name: "O'Brien\\"; alert(1);//", id: "x'y"}, {packages: [pkg({name: "p'q"})]}), null)
  + sysFormHtml([machine({name: "O'Brien\\"; alert(1);//", id: "x'y"})]);
out.nas = sysMachineHtml(machine({kind: 'dsm', name: 'mynas'}, {os: 'DSM 7.3.2-86009 Update 6', kernel: '',
  unattended_upgrades: null, stale: false,
  packages: [pkg({name: 'DSM', from: 'DSM 7.3.2-86009 Update 6', to: '7.3.3'}), pkg({name: 'Git Server', security: true})]}), null);
out.nasUnchecked = sysMachineHtml(machine({kind: 'dsm'}, {os: 'DSM 7.3.2-86009', kernel: '', stale: false, packages: [], unchecked: ['Vim', 'Mono']}), null);
out.win = sysMachineHtml(machine({kind: 'windows', name: 'mypc'}, {os: 'Microsoft Windows 11 Pro 10.0.26300.1', kernel: '',
  unattended_upgrades: null, stale: false, reboot_required: false,
  packages: [pkg({name: '2026-11 Cumulative Update (KB5001)', from: '', to: 'KB5001', security: true, reboot_hint: true})]}), null);
out.winReboot = sysMachineHtml(machine({kind: 'windows'}, {os: 'Windows', kernel: '', stale: false, reboot_required: true,
  reboot_reasons: ['Windows Update'], packages: []}), null);
out.winStale = sysMachineHtml(machine({kind: 'windows'}, {os: 'Windows', kernel: '', stale: true, packages: []}), null);
out.nasReboot = sysMachineHtml(machine({kind: 'dsm'}, {os: 'DSM', kernel: '', stale: false, reboot_required: false, packages: []}), null);
out.aptReboot = sysMachineHtml(machine({}, {reboot_required: true, reboot_reasons: ['linux-image']}), null);
out.nasStale = sysMachineHtml(machine({kind: 'dsm'}, {os: 'DSM 7.3.2-86009', kernel: '', stale: true, packages: []}), null);
out.nasClean = sysMachineHtml(machine({kind: 'dsm'}, {os: 'DSM 7.3.2-86009', kernel: '', stale: false, packages: []}), null);
out.nasOffline = sysMachineHtml(machine({kind: 'dsm', status: 'offline', last_error: 'x'}), null);
out.rebootNone = sysMachineHtml(machine({}, {packages: [pkg({})]}), null);
sysCollapsed.add('dock');
out.foldedPending = sysMachineHtml(machine({}, {reboot_required: true, reboot_reasons: ['x'], packages: [pkg({security: true}), pkg({name: 'curl'}), pkg({name: 'libheld', held_back: true})]}), ['Setting up x', 'finished']);
out.foldedOne = sysMachineHtml(machine({}, {packages: [pkg({name: 'curl'})]}), null);
out.foldedOk = sysMachineHtml(machine({}, {packages: []}), null);
out.foldedOffline = sysMachineHtml(machine({status: 'offline', last_error: 'x'}), null);
out.foldedBusy = sysMachineHtml(machine({updating: true}, {packages: [pkg({})]}), ['log line']);
out.foldedNas = sysMachineHtml(machine({kind: 'dsm'}, {os: 'DSM 7', kernel: '', packages: [pkg({name: 'FFmpeg 4'})]}), null);
sysCollapsed.delete('dock');
out.unfolded = sysMachineHtml(machine({}, {packages: [pkg({security: true}), pkg({name: 'curl'})]}), ['log line']);
out.toggled = [];
sysToggle('dock'); out.toggled.push(sysCollapsed.has('dock'));
sysToggle('dock'); out.toggled.push(sysCollapsed.has('dock'));
out.hostileId = (sysToggle("x'y"), sysMachineHtml(machine({id: "x'y"}, {packages: []}), null));
sysCollapsed.clear();
out.openWarn = systemPanelHtml([], {}, true);
out.count    = [sysPendingCount(null), sysPendingCount([]),
  sysPendingCount([machine({}, {packages: [pkg({}), pkg({name: 'curl'})]}), machine({id: 'b', info: null})])];

out.warnShown = systemPanelHtml([], {}, true);
out.warnNoOpen = systemPanelHtml([], {}, false);
sysDismissOpenWarning();
out.warnDismissed = systemPanelHtml([], {}, true);
out.warnDismissedWithMachine = systemPanelHtml([machine({}, {packages: [pkg({})]})], {}, true);
out.dismissedFlag = sysOpenDismissed;

function tabState(machines, current) {
  const els = {};
  const el = id => (els[id] = els[id] || {style: {}, textContent: ''});
  globalThis.document = {getElementById: el};
  allImages = null; allSystem = machines; currentTab = current; switched = [];
  updateTabCounts([], []);
  return {display: el('tab-system').style.display, count: String(el('tab-count-system').textContent), switched};
}
let allImages = null, currentTab = 'update', switched = [];
function switchTab(tab) { switched.push(tab); }
out.tabs = {
  off:        tabState(null, 'update'),
  offViewing: tabState(null, 'system'),
  on:         tabState([machine({}, {packages: [pkg({}), pkg({name: 'curl'})]})], 'update'),
  onEmpty:    tabState([], 'update'),
  onViewing:  tabState([machine({})], 'system'),
};
out.docker = sysMachineHtml(machine({}, {packages: [pkg({name: 'docker-ce'}), pkg({name: 'curl'}),
  pkg({name: 'containerd.io', held_back: true})]}), null);
out.dockerHere = sysMachineHtml(machine({runs_here: true}, {packages: [pkg({name: 'docker-ce'})]}), null);
out.dockerElsewhere = sysMachineHtml(machine({runs_here: false}, {packages: [pkg({name: 'docker-ce'})]}), null);
out.dockerUnknown = sysMachineHtml(machine({runs_here: null}, {packages: [pkg({name: 'docker-ce'})]}), null);
out.dockerNas = sysMachineHtml(machine({kind: 'dsm'}, {os: 'DSM 7', kernel: '', packages: [pkg({name: 'docker-ce'})]}), null);
out.confirms = [];
globalThis.showConfirm = async (m) => { out.confirms.push(m); return false; };
(async () => {
  allSystem = [machine({}, {packages: [pkg({name: 'docker-ce'}), pkg({name: 'curl'}),
    pkg({name: 'containerd.io', held_back: true})]})];
  await sysUpdate('dock', 'docker-ce');
  await sysUpdate('dock', 'curl');
  await sysUpdateAll('dock', 2);
  allSystem = [machine({}, {packages: [pkg({name: 'curl'}), pkg({name: 'runc', held_back: true})]})];
  await sysUpdateAll('dock', 1);
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SystemUpdatesUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = HARNESS % {
            "catalog": json.dumps(CATALOG["en"]),
            "helpers": "\n".join([
                _extract(r"  function timeAgo\(iso\) \{.*?\n  \}\n"),
                _extract(r"  function esc\(s\) \{.*?\n  \}\n"),
            ]),
            "block": (_extract(r"// <system-updates>.*?// </system-updates>") + "\n"
              + _extract(r"  function sysMachine\(id\) \{[^\n]*\n") + "\n"
              + _extract(r"  async function sysUpdate\(id, pkg\) \{.*?\n  \}\n") + "\n"
              + _extract(r"  async function sysUpdateAll\(id, n\) \{.*?\n  \}\n") + "\n"
              + "async function sysStart() {}"),
            "tabcounts": _extract(r"  function updateTabCounts\(containers, hosts\) \{.*?\n  \}\n"),
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

    def test_no_machines_says_how_to_add_one(self):
        self.assertIn("No machines added yet", self.out["empty"])
        self.assertIn("images-note", self.out["empty"])

    def test_a_machine_with_nothing_pending_says_so(self):
        html = self.out["upToDate"]
        self.assertIn("Everything is up to date", html)
        self.assertNotIn("Update all", html)
        self.assertIn("Ubuntu 26.04 · kernel 7.0.0-44", html)
        self.assertIn("Checked 2h ago", html)

    def test_each_package_shows_versions_and_its_own_buttons(self):
        html = self.out["pending"]
        self.assertIn("python3-oauthlib", html)
        self.assertIn("2026e → 2026f", html)
        self.assertIn("sysUpdate('dock','python3-oauthlib')", html)
        self.assertIn("sysChangelog('dock','python3-oauthlib')", html)

    def test_security_and_reboot_hints_are_badged(self):
        html = self.out["pending"]
        self.assertEqual(html.count('badge-security">Security<'), 1, "one Security label per such package")
        self.assertIn("May need reboot", html)

    def test_a_held_back_package_cannot_be_installed_from_here(self):
        html = self.out["pending"]
        self.assertIn("Held back", html)
        row = re.search(r"<div class=\"container-card.*?libheld.*?</div>\s*</div>\s*</div>", html, re.S)
        assert row
        self.assertRegex(row.group(0), r"<button class=\"btn-primary btn-sm\" disabled")

    def test_a_held_back_package_says_why_in_plain_view(self):
        html = self.out["pending"]
        self.assertEqual(html.count("Held back by apt."), 1, "only the held package gets the note")
        self.assertIn("rolling this update out in stages", html)
        self.assertIn("sys-held-why", html)

    def test_updating_docker_itself_warns_that_docker_restarts(self):
        html = self.out["docker"]
        self.assertEqual(html.count("Updating this restarts Docker"), 1, "only the installable Docker package")
        row = re.search(r"<div class=\"container-card.*?docker-ce.*?sys-docker-note.*?</div>", html, re.S)
        self.assertIsNotNone(row, "the note belongs under docker-ce")
        self.assertNotIn("sys-docker-note", self.out["dockerNas"], "read-only machines are not asked to restart anything")

    def test_the_warning_is_exact_only_where_docker_updater_runs(self):
        here = self.out["dockerHere"]
        self.assertIn("docker-updater runs on this machine", here)
        self.assertNotIn("If docker-updater runs there", here)
        self.assertNotIn("sys-docker-note", self.out["dockerElsewhere"],
                         "another machine's Docker update does not affect docker-updater")
        self.assertIn("If docker-updater runs there", self.out["dockerUnknown"],
                      "when it cannot be told, warn cautiously")

    def test_the_confirmation_repeats_the_warning_only_when_docker_is_involved(self):
        one_docker, one_plain, all_with_docker, all_plain = self.out["confirms"]
        self.assertIn("restarts Docker", one_docker)
        self.assertIn("docker restart docker-updater", one_docker)
        self.assertNotIn("restarts Docker", one_plain)
        self.assertIn("restarts Docker", all_with_docker)
        self.assertNotIn("restarts Docker", all_plain, "a held-back Docker package is not installed, so no warning")

    def test_update_all_counts_only_what_can_be_installed(self):
        self.assertIn("Update all (2)", self.out["pending"])
        self.assertIn("sysUpdateAll('dock', 2)", self.out["pending"])

    def test_while_updating_the_buttons_are_off_and_the_log_shows(self):
        html = self.out["updating"]
        self.assertIn("Updating…", html)
        self.assertIn("Setting up tzdata", html)
        self.assertIn("✔ finished", html)
        self.assertIn("sys-log", html)
        for btn in re.findall(r"<button[^>]*>", html):
            if "sysUpdate" in btn or "sysUpdateAll" in btn:
                self.assertIn("disabled", btn)

    def test_reboot_banner_names_why_and_is_disabled_mid_update(self):
        self.assertIn("Reboot required", self.out["reboot"])
        self.assertIn("linux-image-7.0.0-45", self.out["reboot"])
        self.assertIn("sysReboot('dock')", self.out["reboot"])
        self.assertNotRegex(self.out["reboot"], r"sysReboot\('dock'\)[^>]*disabled|disabled[^>]*sysReboot")
        busy = re.search(r"<button[^>]*sysReboot[^>]*>", self.out["rebootBusy"])
        assert busy
        self.assertIn("disabled", busy.group(0))
        self.assertNotIn("Reboot required", self.out["upToDate"])

    def test_services_that_need_a_restart_are_listed(self):
        self.assertIn("cron.service, ssh.service", self.out["restart"])

    def test_an_unreachable_machine_shows_the_reason(self):
        self.assertIn("Can't reach this machine: Permission denied (publickey).", self.out["offline"])
        self.assertNotIn("Everything is up to date", self.out["offline"])

    def test_a_rebooting_machine_is_not_shown_as_a_fault(self):
        self.assertIn("Rebooting…", self.out["rebooting"])
        self.assertNotIn("Can't reach", self.out["rebooting"])

    def test_it_says_whether_the_machine_updates_itself(self):
        self.assertIn("Security fixes also install on their own", self.out["upToDate"])
        self.assertIn("Automatic updates are off", self.out["unattendedOff"])

    def test_names_and_errors_cannot_inject_markup(self):
        html = self.out["hostile"]
        self.assertNotIn("<img", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("&lt;img", html)
        self.assertIn("&lt;script&gt;alert(2)", html)

    def test_quotes_in_names_cannot_break_out_of_the_onclick_string(self):
        # The browser decodes the attribute BEFORE parsing the script, so an apostrophe must be
        # \u0027 inside the JS string, never a raw ' or &#39;.
        html = self.out["quotes"]
        for m in re.finditer(r'onclick="([^"]*)"', html):
            handler = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
            args = re.sub(r"\\.", "", handler)           # drop escaped characters
            self.assertEqual(args.count("'") % 2, 0, handler)
            self.assertNotIn("alert(1)", re.sub(r"'[^']*'", "''", args), handler)
        self.assertIn("\u0027", html)

    def test_the_update_output_can_be_hidden_once_the_run_is_over(self):
        done = self.out["finished"]
        self.assertIn("sysHideLog('dock')", done)
        hide = re.search(r'<button[^>]*sysHideLog[^>]*>', done).group(0)
        self.assertNotIn("disabled", hide)
        busy = re.search(r'<button[^>]*sysHideLog[^>]*>', self.out["updating"]).group(0)
        self.assertIn("disabled", busy, "cannot hide the output of a run that is still going")
        self.assertNotIn("sysHideLog", self.out["upToDate"], "no output, nothing to hide")

    def test_a_synology_card_is_read_only(self):
        html = self.out["nas"]
        self.assertIn("DSM 7.3.2-86009 Update 6", html)
        self.assertNotIn("kernel", html, "a NAS has no apt kernel line")
        self.assertIn("Update in DSM", html)
        self.assertEqual(html.count("Update in DSM"), 2, "one per row")
        for gone in ("sysUpdate(", "sysChangelog(", "sysUpdateAll(", "sysReboot(", "Update all",
                     "None of these updates", "reboot afterwards", "unattended"):
            self.assertNotIn(gone, html, gone)
        self.assertIn("Read-only", html)
        self.assertIn("Git Server", html)
        self.assertEqual(html.count('badge-security">Security<'), 1, "one Security label per such package")

    def test_a_stale_synology_file_is_called_out_and_a_fresh_one_is_not(self):
        self.assertIn("has not refreshed its status", self.out["nasStale"])
        self.assertNotIn("has not refreshed", self.out["nasClean"])
        self.assertNotIn("Read-only", self.out["nasOffline"], "no read-only note while the NAS is unreachable")
        self.assertIn("sys-banner bad", self.out["nasOffline"])

    def test_packages_that_cannot_be_checked_are_called_out(self):
        html = self.out["nasUnchecked"]
        self.assertIn("2 installed packages are not in any package source", html)
        self.assertIn('title="Vim, Mono"', html)
        self.assertNotIn("not in any package source", self.out["nasClean"])

    def test_a_windows_card_is_read_only_and_says_where_to_update(self):
        html = self.out["win"]
        self.assertIn("Microsoft Windows 11 Pro 10.0.26300.1", html)
        self.assertNotIn("kernel", html)
        self.assertIn("Update in Windows Update", html)
        for gone in ("sysUpdate(", "sysChangelog(", "sysUpdateAll(", "sysReboot(", "Update all", "unattended",
                     "None of these updates", "reboot afterwards"):
            self.assertNotIn(gone, html, gone)
        self.assertIn("Windows Update itself", html, "the read-only note names Windows Update")
        self.assertIn("Defender definition updates are left out", html)
        self.assertEqual(html.count('badge-security">Security<'), 1, "one Security label per such package")

    def test_a_windows_update_row_shows_its_kb_not_a_blank_arrow(self):
        html = self.out["win"]
        self.assertIn(">KB5001<", html)
        self.assertNotIn("→ KB5001", html, "no installed version to show before the arrow")

    def test_a_pending_windows_reboot_is_reported_without_a_reboot_button(self):
        html = self.out["winReboot"]
        self.assertIn("Reboot required", html)
        self.assertIn("Because of: Windows Update", html)
        self.assertNotIn("sysReboot(", html)
        self.assertNotIn("Reboot…", html)
        self.assertIn("sysReboot('dock')", self.out["aptReboot"], "an apt machine still has its Reboot button")

    def test_a_stale_windows_file_is_called_out_with_the_right_wording(self):
        self.assertIn("scheduled task is still running", self.out["winStale"])
        self.assertNotIn("Task Scheduler job", self.out["winStale"], "that wording is for the NAS")
        self.assertNotIn("has not refreshed", self.out["nasReboot"])

    def test_the_no_login_banner_has_a_hide_button_and_says_when_hiding_is_fine(self):
        html = self.out["warnShown"]
        self.assertIn('id="sys-open-warning"', html)
        self.assertIn("sysDismissOpenWarning()", html)
        self.assertIn("Hide this notice", html)
        for fine in ("Cloudflare Access", "network only you use", "Otherwise set a login"):
            self.assertIn(fine, html, fine)
        self.assertIn("Linux machines", html, "only the Linux machines can be changed from the tab")

    def test_once_hidden_the_banner_stays_gone_but_the_rest_of_the_tab_is_untouched(self):
        self.assertTrue(self.out["dismissedFlag"])
        for key in ("warnDismissed", "warnDismissedWithMachine"):
            html = self.out[key]
            self.assertNotIn("sys-open-warning", html, key)
            self.assertNotIn("sysDismissOpenWarning", html, key)
            self.assertIn("images-note", html, key)
        self.assertIn("myserver", self.out["warnDismissedWithMachine"])
        self.assertNotIn("sys-open-warning", self.out["warnNoOpen"], "no banner at all when a login is set")

    def test_a_folded_card_keeps_the_name_summary_and_update_all_but_hides_the_detail(self):
        html = self.out["foldedPending"]
        self.assertIn("myserver", html)
        self.assertIn("3 updates", html, "everything listed is counted, held-back included")
        self.assertIn("Update all (2)", html, "while Update all offers only what can be installed")
        self.assertIn('aria-expanded="false"', html)
        self.assertIn("▸", html)
        self.assertIn("Reboot required", html, "a pending reboot is visible even when folded")
        self.assertIn("Update all (2)", html, "the quick action stays")
        for hidden in ("container-card", "sys-banner", "sys-log", "Setting up x", "May need reboot", "unattended"):
            self.assertNotIn(hidden, html, hidden)

    def test_the_summary_says_what_matters(self):
        self.assertIn("badge-security", self.out["foldedPending"].split("</h3>")[1].split("</div>")[0],
                      "any security update turns the summary red")
        self.assertIn("1 update<", self.out["foldedOne"])
        self.assertIn("Up to date", self.out["foldedOk"])
        self.assertIn("badge-ok", self.out["foldedOk"])
        self.assertIn("Offline", self.out["foldedOffline"])
        self.assertNotIn("Up to date", self.out["foldedOffline"])
        self.assertIn("Updating", self.out["foldedBusy"], "progress shows even when folded")
        self.assertNotIn("log line", self.out["foldedBusy"])

    def test_a_folded_synology_has_no_update_all_either(self):
        self.assertNotIn("Update all", self.out["foldedNas"])
        self.assertIn("1 update<", self.out["foldedNas"])

    def test_an_open_card_shows_everything_and_says_it_can_be_folded(self):
        html = self.out["unfolded"]
        self.assertIn('aria-expanded="true"', html)
        self.assertIn("▾", html)
        self.assertIn("container-card", html)
        self.assertIn("log line", html)
        self.assertIn("Fold this machine away", html)

    def test_toggling_flips_the_state_and_the_whole_title_is_the_click_target(self):
        self.assertEqual(self.out["toggled"], [True, False])
        html = self.out["unfolded"]
        title = re.search(r'<div class="sys-title"[^>]*>', html).group(0)
        self.assertIn("sysToggle('dock')", title)
        self.assertIn('role="button"', title)
        self.assertIn('tabindex="0"', title)
        self.assertNotIn("sysToggle", html.split("</div>", 1)[1].split("sys-title")[0], "no second toggle outside the title")
        update_all = re.search(r'<button[^>]*sysUpdateAll[^>]*>', self.out["foldedPending"]).group(0)
        self.assertNotIn("sysToggle", update_all)

    def test_a_hostile_machine_id_cannot_break_out_of_the_toggle_handler(self):
        for m in re.finditer(r'onclick="([^"]*)"', self.out["hostileId"]):
            handler = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
            args = re.sub(r"\\.", "", handler)
            self.assertEqual(args.count("'") % 2, 0, handler)

    def test_the_add_form_hints_are_generic_and_never_a_real_name_or_address(self):
        body = _extract(r"  function sysKindChanged\(\) \{.*?" + chr(10) + r"  \}" + chr(10))
        hints = re.findall(r"'([^']*@[^']*)'", body)
        self.assertEqual(hints, ["user@192.168.1.50", "user@192.168.1.60", "sysupdate@host.docker.internal"])
        self.assertIn("placeholder = '22'", body, "the same default port for every kind")
        for private in ("kaj", "liqui@", "2247", "192.168.1.9", "192.168.1.100", "192.168.1.178"):
            self.assertNotIn(private, body, private)

    def test_the_add_form_offers_all_three_kinds(self):
        html = self.out["form"]
        self.assertIn("Windows (read-only)", html)
        self.assertIn('<option value="windows">', html)

    def test_the_add_form_offers_both_kinds(self):
        html = self.out["form"]
        self.assertIn('id="sys-kind"', html)
        self.assertIn("Synology (DSM, read-only)", html)
        self.assertIn("Linux (apt)", html)

    def test_it_says_whether_installing_will_need_a_reboot(self):
        self.assertIn("None of these updates should need a reboot", self.out["rebootNone"])
        pending = self.out["pending"]                      # holds linux-image-generic (kernel hint)
        self.assertIn("may need a reboot afterwards (linux-image-generic)", pending)
        self.assertNotIn("libheld)", pending)              # held-back packages are not counted
        self.assertNotIn("None of these updates", pending)
        # Nothing to say when nothing is pending, when the reboot is already required, or while offline.
        self.assertNotIn("reboot afterwards", self.out["upToDate"])
        self.assertNotIn("None of these", self.out["upToDate"])
        self.assertNotIn("None of these", self.out["reboot"])
        self.assertNotIn("None of these", self.out["offline"])

    def test_no_login_gets_a_visible_warning_and_a_login_does_not(self):
        self.assertIn("sys-open-warning", self.out["openWarn"])
        self.assertIn("no login", self.out["openWarn"])
        self.assertNotIn("sys-open-warning", self.out["empty"])

    def test_the_panel_leads_with_the_note_and_lists_each_machine(self):
        html = self.out["panel"]
        self.assertTrue(html.startswith('<div class="images-note">'))   # no login warning when auth is on
        self.assertEqual(html.count('class="sys-machine"'), 2)

    def test_the_add_form_offers_test_and_remove_for_existing_machines(self):
        html = self.out["form"]
        self.assertIn("sysAddMachine()", html)
        self.assertIn("sysTest('dock')", html)
        self.assertIn("sysRemove('dock','myserver')", html)

    def test_pending_count_sums_across_machines(self):
        self.assertEqual(self.out["count"], [0, 0, 2])

    def test_the_tab_exists_only_while_the_option_is_on(self):
        tabs = self.out["tabs"]
        self.assertEqual(tabs["off"]["display"], "none")
        self.assertEqual(tabs["offViewing"]["switched"], ["update"], "leaves the tab if it vanishes")
        self.assertEqual(tabs["on"]["display"], "")
        self.assertEqual(tabs["on"]["count"], "2")
        self.assertEqual(tabs["onEmpty"]["display"], "")
        self.assertEqual(tabs["onViewing"]["switched"], [])


class SystemUpdatesCatalogTests(unittest.TestCase):
    def test_every_key_the_page_uses_exists_in_both_languages(self):
        block = re.search(r"// <system-updates>.*?// </system-updates>", INDEX, re.S)
        assert block
        used = set(re.findall(r"\bt\('([a-z0-9_.]+)'", block.group(0)))
        # actions below the block, and the settings card
        used |= set(re.findall(r"\bt\('((?:sys|confirm\.sys|alert\.sys|modal\.sys|btn\.sys|settings\.system)[a-z0-9_.]*)'", INDEX))
        used |= {"tab.system"}
        for lang in ("en", "zh-CN"):
            missing = sorted(k for k in used if k not in CATALOG[lang])
            self.assertEqual(missing, [], lang)

    def test_notification_keys_exist_in_both_languages(self):
        for lang in ("en", "zh-CN"):
            for key in ("notify.sys_title", "notify.sys_title_plural", "notify.sys_reboot_title",
                        "notify.sys_security", "notify.sys_reboot"):
                self.assertIn(key, CATALOG[lang], (lang, key))

    def test_placeholders_match_between_languages(self):
        ph = lambda s: sorted(re.findall(r"\{(\w+)\}", s))
        for key, en in CATALOG["en"].items():
            if "sys" in key or "system" in key:
                self.assertEqual(ph(en), ph(CATALOG["zh-CN"][key]), key)



BANNER_HARNESS = """
const EN = %(catalog)s;
function t(key) { return EN[key] ?? key; }
function el() { const classes = new Set(); return {textContent: '', disabled: false,
  classList: {toggle: (c, on) => on ? classes.add(c) : classes.delete(c), has: c => classes.has(c)}}; }
const els = {'checking-banner': el(), 'sys-checking-banner': el(), 'check-btn': el()};
globalThis.document = {getElementById: id => els[id]};
let currentTab = 'update', allSystem = null, checkRunning = false, sysCheckRunning = false;
%(fn)s
function state(tab, sys, c, sc) {
  currentTab = tab; allSystem = sys; checkRunning = c; sysCheckRunning = sc; applyCheckBanners();
  return {dock: els['checking-banner'].classList.has('visible'), sysb: els['sys-checking-banner'].classList.has('visible'),
          label: els['check-btn'].textContent, disabled: els['check-btn'].disabled};
}
console.log(JSON.stringify({
  off:         state('update', null, true, false),
  dockerTab:   state('update', [], true, true),
  systemTab:   state('system', [], true, true),
  sysIdle:     state('system', [], true, false),
  dockIdle:    state('update', [], false, true),
  hostsTab:    state('hosts', [], false, false),
}));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CheckBannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = BANNER_HARNESS % {"catalog": json.dumps(CATALOG["en"]),
                                   "fn": _extract(r"  function applyCheckBanners\(\) \{.*?" + chr(10) + r"  \}" + chr(10))}
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(script)
            path = f.name
        try:
            r = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8", timeout=30)
        finally:
            Path(path).unlink(missing_ok=True)
        assert r.returncode == 0, r.stderr
        cls.o = json.loads(r.stdout)

    def test_with_the_option_off_it_is_the_plain_button_and_container_banner(self):
        self.assertEqual(self.o["off"], {"dock": True, "sysb": False, "label": "Check Now", "disabled": True})

    def test_container_tabs_show_the_container_banner_and_button(self):
        o = self.o["dockerTab"]
        self.assertEqual((o["dock"], o["sysb"], o["label"]), (True, False, "Check Docker Now"))

    def test_the_system_tab_shows_the_system_banner_and_button(self):
        o = self.o["systemTab"]
        self.assertEqual((o["dock"], o["sysb"], o["label"]), (False, True, "Check System Now"))

    def test_each_button_is_busy_only_for_its_own_check(self):
        self.assertTrue(self.o["systemTab"]["disabled"])
        self.assertFalse(self.o["sysIdle"]["disabled"], "a container scan must not lock the System button")
        self.assertFalse(self.o["dockIdle"]["disabled"], "a system check must not lock the Docker button")
        self.assertEqual(self.o["hostsTab"]["label"], "Check Docker Now")


class StatusPollingTests(unittest.TestCase):
    """The page must keep polling after the server answers with an error."""

    def test_an_error_answer_schedules_another_poll(self):
        fn = _extract(r"  async function fetchStatus\(\) \{.*?\n  \}\n")
        script = """
const timers = [];
let refreshTimer = null;
const setTimeout = (f, ms) => { timers.push(ms); return timers.length; };
const clearTimeout = () => {};
const fetch = async () => ({ json: async () => ({ error: 'docker unreachable' }) });
%s
(async () => { await fetchStatus(); console.log(JSON.stringify(timers)); })();
""" % fn
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(script)
            path = f.name
        try:
            r = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8", timeout=30)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [10000], "an error answer must retry, not stop polling")


class BetaBadgeTests(unittest.TestCase):
    def test_the_system_setting_is_marked_beta_in_both_languages(self):
        self.assertIn("badge-beta", INDEX)
        for lang in ("en", "zh-CN"):
            self.assertTrue(CATALOG[lang]["settings.beta"].strip())
            self.assertTrue(CATALOG[lang]["settings.beta_title"].strip())


if __name__ == "__main__":
    unittest.main()
