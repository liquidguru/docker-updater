"""Pull progress accounting and API lifecycle tests without a Docker daemon."""

import threading
import unittest
from contextlib import closing
from unittest import mock

from tests.app_loader import load_app_module
from tests.test_safety import FakeDocker, SafetyTestBase


class PullProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def setUp(self):
        self.mod.AUTH_ENABLED = False
        self.mod.app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
        self.mod._active_pulls.clear()
        self.client = self.mod.app.test_client()

    def pulls(self):
        response = self.client.get("/api/pulls")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.get_json()["pulls"]

    def stream(self, chunks, host="local", container="web"):
        client = mock.Mock()
        client.api.pull.return_value = iter(chunks)
        return self.mod._pull_image(client, "example/web:latest", container, host)

    def test_empty_and_authentication(self):
        self.assertEqual(self.pulls(), [])
        self.mod.AUTH_ENABLED = True
        self.assertEqual(self.client.get("/api/pulls").status_code, 401)
        with self.client.session_transaction() as session:
            session["authenticated"] = True
        self.assertEqual(self.pulls(), [])

    def test_aggregates_layers_and_does_not_count_extraction_twice(self):
        chunks = [
            {"id": "a", "status": "Downloading", "progressDetail": {"current": 25, "total": 100}},
            {"id": "a", "status": "Downloading", "progressDetail": {"current": 50, "total": 100}},
            {"id": "b", "status": "Downloading", "progressDetail": {"current": 30, "total": 300}},
            {"id": "a", "status": "Download complete"},
            {"id": "a", "status": "Extracting", "progressDetail": {"current": 1000, "total": 5000}},
            {"id": "cached", "status": "Already exists"},
            {"id": "b", "status": "Pull complete"},
        ]
        with closing(self.stream(chunks)) as stream:
            expected = [(25, 100, 25.0), (50, 100, 50.0), (80, 400, 20.0),
                        (130, 400, 32.5), (130, 400, 32.5), (130, 400, 32.5),
                        (400, 400, 100.0)]
            for progress, total, percent in expected:
                next(stream)
                pull, = self.pulls()
                self.assertEqual(
                    (pull["progress_bytes"], pull["total_bytes"], pull["percent_complete"]),
                    (progress, total, percent),
                )
            self.assertEqual(pull["id"], "web")
            self.assertEqual(pull["image"], "example/web:latest")
            self.assertEqual(pull["container"], "web")
            self.assertEqual(pull["host_id"], "local")
            self.assertTrue(pull["started_at"].endswith("Z"))
            self.assertEqual(list(stream), [])
            self.assertEqual(self.pulls(), [])

    def test_unknown_layer_sizes_are_null_until_reported(self):
        chunks = [
            {"status": "Pulling from library/web", "id": "latest"},
            {"id": "a", "status": "Pulling fs layer"},
            {"id": "b", "status": "Waiting"},
            {"id": "a", "status": "Downloading", "progressDetail": {"current": 10, "total": 100}},
            {"id": "b", "status": "Already exists"},
        ]
        with closing(self.stream(chunks)) as stream:
            for _ in range(4):
                next(stream)
                pull, = self.pulls()
                self.assertIsNone(pull["total_bytes"])
                self.assertIsNone(pull["percent_complete"])
            self.assertEqual(pull["progress_bytes"], 10)
            next(stream)
            pull, = self.pulls()
            self.assertEqual(pull["total_bytes"], 100)
            self.assertEqual(pull["percent_complete"], 10.0)

    def test_cached_image_needs_zero_download_bytes(self):
        with closing(self.stream([{"id": "a", "status": "Already exists"}])) as stream:
            next(stream)
            pull, = self.pulls()
            self.assertEqual(pull["progress_bytes"], 0)
            self.assertEqual(pull["total_bytes"], 0)
            self.assertEqual(pull["percent_complete"], 100.0)

    def test_missing_details_and_overreported_current(self):
        chunks = [
            {"id": "a", "status": "Downloading", "progressDetail": {}},
            {"id": "a", "status": "Downloading", "progressDetail": {"current": 200, "total": 100}},
        ]
        with closing(self.stream(chunks)) as stream:
            next(stream)
            self.assertIsNone(self.pulls()[0]["total_bytes"])
            next(stream)
            pull, = self.pulls()
            self.assertEqual(pull["progress_bytes"], 100)
            self.assertEqual(pull["percent_complete"], 100.0)

    def test_same_image_on_different_hosts_is_tracked_separately(self):
        chunks = [{"id": "a", "status": "Downloading",
                   "progressDetail": {"current": 10, "total": 100}}]
        with closing(self.stream(chunks)) as local, closing(self.stream(chunks, "remote")) as remote:
            next(local)
            next(remote)
            self.assertEqual({p["id"] for p in self.pulls()}, {"web", "remote:web"})
            local.close()
            self.assertEqual([p["host_id"] for p in self.pulls()], ["remote"])
        self.assertEqual(self.pulls(), [])

    def test_stream_exception_and_initial_failure_remove_pull(self):
        def broken_stream():
            yield {"id": "a", "status": "Pulling fs layer"}
            raise RuntimeError("connection lost")

        client = mock.Mock()
        client.api.pull.return_value = broken_stream()
        stream = self.mod._pull_image(client, "web:latest", "web", "local")
        next(stream)
        self.assertEqual(len(self.pulls()), 1)
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            next(stream)
        self.assertEqual(self.pulls(), [])
        client.api.pull.side_effect = RuntimeError("cannot connect")
        with self.assertRaisesRegex(RuntimeError, "cannot connect"):
            next(self.mod._pull_image(client, "web:latest", "web", "local"))
        self.assertEqual(self.pulls(), [])

    def test_same_image_for_two_containers_is_tracked_separately(self):
        chunks = [{"id": "a", "status": "Downloading",
                   "progressDetail": {"current": 1, "total": 3}}]
        with closing(self.stream(chunks)) as web, \
             closing(self.stream(chunks, container="worker")) as worker:
            next(web)
            next(worker)
            pulls = self.pulls()
            self.assertEqual({p["container"] for p in pulls}, {"web", "worker"})
            self.assertEqual({p["percent_complete"] for p in pulls}, {33.33})
        self.assertEqual(self.pulls(), [])

    def test_api_sees_pull_while_docker_waits_for_first_event(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_pull(*_args, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timed out waiting for release")
            return iter([])

        client = mock.Mock()
        client.api.pull.side_effect = blocked_pull
        worker = threading.Thread(
            target=lambda: list(self.mod._pull_image(client, "web:latest", "web", "local")),
        )
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            pull, = self.pulls()
            self.assertEqual(pull["progress_bytes"], 0)
            self.assertIsNone(pull["total_bytes"])
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.pulls(), [])


class UpdatePullProgressTests(SafetyTestBase):
    def test_finished_pull_is_removed_before_image_inspection_or_recreation(self):
        d = FakeDocker(self.mod)
        live = d.new("web")
        d.api.pull.return_value = iter([{"id": "a", "status": "Already exists"}])
        inspected = []

        def inspect_image(_name):
            self.assertEqual(self.mod._active_pulls, {})
            inspected.append(True)
            raise self.mod.docker.errors.ImageNotFound("test: stop before recreation")

        d.images.get = inspect_image
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertEqual(inspected, [True])
        self.assertEqual(live.stop_calls, 0)

    def test_cancel_closes_pull_and_removes_progress_without_touching_container(self):
        d = FakeDocker(self.mod)
        live = d.new("web")
        closed = []
        snapshots = []

        def chunks():
            try:
                yield {"id": "a", "status": "Downloading",
                       "progressDetail": {"current": 25, "total": 100}}
                snapshots.append(self.mod.app.test_client().get("/api/pulls").get_json())
                self.mod._update_cancel.add("web")
                yield {"id": "a", "status": "Downloading",
                       "progressDetail": {"current": 50, "total": 100}}
                self.fail("pull continued after cancellation")
            finally:
                closed.append(True)

        self.mod.AUTH_ENABLED = False
        d.api.pull.return_value = chunks()
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertEqual(snapshots[0]["pulls"][0]["progress_bytes"], 25)
        self.assertEqual(closed, [True])
        self.assertEqual(self.mod._active_pulls, {})
        self.assertEqual(live.stop_calls, 0)
        self.assertIs(d.registry["web"], live)

    def test_streamed_error_removes_progress_without_touching_container(self):
        d = FakeDocker(self.mod)
        live = d.new("web")
        d.api.pull.return_value = iter([
            {"id": "a", "status": "Downloading",
             "progressDetail": {"current": 25, "total": 100}},
            {"error": "unauthorized"},
        ])
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertEqual(self.mod._active_pulls, {})
        self.assertEqual(live.stop_calls, 0)
        self.assertTrue(any("Pull failed" in line for line in self._logs()))


if __name__ == "__main__":
    unittest.main()
