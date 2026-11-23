"""A Compose-managed container's rollback backup must not look like a second copy
of the service (issue #39).

Compose finds a service's containers by their `com.docker.compose.*` labels, and
labels cannot be changed once a container exists. The renamed original therefore
kept those labels, `docker compose up` tried to tidy it away while recreating the
live container, and the live container was left Created but never started (and
the backup deleted). For a Compose-managed container the backup is now a stopped
copy without the Compose labels, made before the new image is pulled.
"""

import base64
import datetime
import json
import os
import unittest
from unittest import mock

from tests.test_safety import FakeContainer, FakeDocker, SafetyTestBase

COMPOSE = {
    "com.docker.compose.project": "stack",
    "com.docker.compose.service": "web",
    "com.docker.compose.container-number": "1",
    "com.docker.compose.config-hash": "abc123",
    "com.docker.compose.image": "sha256:old",
    "org.example.keep": "yes",
}
NETS = {
    "stack_default": {"Aliases": ["web", "cid-orig"], "IPAMConfig": {"IPv4Address": "172.20.0.5"}},
    "proxy": {"Aliases": ["web"], "IPAMConfig": None},
}
OLD_LAYERS, NEW_LAYERS = ["sha256:l-old"], ["sha256:l-new"]


class ComposeContainer(FakeContainer):
    def __init__(self, registry, name, labels=None, nets=None, **kw):
        super().__init__(registry, name, **kw)
        self.labels = dict(labels or {})
        self._nets = nets if nets is not None else {}

    @property
    def attrs(self):
        a = super().attrs
        a["Config"]["Labels"] = dict(self.labels)
        a["HostConfig"]["NetworkMode"] = "stack_default"
        a["NetworkSettings"] = {"Networks": self._nets}
        a["Created"] = getattr(self, "created", "2026-01-01T00:00:00.000000000Z")
        return a


def _host_config_like_docker_py(**kw):
    return {"Binds": kw["binds"], "PortBindings": kw["port_bindings"],
            "NetworkMode": kw["network_mode"], "RestartPolicy": kw["restart_policy"]}


class Stack:
    """A FakeDocker with one Compose-managed container, a moving tag and creation."""

    def __init__(self, mod, labels=None, same_content=True):
        self.d = d = FakeDocker(mod)
        self.pulled = False
        self.original = ComposeContainer(d.registry, "web", labels=COMPOSE if labels is None else labels,
                                         nets=NETS, image_id="sha256:old")
        self.original.id = "cid-orig"
        d.registry["web"] = self.original
        self.creations = []

        def images_get(ref):
            if ref == "img:latest":
                moved = self.pulled or not same_content
                return mock.Mock(id="sha256:new" if self.pulled else "sha256:old",
                                 attrs={"RootFS": {"Layers": NEW_LAYERS if moved else OLD_LAYERS}})
            return mock.Mock(id=ref, attrs={"RootFS": {"Layers": OLD_LAYERS}})

        def pull(*_a, **_k):
            self.pulled = True
            return iter([{"status": "Status: Downloaded newer image"}])

        def create(**kw):
            self.creations.append(kw)
            c = ComposeContainer(d.registry, kw["name"], labels=kw.get("labels"), status="created",
                                 image_id="sha256:new" if self.pulled else "sha256:old")
            d.registry[kw["name"]] = c
            return {"Id": c.id}

        def start(cid):
            c = d.by_id(cid)
            c.status, c.health = "running", "healthy"

        d.images.get = images_get
        d.api.pull.side_effect = pull
        d.api.create_container.side_effect = create
        d.api.start.side_effect = start
        d.api.create_host_config.side_effect = _host_config_like_docker_py
        d.api.create_networking_config.side_effect = lambda cfg: cfg
        d.api.create_endpoint_config.side_effect = lambda **kw: kw
        d.info = mock.Mock(return_value={"DriverStatus": []})

    def backup(self):
        return self.d.registry.get("web_old")


