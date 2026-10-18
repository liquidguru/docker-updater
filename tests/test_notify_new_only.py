"""'Only notify about new updates' (issue #30).

Every scheduled check used to list every pending update, so something left in
the Updates tab for a week produced seven identical notifications. With
`notify_new_only` on, an update is announced once, keyed by the registry digest
it was announced at — so a newer release on top of an ignored one still alerts.
"""

import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.app_loader import load_app_module


def _upd(digest="sha256:aaa", has_update=True):
    return {"has_update": has_update, "remote_digest": digest}


class NotifyNewOnlyBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def setUp(self):
        self.state = {"available": {}, "deferred": {}, "history": [],
                      "rollbacks": {}, "backup_hours": 24}
        self.sent = []          # one (title, body) per notification
        self.deliver = True     # what the fake notifier reports back
        self.remote_available = {}

    def check(self, available, new_only=True, deferred=None, remote=None):
        """Run one notifying check and return the body sent, or None."""
        mod = self.mod
        self.state["notify_new_only"] = new_only
        self.state["deferred"] = deferred or {}
        before = len(self.sent)

        def fake_notify(title, body):
            self.sent.append((title, body))
            return self.deliver

        hosts = [{"id": h, "name": h.title(), "url": "tcp://x"} for h in (remote or {})]

        def fake_scan(_client, host_id):
            return (remote or {}).get(host_id, available)

        with mock.patch.object(mod.docker, "from_env", return_value=mock.Mock()), \
             mock.patch.object(mod, "_scan_host", side_effect=fake_scan), \
             mock.patch.object(mod, "load_state", side_effect=lambda: self.state), \
             mock.patch.object(mod, "save_state",
                               side_effect=lambda s: setattr(self, "state", s)), \
             mock.patch.object(mod, "load_hosts", return_value=hosts), \
             mock.patch.object(mod, "get_docker_client", return_value=mock.Mock()), \
             mock.patch.object(mod, "load_host_state", return_value={}), \
             mock.patch.object(mod, "save_host_state"), \
             mock.patch.object(mod, "send_notification", side_effect=fake_notify), \
             mock.patch.object(mod, "_rate_limit_seen", {}):
            mod.check_for_updates(notify=True)
        return self.sent[-1][1] if len(self.sent) > before else None

    def body(self, *args, **kwargs) -> str:
        """Like check(), but the check must have sent something."""
        sent = self.check(*args, **kwargs)
        self.assertIsNotNone(sent, "expected a notification, got none")
        return sent or ""


class NotifyNewOnlyTests(NotifyNewOnlyBase):
    def test_off_by_default_repeats_everything_every_check(self):
        """Existing behaviour must not change for anyone who hasn't opted in."""
        avail = {"web": _upd(), "db": _upd()}
        first = self.body(avail, new_only=False)
        second = self.body(avail, new_only=False)
        self.assertIn("web", first)
        self.assertIn("web", second)
        self.assertIn("db", second)

    def test_second_identical_check_is_silent(self):
        avail = {"web": _upd()}
        self.assertIsNotNone(self.check(avail))
        self.assertIsNone(self.check(avail),
                          "an update that was already announced was announced again")
        self.assertEqual(len(self.sent), 1)

    def test_only_the_new_update_is_listed_and_earlier_ones_are_counted(self):
        self.check({"web": _upd()})
        body = self.body({"web": _upd(), "db": _upd("sha256:bbb")})
        self.assertIn("db", body)
        self.assertNotIn("web", body, "an already-announced update was listed again")
        self.assertIn("1", body.splitlines()[-1],
                      f"should say how many earlier ones are still pending: {body!r}")

    def test_title_counts_only_the_new_updates(self):
        self.check({"web": _upd(), "db": _upd()})
        self.check({"web": _upd(), "db": _upd(), "cache": _upd()})
        title = self.sent[-1][0]
        self.assertIn("1", title)
        self.assertNotIn("3", title)

    def test_newer_release_of_an_ignored_update_is_announced_again(self):
        self.check({"web": _upd("sha256:aaa")})
        body = self.body({"web": _upd("sha256:bbb")})
        self.assertIn("web", body)

    def test_update_that_was_applied_and_returns_is_announced_again(self):
        self.check({"web": _upd("sha256:aaa")})
        self.check({"web": _upd(has_update=False)})      # user updated it
        self.assertNotIn("local:web", self.state.get("notified", {}),
                         "a finished update should be forgotten")
        body = self.check({"web": _upd("sha256:aaa")})   # same digest, new cycle
        self.assertIsNotNone(body)

    def test_failed_send_is_retried_next_check(self):
        self.deliver = False
        avail = {"web": _upd()}
        self.check(avail)
        self.assertNotIn("local:web", self.state.get("notified", {}),
                         "a notification that never went out was recorded as sent")
        self.deliver = True
        self.assertIsNotNone(self.check(avail), "should retry after a failed send")

    def test_update_without_a_digest_is_always_reported(self):
        """Can't tell whether it is new, so err towards telling the user."""
        avail = {"web": {"has_update": True}}
        self.assertIsNotNone(self.check(avail))
        self.assertIsNotNone(self.check(avail))

    def test_switching_the_option_on_does_not_re_announce_known_updates(self):
        """Digests are recorded even while the option is off."""
        avail = {"web": _upd()}
        self.check(avail, new_only=False)
        self.assertIsNone(self.check(avail, new_only=True))

    def test_deferred_update_is_not_recorded_and_is_announced_when_it_expires(self):
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        avail = {"web": _upd()}
        self.assertIsNone(self.check(avail, deferred={"web": {"until": tomorrow}}))
        body = self.check(avail, deferred={"web": {"until": yesterday}})
        self.assertIsNotNone(body, "an expired deferral should surface the update once")

    def test_announced_update_that_is_then_deferred_is_not_forgotten(self):
        """Pruning must key off 'still has an update', not 'not deferred' —
        otherwise deferring and un-deferring would re-announce it."""
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        avail = {"web": _upd()}
        self.check(avail)
        self.check(avail, deferred={"web": {"until": tomorrow}})
        self.assertIn("local:web", self.state["notified"])
        self.assertIsNone(self.check(avail, deferred={"web": {"until": yesterday}}))

    def test_same_container_name_on_two_hosts_is_tracked_separately(self):
        self.check({"web": _upd()}, remote={"vps": {"web": _upd()}})
        self.assertIn("local:web", self.state["notified"])
        self.assertIn("vps:web", self.state["notified"])
        body = self.body({"web": _upd()},
                         remote={"vps": {"web": _upd("sha256:new")}})
        self.assertIn("web", body)

    def test_offline_host_keeps_its_records(self):
        """A host that can't be scanned says nothing about its updates, so its
        'already notified' records must survive the outage."""
        self.check({"web": _upd()}, remote={"vps": {"web": _upd()}})
        self.state["notified"]["vps:db"] = "sha256:old"
        self.check({"web": _upd()})  # no remote hosts scanned this time
        self.assertIn("vps:db", self.state["notified"])
        self.assertIn("vps:web", self.state["notified"])

    def test_no_update_pending_sends_nothing_and_clears_records(self):
        self.check({"web": _upd()})
        self.assertIsNone(self.check({"web": _upd(has_update=False)}))
        self.assertEqual(self.state["notified"], {})


