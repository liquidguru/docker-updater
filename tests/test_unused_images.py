"""Checking images that no running container uses (issue #31).

Strictly opt-in and strictly informational: nothing is pulled for you, because an
old image may be a database you haven't run in a year and a newer version can
refuse the data the old one wrote. While the option is off, nothing about it is
scanned, stored, returned or shown.
"""

import unittest
from unittest import mock

from tests.app_loader import load_app_module


def image(id_, tags, digests, size=1_000_000, arch="amd64"):
    img = mock.Mock()
    img.id = id_
    img.attrs = {"Id": id_, "RepoTags": tags, "RepoDigests": digests, "Size": size,
                 "Os": "linux", "Architecture": arch}
    return img


def container(image_id, ref):
    c = mock.Mock()
    c.attrs = {"Image": image_id, "Config": {"Image": ref}}
    return c


class FakeClient:
    def __init__(self, images=(), running=()):
        self._images, self._running = list(images), list(running)
        self.containers = mock.Mock()
        self.containers.list.side_effect = lambda *_a, **_k: list(self._running)
        self.images = mock.Mock()
        self.images.list.side_effect = lambda *_a, **_k: list(self._images)
        self.api = mock.Mock()
        self.ping = mock.Mock(return_value=True)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def scan(self, client, remote=None, host_id="local"):
        """Run the scanner with the registry answers in `remote` ({tag: digest|None})."""
        remote = remote or {}
        calls = []

        def fake_remote(tag, _local_digest=None, _image_id=None, _platform=None):
            calls.append(tag)
            return remote.get(tag)

        with mock.patch.object(self.mod, "get_remote_digest", side_effect=fake_remote):
            self.calls = calls
            return self.mod._scan_unused_images(client, host_id)


