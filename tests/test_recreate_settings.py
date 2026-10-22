"""Recreating a container must keep the settings it was started with (issue #32).

Recreation rebuilt HostConfig from a short list of fields, so a GPU reservation,
memory and CPU limits, ulimits, shm size, log options and the like were dropped
on every update — an nvidia image then crash-looped without its GPU. These tests
drive both recreate paths (a normal update and the self-update helper) and check
what actually reaches `create_container`.
"""

import base64
import json
import os
import unittest
from unittest import mock

from tests.test_safety import FakeContainer, FakeDocker, SafetyTestBase

GPU = [{"Driver": "nvidia", "Count": -1, "DeviceIDs": None,
        "Capabilities": [["gpu"]], "Options": {}}]

# What `docker inspect` reports for a container started with a GPU, limits,
# ulimits, shm, log options and so on, plus the keys recreation sets itself.
HOSTCONFIG = {
    "Binds": ["/data:/data"],
    "Mounts": [{"Type": "volume", "Source": "vol", "Target": "/vol"}],
    "PortBindings": {"3001/tcp": [{"HostIp": "", "HostPort": "3001"}]},
    "NetworkMode": "bridge",
    "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
    "ContainerIDFile": "/run/old.cid",
    "DeviceRequests": GPU,
    "Runtime": "nvidia",
    "Memory": 536870912,
    "MemorySwap": 1073741824,
    "NanoCpus": 1500000000,
    "ShmSize": 268435456,
    "Ulimits": [{"Name": "nofile", "Soft": 4096, "Hard": 8192}],
    "Sysctls": {"net.core.somaxconn": "1024"},
    "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m"}},
    "GroupAdd": ["video"],
    "Init": True,
    "ReadonlyRootfs": True,
    "PidsLimit": 200,
}

CONFIG = {
    "Image": "img:latest", "Hostname": "abc123456789", "Env": [],
    "Tty": True, "OpenStdin": True, "StopSignal": "SIGINT", "StopTimeout": 45,
}


class RichContainer(FakeContainer):
    """A container whose inspect data carries a full HostConfig and Config."""

    @property
    def attrs(self):
        a = super().attrs
        a["HostConfig"] = dict(HOSTCONFIG)
        a["Config"] = dict(CONFIG)
        return a


def _host_config_like_docker_py(**kw):
    """The parts of create_host_config the recreate code sets itself."""
    return {"Binds": kw["binds"], "PortBindings": kw["port_bindings"],
            "NetworkMode": kw["network_mode"], "RestartPolicy": kw["restart_policy"]}


class UpdateRecreateTests(SafetyTestBase):
    def _update(self):
        d = FakeDocker(self.mod)
        old = RichContainer(d.registry, "web", image_id="sha256:old")
        d.registry["web"] = old
        d.images.get = lambda ref: mock.Mock(id="sha256:new")
        d.api.pull.return_value = iter([{"status": "Status: Downloaded newer image"}])
        d.api.create_host_config.side_effect = _host_config_like_docker_py
        d.api.create_container.return_value = {"Id": "cid-new"}
        d.api.create_networking_config.return_value = {}
        d.api.create_endpoint_config.return_value = {}

        def _start(_id):
            old.rename("web_old")
            d.new("web", image_id="sha256:new", status="running", health="healthy")

        d.api.start.side_effect = _start
        self._run(self.mod.apply_update, d, "web", "local", reserved=True)
        self.assertTrue(d.api.create_container.called, "the container was never recreated")
        return d.api.create_container.call_args.kwargs

    def test_gpu_reservation_survives_an_update(self):
        hc = self._update()["host_config"]
        self.assertEqual(hc["DeviceRequests"], GPU)
        self.assertEqual(hc["Runtime"], "nvidia")

    def test_limits_ulimits_and_other_settings_survive_an_update(self):
        hc = self._update()["host_config"]
        for key in ("Memory", "MemorySwap", "NanoCpus", "ShmSize", "Ulimits",
                    "Sysctls", "LogConfig", "GroupAdd", "Init", "ReadonlyRootfs",
                    "PidsLimit"):
            self.assertEqual(hc[key], HOSTCONFIG[key], f"{key} was lost on recreate")

    def test_settings_the_update_sets_itself_are_not_overwritten(self):
        hc = self._update()["host_config"]
        self.assertEqual(hc["Binds"], ["/data:/data"])
        self.assertEqual(hc["NetworkMode"], "bridge")
        self.assertEqual(hc["RestartPolicy"]["Name"], "unless-stopped")
        self.assertIn("3001/tcp", hc["PortBindings"])

    def test_mounts_are_not_copied_back_verbatim(self):
        """_mounts_from_hcfg decides what to do with them; a second copy of the
        raw list would be a duplicate-mount error."""
        self.assertNotIn("Mounts", self._update()["host_config"])

    def test_the_old_containers_own_id_file_is_not_carried(self):
        self.assertNotIn("ContainerIDFile", self._update()["host_config"])

    def test_terminal_stdin_and_stop_settings_survive_an_update(self):
        kw = self._update()
        self.assertIs(kw["tty"], True)
        self.assertIs(kw["stdin_open"], True)
        self.assertEqual(kw["stop_signal"], "SIGINT")
        self.assertEqual(kw["stop_timeout"], 45)