class UpdateOfAComposeContainerTests(SafetyTestBase):
    def _update(self, **state):
        self.state.update(state)
        self.stack = Stack(self.mod, **getattr(self, "stack_kw", {}))
        self._run(self.mod.apply_update, self.stack.d, "web", "local", reserved=True)
        return self.stack

    def test_the_kept_backup_has_no_compose_labels(self):
        s = self._update(backup_enabled=True)
        backup = s.backup()
        self.assertIsNotNone(backup, "no backup was kept")
        self.assertFalse([k for k in backup.labels if k.startswith("com.docker.compose.")],
                         f"the backup still carries Compose labels: {backup.labels}")
        self.assertEqual(backup.labels.get("org.example.keep"), "yes",
                         "the container's other labels should be kept")
        self.assertEqual(backup.labels.get(self.mod._BACKUP_OF_LABEL), "web")

    def test_the_labelled_original_is_gone_so_compose_sees_one_container(self):
        s = self._update(backup_enabled=True)
        self.assertTrue(s.original.removed, "the Compose-labelled original was left behind")
        for name in ("web_oldorig", "web_oldtmp"):
            self.assertNotIn(name, s.d.registry, f"{name} left behind")
        labelled = [c.name for c in s.d.registry.values()
                    if "com.docker.compose.project" in c.labels]
        self.assertEqual(labelled, ["web"], f"Compose would see {labelled}")

    def test_the_live_container_keeps_its_compose_labels(self):
        s = self._update(backup_enabled=True)
        live_creation = s.creations[-1]
        self.assertEqual(live_creation["name"], "web")
        self.assertEqual(live_creation["labels"]["com.docker.compose.project"], "stack")

    def test_the_copy_is_made_before_the_pull(self):
        """Once the tag has moved the old image can no longer be used to create
        anything on the containerd image store."""
        order = []
        s = Stack(self.mod)
        real_create, real_pull = s.d.api.create_container.side_effect, s.d.api.pull.side_effect
        s.d.api.create_container.side_effect = lambda **kw: (order.append("create " + kw["name"]), real_create(**kw))[1]
        s.d.api.pull.side_effect = lambda *a, **k: (order.append("pull"), real_pull(*a, **k))[1]
        self.state["backup_enabled"] = True
        self._run(self.mod.apply_update, s.d, "web", "local", reserved=True)
        self.assertLess(order.index("create web_oldtmp"), order.index("pull"), order)

    def test_the_copy_is_stopped_never_restarts_and_has_no_fixed_addresses(self):
        s = self._update(backup_enabled=True)
        clone = s.creations[0]
        self.assertEqual(clone["name"], "web_oldtmp")
        self.assertEqual(clone["host_config"]["RestartPolicy"], {"Name": "no"})
        endpoint_calls = s.d.api.create_endpoint_config.call_args_list
        self.assertNotIn("ipv4_address", endpoint_calls[0].kwargs,
                         "the copy must not claim the live container's fixed address")
        self.assertEqual(s.backup().status, "created", "the backup must not be started")

    def test_the_networks_are_remembered_for_a_rollback(self):
        s = self._update(backup_enabled=True)
        remembered = json.loads(s.backup().labels[self.mod._BACKUP_NETS_LABEL])
        self.assertEqual(remembered["stack_default"]["ipv4_address"], "172.20.0.5")
        self.assertEqual(remembered["stack_default"]["aliases"], ["web"], "own id alias should be dropped")
        self.assertIn("proxy", remembered)

    def test_the_rollback_record_is_written(self):
        self._update(backup_enabled=True)
        self.assertIn("web", self.state["rollbacks"])

    def test_no_copy_is_left_when_no_backup_is_kept(self):
        s = self._update(backup_enabled=False)
        self.assertNotIn("web_oldtmp", s.d.registry)
        self.assertNotIn("web_old", s.d.registry)

    def test_the_copy_is_removed_when_the_pull_fails(self):
        s = Stack(self.mod)
        s.d.api.pull.side_effect = None
        s.d.api.pull.return_value = iter([{"error": "unauthorized"}])
        self._run(self.mod.apply_update, s.d, "web", "local", reserved=True)
        self.assertNotIn("web_oldtmp", s.d.registry, "the copy outlived a failed update")
        self.assertIs(s.d.registry["web"], s.original, "the original must be untouched")
        self.assertEqual(s.original.status, "running")

    def test_the_copy_is_removed_and_the_original_restored_when_the_new_container_fails(self):
        s = Stack(self.mod)
        s.d.api.start.side_effect = lambda cid: (_ for _ in ()).throw(RuntimeError("boom"))
        self._run(self.mod.apply_update, s.d, "web", "local", reserved=True)
        self.assertNotIn("web_oldtmp", s.d.registry)
        self.assertIs(s.d.registry.get("web"), s.original, "the original was not put back")
        self.assertEqual(s.original.status, "running")

    def test_a_plain_container_is_made_exactly_as_before(self):
        self.stack_kw = {"labels": {"org.example.keep": "yes"}}
        s = self._update(backup_enabled=True)
        self.assertEqual([c["name"] for c in s.creations], ["web"], "no copy for a non-Compose container")
        self.assertIs(s.backup(), s.original, "the renamed original is still the backup")

    def test_falls_back_to_the_renamed_original_when_the_tag_has_already_moved(self):
        self.stack_kw = {"same_content": False}
        s = self._update(backup_enabled=True)
        self.assertEqual([c["name"] for c in s.creations], ["web"])
        self.assertIs(s.backup(), s.original)
        self.assertTrue(any("tag no longer points" in ln for ln in self._logs()))

    def test_falls_back_when_the_copy_cannot_be_created(self):
        s = Stack(self.mod)
        real = s.d.api.create_container.side_effect

        def create(**kw):
            if kw["name"].endswith("_oldtmp"):
                raise RuntimeError("no space left")
            return real(**kw)

        s.d.api.create_container.side_effect = create
        self.state["backup_enabled"] = True
        self._run(self.mod.apply_update, s.d, "web", "local", reserved=True)
        self.assertIs(s.backup(), s.original, "the update should still keep the renamed original")
        self.assertEqual(s.d.registry["web"].status, "running")

    def test_a_failed_swap_keeps_the_original_as_the_backup(self):
        s = Stack(self.mod)
        real = s.d.api.create_container.side_effect

        def create(**kw):
            r = real(**kw)
            if kw["name"].endswith("_oldtmp"):
                s.d.registry["web_oldtmp"].rename_fails = True
            return r

        s.d.api.create_container.side_effect = create
        self.state["backup_enabled"] = True
        self._run(self.mod.apply_update, s.d, "web", "local", reserved=True)
        self.assertIs(s.backup(), s.original, "the original must remain the backup if the swap fails")
        self.assertFalse(s.original.removed)
        self.assertNotIn("web_oldtmp", s.d.registry, "the unused copy should be removed")


