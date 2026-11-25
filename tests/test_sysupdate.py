"""Optional "System updates" feature: the host-side script (contrib/dock-sysupdate) and the
app's backend for it.

The thing to get right is trust. The app reaches a machine as root-by-proxy, so the script on
the machine must refuse anything off its short list, package names must be validated before
apt sees them, and nothing in the app may be reachable with the read-only API key — least of
all a reboot.
"""

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.app_loader import load_app_module

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    path = str(ROOT / "contrib" / "dock-sysupdate")
    loader = importlib.machinery.SourceFileLoader("dock_sysupdate", path)
    spec = importlib.util.spec_from_loader("dock_sysupdate", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


APT_LIST = """Listing...
python3-oauthlib/resolute-security 3.3.1-1ubuntu0.1 all [upgradable from: 3.3.1-1build1]
tzdata/resolute-updates 2026f-0ubuntu0.26.04.1 all [upgradable from: 2026e-0ubuntu0.26.04.1]
linux-image-generic/resolute-updates,resolute-security 7.0.0-45.45 amd64 [upgradable from: 7.0.0-44.44]
"""


class HostScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = load_script()

    def test_parses_apt_list(self):
        pk = {p["name"]: p for p in self.s.parse_upgradable(APT_LIST)}
        self.assertEqual(set(pk), {"python3-oauthlib", "tzdata", "linux-image-generic"})
        self.assertTrue(pk["python3-oauthlib"]["security"])
        self.assertFalse(pk["tzdata"]["security"])
        self.assertTrue(pk["linux-image-generic"]["security"])
        self.assertEqual(pk["tzdata"]["from"], "2026e-0ubuntu0.26.04.1")
        self.assertEqual(pk["tzdata"]["to"], "2026f-0ubuntu0.26.04.1")

    def test_held_back_is_what_a_simulated_upgrade_would_not_install(self):
        sim = "Inst tzdata [2026e] (2026f resolute-updates [all])\nConf tzdata (2026f resolute-updates [all])\n"
        self.assertEqual(self.s.parse_simulated_installs(sim), {"tzdata"})

    def test_reboot_hint_covers_kernels_not_ordinary_packages(self):
        for name in ("linux-image-generic", "linux-modules-7.0.0-44", "libc6", "systemd", "udev"):
            self.assertTrue(self.s.REBOOT_HINT_RE.match(name), name)
        for name in ("tzdata", "python3-oauthlib", "curl", "libsystemd-shared"):
            self.assertFalse(self.s.REBOOT_HINT_RE.match(name), name)

    def test_needrestart_batch_output(self):
        out = "NEEDRESTART-VER: 3.11\nNEEDRESTART-KSTA: 3\nNEEDRESTART-SVC: cron.service\nNEEDRESTART-SVC: ssh.service\n"
        self.assertEqual(self.s.parse_needrestart(out),
                         {"kernel_pending": True, "services": ["cron.service", "ssh.service"]})
        self.assertFalse(self.s.parse_needrestart("NEEDRESTART-KSTA: 1\n")["kernel_pending"])

    def test_only_the_five_verbs_are_accepted(self):
        ok = [["status"], ["refresh"], ["reboot"], ["changelog", "tzdata"],
              ["upgrade", "all"], ["upgrade", "tzdata", "curl"]]
        for argv in ok:
            self.s.parse_command(argv)
        bad = [[], ["bash"], ["status", "extra"], ["reboot", "now"], ["changelog"],
               ["changelog", "a", "b"], ["upgrade"], ["upgrade", "all", "tzdata"],
               ["rm", "-rf", "/"], ["status;id"], ["STATUS"]]
        for argv in bad:
            with self.assertRaises(ValueError, msg=str(argv)):
                self.s.parse_command(argv)

    def test_package_names_cannot_carry_options_or_shell(self):
        for name in ("-y", "--allow-unauthenticated", "foo;id", "foo bar", "$(id)", "../x", "a|b",
                     "Foo", "", "x" * 120, "tzdata=1.0", "tzdata/stable"):
            self.assertIsNone(self.s.PKG_RE.match(name), name)
        for name in ("tzdata", "libc6", "python3-oauthlib", "g++-14", "libstdc++6", "libfoo:amd64"):
            self.assertIsNotNone(self.s.PKG_RE.match(name), name)
        with self.assertRaises(ValueError):
            self.s.parse_command(["changelog", "--help"])

    def test_only_currently_upgradable_packages_may_be_upgraded(self):
        up = {"tzdata", "curl"}
        self.assertIsNone(self.s.validate_packages(["tzdata"], up))
        self.assertIsNotNone(self.s.validate_packages(["openssh-server"], up))
        self.assertIsNotNone(self.s.validate_packages([], up))
        self.assertIsNotNone(self.s.validate_packages(["tzdata", "-y"], up))
        self.assertIsNotNone(self.s.validate_packages(["tzdata"] * 300, up))


class SysBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def setUp(self):
        mod = self.mod
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        mod.DATA_DIR = self.tmp.name
        mod.SYSUPDATE_FILE = os.path.join(self.tmp.name, "sysupdate_targets.json")
        mod.SYSUPDATE_STATE_DIR = os.path.join(self.tmp.name, "sysupdate")
        mod.app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
        # On, because that is the state every test but OffByDefaultTests needs.
        self.state = {"available": {}, "deferred": {}, "history": [], "check_system_updates": True}
        mod.load_state = lambda: self.state
        mod.save_state = lambda s: setattr(self, "state", s)
        mod.AUTH_ENABLED = False
        mod.API_KEY = ""
        mod._sys_running.clear()
        mod._sys_logs.clear()
        self.client = mod.app.test_client()

    def add(self, name="myserver", address="sysupdate@host.docker.internal", **kw):
        r = self.client.post("/api/sysupdate/targets", json={"name": name, "address": address, **kw})
        return r


def status_json(packages=(), reboot=False, **kw):
    return json.dumps({"schema": 1, "kind": "apt", "hostname": "myserver", "os": "Ubuntu 26.04",
                       "kernel": "7.0.0-44-generic", "reboot_required": reboot, "reboot_reasons": [],
                       "services_need_restart": [], "unattended_upgrades": True,
                       "packages": list(packages), **kw})


def pkg(name="tzdata", to="2026f", security=False, held=False, hint=False):
    return {"name": name, "from": "1", "to": to, "suites": ["x"], "security": security,
            "held_back": held, "reboot_hint": hint}


class RunsHereTests(SysBase):
    """Which machine docker-updater itself runs on: the page warns about updating Docker only there."""

    def setUp(self):
        super().setUp()
        self.mod._local_docker_name_cache = None
        self.addCleanup(setattr, self.mod, "_local_docker_name_cache", None)

    def test_matching_hostnames_mean_it_runs_here(self):
        with mock.patch.object(self.mod, "_local_docker_name", return_value="LiquidDock"):
            self.assertIs(self.mod._runs_here({"hostname": "liquiddock"}), True)

    def test_a_different_hostname_means_it_does_not(self):
        with mock.patch.object(self.mod, "_local_docker_name", return_value="liquiddock"):
            self.assertIs(self.mod._runs_here({"hostname": "otherbox"}), False)

    def test_not_knowing_either_name_gives_none(self):
        with mock.patch.object(self.mod, "_local_docker_name", return_value=None):
            self.assertIsNone(self.mod._runs_here({"hostname": "liquiddock"}))
        with mock.patch.object(self.mod, "_local_docker_name", return_value="liquiddock"):
            self.assertIsNone(self.mod._runs_here({}))
            self.assertIsNone(self.mod._runs_here(None))

    def test_the_local_name_is_read_from_docker_once_and_failures_are_not_kept(self):
        calls = []

        class FakeClient:
            def info(self_inner):
                calls.append(1)
                return {"Name": "liquiddock"}

        with mock.patch.object(self.mod, "get_docker_client", return_value=FakeClient()):
            self.assertEqual(self.mod._local_docker_name(), "liquiddock")
            self.assertEqual(self.mod._local_docker_name(), "liquiddock")
        self.assertEqual(len(calls), 1)
        self.mod._local_docker_name_cache = None
        with mock.patch.object(self.mod, "get_docker_client", side_effect=RuntimeError("down")):
            self.assertIsNone(self.mod._local_docker_name())
        self.assertIsNone(self.mod._local_docker_name_cache)

    def test_the_status_view_carries_it_for_linux_machines_only(self):
        targets = [{"id": "a", "name": "a", "address": "u@h", "kind": "apt"},
                   {"id": "b", "name": "b", "address": "u@h", "kind": "apt"},
                   {"id": "n", "name": "n", "address": "u@h", "kind": "dsm"}]
        states = {"a": {"info": {"hostname": "liquiddock"}}, "b": {"info": {"hostname": "other"}},
                  "n": {"info": {"hostname": "liquiddock"}}}
        with mock.patch.object(self.mod, "load_sys_targets", return_value=targets),              mock.patch.object(self.mod, "load_sys_state", side_effect=lambda i: states[i]),              mock.patch.object(self.mod, "_local_docker_name", return_value="liquiddock"):
            view = {m["id"]: m["runs_here"] for m in self.mod._sys_view()}
        self.assertEqual(view, {"a": True, "b": False, "n": False})


class OffByDefaultTests(SysBase):
    def setUp(self):
        super().setUp()
        self.state.pop("check_system_updates")        # as on a fresh install

    def test_every_endpoint_is_absent_while_off(self):
        calls = [("get", "/api/sysupdate/targets"), ("post", "/api/sysupdate/targets"),
                 ("delete", "/api/sysupdate/targets/x"), ("post", "/api/sysupdate/targets/x/test"),
                 ("post", "/api/sysupdate/check"), ("post", "/api/sysupdate/x/upgrade"),
                 ("get", "/api/sysupdate/x/log"), ("get", "/api/sysupdate/x/changelog/tzdata"),
                 ("post", "/api/sysupdate/x/reboot")]
        for method, path in calls:
            r = getattr(self.client, method)(path, json={"name": "a", "address": "u@h", "confirm": True,
                                                         "packages": "all"})
            self.assertEqual(r.status_code, 404, (method, path))
        self.assertEqual(self.mod.load_sys_targets(), [], "nothing was stored")
        self.assertFalse(os.path.exists(self.mod.SYSUPDATE_FILE))

    def test_off_unless_asked_for(self):
        self.assertFalse(self.mod.system_updates_enabled(self.state))
        self.assertFalse(self.client.get("/api/settings").get_json()["check_system_updates"])

    def test_status_has_no_trace_while_off(self):
        with mock.patch.object(self.mod, "docker") as d:
            d.from_env.return_value.containers.list.return_value = []
            r = self.client.get("/api/status")
        self.assertNotIn("system_updates", r.get_json())

    def test_env_sets_only_the_initial_default(self):
        with mock.patch.object(self.mod, "CHECK_SYSTEM_UPDATES", True):
            self.assertTrue(self.mod.system_updates_enabled({}))
            self.assertFalse(self.mod.system_updates_enabled({"check_system_updates": False}))

    def test_switching_on_and_off_through_settings(self):
        self.client.post("/api/settings", json={"check_system_updates": True})
        self.assertTrue(self.client.get("/api/settings").get_json()["check_system_updates"])
        self.add()
        self.mod.save_sys_state("myserver", {"info": {"packages": [pkg()]}, "status": "online"})
        self.client.post("/api/settings", json={"check_system_updates": False})
        self.assertFalse(self.client.get("/api/settings").get_json()["check_system_updates"])
        self.assertEqual(self.mod.load_sys_state("myserver"), {}, "what it stored is removed")
        self.assertEqual(len(self.mod.load_sys_targets()), 1, "the machine list is configuration and stays")

    def test_manual_check_refuses_while_off(self):
        self.assertEqual(self.client.post("/api/sysupdate/check").status_code, 404)


class TargetTests(SysBase):
    def test_add_list_and_unique_ids(self):
        self.assertEqual(self.add().get_json()["id"], "myserver")
        self.assertEqual(self.add().get_json()["id"], "myserver-1")
        names = [t["name"] for t in self.client.get("/api/sysupdate/targets").get_json()["targets"]]
        self.assertEqual(names, ["myserver", "myserver"])

    def test_bad_input_is_refused(self):
        for kw in ({"address": "no-at-sign"}, {"address": "a@b;id"}, {"address": "a@b c"},
                   {"address": "-oProxyCommand=x@host"}, {"address": "u@h", "port": 0},
                   {"address": "u@h", "port": 70000}, {"address": "u@h", "port": "x"},
                   {"address": "u@h", "key": "relative/key"}, {"name": ""}):
            r = self.add(**kw) if "name" not in kw else self.client.post("/api/sysupdate/targets", json=kw)
            self.assertEqual(r.status_code, 400, kw)
        self.assertEqual(self.mod.load_sys_targets(), [])

    def test_remove_drops_state_too(self):
        tid = self.add().get_json()["id"]
        self.mod.save_sys_state(tid, {"status": "online"})
        self.client.delete(f"/api/sysupdate/targets/{tid}")
        self.assertEqual(self.mod.load_sys_targets(), [])
        self.assertEqual(self.mod.load_sys_state(tid), {})

    def test_ssh_command_has_no_shell_and_quotes_arguments(self):
        t = {"id": "x", "address": "sysupdate@h", "port": 2222, "key": "/k"}
        cmd = self.mod._sys_ssh_cmd(t, ["upgrade", "tzdata", "a;b"])
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(cmd[cmd.index("-p") + 1], "2222")
        self.assertEqual(cmd[-2], "sysupdate@h")
        self.assertEqual(cmd[-1], "upgrade tzdata 'a;b'")


class CheckTests(SysBase):
    def setUp(self):
        super().setUp()
        self.add()
        self.target = self.mod.load_sys_targets()[0]
        self.sent = []
        self.deliver = True

        def fake_notify(title, body):
            self.sent.append((title, body))
            return self.deliver

        patcher = mock.patch.object(self.mod, "send_notification", side_effect=fake_notify)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, out, rc=0, err="", notify=True):
        with mock.patch.object(self.mod, "_sys_run", return_value=(rc, out, err)):
            self.mod.check_system_updates(notify=notify)
        return self.mod.load_sys_state("myserver")

    def test_stores_what_the_machine_reports(self):
        st = self.check(status_json([pkg("tzdata"), pkg("curl", security=True)]))
        self.assertEqual(st["status"], "online")
        self.assertEqual([p["name"] for p in st["info"]["packages"]], ["tzdata", "curl"])

    def test_announces_a_set_of_updates_once(self):
        out = status_json([pkg("tzdata"), pkg("curl", security=True)])
        self.check(out); self.check(out); self.check(out)
        self.assertEqual(len(self.sent), 1)
        title, body = self.sent[0]
        self.assertIn("2 system updates", title)
        self.assertIn("tzdata", body)
        self.assertIn("1 of them security", body)

    def test_a_changed_set_is_announced_again(self):
        self.check(status_json([pkg("tzdata")]))
        self.check(status_json([pkg("tzdata"), pkg("curl")]))
        self.check(status_json([pkg("tzdata", to="2026g")]))
        self.assertEqual(len(self.sent), 3)

    def test_nothing_pending_clears_the_memory_so_the_next_one_is_announced(self):
        self.check(status_json([pkg("tzdata")]))
        self.check(status_json([]))
        self.check(status_json([pkg("tzdata")]))
        self.assertEqual(len(self.sent), 2)

    def test_pending_reboot_alone_is_announced(self):
        self.check(status_json([], reboot=True))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("reboot required", self.sent[0][0])

    def test_a_manual_check_records_but_does_not_notify(self):
        st = self.check(status_json([pkg()]), notify=False)
        self.assertEqual(self.sent, [])
        self.assertEqual(st["status"], "online")

    def test_a_failed_send_is_not_remembered(self):
        self.deliver = False
        st = self.check(status_json([pkg()]))
        self.assertNotIn("notified_fp", st)

    def test_unreachable_machine_is_offline_with_the_reason(self):
        st = self.check("", rc=255, err="Permission denied (publickey).")
        self.assertEqual(st["status"], "offline")
        self.assertIn("Permission denied", st["last_error"])
        self.assertEqual(self.sent, [])

    def test_garbage_reply_is_an_error_not_a_crash(self):
        st = self.check("this is not json")
        self.assertEqual(st["status"], "offline")
        st = self.check(json.dumps({"schema": 99, "packages": []}))
        self.assertEqual(st["status"], "offline")

    def test_a_machine_being_rebooted_is_not_reported_as_a_fault(self):
        self.mod.save_sys_state("myserver", {"rebooting_since": self._ago(minutes=2), "status": "rebooting"})
        st = self.check("", rc=255, err="Connection refused")
        self.assertEqual(st["status"], "rebooting")
        st = self.check(status_json([]))
        self.assertEqual(st["status"], "online")
        self.assertNotIn("rebooting_since", st)

    def _ago(self, minutes):
        import datetime
        return (datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes)).isoformat() + "Z"

    def test_a_machine_that_never_comes_back_becomes_offline_not_rebooting_for_ever(self):
        self.mod.save_sys_state("myserver", {"rebooting_since": self._ago(minutes=30), "status": "rebooting"})
        st = self.check("", rc=255, err="Connection refused")
        self.assertEqual(st["status"], "offline")
        self.assertNotIn("rebooting_since", st, "the stale marker is dropped")
        self.assertIn("Connection refused", st["last_error"])

    def test_an_unreadable_reboot_marker_is_not_trusted(self):
        for junk in ("garbage", "", None, 12345):
            self.mod.save_sys_state("myserver", {"rebooting_since": junk, "status": "rebooting"})
            self.assertEqual(self.check("", rc=255, err="x")["status"], "offline", repr(junk))

    def test_scheduled_check_runs_it_only_while_enabled(self):
        self.state["check_system_updates"] = False
        self.mod._sys_check_thread = None
        calls = []
        with mock.patch.object(self.mod.docker, "from_env", return_value=mock.Mock()), \
             mock.patch.object(self.mod, "_scan_host", return_value={}), \
             mock.patch.object(self.mod, "load_hosts", return_value=[]), \
             mock.patch.object(self.mod, "check_system_updates",
                               side_effect=lambda notify=False: calls.append(notify)):
            self.mod.check_for_updates(notify=True)
            self.assertIsNone(self.mod._sys_check_thread, "no thread at all while off")
            self.assertEqual(calls, [])
            self.state["check_system_updates"] = True
            self.mod.check_for_updates(notify=True)
            self.mod._sys_check_thread.join(5)
        self.assertEqual(calls, [True])

    def test_a_slow_system_check_never_holds_up_the_container_check(self):
        self.state["check_system_updates"] = True
        release, started = threading.Event(), threading.Event()

        def slow(notify=False):
            started.set()
            release.wait(10)

        with mock.patch.object(self.mod.docker, "from_env", return_value=mock.Mock()),              mock.patch.object(self.mod, "_scan_host", return_value={}),              mock.patch.object(self.mod, "load_hosts", return_value=[]),              mock.patch.object(self.mod, "check_system_updates", side_effect=slow):
            self.mod._sys_check_thread = None
            self.mod.check_for_updates(notify=False)        # returns while the System check is stuck
            self.assertTrue(started.wait(5))
            self.assertFalse(self.mod._check_running, "the container check is finished")
            self.assertTrue(self.mod._sys_check_thread.is_alive(), "the System check is still going")
            release.set()
            self.mod._sys_check_thread.join(5)

    def test_a_manual_check_skips_if_one_is_already_running(self):
        calls = []
        with mock.patch.object(self.mod, "check_system_updates", side_effect=lambda notify=False: calls.append(1)):
            self.mod._sys_check_lock.acquire()
            try:
                self.mod.start_system_check(notify=False).join(5)
            finally:
                self.mod._sys_check_lock.release()
        self.assertEqual(calls, [])


