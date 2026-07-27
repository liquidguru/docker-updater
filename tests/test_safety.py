"""Safety tests for the destructive paths.

The fake Docker client is deliberately *stateful*: it keeps a registry keyed by
container name that rename/remove actually mutate, and it refuses a rename onto
a name that is already taken (as real Docker does). A fake built from a fixed
dict lets a rollback test pass while asserting against a container that no
longer holds that name, which hides exactly the bugs these tests exist to catch.
"""

import threading
import unittest
from unittest import mock

from tests.app_loader import load_app_module


class FakeContainer:
    def __init__(self, registry, name, image="img:latest", image_id="sha256:aaa",
                 status="running", health=None, start_fails=False,
                 rename_fails=False, exit_code=0):
        self._reg = registry
        self.name = name
        self.id = "cid-" + name
        self.status = status
        self.image = mock.Mock(id=image_id)
        self._image_name = image
        self.health = health
        self.start_fails = start_fails
        self.rename_fails = rename_fails
        self.exit_code = exit_code
        self.restart_policy = {"Name": "unless-stopped"}
        self.labels = {}
        self.removed = False
        self.start_calls = 0
        self.stop_calls = 0
        self.rename_calls = []

    @property
    def attrs(self):
        state = {"Status": self.status, "ExitCode": self.exit_code}
        if self.health:
            state["Health"] = {"Status": self.health}
        return {
            "Config": {"Image": self._image_name, "Hostname": "abc123456789", "Env": []},
            "HostConfig": {"RestartPolicy": self.restart_policy, "Binds": []},
            "NetworkSettings": {"Networks": {}},
            "Image": self.image.id,
            "State": state,
        }

    def rename(self, new_name):
        self.rename_calls.append(new_name)
        if self.rename_fails:
            raise RuntimeError("simulated rename failure")
        if new_name in self._reg and self._reg[new_name] is not self:
            raise RuntimeError(f"conflict: name {new_name} already in use")
        self._reg.pop(self.name, None)
        self.name = new_name
        self._reg[new_name] = self

    def remove(self, **_):
        self.removed = True
        self._reg.pop(self.name, None)

    def stop(self, **_):
        self.stop_calls += 1
        self.status = "exited"

    def start(self, **_):
        self.start_calls += 1
        if self.start_fails:
            raise RuntimeError("simulated start failure")
        self.status = "running"


class FakeDocker:
    """Minimal stateful stand-in for a docker client."""

    def __init__(self, mod, missing_images=()):
        self.mod = mod
        self.registry = {}
        self.missing_images = set(missing_images)
        self.containers = mock.Mock()
        self.containers.get = self._get
        self.containers.list = self._list
        self.images = mock.Mock()
        self.images.get = self._image_get
        self.api = mock.Mock()
        self.api.update_container = self._update_container

    def new(self, name, **kw):
        c = FakeContainer(self.registry, name, **kw)
        self.registry[name] = c
        return c

    def by_id(self, cid):
        return next((c for c in self.registry.values() if c.id == cid), None)

    def _update_container(self, cid, restart_policy=None, **_):
        c = self.by_id(cid)
        if c is not None and restart_policy is not None:
            c.restart_policy = restart_policy
        return {}

    def _get(self, name):
        if name in self.registry:
            return self.registry[name]
        raise self.mod.docker.errors.NotFound(f"no such container: {name}")

    def _list(self, **_):
        return list(self.registry.values())

    def _image_get(self, ref):
        if ref in self.missing_images:
            raise self.mod.docker.errors.ImageNotFound(f"no such image: {ref}")
        return mock.Mock(id=ref)


class SafetyTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def setUp(self):
        self.mod._update_running.discard("web")
        self.mod._update_logs.pop("web", None)
        self.state = {"rollbacks": {}, "history": [], "available": {},
                      "backup_hours": 24, "backup_enabled": False}

    def tearDown(self):
        self.mod._update_running.discard("web")

    def _run(self, fn, client, *args, **kwargs):
        mod = self.mod
        patches = [
            mock.patch.object(mod, "get_docker_client", return_value=client),
            mock.patch.object(mod, "load_state", side_effect=lambda: self.state),
            mock.patch.object(mod, "save_state",
                              side_effect=lambda s: setattr(self, "state", s)),
            mock.patch.object(mod, "_persist_log"),
            mock.patch.object(mod.time, "sleep"),
        ]
        for p in patches:
            p.start()
        try:
            fn(*args, **kwargs)
        finally:
            for p in reversed(patches):
                p.stop()

    def _logs(self):
        return self.mod._update_logs.get("web", [])


class RollbackPreflightTests(SafetyTestBase):
    """Never damage the live container before a usable backup is proven."""

    def test_missing_backup_leaves_live_container_running(self):
        d = FakeDocker(self.mod)
        live = d.new("web")
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertEqual(live.status, "running")
        self.assertEqual(live.stop_calls, 0, "live container stopped with no backup")
        self.assertFalse(live.removed)
        self.assertIs(d.registry.get("web"), live, "live container lost its name")

    def test_backup_with_missing_image_leaves_live_container_running(self):
        d = FakeDocker(self.mod, missing_images={"sha256:bbb"})
        live = d.new("web")
        d.new("web_old", image_id="sha256:bbb", status="exited")
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertEqual(live.status, "running")
        self.assertEqual(live.stop_calls, 0)
        self.assertIs(d.registry.get("web"), live)


class RollbackCutoverTests(SafetyTestBase):
    """Compensation must leave something serving, and the outgoing container
    must survive until the replacement is actually proven."""

    def test_successful_rollback_promotes_backup_and_removes_outgoing(self):
        d = FakeDocker(self.mod)
        live = d.new("web")
        backup = d.new("web_old", status="exited")
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertIs(d.registry.get("web"), backup, "backup was not promoted")
        self.assertEqual(backup.status, "running")
        self.assertTrue(live.removed, "outgoing container should be cleaned up once up")
        self.assertNotIn("web_rollingback", d.registry, "parked container left behind")

    def test_backup_that_will_not_start_restores_the_outgoing_container(self):
        d = FakeDocker(self.mod)
        live = d.new("web")
        backup = d.new("web_old", status="exited", start_fails=True)
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertIs(d.registry.get("web"), live,
                      "outgoing container must be serving under its own name again")
        self.assertEqual(live.status, "running", "outgoing container left stopped")
        self.assertFalse(live.removed)
        self.assertIs(d.registry.get("web_old"), backup, "backup should be demoted back")

    def test_crash_looping_backup_is_rejected(self):
        """`restarting` used to read as success, which then destroyed the
        known-good container."""
        d = FakeDocker(self.mod)
        live = d.new("web")
        backup = d.new("web_old", status="exited")
        real_start = backup.start

        def start_then_flap(**kw):
            real_start(**kw)
            backup.status = "restarting"

        backup.start = start_then_flap
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertIs(d.registry.get("web"), live,
                      "a crash-looping backup must not keep the name")
        self.assertFalse(live.removed, "known-good container destroyed for a crash loop")
        self.assertEqual(live.status, "running")

    def test_failed_backup_is_stopped_before_being_demoted(self):
        """It still owns its published ports while running, so the container we
        restore would fail to bind if we only renamed it."""
        d = FakeDocker(self.mod)
        d.new("web")
        backup = d.new("web_old", status="exited")
        real_start = backup.start

        def start_then_flap(**kw):
            real_start(**kw)
            backup.status = "restarting"

        backup.start = start_then_flap
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertGreaterEqual(backup.stop_calls, 1,
                                "failed backup must be stopped before demotion")

    def test_demoted_backup_restart_policy_is_reset_to_no(self):
        """Otherwise a reboot could start it alongside the live container."""
        d = FakeDocker(self.mod)
        d.new("web")
        backup = d.new("web_old", status="exited", start_fails=True)
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertEqual(backup.restart_policy, {"Name": "no"})

    def test_failed_promotion_restarts_the_outgoing_container(self):
        d = FakeDocker(self.mod)
        live = d.new("web")
        d.new("web_old", status="exited", rename_fails=True)
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertIs(d.registry.get("web"), live)
        self.assertEqual(live.status, "running",
                         "outgoing container left stopped after a failed promotion")

    def test_unproven_health_keeps_outgoing_container_as_tracked_backup(self):
        d = FakeDocker(self.mod)
        live = d.new("web")
        backup = d.new("web_old", status="exited", health="starting")
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertIs(d.registry.get("web"), backup)
        self.assertFalse(live.removed, "fallback destroyed despite unproven health")
        self.assertIs(d.registry.get("web_old"), live, "fallback should be kept as _old")
        self.assertEqual(live.restart_policy, {"Name": "no"})
        self.assertIn("web", self.state.get("rollbacks", {}),
                      "kept backup must be tracked so it is shown and later expired")