class ScannerTests(Base):
    def test_an_unused_tagged_image_with_a_newer_version_is_reported(self):
        client = FakeClient([image("sha256:i1", ["postgres:13"], ["postgres@sha256:old"], size=400)])
        found = self.scan(client, {"postgres:13": "sha256:new"})
        self.assertEqual(list(found), ["postgres:13"])
        info = found["postgres:13"]
        self.assertTrue(info["has_update"])
        self.assertTrue(info["checked"])
        self.assertEqual((info["local_digest"], info["remote_digest"], info["size"]),
                         ("sha256:old", "sha256:new", 400))

    def test_an_up_to_date_image_is_listed_but_not_flagged(self):
        client = FakeClient([image("sha256:i1", ["redis:7"], ["redis@sha256:same"])])
        found = self.scan(client, {"redis:7": "sha256:same"})
        self.assertFalse(found["redis:7"]["has_update"])
        self.assertTrue(found["redis:7"]["checked"])

    def test_an_unreachable_registry_is_unknown_not_an_update(self):
        client = FakeClient([image("sha256:i1", ["redis:7"], ["redis@sha256:a"])])
        info = self.scan(client, {"redis:7": None})["redis:7"]
        self.assertFalse(info["has_update"])
        self.assertFalse(info["checked"])

    def test_an_image_a_running_container_uses_is_left_to_the_normal_scan(self):
        client = FakeClient([image("sha256:i1", ["nginx:alpine"], ["nginx@sha256:a"])],
                            running=[container("sha256:i1", "nginx:alpine")])
        self.assertEqual(self.scan(client, {"nginx:alpine": "sha256:b"}), {})
        self.assertEqual(self.calls, [], "asked the registry about a running container's image")

    def test_a_running_containers_image_is_not_listed_even_if_the_ids_disagree(self):
        """Some daemons (the containerd image store) report image IDs that don't
        match between `images` and a container's `Image`. The reference the
        container was created from still does — otherwise a running container's
        own image would show up here as if it were unused."""
        client = FakeClient([image("sha256:index-digest", ["nginx:alpine"], ["nginx@sha256:a"])],
                            running=[container("sha256:config-digest", "nginx:alpine")])
        self.assertEqual(self.scan(client, {"nginx:alpine": "sha256:b"}), {})

    def test_a_running_containers_image_is_not_listed_when_only_the_id_matches(self):
        """A container created from a digest or an image ID has no tag to match —
        only its image ID says which image it is running."""
        for ref in ("nginx@sha256:a", "sha256:i1", "abc123def456"):
            with self.subTest(ref):
                client = FakeClient([image("sha256:i1", ["nginx:alpine"], ["nginx@sha256:a"])],
                                    running=[container("sha256:i1", ref)])
                self.assertEqual(self.scan(client, {"nginx:alpine": "sha256:b"}), {})

    def test_an_implicit_latest_matches_the_running_reference(self):
        client = FakeClient([image("sha256:i1", ["redis:latest"], ["redis@sha256:a"])],
                            running=[container("sha256:other", "redis")])
        self.assertEqual(self.scan(client, {"redis:latest": "sha256:b"}), {})

    def test_only_running_containers_count_as_using_an_image(self):
        """An image kept by a stopped container is exactly the "standby" image
        the request is about, so the scanner must ask for running ones only."""
        client = FakeClient([image("sha256:i1", ["mariadb:10"], ["mariadb@sha256:a"])])
        self.assertIn("mariadb:10", self.scan(client, {"mariadb:10": "sha256:b"}))
        for call in client.containers.list.call_args_list:
            self.assertFalse(call.kwargs.get("all"), "stopped containers must not hide their images")

    def test_untagged_images_are_ignored(self):
        """What every update leaves behind — pure noise here."""
        client = FakeClient([image("sha256:i1", [], ["nginx@sha256:a"]),
                             image("sha256:i2", ["<none>:<none>"], ["nginx@sha256:b"])])
        self.assertEqual(self.scan(client), {})
        self.assertEqual(self.calls, [])

    def test_locally_built_images_are_ignored(self):
        client = FakeClient([image("sha256:i1", ["myapp:dev"], [])])
        self.assertEqual(self.scan(client), {})
        self.assertEqual(self.calls, [])

    def test_a_digest_from_another_repository_is_not_compared(self):
        """get_local_digest falls back to the first digest of *any* repository;
        comparing that with this tag's registry would invent an update."""
        client = FakeClient([image("sha256:i1", ["mine/app:1"], ["other/thing@sha256:zzz"])])
        self.assertEqual(self.scan(client, {"mine/app:1": "sha256:new"}), {})
        self.assertEqual(self.calls, [])

    def test_every_tag_of_an_image_is_checked_separately(self):
        client = FakeClient([image("sha256:i1", ["nginx:1.25", "nginx:stable"], ["nginx@sha256:a"])])
        found = self.scan(client, {"nginx:1.25": "sha256:a", "nginx:stable": "sha256:b"})
        self.assertFalse(found["nginx:1.25"]["has_update"])
        self.assertTrue(found["nginx:stable"]["has_update"])

    def test_the_spellings_docker_hub_digests_come_in_all_match(self):
        for digest_ref in ("nginx@sha256:a", "docker.io/library/nginx@sha256:a",
                           "library/nginx@sha256:a"):
            with self.subTest(digest_ref):
                client = FakeClient([image("sha256:i1", ["nginx:1.24"], [digest_ref])])
                self.assertEqual(self.scan(client, {"nginx:1.24": "sha256:b"})["nginx:1.24"]["local_digest"],
                                 "sha256:a")

    def test_a_private_registry_image_matches_its_own_repository(self):
        client = FakeClient([image("sha256:i1", ["ghcr.io/me/app:v1"], ["ghcr.io/me/app@sha256:a"])])
        self.assertTrue(self.scan(client, {"ghcr.io/me/app:v1": "sha256:b"})["ghcr.io/me/app:v1"]["has_update"])

    def test_nothing_is_ever_pulled(self):
        client = FakeClient([image("sha256:i1", ["postgres:13"], ["postgres@sha256:old"])])
        self.scan(client, {"postgres:13": "sha256:new"})
        client.images.pull.assert_not_called()
        client.api.pull.assert_not_called()

    def test_the_scanner_has_no_way_to_pull(self):
        import inspect
        src = inspect.getsource(self.mod._scan_unused_images)
        self.assertNotIn(".pull(", src)
        self.assertNotIn("api.pull", src)


