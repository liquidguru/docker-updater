"""Import app.py with lightweight stubs for unavailable runtime-only dependencies."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_app_module():
    os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")

    class DummyModule(types.ModuleType):
        def __getattr__(self, name):
            value = type(name, (), {})
            setattr(self, name, value)
            return value

    apprise = DummyModule("apprise")
    apprise.Apprise = type("Apprise", (), {})
    sys.modules.setdefault("apprise", apprise)

    class DockerErrors:
        NotFound = type("NotFound", (Exception,), {})
        ImageNotFound = type("ImageNotFound", (Exception,), {})

    docker = DummyModule("docker")
    docker.errors = DockerErrors
    docker.types = DummyModule("docker.types")
    docker.DockerClient = type("DockerClient", (), {})
    docker.from_env = lambda **_kwargs: None
    sys.modules.setdefault("docker", docker)
    sys.modules.setdefault("docker.types", docker.types)
    sys.modules.setdefault("docker.errors", docker.errors)

    apscheduler = DummyModule("apscheduler")
    schedulers = DummyModule("apscheduler.schedulers")
    background = DummyModule("apscheduler.schedulers.background")
    triggers = DummyModule("apscheduler.triggers")
    cron = DummyModule("apscheduler.triggers.cron")

    background.BackgroundScheduler = type("BackgroundScheduler", (), {})

    class CronTrigger:
        def __init__(self, *_args, **_kwargs):
            pass

        @classmethod
        def from_crontab(cls, *_args, **_kwargs):
            return cls()

    cron.CronTrigger = CronTrigger
    for name, module in (
        ("apscheduler", apscheduler),
        ("apscheduler.schedulers", schedulers),
        ("apscheduler.schedulers.background", background),
        ("apscheduler.triggers", triggers),
        ("apscheduler.triggers.cron", cron),
    ):
        sys.modules.setdefault(name, module)

    module_name = "docker_updater_test_app"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module