class RollbackRecoveryTests(SafetyTestBase):
    """A crash mid-rollback leaves a parked container a '_old' scan can't see."""

    def _recover(self, d):
        with mock.patch.object(self.mod.docker, "from_env", return_value=d), \
             mock.patch.object(self.mod, "load_state", return_value=self.state), \
             mock.patch.object(self.mod, "save_state"), \
             mock.patch.object(self.mod.time, "sleep"):
            self.mod.recover_interrupted_operations()

    def test_parked_container_is_restored_when_nothing_serves_the_name(self):
        d = FakeDocker(self.mod)
        parked = d.new("web_rollingback", status="exited")
        self._recover(d)
        self.assertIs(d.registry.get("web"), parked,
                      "nothing was restored to the service name")
        self.assertEqual(parked.status, "running")

    def test_parked_container_is_left_alone_when_the_name_is_taken(self):
        """Never fight a container that already holds the name. Asserting that
        no rename was even *attempted* proves the explicit guard ran, rather
        than just proving Docker rejects a collision."""
        d = FakeDocker(self.mod)
        serving = d.new("web", status="running")
        parked = d.new("web_rollingback", status="exited")
        self._recover(d)
        self.assertIs(d.registry.get("web"), serving)
        self.assertIs(d.registry.get("web_rollingback"), parked)
        self.assertEqual(parked.start_calls, 0)
        self.assertEqual(parked.rename_calls, [],
                         "recovery should not even attempt the rename when the "
                         "name is taken")


class PullErrorTests(SafetyTestBase):
    """A streamed pull error must abort before anything is touched."""

    def test_streamed_pull_error_aborts_before_container_change(self):
        d = FakeDocker(self.mod)
        live = d.new("web")
        d.api.pull.return_value = iter([
            {"status": "Pulling from library/web"},
            {"error": "unauthorized: authentication required"},
        ])
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertEqual(live.stop_calls, 0, "container stopped after a failed pull")
        self.assertFalse(live.removed)
        self.assertIs(d.registry.get("web"), live)
        self.assertTrue(any("Pull failed" in l for l in self._logs()),
                        f"expected an explicit pull-failure message: {self._logs()}")