class EnabledTests(Base):
    def test_off_by_default(self):
        with mock.patch.object(self.mod, "CHECK_UNRUNNING_IMAGES", False):
            self.assertFalse(self.mod.unused_images_enabled({}))

    def test_the_environment_variable_sets_the_initial_default(self):
        with mock.patch.object(self.mod, "CHECK_UNRUNNING_IMAGES", True):
            self.assertTrue(self.mod.unused_images_enabled({}))

    def test_a_saved_choice_wins_over_the_environment_default(self):
        with mock.patch.object(self.mod, "CHECK_UNRUNNING_IMAGES", True):
            self.assertFalse(self.mod.unused_images_enabled({"check_unused_images": False}))
        with mock.patch.object(self.mod, "CHECK_UNRUNNING_IMAGES", False):
            self.assertTrue(self.mod.unused_images_enabled({"check_unused_images": True}))

    def test_the_environment_variable_is_parsed_strictly(self):
        import os
        import importlib.util
        for value, expected in (("true", True), ("1", True), ("YES", True), ("on", True),
                                ("", False), ("false", False), ("0", False), ("no", False),
                                ("maybe", False)):
            with self.subTest(value):
                with mock.patch.dict(os.environ, {"CHECK_UNRUNNING_IMAGES": value}):
                    mod = load_app_module()
                self.assertIs(mod.CHECK_UNRUNNING_IMAGES, expected)


class CheckBase(Base):
    def setUp(self):
        self.state = {"available": {}, "deferred": {}, "history": [], "rollbacks": {},
                      "backup_hours": 24}
        self.sent = []
        self.host_states = {}

    def check(self, *, enabled, images=(), remote=None, available=None, new_only=False,
              hosts=None, host_images=None, scan_raises=False):
        """One notifying check. `remote` answers registry lookups for unused images;
        `hosts` are extra Docker hosts, with their own `host_images`."""
        mod = self.mod
        self.state["check_unused_images"] = enabled
        self.state["notify_new_only"] = new_only
        remote = remote or {}
        local = FakeClient(images)
        extra = {h["id"]: FakeClient((host_images or {}).get(h["id"], ())) for h in (hosts or [])}
        self.scanned = []
        before = len(self.sent)

        def fake_notify(title, body):
            self.sent.append((title, body))
            return True

        def fake_remote(tag, *_a, **_k):
            self.scanned.append(tag)
            return remote.get(tag)

        def fake_scan_host(_client, host_id):
            return (available or {}) if host_id == "local" else {}

        def get_client(url=None):
            return extra[next(h["id"] for h in hosts if h["url"] == url)]

        real_scan = mod._scan_unused_images
        def scan(client, host_id):
            if scan_raises:
                raise RuntimeError("boom")
            return real_scan(client, host_id)

        host_list = hosts or []
        with mock.patch.object(mod.docker, "from_env", return_value=local), \
             mock.patch.object(mod, "_scan_host", side_effect=fake_scan_host), \
             mock.patch.object(mod, "_scan_unused_images", side_effect=scan), \
             mock.patch.object(mod, "get_remote_digest", side_effect=fake_remote), \
             mock.patch.object(mod, "load_state", side_effect=lambda: self.state), \
             mock.patch.object(mod, "save_state", side_effect=lambda s: setattr(self, "state", s)), \
             mock.patch.object(mod, "load_hosts", return_value=host_list), \
             mock.patch.object(mod, "get_docker_client", side_effect=get_client), \
             mock.patch.object(mod, "load_host_state",
                               side_effect=lambda h: self.host_states.setdefault(h, {})), \
             mock.patch.object(mod, "save_host_state",
                               side_effect=lambda h, s: self.host_states.__setitem__(h, s)), \
             mock.patch.object(mod, "send_notification", side_effect=fake_notify), \
             mock.patch.object(mod, "_rate_limit_seen", {}):
            mod.check_for_updates(notify=True)
        return self.sent[-1] if len(self.sent) > before else None

    def got(self, **kwargs) -> tuple[str, str]:
        """Like check(), but a notification must have gone out."""
        sent = self.check(**kwargs)
        self.assertIsNotNone(sent, "expected a notification, got none")
        return sent or ("", "")


