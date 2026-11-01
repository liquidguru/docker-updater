"""Reclaiming dangling images: the list, the removal job and its lifetime.

Covers what the page needs to be honest and safe to leave: images a backup still
holds are marked rather than offered for deletion, a slow daemon is waited on
instead of reported as a failure, and a job outlives the page that started it.
"""

import time
import unittest
from unittest import mock

import requests

from tests.app_loader import load_app_module


class FakeImage:
    def __init__(self, image_id, size=1_000_000, repo="ghcr.io/x/app"):
        self.id = image_id
        self.short_id = image_id[:19]
        self.attrs = {"Size": size, "Created": "2026-10-01T00:00:00Z",
                      "RepoDigests": [f"{repo}@sha256:abc"] if repo else []}


class Refused(Exception):
    def __init__(self, explanation):
        super().__init__(explanation)
        self.explanation = explanation


class FakeContainer:
    def __init__(self, name, image_id, status="exited"):
        self.name = name
        self.id = "abc123def456"   # Docker names the blocker by id, not by name
        self.attrs = {"Image": image_id, "State": {"Status": status}}


class FakeDocker:
    """Images and containers by id; `remove_behaviour` scripts what a delete does."""

    def __init__(self, mod, images=(), containers=()):
        self.mod = mod
        self.present = {i.id: i for i in images}
        self.container_list = list(containers)
        self.remove_behaviour = {}   # image id -> callable(fake) or exception
        self.removed = []
        self.images = mock.Mock()
        self.images.list = lambda **_kw: [self.present[i] for i in list(self.present)]
        self.images.get = self._image_get
        self.images.remove = self._image_remove
        self.containers = mock.Mock()
        self.containers.list = lambda **_kw: list(self.container_list)
        self.containers.get = self._container_get

    def _image_get(self, image_id):
        if image_id not in self.present:
            raise self.mod.docker.errors.ImageNotFound(image_id)
        return self.present[image_id]

    def _container_get(self, cid):
        for c in self.container_list:
            if c.id.startswith(cid):
                return c
        raise KeyError(cid)

    def _image_remove(self, image_id, force=False):
        action = self.remove_behaviour.get(image_id)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            action(self)
            return
        self.present.pop(image_id, None)
        self.removed.append(image_id)


class ReclaimBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def setUp(self):
        self.mod.AUTH_ENABLED = False
        self.mod.app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
        self.mod._prune_jobs.clear()
        self.http = self.mod.app.test_client()
        self.rollbacks = {}
        patcher = mock.patch.object(self.mod, "_rollbacks_for_host",
                                    side_effect=lambda _h: self.rollbacks)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(self.mod.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def tearDown(self):
        self.mod._prune_jobs.clear()

    def run_job(self, fake, ids, host="local"):
        job_id = "job1"
        self.mod._prune_jobs[job_id] = {
            "status": "running", "total": len(ids), "done": 0, "host_id": host,
            "started_at": "2026-11-01T00:00:00Z", "current": None, "current_size": None,
        }
        self.mod._run_prune_job(job_id, fake, ids, host)
        return self.mod._prune_jobs.get(job_id)


class DanglingListTests(ReclaimBase):
    def listing(self, fake):
        with mock.patch.object(self.mod, "_client_for_host", return_value=fake):
            response = self.http.get("/api/dangling-images")
        self.assertEqual(response.status_code, 200)
        return {i["id"]: i for i in response.get_json()["images"]}

    def test_an_image_nothing_uses_is_not_held(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:free")])
        self.assertEqual(self.listing(fake)["sha256:free"]["held_by"], [])

    def test_a_rollback_backup_is_named_with_its_expiry(self):
        self.rollbacks = {"web": {"expires_at": "2026-11-02T00:00:00Z"}}
        fake = FakeDocker(self.mod, [FakeImage("sha256:old")],
                          [FakeContainer("web_old", "sha256:old")])
        held = self.listing(fake)["sha256:old"]["held_by"]
        self.assertEqual(held, [{"name": "web_old", "state": "exited", "backup": True,
                                 "expires_at": "2026-11-02T00:00:00Z"}])

    def test_a_running_container_holds_its_image_but_is_not_a_backup(self):
        """The #23 case: a container still running an image whose tag moved on."""
        fake = FakeDocker(self.mod, [FakeImage("sha256:live")],
                          [FakeContainer("web", "sha256:live", status="running")])
        held = self.listing(fake)["sha256:live"]["held_by"]
        self.assertEqual([(h["name"], h["state"], h["backup"]) for h in held],
                         [("web", "running", False)])

    def test_a_stopped_container_that_is_not_a_backup_is_not_called_one(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:x")],
                          [FakeContainer("old_tool_old", "sha256:x")])
        self.assertFalse(self.listing(fake)["sha256:x"]["held_by"][0]["backup"],
                         "no rollback record, so it is just a stopped container")

    def test_images_held_by_something_else_stay_unmarked(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:a"), FakeImage("sha256:b")],
                          [FakeContainer("web_old", "sha256:a")])
        listing = self.listing(fake)
        self.assertTrue(listing["sha256:a"]["held_by"])
        self.assertEqual(listing["sha256:b"]["held_by"], [])

    def test_failing_to_work_out_holders_still_lists_the_images(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:a")])
        fake.containers.list = mock.Mock(side_effect=RuntimeError("daemon busy"))
        self.assertEqual(self.listing(fake)["sha256:a"]["held_by"], [])


class PruneJobTests(ReclaimBase):
    def test_removes_images_and_reports_what_it_freed(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:a", 2 * 1024 ** 3),
                                     FakeImage("sha256:b", 1024 ** 3)])
        job = self.run_job(fake, ["sha256:a", "sha256:b"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["count"], 2)
        self.assertEqual(job["freed"], "3.00GB")
        self.assertEqual(job["skipped"], [])
        self.assertEqual(job["done"], 2)
        self.assertTrue(job["finished_at"].endswith("Z"))

    def test_an_image_already_gone_still_counts_as_handled(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:a")])
        job = self.run_job(fake, ["sha256:gone", "sha256:a"])
        self.assertEqual(job["done"], 2, "progress stalled below the total")
        self.assertEqual(job["count"], 1)

    def test_an_image_a_backup_still_uses_is_skipped_with_the_backup_named(self):
        self.rollbacks = {"web": {}}
        fake = FakeDocker(self.mod, [FakeImage("sha256:old")],
                          [FakeContainer("web_old", "sha256:old")])
        fake.remove_behaviour["sha256:old"] = Refused(
            "conflict: unable to delete (cannot be forced) - image is being used by "
            "stopped container abc123def456")
        job = self.run_job(fake, ["sha256:old"])
        self.assertEqual(job["count"], 0)
        skip = job["skipped"][0]
        self.assertEqual((skip["kind"], skip["state"], skip["name"], skip["backup"]),
                         ("container", "stopped", "web_old", True))
        self.assertIn("web_old", skip["reason"])

    def test_dependent_children_and_unknown_refusals_are_told_apart(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:a"), FakeImage("sha256:b")])
        fake.remove_behaviour["sha256:a"] = Refused("image has dependent child images")
        fake.remove_behaviour["sha256:b"] = Refused("something odd (detail)")
        job = self.run_job(fake, ["sha256:a", "sha256:b"])
        self.assertEqual([s["kind"] for s in job["skipped"]], ["dependents", "other"])
        self.assertEqual(job["skipped"][1]["reason"], "something odd")

    def test_one_refusal_does_not_stop_the_rest(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:a"), FakeImage("sha256:b")])
        fake.remove_behaviour["sha256:a"] = Refused("nope")
        job = self.run_job(fake, ["sha256:a", "sha256:b"])
        self.assertEqual((job["count"], len(job["skipped"])), (1, 1))

    def test_a_timeout_that_the_daemon_finished_anyway_counts_as_removed(self):
        """The reported bug: the request gave up after 60 s while Docker kept
        deleting, so a removed image was listed as a failure."""
        fake = FakeDocker(self.mod, [FakeImage("sha256:big", 2 * 1024 ** 3)])

        def slow_delete(f):
            f.present.pop("sha256:big")        # the daemon finishes the job...
            raise requests.exceptions.ReadTimeout("read timed out")   # ...too late

        fake.remove_behaviour["sha256:big"] = slow_delete
        job = self.run_job(fake, ["sha256:big"])
        self.assertEqual(job["count"], 1)
        self.assertEqual(job["freed"], "2.00GB")
        self.assertEqual(job["skipped"], [])

    def test_a_timeout_on_an_image_that_is_still_there_is_reported_as_slow(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:big")])
        fake.remove_behaviour["sha256:big"] = requests.exceptions.ReadTimeout("slow")
        with mock.patch.object(self.mod, "_PRUNE_SLOW_WAIT", 0):
            job = self.run_job(fake, ["sha256:big"])
        self.assertEqual(job["count"], 0)
        self.assertEqual(job["skipped"][0]["kind"], "slow")
        self.assertIn("still removing", job["skipped"][0]["reason"])

    def test_a_dropped_connection_is_handled_like_a_timeout(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:a")])
        fake.remove_behaviour["sha256:a"] = requests.exceptions.ConnectionError("reset")
        with mock.patch.object(self.mod, "_PRUNE_SLOW_WAIT", 0):
            job = self.run_job(fake, ["sha256:a"])
        self.assertEqual(job["skipped"][0]["kind"], "slow")

    def test_the_wait_polls_until_the_image_disappears(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:a")])
        polls = []

        def get(image_id):
            polls.append(image_id)
            if len(polls) >= 3:
                raise self.mod.docker.errors.ImageNotFound(image_id)
            return FakeImage(image_id)

        fake.images.get = get
        self.assertTrue(self.mod._removal_finished(fake, "sha256:a", wait=60, step=1))
        self.assertEqual(len(polls), 3)

    def test_the_job_reports_which_image_it_is_on(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:a" + "0" * 20, 1024 ** 3)])
        seen = []
        original = self.mod._prune_update

        def spy(job_id, **fields):
            if "current_size" in fields and fields["current_size"]:
                seen.append(fields["current_size"])
            return original(job_id, **fields)

        with mock.patch.object(self.mod, "_prune_update", side_effect=spy):
            self.run_job(fake, ["sha256:a" + "0" * 20])
        self.assertEqual(seen, ["1.00GB"])

    def test_a_cleared_job_stops_quietly(self):
        fake = FakeDocker(self.mod, [FakeImage("sha256:a"), FakeImage("sha256:b")])

        def clear(_f):
            self.mod._prune_jobs.clear()

        fake.remove_behaviour["sha256:a"] = clear
        self.run_job(fake, ["sha256:a", "sha256:b"])
        self.assertIn("sha256:b", fake.present, "kept deleting after the job was cleared")


class PruneApiTests(ReclaimBase):
    def start(self, host="local", ids=("sha256:a",)):
        fake = FakeDocker(self.mod, [FakeImage(i) for i in ids])
        clients = []

        def pick(host_id, timeout=None):
            clients.append((host_id, timeout))
            return fake

        with mock.patch.object(self.mod, "_client_for_host", side_effect=pick), \
             mock.patch.object(self.mod.threading, "Thread") as thread:
            response = self.http.post("/api/prune-images",
                                      json={"ids": list(ids), "host_id": host})
        return response, clients, thread

    def test_the_job_uses_a_long_request_timeout(self):
        response, clients, thread = self.start()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(clients, [("local", self.mod._PRUNE_REQUEST_TIMEOUT)])
        self.assertGreaterEqual(self.mod._PRUNE_REQUEST_TIMEOUT, 600)
        thread.return_value.start.assert_called_once()

    def test_the_default_client_still_has_its_own_timeout(self):
        with mock.patch.object(self.mod.docker, "from_env") as from_env:
            self.mod._client_for_host("local", timeout=900)
            from_env.assert_called_with(timeout=900)
            from_env.reset_mock()
            self.mod._client_for_host("local")
            from_env.assert_called_with()

    def test_starting_a_job_records_the_host_and_time(self):
        response, _c, _t = self.start(host="vps")
        job = self.mod._prune_jobs[response.get_json()["job_id"]]
        self.assertEqual((job["status"], job["host_id"], job["total"]), ("running", "vps", 1))
        self.assertTrue(job["started_at"].endswith("Z"))

    def test_a_second_reclaim_on_the_same_host_is_refused_and_points_at_the_first(self):
        first, _c, _t = self.start()
        second, _c2, _t2 = self.start()
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["job_id"], first.get_json()["job_id"])

    def test_another_host_can_reclaim_at_the_same_time(self):
        self.start(host="local")
        other, _c, _t = self.start(host="vps")
        self.assertEqual(other.status_code, 200)

    def test_no_ids_is_a_400(self):
        self.assertEqual(self.http.post("/api/prune-images", json={"ids": []}).status_code, 400)

    def _finished(self, **extra):
        job = {"status": "done", "total": 1, "done": 1, "host_id": "local",
               "started_at": "2026-11-01T00:00:00Z", "count": 1, "freed": "1.00GB",
               "skipped": [], "finished_at": "2026-11-01T00:01:00Z",
               "finished_ts": time.time(), "dismissed": False}
        job.update(extra)
        self.mod._prune_jobs["j1"] = job
        return job

    def test_a_finished_job_can_be_read_more_than_once(self):
        """It used to vanish after the first read, so a page that missed the
        moment of completion never learned what had happened."""
        self._finished()
        for _ in range(2):
            response = self.http.get("/api/prune-images/j1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["freed"], "1.00GB")

    def test_internal_fields_are_not_exposed_and_the_id_is(self):
        self._finished()
        body = self.http.get("/api/prune-images/j1").get_json()
        self.assertNotIn("finished_ts", body)
        self.assertNotIn("dismissed", body)
        self.assertEqual(body["job_id"], "j1")

    def test_an_unknown_job_is_a_404(self):
        self.assertEqual(self.http.get("/api/prune-images/nope").status_code, 404)

    def test_a_returning_page_is_given_the_running_job(self):
        self.mod._prune_jobs["old"] = {**self._finished(), "started_at": "2026-11-01T00:00:00Z"}
        self.mod._prune_jobs["live"] = {"status": "running", "total": 5, "done": 2,
                                        "host_id": "local", "started_at": "2026-11-01T00:05:00Z"}
        body = self.http.get("/api/prune-job?host=local").get_json()
        self.assertEqual((body["job"]["job_id"], body["job"]["status"]), ("live", "running"))

    def test_otherwise_it_is_given_the_latest_finished_job(self):
        self.mod._prune_jobs["a"] = {**self._finished(), "started_at": "2026-11-01T00:00:00Z"}
        self.mod._prune_jobs["b"] = {**self._finished(), "started_at": "2026-11-01T00:09:00Z"}
        self.assertEqual(self.http.get("/api/prune-job").get_json()["job"]["job_id"], "b")

    def test_a_job_on_another_host_is_not_shown(self):
        self._finished(host_id="vps")
        self.assertIsNone(self.http.get("/api/prune-job?host=local").get_json()["job"])

    def test_no_job_is_null(self):
        self.assertEqual(self.http.get("/api/prune-job").get_json(), {"job": None})

    def test_dismissing_removes_the_result(self):
        self._finished()
        self.assertEqual(self.http.post("/api/prune-images/j1/dismiss").status_code, 200)
        self.assertIsNone(self.http.get("/api/prune-job").get_json()["job"])
        self.assertEqual(self.http.get("/api/prune-images/j1").status_code, 404)

    def test_a_running_job_cannot_be_dismissed(self):
        self.mod._prune_jobs["live"] = {"status": "running", "total": 1, "done": 0,
                                        "host_id": "local", "started_at": "x"}
        self.assertEqual(self.http.post("/api/prune-images/live/dismiss").status_code, 409)
        self.assertIn("live", self.mod._prune_jobs)

    def test_dismissing_something_already_gone_is_fine(self):
        self.assertEqual(self.http.post("/api/prune-images/nope/dismiss").status_code, 200)

    def test_an_old_finished_job_is_forgotten(self):
        self._finished(finished_ts=time.time() - self.mod._PRUNE_KEEP_SECONDS - 5)
        self.assertEqual(self.http.get("/api/prune-images/j1").status_code, 404)
        self.assertIsNone(self.http.get("/api/prune-job").get_json()["job"])

    def test_a_recent_finished_job_is_kept(self):
        self._finished(finished_ts=time.time() - self.mod._PRUNE_KEEP_SECONDS + 60)
        self.assertEqual(self.http.get("/api/prune-images/j1").status_code, 200)

    def test_a_running_job_is_never_expired(self):
        self.mod._prune_jobs["live"] = {"status": "running", "total": 1, "done": 0,
                                        "host_id": "local", "started_at": "2020-01-01T00:00:00Z"}
        self.assertEqual(self.http.get("/api/prune-images/live").status_code, 200)

    def test_the_new_endpoints_require_login_when_auth_is_on(self):
        self.mod.AUTH_ENABLED = True
        try:
            for method, url in (("get", "/api/prune-job"),
                                ("get", "/api/prune-images/j1"),
                                ("post", "/api/prune-images/j1/dismiss")):
                self.assertEqual(getattr(self.http, method)(url).status_code, 401, url)
        finally:
            self.mod.AUTH_ENABLED = False


if __name__ == "__main__":
    unittest.main()
