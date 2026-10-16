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


class PullEstimateTests(SafetyTestBase):
    """Docker downloads a few layers at a time and a layer only reports its size
    once it starts, so the stream alone gave no percentage for the first 57% of
    a real 8-layer pull. The checker's stored download_size fills that gap."""

    EST = 1000

    def setUp(self):
        super().setUp()
        self.mod.AUTH_ENABLED = False
        self.mod.app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
        self.mod._active_pulls.clear()

    def _entry(self, chunks, expected: int | None = EST, nth=1):
        """Feed `chunks` through the tracker and return the API entry after the
        nth chunk, the way a polling client would see it."""
        client = mock.Mock()
        client.api.pull.return_value = iter(chunks)
        stream = self.mod._pull_image(client, "img:latest", "web", "local", expected)
        with closing(stream):
            for _ in range(nth):
                next(stream)
            return self.mod.app.test_client().get("/api/pulls").get_json()["pulls"][0]

    def test_estimate_is_used_while_later_layers_have_not_reported_a_size(self):
        pull = self._entry([
            {"id": "a", "status": "Downloading", "progressDetail": {"current": 100, "total": 400}},
            {"id": "b", "status": "Waiting"},          # queued: size not known yet
        ], nth=2)
        self.assertEqual(pull["total_bytes"], self.EST)
        self.assertEqual(pull["progress_bytes"], 100)
        self.assertEqual(pull["percent_complete"], 10.0)
        self.assertTrue(pull["total_estimated"])

    def test_entry_shows_the_estimate_before_docker_sends_anything(self):
        """Without it the first poll said null even though we knew the size.
        The peek has to happen while the stream is still silent: once a chunk
        arrives the estimate is computed anyway, which hides a missing seed."""
        seen = []

        def silent_then_one_chunk():
            seen.append(self.mod.app.test_client().get("/api/pulls").get_json()["pulls"][0])
            yield {"id": "a", "status": "Waiting"}

        client = mock.Mock()
        client.api.pull.return_value = silent_then_one_chunk()
        with closing(self.mod._pull_image(client, "img:latest", "web", "local", self.EST)) as stream:
            next(stream)
        pull = seen[0]
        self.assertEqual(
            (pull["progress_bytes"], pull["total_bytes"], pull["percent_complete"], pull["total_estimated"]),
            (0, self.EST, 0.0, True),
        )

    def test_streams_own_total_takes_over_once_every_layer_has_reported(self):
        pull = self._entry([
            {"id": "a", "status": "Downloading", "progressDetail": {"current": 100, "total": 400}},
            {"id": "b", "status": "Downloading", "progressDetail": {"current": 100, "total": 800}},
        ], nth=2)
        self.assertEqual((pull["total_bytes"], pull["percent_complete"]), (1200, round(200 / 1200 * 100, 2)))
        self.assertFalse(pull["total_estimated"], "a real total must not be flagged as an estimate")

    def test_an_estimate_never_claims_the_download_is_finished(self):
        """The checker's figure can be stale or low. 100 is reserved for the
        stream's own total; an estimate stops at 99 and the total grows rather
        than falling below what has already arrived."""
        pull = self._entry([
            {"id": "a", "status": "Downloading", "progressDetail": {"current": 1500, "total": 1500}},
            {"id": "b", "status": "Waiting"},
        ], nth=2)
        self.assertEqual(pull["percent_complete"], 99.0)
        self.assertGreaterEqual(pull["total_bytes"], pull["progress_bytes"])
        self.assertTrue(pull["total_estimated"])

    def test_no_estimate_behaves_exactly_as_before(self):
        pull = self._entry([
            {"id": "a", "status": "Downloading", "progressDetail": {"current": 100, "total": 400}},
            {"id": "b", "status": "Waiting"},
        ], expected=None, nth=2)
        self.assertEqual((pull["total_bytes"], pull["percent_complete"], pull["total_estimated"]),
                         (None, None, False))

    def test_expected_bytes_come_from_local_state(self):
        with mock.patch.object(self.mod, "load_state",
                               return_value={"available": {"web": {"download_size": 398351173}}}):
            self.assertEqual(self.mod._expected_download_bytes("web", "local"), 398351173)

    def test_expected_bytes_come_from_the_hosts_own_state_not_the_local_one(self):
        local = {"available": {"web": {"download_size": 111}}}
        remote = {"available": {"web": {"download_size": 222}}}
        with mock.patch.object(self.mod, "load_state", return_value=local), \
             mock.patch.object(self.mod, "load_host_state", return_value=remote) as loader:
            self.assertEqual(self.mod._expected_download_bytes("web", "site-b"), 222)
        loader.assert_called_once_with("site-b")

    def test_unusable_estimates_are_ignored(self):
        for entry in ({}, {"download_size": None}, {"download_size": 0},
                      {"download_size": -5}, {"download_size": True}, {"download_size": "12"}):
            with mock.patch.object(self.mod, "load_state",
                                   return_value={"available": {"web": entry}}):
                self.assertIsNone(self.mod._expected_download_bytes("web", "local"), entry)

    def test_state_failures_never_break_an_update(self):
        with mock.patch.object(self.mod, "load_state", side_effect=RuntimeError("corrupt state.json")):
            self.assertIsNone(self.mod._expected_download_bytes("web", "local"))

    def test_apply_update_feeds_the_checkers_estimate_into_the_live_entry(self):
        """The whole path: state -> apply_update -> /api/pulls."""
        d = FakeDocker(self.mod)
        d.new("web")
        self.state["available"]["web"] = {"download_size": self.EST}
        seen = []

        def chunks():
            yield {"id": "a", "status": "Waiting"}
            seen.append(self.mod.app.test_client().get("/api/pulls").get_json()["pulls"][0])
            yield {"error": "stop here"}

        d.api.pull.return_value = chunks()
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertEqual((seen[0]["total_bytes"], seen[0]["total_estimated"]), (self.EST, True))

    # ── stale estimates: another container already pulled this tag ───────────

    def _images_with(self, repo_digests):
        client = mock.Mock()
        client.images.get.return_value = mock.Mock(attrs={"RepoDigests": repo_digests})
        return client

    def _expected(self, client, entry=None):
        entry = {"download_size": self.EST, "remote_digest": "sha256:new"} if entry is None else entry
        with mock.patch.object(self.mod, "load_state",
                               return_value={"available": {"web": entry}}):
            return self.mod._expected_download_bytes("web", "local", client, "img:latest")

    def test_estimate_is_dropped_when_the_target_digest_is_already_local(self):
        """nginx:alpine is shared by four containers; after the first updated, the
        others still said 25MB for a pull that downloads nothing. Docker sends no
        layer events for an already-current image, so nothing replaced it."""
        self.assertIsNone(self._expected(self._images_with(["img@sha256:new"])))

    def test_estimate_is_kept_when_the_digest_is_not_local(self):
        self.assertEqual(self._expected(self._images_with(["img@sha256:old"])), self.EST)

    def test_digest_match_is_anchored_to_the_at_sign(self):
        """The match is `repo@<digest>`. This string ends with the stored digest
        but isn't preceded by '@', so an unanchored endswith would wrongly
        treat it as present (and drop a legitimate estimate)."""
        entry = {"download_size": self.EST, "remote_digest": "sha256:abc"}
        self.assertEqual(self._expected(self._images_with(["img@xsha256:abc"]), entry), self.EST)

    def test_estimate_is_kept_when_the_local_lookup_fails(self):
        """Can't tell either way — keep the estimate rather than guess."""
        client = mock.Mock()
        client.images.get.side_effect = RuntimeError("daemon hiccup")
        self.assertEqual(self._expected(client), self.EST)

    def test_estimate_is_kept_when_no_digest_was_stored(self):
        self.assertEqual(self._expected(self._images_with(["img@sha256:new"]),
                                        {"download_size": self.EST}), self.EST)

    def test_apply_update_shows_no_estimate_for_a_pull_that_will_download_nothing(self):
        d = FakeDocker(self.mod)
        d.new("web")
        d.images.get = lambda _ref: mock.Mock(attrs={"RepoDigests": ["img@sha256:new"]})
        self.state["available"]["web"] = {"download_size": self.EST, "remote_digest": "sha256:new"}
        seen = []

        def chunks():
            yield {"status": "Pulling from library/img"}      # no layer id, as Docker sends
            seen.append(self.mod.app.test_client().get("/api/pulls").get_json()["pulls"][0])
            yield {"error": "stop here"}

        d.api.pull.return_value = chunks()
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertEqual((seen[0]["total_bytes"], seen[0]["percent_complete"], seen[0]["total_estimated"]),
                         (None, None, False))


if __name__ == "__main__":
    unittest.main()