class UpgradeTests(SysBase):
    def setUp(self):
        super().setUp()
        self.add()
        self.started = []
        patcher = mock.patch.object(self.mod, "_run_system_upgrade",
                                    side_effect=lambda t, pkgs: self.started.append(pkgs))
        patcher.start()
        self.addCleanup(patcher.stop)

    def up(self, body):
        r = self.client.post("/api/sysupdate/myserver/upgrade", json=body)
        for th in threading.enumerate():
            if th is not threading.current_thread() and th.daemon and th.is_alive():
                th.join(1)
        return r

    def test_all_or_a_list_of_valid_names(self):
        self.assertEqual(self.up({"packages": "all"}).status_code, 200)
        self.mod._sys_running.clear()
        self.assertEqual(self.up({"packages": ["tzdata", "libfoo:amd64"]}).status_code, 200)
        self.assertEqual(self.started, [["all"], ["tzdata", "libfoo:amd64"]])

    def test_anything_else_is_refused_before_any_ssh(self):
        for body in ({}, {"packages": []}, {"packages": "tzdata"}, {"packages": ["-y"]},
                     {"packages": ["a;b"]}, {"packages": [1]}, {"packages": ["tzdata"] * 201},
                     {"packages": None}):
            self.assertEqual(self.up(body).status_code, 400, body)
        self.assertEqual(self.started, [])

    def test_unknown_machine(self):
        r = self.client.post("/api/sysupdate/nope/upgrade", json={"packages": "all"})
        self.assertEqual(r.status_code, 404)

    def test_one_update_at_a_time_per_machine(self):
        self.mod._sys_running.add("myserver")
        self.assertEqual(self.up({"packages": "all"}).status_code, 409)