class UpdateHealthTests(SafetyTestBase):
    """The same weak check existed on the update path: a crash-looping
    replacement counted as success and the old container was removed."""

    def _prepare(self, new_status="running", new_health=None):
        d = FakeDocker(self.mod)
        old = d.new("web", image_id="sha256:old")
        d.images.get = lambda ref: mock.Mock(id="sha256:new")
        d.api.pull.return_value = iter([{"status": "Status: Downloaded newer image for web"}])
        d.api.create_host_config.return_value = {}
        d.api.create_container.return_value = {"Id": "cid-new"}
        d.api.create_networking_config.return_value = {}
        d.api.create_endpoint_config.return_value = {}

        def _start(_id):
            old.rename("web_old")
            d.new("web", image_id="sha256:new", status=new_status, health=new_health)

        d.api.start.side_effect = _start
        return d, old

    def test_crash_looping_replacement_is_replaced_by_the_old_container(self):
        """Not just 'the old one survived' — it must actually be serving again,
        and the crash-looping replacement must no longer hold the name."""
        d, old = self._prepare(new_status="restarting")
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertFalse(old.removed,
                         "known-good container removed for a crash-looping replacement")
        self.assertIs(d.registry.get("web"), old,
                      "the old container should be back in service under its own name")
        self.assertEqual(old.status, "running", "the old container was left stopped")

    def test_unproven_health_keeps_the_old_container_as_a_tracked_backup(self):
        d, old = self._prepare(new_status="running", new_health="starting")
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertFalse(old.removed, "fallback removed despite unproven health")
        self.assertIn("web", self.state.get("rollbacks", {}),
                      "kept backup must be tracked so it is shown and later expired")

    def test_healthy_replacement_removes_the_old_container_when_retention_is_off(self):
        d, old = self._prepare(new_status="running", new_health="healthy")
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertTrue(old.removed,
                        "a proven-healthy update with retention off should clean up")


class ContainerIsUpTests(SafetyTestBase):
    def _check(self, **kw):
        d = FakeDocker(self.mod)
        d.new("web", **kw)
        with mock.patch.object(self.mod.time, "sleep"):
            return self.mod._container_is_up(d, "web", checks=2, interval=0)

    def test_restarting_is_a_failure(self):
        ok, health_ok, why = self._check(status="restarting")
        self.assertFalse(ok)
        self.assertIn("restarting", why)

    def test_exited_is_a_failure(self):
        ok, _, _ = self._check(status="exited", exit_code=1)
        self.assertFalse(ok)

    def test_unhealthy_is_a_failure(self):
        ok, _, why = self._check(status="running", health="unhealthy")
        self.assertFalse(ok)
        self.assertIn("unhealthy", why)

    def test_healthy_is_confirmed(self):
        ok, health_ok, _ = self._check(status="running", health="healthy")
        self.assertTrue(ok)
        self.assertTrue(health_ok)

    def test_running_without_a_healthcheck_is_confirmed(self):
        ok, health_ok, _ = self._check(status="running")
        self.assertTrue(ok)
        self.assertTrue(health_ok)

    def test_still_starting_is_up_but_unconfirmed(self):
        ok, health_ok, _ = self._check(status="running", health="starting")
        self.assertTrue(ok, "a container whose healthcheck is starting is serving")
        self.assertFalse(health_ok, "must not count as proven")

    def test_missing_container_is_a_failure(self):
        d = FakeDocker(self.mod)
        ok, _, _ = self.mod._container_is_up(d, "web", checks=1)
        self.assertFalse(ok)

    def test_a_container_that_dies_between_checks_is_caught(self):
        d = FakeDocker(self.mod)
        c = d.new("web", status="running")
        calls = {"n": 0}
        original = d._get

        def flaky(name):
            calls["n"] += 1
            if calls["n"] > 1:
                c.status = "exited"
            return original(name)

        d.containers.get = flaky
        with mock.patch.object(self.mod.time, "sleep"):
            ok, _, _ = self.mod._container_is_up(d, "web", checks=3, interval=0)
        self.assertFalse(ok, "a container that exits after the first check must fail")