class SelfUpdateHelperRecreateTests(SafetyTestBase):
    def test_helper_keeps_the_settings_too(self):
        mod = self.mod
        d = FakeDocker(mod)
        old = RichContainer(d.registry, "web", image_id="sha256:old")
        d.registry["web"] = old
        d.api.create_host_config.side_effect = _host_config_like_docker_py
        d.api.create_container.return_value = {"Id": "cid-new"}
        d.api.create_networking_config.return_value = {}
        d.api.create_endpoint_config.return_value = {}

        def _start(_id):
            d.new("web", image_id="sha256:new", status="running", health="healthy")

        d.api.start.side_effect = _start
        spec = {"name": "web", "image": "img:latest", "cfg": dict(CONFIG),
                "hcfg": dict(HOSTCONFIG), "full_nets": {}}
        env = {"DOCKER_UPDATER_SELF_UPDATE_SPEC_B64":
               base64.b64encode(json.dumps(spec).encode()).decode()}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(mod, "get_docker_client", return_value=d), \
             mock.patch.object(mod, "_helper_reserve_backup"), \
             mock.patch.object(mod, "_helper_write_state"), \
             mock.patch.object(mod, "_container_is_up", return_value=(True, True, "healthy")), \
             mock.patch.object(mod.time, "sleep"):
            mod._run_self_update_helper()
        self.assertTrue(d.api.create_container.called, "the helper never recreated it")
        kw = d.api.create_container.call_args.kwargs
        self.assertEqual(kw["host_config"]["DeviceRequests"], GPU)
        self.assertEqual(kw["host_config"]["Memory"], HOSTCONFIG["Memory"])
        self.assertNotIn("Mounts", kw["host_config"])
        self.assertIs(kw["tty"], True)


class CarryOverHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.app_loader import load_app_module
        cls.mod = load_app_module()

    def test_keys_already_set_are_left_alone(self):
        hc = {"Memory": 1, "Binds": ["/a:/b"]}
        self.mod._carry_over_hostconfig(hc, {"Memory": 2, "Ulimits": [1]})
        self.assertEqual(hc, {"Memory": 1, "Binds": ["/a:/b"], "Ulimits": [1]})

    def test_an_empty_hostconfig_changes_nothing(self):
        hc = {"Binds": []}
        self.mod._carry_over_hostconfig(hc, {})
        self.assertEqual(hc, {"Binds": []})

    def test_extras_are_empty_for_a_plain_container(self):
        self.assertEqual(self.mod._container_extras({"Env": []}), {})

    def test_a_zero_stop_timeout_is_kept(self):
        """0 means 'kill immediately', which is different from 'not set'."""
        self.assertEqual(self.mod._container_extras({"StopTimeout": 0}),
                         {"stop_timeout": 0})


if __name__ == "__main__":
    unittest.main()