class WorkerTests(SysBase):
    def test_worker_streams_the_log_and_rereads_the_machine(self):
        class FakePopen:
            def __init__(self, cmd, **kw):
                self.cmd = cmd
                self.stdout = iter(["Reading package lists...\n", "Setting up tzdata\n"])

            def wait(self):
                return 0

            def kill(self):
                pass

        mod = self.mod
        rechecked = []
        fake_recheck = mock.patch.object(mod, "check_system_target",
                                         side_effect=lambda t, refresh=True: rechecked.append(refresh))
        with mock.patch.object(mod.subprocess, "Popen", FakePopen), fake_recheck:
            mod._sys_running.add("myserver")
            mod._run_system_upgrade({"id": "myserver", "address": "u@h", "port": 22}, ["tzdata"])
        log = mod._sys_logs["myserver"]
        self.assertIn("Setting up tzdata", log)
        self.assertIn("✔ finished", log)
        self.assertEqual(rechecked, [False], "re-reads the machine without another apt update")
        self.assertNotIn("myserver", mod._sys_running)

    def test_failure_is_logged_and_the_machine_is_released(self):
        class FakePopen:
            def __init__(self, cmd, **kw):
                self.stdout = iter(["E: Unable to locate package\n"])

            def wait(self):
                return 100

            def kill(self):
                pass

        mod = self.mod
        with mock.patch.object(mod.subprocess, "Popen", FakePopen), \
                mock.patch.object(mod, "check_system_target"):
            mod._sys_running.add("myserver")
            mod._run_system_upgrade({"id": "myserver", "address": "u@h", "port": 22}, ["all"])
        self.assertTrue(any("failed (exit status 100)" in ln for ln in mod._sys_logs["myserver"]))
        self.assertNotIn("myserver", mod._sys_running)


