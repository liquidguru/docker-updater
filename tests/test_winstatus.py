"""Windows support in the System tab: the machine-side script (contrib/dock-winstatus.ps1) and how the
app turns its file into "what is waiting". Read-only by design - nothing here may install or reboot."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_sysupdate import SysBase

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "contrib" / "dock-winstatus.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def win_file(updates=(), reboot=False, reasons=(), **kw):
    return {"schema": 1, "kind": "windows", "time": int(time.time()), "hostname": "MYPC",
            "os": "Microsoft Windows 11 Pro 10.0.26300.1234", "reboot_required": reboot,
            "reboot_reasons": list(reasons), "updates": list(updates), **kw}


def upd(title="2026-11 Cumulative Update for Windows 11 (KB5001234)", kb="KB5001234", security=True, reboot_hint=True):
    return {"title": title, "kb": kb, "categories": ["Security Updates"], "security": security,
            "reboot_hint": reboot_hint, "size_mb": 400, "downloaded": False}


class ScriptTests(unittest.TestCase):
    def test_it_only_searches_and_never_changes_anything(self):
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(".Search(", src)
        for forbidden in ("CreateUpdateDownloader", "CreateUpdateInstaller", "Download(", "Install(",
                          "Restart-Computer", "Stop-Computer", "shutdown", "Install-WindowsUpdate",
                          "Hide-WindowsUpdate", "Uninstall", "Set-ItemProperty", "Remove-Item", "Set-Service"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_defender_definitions_are_left_out_unless_asked_for(self):
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Definition Updates", src)
        self.assertIn("-not $Definitions", src)
        self.assertIn("IncludeDefinitions", src)

    @unittest.skipUnless(POWERSHELL, "PowerShell is not installed")
    def test_a_run_writes_valid_json_without_a_bom_and_without_contacting_windows_update(self):
        if os.name != "nt":
            self.skipTest("the script reads the Windows registry and WMI")
        assert POWERSHELL is not None
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "sub", "status.json")          # the folder does not exist yet
            r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                                "-Out", out, "-SkipSearch"], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            raw = open(out, "rb").read()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "a BOM would break json.loads")
            data = json.loads(raw)
            self.assertEqual((data["schema"], data["kind"]), (1, "windows"))
            self.assertEqual(data["updates"], [])
            self.assertIsInstance(data["reboot_reasons"], list)
            self.assertIsInstance(data["reboot_required"], bool)
            self.assertTrue(data["hostname"] and data["os"])
            self.assertEqual(os.listdir(os.path.dirname(out)), ["status.json"], "no temp file left behind")


ENTRY = ROOT / "contrib" / "dock-winstatus-ssh.ps1"


class EntryScriptTests(unittest.TestCase):
    """The forced command for the Windows key: print the file, and on `refresh` run the one fixed task."""

    def test_nothing_from_the_connection_is_ever_run_or_used_as_a_path(self):
        text = ENTRY.read_text(encoding="utf-8")
        src = re.sub(r"<#.*?#>", "", text, flags=re.S)                 # the code, not the explanation
        src = chr(10).join(line.split("  # ")[0] for line in src.splitlines() if not line.strip().startswith("#"))
        self.assertEqual(src.count("SSH_ORIGINAL_COMMAND"), 1, "the connection's text is read in exactly one place")
        for forbidden in ("Invoke-Expression", "iex ", "Invoke-Command", "Start-Process", "cmd /c", "& $", "&$",
                          "Restart-Computer", "Stop-Computer", "Install", "Remove-Item", "Set-Item", "Out-File",
                          "Set-Content", "Add-Content", "WriteAllText"):
            self.assertNotIn(forbidden, src, forbidden)
        self.assertEqual(src.count("Start-ScheduledTask"), 1, "one place starts the one fixed task")
        self.assertIn("-TaskName $TaskName", src)
        self.assertRegex(src, r"\$verb -eq 'refresh'")

    def run_entry(self, verb, status_file, task="No such task for the test"):
        assert POWERSHELL is not None
        env = dict(os.environ, SSH_ORIGINAL_COMMAND=verb)
        return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ENTRY),
                               "-StatusFile", status_file, "-TaskName", task, "-TimeoutSeconds", "5"],
                              capture_output=True, text=True, timeout=60, env=env)

    @unittest.skipUnless(POWERSHELL and os.name == "nt", "needs Windows PowerShell")
    def test_every_word_prints_only_the_file_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "status.json")
            payload = json.dumps(win_file([upd()]), separators=(",", ":"))
            with open(f, "w", encoding="utf-8", newline="") as fh:
                fh.write(payload)
            for verb in ("status", "refresh", "", "rm -rf /", "refresh; calc.exe", "status && whoami"):
                r = self.run_entry(verb, f)
                self.assertEqual(r.returncode, 0, (verb, r.stderr))
                self.assertEqual(r.stdout, payload, "stdout is exactly the file for " + repr(verb))
            self.assertNotIn("Calculator", r.stdout)

    @unittest.skipUnless(POWERSHELL and os.name == "nt", "needs Windows PowerShell")
    def test_a_missing_file_is_a_failure_not_an_empty_success(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_entry("status", os.path.join(d, "nope.json"))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")


class WindowsComparisonTests(SysBase):
    def norm(self, **kw):
        return self.mod._sys_parse_status(json.dumps(win_file(**kw)))

    def test_each_update_becomes_a_row_with_its_kb(self):
        info = self.norm(updates=[upd(), upd("Some .NET update (KB5009999)", "KB5009999", security=False, reboot_hint=False)])
        self.assertEqual([p["to"] for p in info["packages"]], ["KB5001234", "KB5009999"])
        self.assertEqual([p["from"] for p in info["packages"]], ["", ""])
        self.assertTrue(info["packages"][0]["security"])
        self.assertFalse(info["packages"][1]["security"])
        self.assertTrue(info["packages"][0]["reboot_hint"])

    def test_nothing_waiting_is_an_empty_list(self):
        self.assertEqual(self.norm()["packages"], [])

    def test_it_is_read_only_and_a_pending_reboot_is_carried_through(self):
        info = self.norm(reboot=True, reasons=["Windows Update"])
        self.assertTrue(info["read_only"])
        self.assertEqual(info["kind"], "windows")
        self.assertTrue(info["reboot_required"])
        self.assertEqual(info["reboot_reasons"], ["Windows Update"])

    def test_an_update_with_no_kb_still_gets_a_usable_identity(self):
        info = self.norm(updates=[{"title": "Odd update", "kb": ""}])
        self.assertEqual((info["packages"][0]["name"], info["packages"][0]["to"]), ("Odd update", "Odd update"))

    def test_an_old_file_is_marked_stale(self):
        self.assertFalse(self.norm()["stale"])
        self.assertTrue(self.norm(time=int(time.time()) - 13 * 3600)["stale"])

    def test_an_error_file_or_a_wrong_shape_is_an_error_not_a_pass(self):
        with self.assertRaises(ValueError):
            self.mod._sys_parse_status(json.dumps({"schema": 1, "kind": "windows", "error": "search failed"}))
        with self.assertRaises(ValueError):
            self.mod._sys_parse_status(json.dumps({"schema": 1, "kind": "windows", "updates": "x"}))

    def test_a_new_update_or_a_new_reboot_need_changes_the_fingerprint(self):
        quiet = self.mod._sys_fingerprint(self.norm())
        self.assertEqual(quiet, "")
        self.assertNotEqual(quiet, self.mod._sys_fingerprint(self.norm(updates=[upd()])))
        self.assertNotEqual(quiet, self.mod._sys_fingerprint(self.norm(reboot=True, reasons=["Windows Update"])))

    def test_the_other_kinds_are_unaffected(self):
        from tests.test_sysupdate import status_json, pkg
        info = self.mod._sys_parse_status(status_json([pkg("tzdata")]))
        self.assertNotIn("read_only", info)
        self.assertEqual([p["name"] for p in info["packages"]], ["tzdata"])


class WindowsEndpointTests(SysBase):
    def add_win(self):
        r = self.client.post("/api/sysupdate/targets", json={"name": "mypc", "kind": "windows",
                                                              "address": "admin@192.168.1.60", "port": 22})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def test_kind_is_stored_and_listed(self):
        tid = self.add_win()
        t = [x for x in self.mod.load_sys_targets() if x["id"] == tid][0]
        self.assertEqual(t["kind"], "windows")
        self.assertEqual(self.client.get("/api/sysupdate/targets").get_json()["targets"][0]["kind"], "windows")

    def test_an_unknown_kind_is_still_refused(self):
        r = self.client.post("/api/sysupdate/targets", json={"name": "x", "address": "u@h", "kind": "macos"})
        self.assertEqual(r.status_code, 400)

    def test_nothing_can_be_installed_changelogged_or_rebooted_on_windows(self):
        tid = self.add_win()
        with mock.patch.object(self.mod, "_sys_run") as run, mock.patch.object(self.mod.threading, "Thread") as th:
            self.assertEqual(self.client.post(f"/api/sysupdate/{tid}/upgrade",
                                              json={"packages": "all"}).status_code, 400)
            self.assertEqual(self.client.get(f"/api/sysupdate/{tid}/changelog/kb5001234").status_code, 400)
            self.assertEqual(self.client.post(f"/api/sysupdate/{tid}/reboot",
                                              json={"confirm": True}).status_code, 400)
        run.assert_not_called()
        th.assert_not_called()

    def test_a_check_asks_for_a_refresh_and_a_plain_read_asks_for_status(self):
        tid = self.add_win()
        target = [x for x in self.mod.load_sys_targets() if x["id"] == tid][0]
        with mock.patch.object(self.mod, "_sys_run", return_value=(0, json.dumps(win_file([upd()])), "")) as run:
            st = self.mod.check_system_target(target)
            self.assertEqual(run.call_args.args[1], ["refresh"], "Check System Now runs the task on the machine")
            self.mod.check_system_target(target, refresh=False)
            self.assertEqual(run.call_args.args[1], ["status"])
        self.assertEqual(st["status"], "online")
        self.assertEqual(len(st["info"]["packages"]), 1)

    def test_a_synology_still_only_ever_gets_status(self):
        self.client.post("/api/sysupdate/targets", json={"name": "nas", "kind": "dsm", "address": "k@h", "port": 2222})
        target = self.mod.load_sys_targets()[0]
        from tests.test_dsmstatus import nas_file
        with mock.patch.object(self.mod, "_sys_run", return_value=(0, json.dumps(nas_file()), "")) as run:
            self.mod.check_system_target(target)
        self.assertEqual(run.call_args.args[1], ["status"])

    def test_the_notification_names_the_update(self):
        info = self.mod._sys_parse_status(json.dumps(win_file([upd()], reboot=True, reasons=["Windows Update"])))
        title, body = self.mod._sys_notification({"id": "b", "name": "mypc"}, info)
        self.assertIn("mypc", title)
        self.assertIn("KB5001234", body + title + "".join(p["name"] for p in info["packages"]))


if __name__ == "__main__":
    unittest.main()