class CheckIntegrationTests(CheckBase):
    OLD = [image("sha256:i1", ["postgres:13"], ["postgres@sha256:old"])]

    def test_while_off_nothing_is_scanned_stored_or_announced(self):
        _title, body = self.got(enabled=False, images=self.OLD, remote={"postgres:13": "sha256:new"},
                                available={"web": {"has_update": True, "remote_digest": "sha256:w"}})
        self.assertEqual(self.scanned, [], "the registry was asked about an unused image while off")
        self.assertNotIn("unused_images", self.state)
        self.assertNotIn("postgres", body)

    def test_while_on_an_unused_image_update_is_announced_and_counted(self):
        title, body = self.got(enabled=True, images=self.OLD, remote={"postgres:13": "sha256:new"})
        self.assertIn("postgres:13 (unused image)", body)
        self.assertIn("1", title)
        self.assertTrue(self.state["unused_images"]["postgres:13"]["has_update"])

    def test_it_is_listed_alongside_container_updates_in_one_notification(self):
        title, body = self.got(enabled=True, images=self.OLD, remote={"postgres:13": "sha256:new"},
                               available={"web": {"has_update": True, "remote_digest": "sha256:w"}})
        self.assertIn("web", body)
        self.assertIn("postgres:13", body)
        self.assertIn("2", title)
        self.assertEqual(len(self.sent), 1)

    def test_an_up_to_date_unused_image_says_nothing_but_is_still_stored(self):
        sent = self.check(enabled=True, images=self.OLD, remote={"postgres:13": "sha256:old"})
        self.assertIsNone(sent)
        self.assertFalse(self.state["unused_images"]["postgres:13"]["has_update"])

    def test_only_notify_about_new_updates_covers_unused_images(self):
        self.assertIsNotNone(self.check(enabled=True, new_only=True, images=self.OLD,
                                        remote={"postgres:13": "sha256:new"}))
        self.assertIsNone(self.check(enabled=True, new_only=True, images=self.OLD,
                                     remote={"postgres:13": "sha256:new"}),
                          "an announced unused image was announced again")
        again = self.check(enabled=True, new_only=True, images=self.OLD,
                           remote={"postgres:13": "sha256:newer"})
        self.assertIsNotNone(again, "a newer version of the image must alert again")

    def test_its_notified_record_survives_pruning_while_it_still_has_an_update(self):
        """Pruning keeps only keys that are still pending; an unused image that
        weren't counted as pending would be forgotten and re-announced daily."""
        self.check(enabled=True, new_only=True, images=self.OLD, remote={"postgres:13": "sha256:new"})
        self.assertIn("local:image:postgres:13", self.state["notified"])
        self.check(enabled=True, new_only=True, images=self.OLD, remote={"postgres:13": "sha256:new"})
        self.assertIn("local:image:postgres:13", self.state["notified"])

    def test_its_record_is_forgotten_once_the_update_is_gone(self):
        self.check(enabled=True, new_only=True, images=self.OLD, remote={"postgres:13": "sha256:new"})
        self.check(enabled=True, new_only=True, images=self.OLD, remote={"postgres:13": "sha256:old"})
        self.assertNotIn("local:image:postgres:13", self.state["notified"])

    def test_a_failing_scan_never_breaks_the_container_notification(self):
        _title, body = self.got(enabled=True, scan_raises=True,
                                available={"web": {"has_update": True, "remote_digest": "sha256:w"}})
        self.assertIn("web", body)

    def test_a_failing_scan_keeps_the_last_good_result(self):
        self.check(enabled=True, images=self.OLD, remote={"postgres:13": "sha256:new"})
        self.check(enabled=True, scan_raises=True)
        self.assertIn("postgres:13", self.state["unused_images"])

    def test_remote_hosts_are_scanned_and_kept_per_host(self):
        hosts = [{"id": "vps", "name": "VPS", "url": "tcp://vps"}]
        _title, body = self.got(enabled=True, hosts=hosts,
                                available={"web": {"has_update": True, "remote_digest": "sha256:w"}},
                                host_images={"vps": [image("sha256:r1", ["mysql:8"], ["mysql@sha256:old"])]},
                                remote={"mysql:8": "sha256:new"})
        self.assertIn("mysql:8", self.host_states["vps"]["unused_images"])
        self.assertNotIn("mysql:8", self.state.get("unused_images", {}))
        # with updates on two hosts the message groups them by host
        self.assertIn("VPS: mysql:8 (unused image)", body)

    def test_the_same_tag_on_two_hosts_is_tracked_separately(self):
        hosts = [{"id": "vps", "name": "VPS", "url": "tcp://vps"}]
        both = image("sha256:x", ["redis:6"], ["redis@sha256:old"])
        self.check(enabled=True, new_only=True, images=[both], hosts=hosts,
                   host_images={"vps": [image("sha256:y", ["redis:6"], ["redis@sha256:old"])]},
                   remote={"redis:6": "sha256:new"})
        self.assertIn("local:image:redis:6", self.state["notified"])
        self.assertIn("vps:image:redis:6", self.state["notified"])