class RebootAndAccessTests(SysBase):
    def setUp(self):
        super().setUp()
        self.add()

    def test_reboot_needs_an_explicit_confirmation(self):
        with mock.patch.object(self.mod, "_sys_run", return_value=(0, "", "")) as run:
            for body in ({}, {"confirm": False}, {"confirm": "yes"}, {"confirm": 1}):
                self.assertEqual(self.client.post("/api/sysupdate/myserver/reboot", json=body).status_code, 400, body)
            run.assert_not_called()
            r = self.client.post("/api/sysupdate/myserver/reboot", json={"confirm": True})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.mod.load_sys_state("myserver")["status"], "rebooting")

    def test_no_reboot_while_an_update_runs(self):
        self.mod._sys_running.add("myserver")
        r = self.client.post("/api/sysupdate/myserver/reboot", json={"confirm": True})
        self.assertEqual(r.status_code, 409)

    def test_changelog_validates_the_name(self):
        with mock.patch.object(self.mod, "_sys_run", return_value=(0, "tzdata (1) x\n", "")):
            ok = self.client.get("/api/sysupdate/myserver/changelog/tzdata").get_json()
            self.assertTrue(ok["ok"])
            self.assertEqual(self.client.get("/api/sysupdate/myserver/changelog/a;b").status_code, 400)

    def test_login_covers_every_endpoint_and_the_api_key_covers_none(self):
        self.mod.AUTH_ENABLED = True
        self.mod.AUTH_USERNAME, self.mod.AUTH_PASSWORD = "admin", "pw"
        self.mod.API_KEY = "k" * 20
        hdr = {"Authorization": "Bearer " + "k" * 20}
        calls = [("get", "/api/sysupdate/targets"), ("post", "/api/sysupdate/targets"),
                 ("delete", "/api/sysupdate/targets/myserver"), ("post", "/api/sysupdate/targets/myserver/test"),
                 ("post", "/api/sysupdate/check"), ("post", "/api/sysupdate/myserver/upgrade"),
                 ("get", "/api/sysupdate/myserver/log"), ("get", "/api/sysupdate/myserver/changelog/tzdata"),
                 ("post", "/api/sysupdate/myserver/reboot")]
        for method, path in calls:
            for headers in ({}, hdr):
                r = getattr(self.client, method)(path, headers=headers, json={"confirm": True})
                self.assertEqual(r.status_code, 401, (method, path, bool(headers)))
        self.assertEqual(len(self.mod.load_sys_targets()), 1, "nothing was changed")


