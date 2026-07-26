"""Safety tests for the destructive paths (AUDIT_RECOMMENDATIONS P0 1-3).

Failure injection for the cases that actually matter: a rollback with no usable
backup must not take the live container down, a failed pull must not lead to the
container being replaced, and concurrent requests must not both start a worker.
"""

import threading
import unittest
from unittest import mock

from tests.app_loader import load_app_module


class _FakeContainer:
    def __init__(self, name, image="img:latest", image_id="sha256:aaa", status="running"):
        self.name = name
        self.id = "cid-" + name
        self.status = status
        self.image = mock.Mock(id=image_id)
        self.attrs = {
            "Config": {"Image": image, "Hostname": "abc123456789", "Env": []},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "Binds": []},
            "NetworkSettings": {"Networks": {}},
            "Image": image_id,
        }
        self.stopped = False
        self.removed = False
        self.was_started = False
        self.renamed_to = []

    def stop(self, **_):
        self.stopped = True

    def remove(self, **_):
        self.removed = True

    def start(self, **_):
        self.was_started = True

    def rename(self, new_name):
        self.renamed_to.append(new_name)
        self.name = new_name


class SafetyTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()
        cls.docker = cls.mod.docker

    def setUp(self):
        self.mod._update_running.discard("web")
        self.mod._update_logs.pop("web", None)

    def tearDown(self):
        self.mod._update_running.discard("web")


class RollbackPreflightTests(SafetyTestBase):
    """P0.1 — never destroy the live container before confirming a usable backup."""

    def _run_rollback(self, containers, image_missing=False):
        mod = self.mod

        def _get(name):
            if name in containers:
                return containers[name]
            raise self.docker.errors.NotFound(f"no such container {name}")

        client = mock.Mock()
        client.containers.get.side_effect = _get
        if image_missing:
            client.images.get.side_effect = self.docker.errors.ImageNotFound("gone")
        else:
            client.images.get.return_value = mock.Mock(id="sha256:bbb")

        with mock.patch.object(mod, "get_docker_client", return_value=client), \
             mock.patch.object(mod, "load_state",
                               return_value={"rollbacks": {}, "history": []}), \
             mock.patch.object(mod, "save_state"), \
             mock.patch.object(mod, "_persist_log"), \
             mock.patch.object(mod.time, "sleep"):
            mod.apply_rollback("web", "local", reserved=True)

    def test_missing_backup_leaves_live_container_untouched(self):
        live = _FakeContainer("web")
        self._run_rollback({"web": live})
        self.assertFalse(live.stopped, "live container stopped with no backup present")
        self.assertFalse(live.removed, "live container REMOVED with no backup present")

    def test_backup_with_missing_image_leaves_live_container_untouched(self):
        live = _FakeContainer("web")
        self._run_rollback(
            {"web": live, "web_old": _FakeContainer("web_old")}, image_missing=True
        )
        self.assertFalse(live.stopped, "live container stopped though backup image missing")
        self.assertFalse(live.removed, "live container removed though backup image missing")

    def test_live_container_is_preserved_during_cutover(self):
        """On the happy path the outgoing container is renamed aside, not
        deleted outright, so it can be restored if the backup won't start."""
        live = _FakeContainer("web")
        self._run_rollback({"web": live, "web_old": _FakeContainer("web_old")})
        self.assertIn("web_rollingback", live.renamed_to,
                      "outgoing container should be renamed aside for recovery")


class PullErrorTests(SafetyTestBase):
    """P0.3 — an error inside the pull stream must abort before any change."""

    def test_streamed_pull_error_aborts_before_container_change(self):
        mod = self.mod
        live = _FakeContainer("web")
        client = mock.Mock()
        client.containers.get.return_value = live
        client.api.pull.return_value = iter([
            {"status": "Pulling from library/web"},
            {"error": "unauthorized: authentication required"},
        ])

        with mock.patch.object(mod, "get_docker_client", return_value=client), \
             mock.patch.object(mod, "load_state",
                               return_value={"history": [], "available": {}}), \
             mock.patch.object(mod, "save_state"), \
             mock.patch.object(mod, "_persist_log"), \
             mock.patch.object(mod.time, "sleep"):
            mod.apply_update("web", "local", reserved=True)

        self.assertFalse(live.stopped, "container stopped after a failed pull")
        self.assertFalse(live.removed, "container removed after a failed pull")
        logs = mod._update_logs.get("web", [])
        self.assertTrue(any("Pull failed" in line for line in logs),
                        f"expected an explicit pull-failure message, got: {logs}")


class OperationReservationTests(SafetyTestBase):
    """P0.2 — the claim must be atomic with the check."""

    def test_second_reservation_is_refused(self):
        self.assertIsNotNone(self.mod._reserve_operation("web", "local"))
        self.assertIsNone(self.mod._reserve_operation("web", "local"),
                          "a second reservation should be refused")

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
        self.assertEqual(len(winners), 1,
                         f"expected exactly one winner, got {len(winners)}")

    def test_release_allows_a_later_operation(self):
        key = self.mod._reserve_operation("web", "local")
        self.mod._release_operation(key)
        self.assertIsNotNone(self.mod._reserve_operation("web", "local"))


if __name__ == "__main__":
    unittest.main()