class ApiTests(Base):
    def setUp(self):
        self.state = {"available": {}, "deferred": {}, "history": [], "rollbacks": {}}
        self.patches = [
            mock.patch.object(self.mod, "AUTH_ENABLED", False),
            mock.patch.object(self.mod, "CHECK_UNRUNNING_IMAGES", False),
            mock.patch.object(self.mod, "load_state", side_effect=lambda: self.state),
            mock.patch.object(self.mod, "save_state", side_effect=lambda s: setattr(self, "state", s)),
            mock.patch.object(self.mod.docker, "from_env", return_value=FakeClient()),
            mock.patch.object(self.mod, "sweep_local_backups"),
            mock.patch.object(self.mod, "load_hosts", return_value=[]),
        ]
        for p in self.patches:
            p.start()
        self.client = self.mod.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_settings_default_to_off_and_round_trip(self):
        self.assertIs(self.client.get("/api/settings").get_json()["check_unused_images"], False)
        self.assertEqual(self.client.post("/api/settings", json={"check_unused_images": True}).status_code, 200)
        self.assertIs(self.client.get("/api/settings").get_json()["check_unused_images"], True)
        self.client.post("/api/settings", json={"check_unused_images": False})
        self.assertIs(self.client.get("/api/settings").get_json()["check_unused_images"], False)

    def test_unrelated_settings_posts_leave_it_alone(self):
        self.client.post("/api/settings", json={"check_unused_images": True})
        self.client.post("/api/settings", json={"backup_hours": 6})
        self.assertIs(self.client.get("/api/settings").get_json()["check_unused_images"], True)

    def test_the_status_payload_has_no_trace_of_it_while_off(self):
        """Not an empty list — no key at all, so the page can't tell it exists."""
        self.state["unused_images"] = {"postgres:13": {"image": "postgres:13", "has_update": True,
                                                      "checked": True}}   # left over from an earlier run
        data = self.client.get("/api/status").get_json()
        self.assertNotIn("unused_images", data)
        self.assertNotIn("postgres", str(data))

    def test_the_status_payload_lists_them_updates_first_while_on(self):
        self.state["check_unused_images"] = True
        self.state["unused_images"] = {
            "redis:7":     {"image": "redis:7", "has_update": False, "checked": True, "size": 5},
            "postgres:13": {"image": "postgres:13", "has_update": True, "checked": True, "size": 9},
            "odd:1":       {"image": "odd:1", "has_update": False, "checked": False},
        }
        rows = self.client.get("/api/status").get_json()["unused_images"]
        self.assertEqual([(r["image"], r["status"]) for r in rows],
                         [("postgres:13", "update"), ("odd:1", "unknown"), ("redis:7", "ok")])
        self.assertEqual(rows[0]["host_id"], "local")
        self.assertEqual(rows[0]["size"], 9)

    def test_remote_hosts_appear_with_their_name(self):
        self.state["check_unused_images"] = True
        hosts = [{"id": "vps", "name": "VPS", "url": "tcp://vps"}]
        with mock.patch.object(self.mod, "load_hosts", return_value=hosts), \
             mock.patch.object(self.mod, "load_host_state",
                               return_value={"status": "online", "unused_images": {
                                   "mysql:8": {"image": "mysql:8", "has_update": True, "checked": True}}}):
            rows = self.client.get("/api/status").get_json()["unused_images"]
        self.assertEqual([(r["host_id"], r["host_name"], r["image"]) for r in rows],
                         [("vps", "VPS", "mysql:8")])

    def test_enabled_but_never_checked_is_an_empty_list_not_a_missing_key(self):
        self.state["check_unused_images"] = True
        self.assertEqual(self.client.get("/api/status").get_json()["unused_images"], [])

    def _populate(self):
        self.state.update({
            "check_unused_images": True,
            "unused_images": {"postgres:13": {"image": "postgres:13", "has_update": True, "checked": True}},
            "notified": {"local:image:postgres:13": "sha256:n", "local:web": "sha256:w",
                         "vps:image:mysql:8": "sha256:m"},
        })

    def test_switching_it_off_deletes_what_it_stored(self):
        self._populate()
        self.client.post("/api/settings", json={"check_unused_images": False})
        self.assertNotIn("unused_images", self.state)
        self.assertEqual(self.state["notified"], {"local:web": "sha256:w"},
                         "only the container's record should survive")

    def test_switching_it_off_clears_every_remote_host_too(self):
        self._populate()
        hosts = [{"id": "vps", "name": "VPS", "url": "tcp://vps"}, {"id": "nas2", "name": "N", "url": "tcp://n"}]
        stores = {"vps": {"status": "online", "unused_images": {"mysql:8": {}}, "available": {"a": 1}},
                  "nas2": {"status": "online"}}
        saved = {}
        with mock.patch.object(self.mod, "load_hosts", return_value=hosts),              mock.patch.object(self.mod, "load_host_state", side_effect=lambda h: dict(stores[h])),              mock.patch.object(self.mod, "save_host_state", side_effect=lambda h, s: saved.__setitem__(h, s)):
            self.client.post("/api/settings", json={"check_unused_images": False})
        self.assertNotIn("unused_images", saved["vps"])
        self.assertEqual(saved["vps"]["available"], {"a": 1}, "the host's other data must be untouched")
        self.assertNotIn("nas2", saved, "a host with nothing stored needn't be rewritten")

    def test_switching_it_on_or_changing_other_settings_keeps_the_data(self):
        self._populate()
        self.client.post("/api/settings", json={"check_unused_images": True})
        self.client.post("/api/settings", json={"backup_hours": 6})
        self.assertIn("postgres:13", self.state["unused_images"])
        self.assertIn("local:image:postgres:13", self.state["notified"])

    def test_the_page_ships_with_the_tab_and_panel_hidden(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertRegex(html, r'id="tab-images"[^>]*style="display:none"')
        self.assertRegex(html, r'id="images-panel" style="display:none"')


if __name__ == "__main__":
    unittest.main()