class OperationReservationTests(SafetyTestBase):
    """The claim must be atomic with the check."""

    def test_second_reservation_is_refused(self):
        self.assertIsNotNone(self.mod._reserve_operation("web", "local"))
        self.assertIsNone(self.mod._reserve_operation("web", "local"))

    def test_concurrent_reservations_yield_exactly_one_winner(self):
        winners = []
        barrier = threading.Barrier(12)

        def contend():
            barrier.wait()
            if self.mod._reserve_operation("web", "local") is not None:
                winners.append(1)

        threads = [threading.Thread(target=contend) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1, f"expected one winner, got {len(winners)}")

    def test_release_allows_a_later_operation(self):
        key = self.mod._reserve_operation("web", "local")
        self.mod._release_operation(key)
        self.assertIsNotNone(self.mod._reserve_operation("web", "local"))


class SelfUpdateBackupReservationTests(SafetyTestBase):
    """The replacement updater runs its own startup recovery while the helper is
    still verifying it. With retention off and no rollback entry, that recovery
    deletes `_old` as an orphan — the very container the helper may need."""

    def test_reservation_stops_startup_recovery_deleting_the_fallback(self):
        d = FakeDocker(self.mod)
        d.new("web", status="running")          # the replacement, already up
        backup = d.new("web_old", status="exited")
        self.state["backup_enabled"] = False    # the dangerous configuration

        # What the helper now does before starting the replacement.
        with mock.patch.object(self.mod.os.path, "exists", return_value=True), \
             mock.patch.object(self.mod, "load_state", return_value=self.state), \
             mock.patch.object(self.mod, "save_state",
                               side_effect=lambda s: setattr(self, "state", s)):
            self.mod._helper_reserve_backup("web", 24, {"Name": "unless-stopped"})
        self.assertIn("web", self.state.get("rollbacks", {}),
                      "helper should reserve the backup before starting the replacement")

        # Now the replacement boots and runs recovery.
        with mock.patch.object(self.mod.docker, "from_env", return_value=d), \
             mock.patch.object(self.mod, "load_state", return_value=self.state), \
             mock.patch.object(self.mod, "save_state"), \
             mock.patch.object(self.mod.time, "sleep"):
            self.mod.recover_interrupted_operations()

        self.assertFalse(backup.removed,
                         "startup recovery deleted the fallback mid-verification")
        self.assertIn("web_old", d.registry)

    def test_without_a_reservation_the_fallback_is_removed(self):
        """Confirms the above test is actually exercising the reservation and
        not passing for some unrelated reason."""
        d = FakeDocker(self.mod)
        d.new("web", status="running")
        backup = d.new("web_old", status="exited")
        self.state["backup_enabled"] = False
        self.state["rollbacks"] = {}            # no reservation
        with mock.patch.object(self.mod.docker, "from_env", return_value=d), \
             mock.patch.object(self.mod, "load_state", return_value=self.state), \
             mock.patch.object(self.mod, "save_state"), \
             mock.patch.object(self.mod.time, "sleep"):
            self.mod.recover_interrupted_operations()
        self.assertTrue(backup.removed,
                        "unreserved orphan should still be cleaned up (baseline)")


