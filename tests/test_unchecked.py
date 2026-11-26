"""Why a container is unchecked, and not putting one there by mistake.

An updated container used to lose its check result, so a local one showed as
"Not Checked" until the next scan; a digest-pinned one said the same forever;
and a remote container whose registry lookup failed vanished from every tab.
"""

from unittest import mock

from tests.test_safety import FakeDocker, SafetyTestBase

PINNED = "ghcr.io/me/app@sha256:" + "a" * 64


def _scanned(image="nginx:alpine", repo_digests=("nginx@sha256:old",), name="web"):
    class Img:
        attrs = {"RepoDigests": list(repo_digests), "RepoTags": [image],
                 "Id": "sha256:" + "f" * 64, "Os": "linux", "Architecture": "amd64"}

    class C:
        labels = {}
        attrs = {"Config": {"Image": image}}

    C.name = name
    C.image = Img()
    return C()


class ScanReasonTests(SafetyTestBase):
    def _scan(self, container, **lookups):
        mod = self.mod
        client = mock.Mock()
        client.containers.list.return_value = [container]
        with mock.patch.object(mod, "load_state", return_value={}), \
             mock.patch.object(mod, "CHECK_IMAGES_WITHOUT_DIGEST", False), \
             mock.patch.object(mod, "get_remote_digest", **lookups) as digest:
            return mod._scan_host(client, "local"), digest

    def test_pinned_image_is_recorded_without_asking_the_registry(self):
        for ref in (PINNED, "nginx:alpine@sha256:" + "b" * 64):
            with self.subTest(ref=ref):
                found, digest = self._scan(_scanned(ref))
                self.assertEqual(found["web"]["unchecked_reason"], "pinned_digest")
                self.assertFalse(found["web"]["has_update"])
                digest.assert_not_called()

    def test_failed_lookup_keeps_the_container(self):
        self.mod._lookup_failures.clear()
        found, _ = self._scan(_scanned(), return_value=None)
        self.assertEqual(found["web"]["unchecked_reason"], "lookup_failed")
        self.assertFalse(found["web"]["has_update"])
        self.assertIsNone(found["web"]["checked_at"])

    def test_registry_status_gives_the_reason(self):
        mod = self.mod
        for status, reason in ((404, "not_found"), (403, "not_found"),
                               (429, "rate_limited"), (500, "lookup_failed")):
            with self.subTest(status=status):
                reply = mock.Mock(status_code=status, headers={})
                client = mock.Mock()
                client.containers.list.return_value = [_scanned()]
                with mock.patch.object(mod, "load_state", return_value={}), \
                     mock.patch.object(mod.requests, "head", return_value=reply, create=True):
                    found = mod._scan_host(client, "local")
                self.assertEqual(found["web"]["unchecked_reason"], reason)

    def test_an_earlier_failure_does_not_stick(self):
        mod = self.mod
        mod._lookup_failures["nginx:alpine"] = "rate_limited"
        reply = mock.Mock(status_code=500, headers={})
        with mock.patch.object(mod.requests, "head", return_value=reply, create=True):
            self.assertIsNone(mod.get_remote_digest("nginx:alpine", "sha256:old"))
        self.assertNotIn("nginx:alpine", mod._lookup_failures)

    def test_unreadable_local_image_is_kept(self):
        mod = self.mod
        client = mock.Mock()
        c = _scanned(repo_digests=())
        c.image.attrs = {}
        client.containers.list.return_value = [c]
        with mock.patch.object(mod, "load_state", return_value={}), \
             mock.patch.object(mod, "get_remote_image_id", return_value=None):
            found = mod._scan_host(client, "local")
        self.assertEqual(found["web"]["unchecked_reason"], "no_local_image")


class StatusReasonTests(SafetyTestBase):
    def test_pinned_is_shown_at_once_locally_and_remotely(self):
        """Before any scan, and for a remote cache written by an older version."""
        mod = self.mod
        self.state["deferred"] = {}
        client = mock.Mock()
        client.containers.list.return_value = [_scanned(PINNED)]
        host_state = {"available": {"far": {"image": PINNED, "has_update": False}}}
        with mock.patch.object(mod.docker, "from_env", return_value=client), \
             mock.patch.object(mod, "AUTH_ENABLED", False), \
             mock.patch.object(mod, "load_state", return_value=self.state), \
             mock.patch.object(mod, "load_hosts",
                               return_value=[{"id": "nas", "name": "NAS"}]), \
             mock.patch.object(mod, "load_host_state", return_value=host_state), \
             mock.patch.object(mod, "get_notify_info", return_value=None), \
             mock.patch.object(mod, "sweep_local_backups"):
            response = mod.app.test_client().get("/api/status")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(
            sorted((r["name"], r["status"], r["unchecked_reason"])
                   for r in response.get_json()["containers"]),
            [("far", "unknown", "pinned_digest"), ("web", "unknown", "pinned_digest")])


def _result(**extra):
    return {"image": "img:latest", "has_update": True, "local_digest": "sha256:old",
            "remote_digest": "sha256:new", "checked_at": "2026-01-01T00:00:00Z",
            "download_size": 123, **extra}


class AfterUpdateTests(SafetyTestBase):
    def test_updated_local_container_is_up_to_date_not_unchecked(self):
        d = FakeDocker(self.mod)
        d.new("web")
        self.state["available"]["web"] = _result()
        d.api.pull.return_value = iter([{"status": "Pulling from library/web"}])

        def create(**kw):
            d.new("web", health="healthy")
            return {"Id": "cid-web"}

        d.api.create_container.side_effect = create
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertEqual(self.state["history"][0]["status"], "success", self._logs())
        entry = self.state["available"]["web"]
        self.assertFalse(entry["has_update"])
        self.assertEqual(entry["local_digest"], "sha256:new")
        self.assertNotEqual(entry["checked_at"], "2026-01-01T00:00:00Z")
        self.assertIsNone(entry["download_size"])

    def test_self_update_success_and_failure(self):
        mod = self.mod
        for status, has_update in (("success", False), ("error: boom", True)):
            with self.subTest(status=status):
                self.state["available"] = {"web": _result()}
                with mock.patch.object(mod, "load_state", side_effect=lambda: self.state), \
                     mock.patch.object(mod, "save_state"), \
                     mock.patch.object(mod.os.path, "exists", return_value=True):
                    mod._helper_write_state("web", {"status": status}, None)
                self.assertEqual(self.state["available"]["web"]["has_update"], has_update)

    def test_container_never_checked_is_not_declared_current(self):
        store = {"available": {"web": {"image": "img", "has_update": False,
                                       "unchecked_reason": "lookup_failed",
                                       "checked_at": None}}}
        self.mod._mark_current(store, "web")
        self.assertEqual(store["available"]["web"]["unchecked_reason"], "lookup_failed")
        self.assertIsNone(store["available"]["web"]["checked_at"])
        empty = {"available": {}}
        self.mod._mark_current(empty, "web")
        self.assertEqual(empty["available"], {})

    def test_rollback_does_not_leave_it_looking_up_to_date(self):
        d = FakeDocker(self.mod)
        d.new("web")
        d.new("web_old", status="exited")
        self.state["available"]["web"] = _result(has_update=False)
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        entry = self.state["available"]["web"]
        self.assertEqual(entry["unchecked_reason"], "rolled_back")
        self.assertIsNone(entry["checked_at"])

    def test_update_after_a_rollback_clears_the_rolled_back_reason(self):
        store = {"available": {"web": _result()}}
        self.mod._mark_rolled_back(store, "web")
        self.mod._mark_current(store, "web")
        self.assertNotIn("unchecked_reason", store["available"]["web"])