class SelfUpdateHelperTests(SafetyTestBase):
    def _helper(self, backup_enabled):
        mod = self.mod
        s = Stack(mod)
        d = s.d
        d.new("web_oldtmp", status="created")
        d.registry["web_oldtmp"].labels = {mod._BACKUP_OF_LABEL: "web"}
        spec = {"name": "web", "image": "img:latest", "cfg": {"Image": "img:latest", "Labels": dict(COMPOSE)},
                "hcfg": {"RestartPolicy": {"Name": "unless-stopped"}}, "full_nets": {},
                "backup_enabled": backup_enabled, "backup_clone": "web_oldtmp"}
        env = {"DOCKER_UPDATER_SELF_UPDATE_SPEC_B64": base64.b64encode(json.dumps(spec).encode()).decode()}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(mod, "get_docker_client", return_value=d), \
             mock.patch.object(mod, "_helper_reserve_backup"), \
             mock.patch.object(mod, "_helper_write_state"), \
             mock.patch.object(mod, "_container_is_up", return_value=(True, True, "healthy")), \
             mock.patch.object(mod.time, "sleep"):
            mod._run_self_update_helper()
        return s

    def test_the_helper_swaps_the_copy_in_as_the_backup(self):
        s = self._helper(backup_enabled=True)
        self.assertEqual(s.d.registry["web_old"].labels, {self.mod._BACKUP_OF_LABEL: "web"})
        self.assertTrue(s.original.removed)
        self.assertNotIn("web_oldtmp", s.d.registry)

    def test_the_helper_drops_the_copy_when_no_backup_is_kept(self):
        s = self._helper(backup_enabled=False)
        self.assertNotIn("web_oldtmp", s.d.registry)
        self.assertNotIn("web_old", s.d.registry)