class ImageReferenceParsingTests(SafetyTestBase):
    """Docker Hub can be spelled several ways, but only registry-1.docker.io
    serves the v2 API. Getting this wrong made the digest check fail, and the
    container was then dropped from the update list entirely (issue #19)."""

    def test_dockerhub_aliases_normalise_to_the_api_host(self):
        for ref in ("searxng/searxng:latest",
                    "docker.io/searxng/searxng:latest",
                    "index.docker.io/searxng/searxng:latest",
                    "registry.hub.docker.com/searxng/searxng:latest",
                    "registry-1.docker.io/searxng/searxng:latest"):
            registry, repo, tag = self.mod.parse_image(ref)
            self.assertEqual(registry, "registry-1.docker.io", ref)
            self.assertEqual(repo, "searxng/searxng", ref)
            self.assertEqual(tag, "latest", ref)

    def test_official_images_get_the_library_namespace_for_every_spelling(self):
        for ref in ("nginx:latest", "docker.io/nginx:latest",
                    "index.docker.io/nginx:latest"):
            registry, repo, _ = self.mod.parse_image(ref)
            self.assertEqual(registry, "registry-1.docker.io", ref)
            self.assertEqual(repo, "library/nginx", ref)

    def test_library_namespace_is_not_doubled(self):
        _, repo, _ = self.mod.parse_image("docker.io/library/nginx:latest")
        self.assertEqual(repo, "library/nginx")

    def test_other_registries_are_left_alone(self):
        cases = {
            "ghcr.io/home-assistant/home-assistant:stable":
                ("ghcr.io", "home-assistant/home-assistant", "stable"),
            "lscr.io/linuxserver/calibre-web:latest":
                ("lscr.io", "linuxserver/calibre-web", "latest"),
            "localhost:5000/myapp:1.0": ("localhost:5000", "myapp", "1.0"),
            "registry.example.com/team/app:v2":
                ("registry.example.com", "team/app", "v2"),
        }
        for ref, expected in cases.items():
            self.assertEqual(self.mod.parse_image(ref), expected, ref)

    def test_no_library_namespace_added_for_other_registries(self):
        """`localhost:5000/myapp` must not become `library/myapp`."""
        _, repo, _ = self.mod.parse_image("localhost:5000/myapp:1.0")
        self.assertEqual(repo, "myapp")

    def test_tag_defaults_and_ports_do_not_confuse_the_split(self):
        self.assertEqual(self.mod.parse_image("nginx"),
                         ("registry-1.docker.io", "library/nginx", "latest"))
        self.assertEqual(self.mod.parse_image("localhost:5000/myapp"),
                         ("localhost:5000", "myapp", "latest"))

    def test_credentials_follow_every_dockerhub_spelling(self):
        with mock.patch.object(self.mod, "DOCKERHUB_USERNAME", "user"), \
             mock.patch.object(self.mod, "DOCKERHUB_TOKEN", "tok"):
            for host in ("registry-1.docker.io", "docker.io", "index.docker.io",
                         "registry.hub.docker.com"):
                self.assertEqual(self.mod._registry_credentials(host),
                                 ("user", "tok"), host)