class ReviewFixTests(SysBase):
    """Findings from the pre-release review of the System tab."""

    def test_address_cannot_start_with_a_dash_in_either_half(self):
        # A leading "-" is read by ssh as an option: -oProxyCommand=... runs a command.
        for bad in ("-oProxyCommand=id@host", "u@-oProxyCommand=id", "-u@host", "u@-h"):
            self.assertEqual(self.add(address=bad).status_code, 400, bad)
        self.assertEqual(self.add(address="u-1@h-1.example").status_code, 200)

    def test_ssh_gets_an_end_of_options_marker_before_the_address(self):
        cmd = self.mod._sys_ssh_cmd({"id": "x", "address": "u@h", "port": 22, "key": "/k"}, ["status"])
        self.assertEqual(cmd[-3:], ["--", "u@h", "status"])

    def test_a_hand_edited_targets_file_cannot_smuggle_an_option(self):
        t = {"id": "x", "address": "-oProxyCommand=id", "port": 22, "key": "/k"}
        with self.assertRaises(ValueError):
            self.mod._sys_ssh_cmd(t, ["status"])

    def test_package_names_from_the_machine_are_filtered(self):
        reply = status_json([pkg("tzdata"), pkg("a'),alert(1),('"), pkg("-oFoo"), pkg("UPPER"), pkg("libc6:amd64")])
        names = [p["name"] for p in self.mod._sys_parse_status(reply)["packages"]]
        self.assertEqual(names, ["tzdata", "libc6:amd64"])

    def test_upgrade_never_removes_packages(self):
        self.assertIn('"--no-remove"', (ROOT / "contrib" / "dock-sysupdate").read_text(encoding="utf-8"))

    def _status(self, headers=None, authed=False):
        self.state["check_system_updates"] = True
        if authed:        # log in first: adding a machine needs it once a login is configured
            with self.client.session_transaction() as sess:
                sess["authenticated"] = True
        elif self.mod.AUTH_ENABLED:
            self.mod.AUTH_ENABLED = False
            self.add()
            self.mod.AUTH_ENABLED = True
        if authed or not self.mod.AUTH_ENABLED:
            self.add()
        with mock.patch.object(self.mod, "docker") as d:
            d.from_env.return_value.containers.list.return_value = []
            return self.client.get("/api/status", headers=headers or {}).get_json()

    def test_the_read_only_api_key_does_not_see_system_updates(self):
        self.mod.AUTH_ENABLED = True
        self.mod.AUTH_USERNAME, self.mod.AUTH_PASSWORD = "admin", "pw"
        self.mod.API_KEY = "k" * 20
        data = self._status(headers={"Authorization": "Bearer " + "k" * 20})
        self.assertNotIn("system_updates", data)
        self.assertNotIn("system_updates_open", data)

    def test_a_logged_in_session_sees_them_without_the_open_warning(self):
        self.mod.AUTH_ENABLED = True
        self.mod.AUTH_USERNAME, self.mod.AUTH_PASSWORD = "admin", "pw"
        data = self._status(authed=True)
        self.assertEqual(len(data["system_updates"]), 1)
        self.assertFalse(data["system_updates_open"])

    def test_with_no_login_at_all_the_ui_is_told_to_warn(self):
        data = self._status()
        self.assertEqual(len(data["system_updates"]), 1)
        self.assertTrue(data["system_updates_open"])