class NotifyNewOnlyPersistenceTests(NotifyNewOnlyBase):
    def test_records_survive_a_restart(self):
        """Through the real load_state/save_state, so the record is plain JSON
        that a restart (which drops all in-memory state) can read back."""
        mod = self.mod
        with tempfile.TemporaryDirectory() as tmp:
            sf = os.path.join(tmp, "state.json")
            with mock.patch.object(mod, "DATA_DIR", tmp), \
                 mock.patch.object(mod, "STATE_FILE", sf), \
                 mock.patch.object(mod, "load_hosts", return_value=[]), \
                 mock.patch.object(mod.docker, "from_env", return_value=mock.Mock()), \
                 mock.patch.object(mod, "_scan_host", return_value={"web": _upd()}), \
                 mock.patch.object(mod, "send_notification", return_value=True) as send, \
                 mock.patch.object(mod, "_rate_limit_seen", {}):
                with open(sf, "w") as f:
                    json.dump({"notify_new_only": True, "available": {},
                               "history": [], "rollbacks": {}}, f)
                mod.check_for_updates(notify=True)
                mod.check_for_updates(notify=True)
                self.assertEqual(send.call_count, 1)
                with open(sf) as f:
                    on_disk = json.load(f)
                self.assertEqual(on_disk["notified"], {"local:web": "sha256:aaa"})


class SettingsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def setUp(self):
        self.state = {}
        self.mod.app.config.update(TESTING=True)
        self.patches = [
            mock.patch.object(self.mod, "AUTH_ENABLED", False),
            mock.patch.object(self.mod, "load_state", side_effect=lambda: self.state),
            mock.patch.object(self.mod, "save_state",
                              side_effect=lambda s: setattr(self, "state", s)),
        ]
        for p in self.patches:
            p.start()
        self.client = self.mod.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_defaults_to_off(self):
        self.assertIs(self.client.get("/api/settings").get_json()["notify_new_only"], False)

    def test_setting_round_trips(self):
        r = self.client.post("/api/settings", json={"notify_new_only": True})
        self.assertEqual(r.status_code, 200)
        self.assertIs(self.client.get("/api/settings").get_json()["notify_new_only"], True)
        self.client.post("/api/settings", json={"notify_new_only": False})
        self.assertIs(self.client.get("/api/settings").get_json()["notify_new_only"], False)

    def test_unrelated_settings_posts_leave_it_alone(self):
        self.client.post("/api/settings", json={"notify_new_only": True})
        self.client.post("/api/settings", json={"backup_hours": 6})
        self.assertIs(self.client.get("/api/settings").get_json()["notify_new_only"], True)


class SendNotificationResultTests(unittest.TestCase):
    """The 'already notified' record is only as honest as this return value."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def _send(self, url, notify_result=True, raises=False):
        fake = mock.Mock()
        if raises:
            fake.notify.side_effect = RuntimeError("boom")
        else:
            fake.notify.return_value = notify_result
        with mock.patch.object(self.mod, "get_effective_notify_url", return_value=url), \
             mock.patch.object(self.mod.apprise, "Apprise", return_value=fake):
            return self.mod.send_notification("t", "b")

    def test_no_url_is_not_a_send(self):
        self.assertIs(self._send(None), False)

    def test_success(self):
        self.assertIs(self._send("ntfy://x/y"), True)

    def test_apprise_reporting_failure_is_not_a_send(self):
        self.assertIs(self._send("ntfy://x/y", notify_result=False), False)

    def test_exception_is_not_a_send(self):
        self.assertIs(self._send("ntfy://x/y", raises=True), False)


if __name__ == "__main__":
    unittest.main()
