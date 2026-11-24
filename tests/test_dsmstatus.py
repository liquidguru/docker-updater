"""Synology (DSM) support in the System tab: the NAS-side script (contrib/dock-dsmstatus) and how the
app turns its file into "what is out of date". Read-only by design - nothing here may install."""

import importlib.machinery
import importlib.util
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_sysupdate import SysBase, status_json, pkg

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "contrib" / "dock-dsmstatus"


def load_script():
    loader = importlib.machinery.SourceFileLoader("dock_dsmstatus", str(SCRIPT))
    spec = importlib.util.spec_from_loader("dock_dsmstatus", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


HEADER = ("[Line 295] Exec WebAPI:  api=SYNO.Core.Upgrade.Server, version=1, method=check, param={}, "
          "runner=SYSTEM_ADMIN\n")
VERSION = {"productversion": "7.3.2", "buildnumber": "86009", "smallfixnumber": "6"}


def nas_file(installed=(), server=(), dsm=None, **kw):
    return {"schema": 1, "kind": "dsm", "time": int(time.time()), "hostname": "mynas",
            "os": "DSM 7.3.2-86009 Update 6", "dsm": dsm if dsm is not None else {"available": False},
            "installed": [{"id": i, "name": n, "version": v} for i, n, v in installed],
            "server": [{"id": i, "dname": i, "version": v, "is_security_version": s} for i, v, s in server],
            **kw}


class NasScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = load_script()

    def run_call(self, stdout):
        with mock.patch.object(self.s.subprocess, "run",
                               return_value=mock.Mock(stdout=stdout, stderr="", returncode=0)):
            return self.s.call("SYNO.Core.Upgrade.Server", "check", 1)

    def test_the_json_is_found_after_a_header_that_itself_contains_braces(self):
        reply = {"data": {"available": False}, "httpd_restart": False, "success": True}
        self.assertEqual(self.run_call(HEADER + json.dumps(reply, indent=3)), {"available": False})

    def test_a_refusal_or_garbage_is_an_error_not_silence(self):
        with self.assertRaises(RuntimeError):
            self.run_call(HEADER + json.dumps({"success": False, "error": {"code": 105}}))
        with self.assertRaises(RuntimeError):
            self.run_call("nothing like json")

    def test_it_only_ever_asks_the_three_read_only_questions(self):
        src = SCRIPT.read_text(encoding="utf-8")
        asked = sorted(set(re.findall(r'call\("(SYNO[\w.]+)", "(\w+)"', src)))
        self.assertEqual(asked, [("SYNO.Core.Package", "list"), ("SYNO.Core.Package.Server", "list"),
                                 ("SYNO.Core.Upgrade.Server", "check")])
        for forbidden in ("synopkg", "upgradeall", "start-force", "uninstall", "--autoupdate", "method=install"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_the_label_matches_what_dsm_shows(self):
        self.assertEqual(self.s.os_label(VERSION), "DSM 7.3.2-86009 Update 6")
        self.assertEqual(self.s.os_label({"productversion": "7.2", "buildnumber": "1", "smallfixnumber": "0"}),
                         "DSM 7.2-1")

    def test_read_version_parses_the_dsm_version_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write('majorversion="7"\nproductversion="7.3.2"\nbuildnumber="86009"\nsmallfixnumber="6"\n')
        try:
            v = self.s.read_version(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(self.s.os_label(v), "DSM 7.3.2-86009 Update 6")

    def test_only_the_needed_fields_are_kept(self):
        installed = {"packages": [{"id": "Git", "name": "Git Server", "version": "2.1", "additional": {"x": 1}}]}
        server = {"banners": [{"huge": "x" * 1000}], "packages": [
            {"id": "Git", "dname": "Git Server", "version": "2.2", "is_security_version": True, "desc": "long text"},
            {"id": "NoVersion"}]}
        out = self.s.build_status({"available": False}, installed, server, VERSION, "nas", 1000.9)
        self.assertEqual(out["installed"], [{"id": "Git", "name": "Git Server", "version": "2.1"}])
        self.assertEqual(out["server"], [{"id": "Git", "dname": "Git Server", "version": "2.2",
                                          "is_security_version": True, "beta": False}])
        self.assertEqual((out["schema"], out["kind"], out["time"], out["hostname"]), (1, "dsm", 1000, "nas"))
        self.assertLess(len(json.dumps(out)), 600, "the 350 KB catalogue is trimmed to what is needed")

    def test_the_file_is_written_atomically_and_world_readable(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".dock-dsmstatus.json")
            self.s.write_atomic(path, {"a": 1})
            self.s.write_atomic(path, {"a": 2})
            with open(path) as f:
                self.assertEqual(json.load(f), {"a": 2})
            self.assertEqual(os.listdir(d), [".dock-dsmstatus.json"], "no temp file left behind")
            if os.name == "posix":
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)

    def test_a_failure_leaves_an_honest_error_file_and_a_failing_exit_code(self):
        written = {}

        def fake_write(path, payload, owner=None):
            written["payload"] = payload

        with mock.patch.object(self.s, "call", side_effect=RuntimeError("boom")), \
             mock.patch.object(self.s, "write_atomic", side_effect=fake_write):
            code = self.s.main(["--owner", "admin", "--out", "/x/s.json"])
        self.assertEqual(code, 1)
        self.assertEqual(written["payload"]["error"], "boom")
        self.assertNotIn("installed", written["payload"])

    def test_third_party_sources_are_loaded_so_their_packages_can_be_compared(self):
        # Regression: with blloadothers=false the FFmpeg/SynoCommunity packages had no server entry and
        # silently looked up to date while Package Center showed two updates.
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('("false", "true")', src, "both the official and the third-party catalogue are asked for")

    def test_the_official_and_the_third_party_replies_are_merged_not_one_instead_of_the_other(self):
        # Regression: DSM returns ONLY the third-party catalogue for blloadothers=true, so using that
        # alone dropped every official package (42 of them showed as "cannot be checked").
        official = {"packages": [{"id": "Git", "version": "2", "dname": "Git"}]}
        others = {"packages": [{"id": "ffmpeg", "version": "4.4.8-53"}, {"id": "Git", "version": "2"}]}
        merged = self.s.package_lists([official, others])
        self.assertEqual([(p["id"], p["version"]) for p in merged], [("Git", "2"), ("ffmpeg", "4.4.8-53")])
        out = self.s.build_status({}, {"packages": []}, [official, others], VERSION, "n", 1)
        self.assertEqual(sorted(p["id"] for p in out["server"]), ["Git", "ffmpeg"])

    def test_a_beta_beside_the_stable_release_keeps_both_and_the_beta_flag(self):
        # Regression: Universal Viewer 1.5.0-0854 (beta) was listed before 1.4.0-0712 (stable); keeping only
        # the first entry per id made a beta look like an update Package Center never offered.
        reply = {"packages": [{"id": "UniversalViewer", "version": "1.5.0-0854", "beta": True},
                              {"id": "UniversalViewer", "version": "1.4.0-0712", "beta": False}]}
        out = self.s.build_status({}, {"packages": []}, [reply], VERSION, "n", 1)
        self.assertEqual([(p["version"], p["beta"]) for p in out["server"]],
                         [("1.5.0-0854", True), ("1.4.0-0712", False)])

    def test_the_script_asks_dsm_for_both_catalogues(self):
        calls = []

        def fake_call(api, method, version, **params):
            calls.append((method, params.get("blloadothers")))
            if params.get("blloadothers") == "true":
                return {"packages": [{"id": "ffmpeg", "version": "4.4.8-53"}]}
            if api == "SYNO.Core.Package.Server":
                return {"packages": [{"id": "Git", "version": "2"}]}
            return {"packages": []} if method == "list" else {"available": False}

        written = {}
        with mock.patch.object(self.s, "call", side_effect=fake_call),              mock.patch.object(self.s, "read_version", return_value=VERSION),              mock.patch.object(self.s, "write_atomic", side_effect=lambda p, payload, o=None: written.update(payload=payload)):
            self.assertEqual(self.s.main(["--owner", "admin"]), 0)
        self.assertIn(("list", "false"), calls)
        self.assertIn(("list", "true"), calls)
        self.assertEqual(sorted(p["id"] for p in written["payload"]["server"]), ["Git", "ffmpeg"])

    def test_packages_from_every_list_in_the_reply_are_collected(self):
        reply = {"banners": [{"x": 1}], "packages": [{"id": "A", "version": "1"}],
                 "others": [{"id": "B", "version": "2"}], "total": 2, "note": "text"}
        self.assertEqual([p["id"] for p in self.s.package_lists(reply)], ["A", "B"])
        out = self.s.build_status({}, {"packages": []}, reply, VERSION, "n", 1)
        self.assertEqual([p["id"] for p in out["server"]], ["A", "B"])

    def test_a_good_run_writes_the_status_and_exits_cleanly(self):
        written = {}
        replies = {"check": {"available": False}, "list": {"packages": []}}

        def fake_call(api, method, version, **params):
            return replies[method]

        with mock.patch.object(self.s, "call", side_effect=fake_call), \
             mock.patch.object(self.s, "read_version", return_value=VERSION), \
             mock.patch.object(self.s, "write_atomic", side_effect=lambda p, payload, o=None: written.update(p=p, payload=payload, o=o)):
            code = self.s.main(["--owner", "admin"])
        self.assertEqual(code, 0)
        self.assertEqual(written["p"], "/var/services/homes/admin/.dock-dsmstatus.json")
        self.assertEqual(written["o"], "admin")
        self.assertEqual(written["payload"]["os"], "DSM 7.3.2-86009 Update 6")


class DsmComparisonTests(SysBase):
    def norm(self, **kw):
        return self.mod._sys_parse_status(json.dumps(nas_file(**kw)))

    def test_a_package_is_out_of_date_only_when_the_server_version_is_newer(self):
        info = self.norm(installed=[("Git", "Git Server", "2.1.0-0100"), ("Node", "Node.js", "20.9.0-0200"),
                                    ("Old", "Ahead", "3.0-0005"), ("Gone", "Not on server", "1.0-1")],
                         server=[("Git", "2.1.0-0101", False), ("Node", "20.9.0-0200", False),
                                 ("Old", "2.9-0004", False)])
        self.assertEqual([p["name"] for p in info["packages"]], ["Git Server"])
        self.assertEqual((info["packages"][0]["from"], info["packages"][0]["to"]), ("2.1.0-0100", "2.1.0-0101"))

    def test_versions_compare_as_numbers_not_text(self):
        info = self.norm(installed=[("A", "A", "1.9.0-0009")], server=[("A", "1.10.0-0001", False)])
        self.assertEqual(len(info["packages"]), 1, "1.10 is newer than 1.9")

    def test_security_versions_are_flagged(self):
        info = self.norm(installed=[("A", "A", "1-1"), ("B", "B", "1-1")],
                         server=[("A", "1-2", True), ("B", "1-2", False)])
        self.assertEqual([(p["name"], p["security"]) for p in info["packages"]], [("A", True), ("B", False)])

    def test_a_dsm_update_is_listed_first(self):
        info = self.norm(dsm={"available": True, "version": "7.3.3-86100"},
                         installed=[("A", "A", "1-1")], server=[("A", "1-2", False)])
        self.assertEqual([p["name"] for p in info["packages"]], ["DSM", "A"])
        self.assertEqual(info["packages"][0]["to"], "7.3.3-86100")
        self.assertEqual(info["packages"][0]["from"], "DSM 7.3.2-86009 Update 6")

    def test_the_ffmpeg_updates_package_center_showed_are_found(self):
        # The real versions from the 24 Nov Package Center screenshot.
        info = self.norm(installed=[("ffmpeg", "FFmpeg 4", "4.4.8-52"), ("ffmpeg8", "FFmpeg 8", "8.1.3-7"),
                                    ("ffmpeg6", "FFmpeg 6", "6.1.6-9")],
                         server=[("ffmpeg", "4.4.8-53", False), ("ffmpeg8", "8.1.3-8", False),
                                 ("ffmpeg6", "6.1.6-9", False)])
        self.assertEqual([(p["name"], p["from"], p["to"]) for p in info["packages"]],
                         [("FFmpeg 4", "4.4.8-52", "4.4.8-53"), ("FFmpeg 8", "8.1.3-7", "8.1.3-8")])
        self.assertEqual(info["unchecked"], [])

    def test_a_package_no_source_knows_is_reported_as_unchecked_not_as_up_to_date(self):
        info = self.norm(installed=[("vim", "Vim", "9.0-1"), ("Git", "Git", "2-1")], server=[("Git", "2-1", False)])
        self.assertEqual(info["packages"], [])
        self.assertEqual(info["unchecked"], ["Vim"])

    def test_a_beta_release_is_not_an_update_but_a_newer_stable_one_is(self):
        # The real Universal Viewer entries from the 24 Nov probe: beta first, stable second.
        beta_first = [("UniversalViewer", "1.5.0-0854", False, True), ("UniversalViewer", "1.4.0-0712", False, False)]
        info = self._norm_beta([("UniversalViewer", "Universal Viewer", "1.4.0-0712")], beta_first)
        self.assertEqual(info["packages"], [], "Package Center hides betas, so there is no update")
        self.assertEqual(info["unchecked"], [])
        newer = beta_first + [("UniversalViewer", "1.4.1-0720", True, False)]
        info = self._norm_beta([("UniversalViewer", "Universal Viewer", "1.4.0-0712")], newer)
        self.assertEqual([(p["from"], p["to"]) for p in info["packages"]], [("1.4.0-0712", "1.4.1-0720")])

    def test_a_package_with_only_a_beta_on_offer_is_checked_not_unchecked(self):
        info = self._norm_beta([("X", "Xpkg", "1-1")], [("X", "2-1", False, True)])
        self.assertEqual((info["packages"], info["unchecked"]), ([], []))

    def test_an_older_file_without_the_beta_flag_still_works(self):
        info = self.norm(installed=[("A", "A", "1-1")], server=[("A", "1-2", False)])   # no `beta` key at all
        self.assertEqual(len(info["packages"]), 1)

    def _norm_beta(self, installed, server):
        data = nas_file(installed=installed)
        data["server"] = [{"id": i, "dname": i, "version": v, "is_security_version": sec, "beta": beta}
                          for i, v, sec, beta in server]
        return self.mod._sys_parse_status(json.dumps(data))

    def test_dsm_up_to_date_lists_nothing(self):
        self.assertEqual(self.norm()["packages"], [])

    def test_it_looks_like_a_read_only_machine_with_nothing_to_reboot(self):
        info = self.norm()
        self.assertTrue(info["read_only"])
        self.assertFalse(info["reboot_required"])
        self.assertEqual(info["os"], "DSM 7.3.2-86009 Update 6")

    def test_an_old_file_is_marked_stale(self):
        self.assertFalse(self.norm()["stale"])
        self.assertTrue(self.norm(time=int(time.time()) - 13 * 3600)["stale"])

    def test_an_error_file_or_a_wrong_shape_is_an_error_not_a_pass(self):
        with self.assertRaises(ValueError):
            self.mod._sys_parse_status(json.dumps({"schema": 1, "kind": "dsm", "error": "DSM refused"}))
        with self.assertRaises(ValueError):
            self.mod._sys_parse_status(json.dumps({"schema": 1, "kind": "dsm", "installed": "x"}))

    def test_an_apt_machines_reply_is_unaffected(self):
        info = self.mod._sys_parse_status(status_json([pkg("tzdata")]))
        self.assertEqual([p["name"] for p in info["packages"]], ["tzdata"])
        self.assertNotIn("read_only", info)

    def test_a_new_dsm_update_or_package_changes_the_fingerprint(self):
        a = self.mod._sys_fingerprint(self.norm())
        b = self.mod._sys_fingerprint(self.norm(dsm={"available": True, "version": "7.3.3"}))
        self.assertEqual(a, "")
        self.assertNotEqual(a, b)


class DsmEndpointTests(SysBase):
    def add_dsm(self):
        r = self.client.post("/api/sysupdate/targets", json={"name": "mynas", "kind": "dsm",
                                                              "address": "admin@192.168.1.50", "port": 2222})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def test_kind_is_stored_and_listed(self):
        tid = self.add_dsm()
        t = [x for x in self.mod.load_sys_targets() if x["id"] == tid][0]
        self.assertEqual((t["kind"], t["port"]), ("dsm", 2222))
        self.assertEqual(self.client.get("/api/sysupdate/targets").get_json()["targets"][0]["kind"], "dsm")

    def test_an_unknown_kind_is_refused_and_apt_stays_the_default(self):
        r = self.client.post("/api/sysupdate/targets", json={"name": "x", "address": "u@h", "kind": "macos"})
        self.assertEqual(r.status_code, 400)
        self.client.post("/api/sysupdate/targets", json={"name": "y", "address": "u@h"})
        self.assertEqual(self.mod.load_sys_targets()[0]["kind"], "apt")

    def test_nothing_can_be_installed_changelogged_or_rebooted_on_a_synology(self):
        tid = self.add_dsm()
        with mock.patch.object(self.mod, "_sys_run") as run, mock.patch.object(self.mod.threading, "Thread") as th:
            self.assertEqual(self.client.post(f"/api/sysupdate/{tid}/upgrade",
                                              json={"packages": "all"}).status_code, 400)
            self.assertEqual(self.client.post(f"/api/sysupdate/{tid}/upgrade",
                                              json={"packages": ["git"]}).status_code, 400)
            self.assertEqual(self.client.get(f"/api/sysupdate/{tid}/changelog/git").status_code, 400)
            self.assertEqual(self.client.post(f"/api/sysupdate/{tid}/reboot",
                                              json={"confirm": True}).status_code, 400)
        run.assert_not_called()
        th.assert_not_called()
        self.assertNotIn(tid, self.mod._sys_running)

    def test_a_check_reads_the_file_with_status_and_never_sends_refresh(self):
        tid = self.add_dsm()
        target = [x for x in self.mod.load_sys_targets() if x["id"] == tid][0]
        reply = json.dumps(nas_file(installed=[("A", "A", "1-1")], server=[("A", "1-2", True)]))
        with mock.patch.object(self.mod, "_sys_run", return_value=(0, reply, "")) as run:
            st = self.mod.check_system_target(target)
        self.assertEqual(run.call_args.args[1], ["status"])
        self.assertEqual(st["status"], "online")
        self.assertEqual([p["name"] for p in st["info"]["packages"]], ["A"])
        self.assertTrue(self.client.get("/api/sysupdate/targets").get_json()["targets"][0]["info"]["read_only"])

    def test_an_apt_check_still_sends_refresh(self):
        self.client.post("/api/sysupdate/targets", json={"name": "y", "address": "u@h"})
        target = self.mod.load_sys_targets()[0]
        with mock.patch.object(self.mod, "_sys_run", return_value=(0, status_json([]), "")) as run:
            self.mod.check_system_target(target)
        self.assertEqual(run.call_args.args[1], ["refresh"])

    def test_the_notification_for_a_synology_names_its_updates(self):
        info = self.mod._sys_parse_status(json.dumps(nas_file(
            dsm={"available": True, "version": "7.3.3"}, installed=[("A", "Alpha", "1-1")],
            server=[("A", "1-2", True)])))
        title, body = self.mod._sys_notification({"id": "n", "name": "mynas"}, info)
        self.assertIn("mynas", title)
        self.assertIn("DSM", body)
        self.assertIn("Alpha", body)


if __name__ == "__main__":
    unittest.main()