class IndependentChecksTests(SysBase):
    """The container check and the System check are separate: own thread, own lock, own button."""

    def _run_container_check(self, **kw):
        with mock.patch.object(self.mod.docker, "from_env", return_value=mock.Mock()),              mock.patch.object(self.mod, "_scan_host", return_value={}),              mock.patch.object(self.mod, "load_hosts", return_value=[]),              mock.patch.object(self.mod, "check_system_updates") as sysc:
            self.mod._sys_check_thread = None
            self.mod.check_for_updates(**kw)
            if self.mod._sys_check_thread:
                self.mod._sys_check_thread.join(5)
            return sysc

    def test_the_system_check_starts_with_the_container_check_not_after_it(self):
        started_at, order = {}, []
        def fake_scan(client, host_id):
            order.append("scan"); return {}
        def fake_sys(notify=False):
            order.append("sys")
        with mock.patch.object(self.mod.docker, "from_env", return_value=mock.Mock()),              mock.patch.object(self.mod, "_scan_host", side_effect=fake_scan),              mock.patch.object(self.mod, "load_hosts", return_value=[]),              mock.patch.object(self.mod, "check_system_updates", side_effect=fake_sys):
            self.mod._sys_check_thread = None
            real_start = self.mod.start_system_check
            seen = []
            with mock.patch.object(self.mod, "start_system_check",
                                   side_effect=lambda notify=False: (seen.append(list(order)), real_start(notify))[1]):
                self.mod.check_for_updates(notify=False)
            if self.mod._sys_check_thread:
                self.mod._sys_check_thread.join(5)
        self.assertEqual(seen, [[]], "it was started before the first container was scanned")

    def test_the_dashboards_docker_button_checks_containers_only(self):
        with mock.patch.object(self.mod, "check_for_updates") as chk,              mock.patch.object(self.mod.threading, "Thread") as thread:
            self.client.post("/api/check")
        self.assertEqual(thread.call_args.kwargs["args"], (False, False))     # notify=False, include_system=False
        self.assertFalse(self._run_container_check(notify=False, include_system=False).called)

    def test_scheduled_and_startup_checks_still_include_the_system(self):
        self.assertTrue(self._run_container_check(notify=False).called)

    def test_the_status_reports_whether_a_system_check_is_running(self):
        self.state["check_system_updates"] = True
        def flag():
            with mock.patch.object(self.mod, "docker") as d:
                d.from_env.return_value.containers.list.return_value = []
                return self.client.get("/api/status").get_json()["system_check_running"]
        self.assertFalse(flag())
        self.mod._sys_check_lock.acquire()
        try:
            self.assertTrue(flag())
        finally:
            self.mod._sys_check_lock.release()
        self.assertFalse(flag())


if __name__ == "__main__":
    unittest.main()