class RollbackTests(SafetyTestBase):
    def test_a_containerd_host_does_not_refuse_because_the_old_image_cannot_be_found(self):
        """On the containerd image store the superseded image stops being
        addressable once its tag has moved, but the backup container is intact.
        Every rollback used to be refused with 'backup image missing'."""
        d = FakeDocker(self.mod, missing_images={"sha256:bbb"})
        d.info = mock.Mock(return_value={"DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]]})
        live = d.new("web")
        backup = d.new("web_old", image_id="sha256:bbb", status="exited")
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertIs(d.registry.get("web"), backup, "the rollback should have gone ahead")
        self.assertEqual(backup.status, "running")
        self.assertTrue(live.removed)

    def test_a_containerd_host_still_restores_the_live_container_if_the_backup_will_not_start(self):
        d = FakeDocker(self.mod, missing_images={"sha256:bbb"})
        d.info = mock.Mock(return_value={"DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]]})
        live = d.new("web")
        d.new("web_old", image_id="sha256:bbb", status="exited", start_fails=True)
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertIs(d.registry.get("web"), live)
        self.assertEqual(live.status, "running")

    def test_the_classic_store_keeps_the_strict_check(self):
        d = FakeDocker(self.mod, missing_images={"sha256:bbb"})
        d.info = mock.Mock(return_value={"Driver": "overlay2", "DriverStatus": [["Backing Filesystem", "extfs"]]})
        live = d.new("web")
        d.new("web_old", image_id="sha256:bbb", status="exited")
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertEqual(live.stop_calls, 0, "the live container must not be touched")

    def _compose_safe_rollback(self):
        d = FakeDocker(self.mod)
        d.new("web")
        backup = ComposeContainer(d.registry, "web_old", status="exited", image_id="sha256:bbb",
                                  nets={"stack_default": {}},
                                  labels={self.mod._BACKUP_OF_LABEL: "web",
                                          self.mod._BACKUP_NETS_LABEL: json.dumps({
                                              "stack_default": {"aliases": ["web"], "ipv4_address": "172.20.0.5",
                                                                "ipv6_address": None},
                                              "proxy": {"aliases": ["web"], "ipv4_address": None,
                                                        "ipv6_address": None}})})
        backup.reload = lambda: None
        d.registry["web_old"] = backup
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        return d, backup

    def test_a_compose_safe_backup_is_reconnected_to_its_networks(self):
        d, backup = self._compose_safe_rollback()
        connects = {c.args[1]: c.kwargs for c in d.api.connect_container_to_network.call_args_list}
        self.assertEqual(connects["stack_default"]["ipv4_address"], "172.20.0.5")
        self.assertEqual(connects["stack_default"]["aliases"], ["web"])
        self.assertIn("proxy", connects)
        # the primary network it was created on is detached first so the fixed address can be set
        disconnected = [c.args[1] for c in d.api.disconnect_container_from_network.call_args_list]
        self.assertEqual(disconnected, ["stack_default"])
        self.assertEqual(backup.status, "running")

    def test_the_rollback_says_compose_no_longer_recognises_the_container(self):
        self._compose_safe_rollback()
        text = "\n".join(self._logs())
        self.assertIn("Docker Compose's labels", text)
        self.assertIn("docker rm -f web", text)

    def test_an_ordinary_rollback_says_nothing_about_compose(self):
        d = FakeDocker(self.mod)
        d.new("web")
        d.new("web_old", status="exited")
        self._run(self.mod.apply_rollback, d, "web", "local", reserved=True)
        self.assertNotIn("Compose", "\n".join(self._logs()))
        d.api.connect_container_to_network.assert_not_called()


class RestoreNetworksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.app_loader import load_app_module
        cls.mod = load_app_module()

    def _container(self, labels, attached=()):
        c = mock.Mock(id="cid")
        c.attrs = {"Config": {"Labels": labels}, "NetworkSettings": {"Networks": {n: {} for n in attached}}}
        return c

    def test_host_and_none_networks_are_never_touched(self):
        client = mock.Mock()
        nets = {"host": {"aliases": [], "ipv4_address": None, "ipv6_address": None},
                "none": {"aliases": [], "ipv4_address": None, "ipv6_address": None}}
        c = self._container({self.mod._BACKUP_NETS_LABEL: json.dumps(nets)})
        self.mod._restore_backup_networks(client, c, print)
        client.api.connect_container_to_network.assert_not_called()

    def test_a_network_that_will_not_connect_is_reported_and_the_rest_continue(self):
        client = mock.Mock()
        client.api.connect_container_to_network.side_effect = [RuntimeError("address in use"), None]
        nets = {"a": {"aliases": [], "ipv4_address": "1.2.3.4", "ipv6_address": None},
                "b": {"aliases": [], "ipv4_address": None, "ipv6_address": None}}
        c = self._container({self.mod._BACKUP_NETS_LABEL: json.dumps(nets)})
        said = []
        self.mod._restore_backup_networks(client, c, said.append)
        self.assertEqual(client.api.connect_container_to_network.call_count, 2)
        self.assertTrue(any("'a'" in s for s in said), said)

    def test_a_missing_or_broken_label_does_nothing(self):
        client = mock.Mock()
        for labels in ({}, {self.mod._BACKUP_NETS_LABEL: "not json"}):
            self.mod._restore_backup_networks(client, self._container(labels), print)
        client.api.connect_container_to_network.assert_not_called()


class HelpersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.app_loader import load_app_module
        cls.mod = load_app_module()

    def test_only_compose_labels_are_stripped(self):
        out = self.mod._without_compose_labels({"com.docker.compose.project": "p", "a": "b",
                                                "com.docker.composex": "keep?"})
        self.assertEqual(out, {"a": "b", "com.docker.composex": "keep?"})

    def test_the_unfinished_copy_is_hidden_like_the_other_helper_containers(self):
        self.assertTrue(self.mod._is_helper_container("web_oldtmp"))
        self.assertFalse(self.mod._is_helper_container("web"))

    def test_a_compose_project_is_detected_from_the_label(self):
        self.assertEqual(self.mod._compose_project({"Labels": COMPOSE}), "stack")
        self.assertIsNone(self.mod._compose_project({"Labels": {}}))
        self.assertIsNone(self.mod._compose_project({}))

    def test_containerd_is_recognised_from_the_daemon_info(self):
        c = mock.Mock()
        c.info.return_value = {"DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]]}
        self.assertTrue(self.mod._containerd_image_store(c))
        c.info.return_value = {"DriverStatus": [["Backing Filesystem", "extfs"]]}
        self.assertFalse(self.mod._containerd_image_store(c))
        c.info.side_effect = RuntimeError("down")
        self.assertFalse(self.mod._containerd_image_store(c))


class StrayCopyRecoveryTests(SafetyTestBase):
    def _recover(self, d):
        with mock.patch.object(self.mod.docker, "from_env", return_value=d), \
             mock.patch.object(self.mod, "load_state", return_value=self.state), \
             mock.patch.object(self.mod, "save_state"), \
             mock.patch.object(self.mod.time, "sleep"):
            self.mod.recover_interrupted_operations()

    def _copy(self, d, age_hours, labelled=True):
        c = ComposeContainer(d.registry, "web_oldtmp", status="created",
                             labels={self.mod._BACKUP_OF_LABEL: "web"} if labelled else {})
        born = datetime.datetime.utcnow() - datetime.timedelta(hours=age_hours)
        c.created = born.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
        d.registry["web_oldtmp"] = c
        return c

    def test_an_old_unfinished_copy_is_removed_at_startup(self):
        d = FakeDocker(self.mod)
        c = self._copy(d, age_hours=5)
        self._recover(d)
        self.assertTrue(c.removed)

    def test_a_recent_copy_is_left_alone(self):
        """An update, or a self-update helper about to swap it in, may still need it."""
        d = FakeDocker(self.mod)
        c = self._copy(d, age_hours=0.1)
        self._recover(d)
        self.assertFalse(c.removed)

    def test_a_container_that_merely_has_that_name_is_not_ours_to_remove(self):
        d = FakeDocker(self.mod)
        c = self._copy(d, age_hours=5, labelled=False)
        self._recover(d)
        self.assertFalse(c.removed)


if __name__ == "__main__":
    unittest.main()