class RegistryAuthTests(SafetyTestBase):
    """Docker Hub credentials for digest checks (issue #17)."""

    def test_credentials_are_used_for_docker_hub(self):
        with mock.patch.object(self.mod, "DOCKERHUB_USERNAME", "user"), \
             mock.patch.object(self.mod, "DOCKERHUB_TOKEN", "tok"):
            self.assertEqual(
                self.mod._registry_credentials("registry-1.docker.io"), ("user", "tok"))
            self.assertEqual(
                self.mod._registry_credentials("index.docker.io"), ("user", "tok"))

    def test_credentials_are_not_sent_to_other_registries(self):
        with mock.patch.object(self.mod, "DOCKERHUB_USERNAME", "user"), \
             mock.patch.object(self.mod, "DOCKERHUB_TOKEN", "tok"):
            self.assertIsNone(self.mod._registry_credentials("ghcr.io"))
            self.assertIsNone(self.mod._registry_credentials("lscr.io"))
            self.assertIsNone(self.mod._registry_credentials("registry.example.com"))

    def test_partial_credentials_are_ignored(self):
        with mock.patch.object(self.mod, "DOCKERHUB_USERNAME", "user"), \
             mock.patch.object(self.mod, "DOCKERHUB_TOKEN", ""):
            self.assertIsNone(self.mod._registry_credentials("registry-1.docker.io"))

    def test_token_request_presents_credentials(self):
        captured = {}

        def fake_get(url, params=None, timeout=None, auth=None, **kw):
            captured["auth"] = auth
            return mock.Mock(status_code=200, json=lambda: {"token": "abc"})

        with mock.patch.object(self.mod, "DOCKERHUB_USERNAME", "user"), \
             mock.patch.object(self.mod, "DOCKERHUB_TOKEN", "tok"), \
             mock.patch.object(self.mod.requests, "get", side_effect=fake_get):
            token = self.mod._token_from_challenge(
                'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"',
                "registry-1.docker.io")
        self.assertEqual(token, "abc")
        self.assertEqual(captured["auth"], ("user", "tok"))

    def test_rejected_credentials_fall_back_to_anonymous(self):
        """A bad token must not break update checks entirely."""
        calls = []

        def fake_get(url, params=None, timeout=None, auth=None, **kw):
            calls.append(auth)
            if auth is not None:
                return mock.Mock(status_code=401, json=lambda: {})
            return mock.Mock(status_code=200, json=lambda: {"token": "anon"})

        with mock.patch.object(self.mod, "DOCKERHUB_USERNAME", "user"), \
             mock.patch.object(self.mod, "DOCKERHUB_TOKEN", "bad"), \
             mock.patch.object(self.mod.requests, "get", side_effect=fake_get):
            token = self.mod._token_from_challenge(
                'Bearer realm="https://auth.docker.io/token"', "registry-1.docker.io")
        self.assertEqual(token, "anon", "should retry anonymously after a 401")
        self.assertEqual(calls, [("user", "bad"), None])

    def test_secret_is_never_logged(self):
        import io as _io
        from contextlib import redirect_stdout

        def fake_get(url, params=None, timeout=None, auth=None, **kw):
            return mock.Mock(status_code=401, json=lambda: {})

        buf = _io.StringIO()
        with mock.patch.object(self.mod, "DOCKERHUB_USERNAME", "user"), \
             mock.patch.object(self.mod, "DOCKERHUB_TOKEN", "sup3rsecret"), \
             mock.patch.object(self.mod.requests, "get", side_effect=fake_get), \
             redirect_stdout(buf):
            self.mod._token_from_challenge(
                'Bearer realm="https://auth.docker.io/token"', "registry-1.docker.io")
        self.assertNotIn("sup3rsecret", buf.getvalue())

    def test_rate_limit_header_is_recorded(self):
        self.mod._rate_limit_seen.clear()
        resp = mock.Mock(headers={"RateLimit-Remaining": "76;w=21600",
                                  "RateLimit-Limit": "100;w=21600"})
        self.mod._note_rate_limit("registry-1.docker.io", resp)
        self.assertEqual(self.mod._rate_limit_seen["registry-1.docker.io"], "76/100")

    def test_missing_rate_limit_header_is_ignored(self):
        self.mod._rate_limit_seen.clear()
        self.mod._note_rate_limit("ghcr.io", mock.Mock(headers={}))
        self.assertEqual(self.mod._rate_limit_seen, {})


class HelperContainerFilterTests(SafetyTestBase):
    def test_helper_suffixes_are_recognised(self):
        self.assertTrue(self.mod._is_helper_container("web_old"))
        self.assertTrue(self.mod._is_helper_container("web_rollingback"))
        self.assertFalse(self.mod._is_helper_container("web"))
        self.assertFalse(self.mod._is_helper_container("oldschool"))

    def test_parked_container_is_excluded_from_update_scanning(self):
        """Proves the call site uses the filter, not just that the filter works."""
        d = FakeDocker(self.mod)
        d.new("web")
        d.new("web_rollingback", status="exited")
        d.new("web_old", status="exited")
        with mock.patch.object(self.mod, "get_local_digest", return_value="sha256:x"), \
             mock.patch.object(self.mod, "get_remote_digest", return_value="sha256:x"), \
             mock.patch.object(self.mod, "is_locally_built", return_value=False):
            available = self.mod._scan_host(d, "local")
        self.assertIn("web", available)
        self.assertNotIn("web_rollingback", available,
                         "a parked container must not be treated as a managed service")
        self.assertNotIn("web_old", available)


if __name__ == "__main__":
    unittest.main()
