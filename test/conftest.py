# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0

import subprocess
import tempfile

import pytest

from git_remote_s3 import common, walstore


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_git_shallow_check: exempt from remote_test.py's autouse is_shallow_repository stub",
    )


@pytest.fixture
def temp_git_repo(monkeypatch):
    """A throwaway git repo as cwd, with every env var the helper reads cleared.

    GIT_REMOTE_S3_AUTO_INSTALL_LFS is unset rather than pinned so the auto-install tests exercise
    the default; the config writes it causes land in this repo and never in the project's own.
    """
    with tempfile.TemporaryDirectory(prefix="git_remote_s3_test_") as repo:
        subprocess.run(
            ["git", "init", "-q", repo],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        monkeypatch.chdir(repo)
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_REMOTE_S3_AUTO_INSTALL_LFS"):
            monkeypatch.delenv(var, raising=False)
        yield repo


def git_config_get(key: str) -> str | None:
    res = subprocess.run(["git", "config", "--get", key], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        return None
    return res.stdout.decode("utf-8").strip() or None


@pytest.fixture(autouse=True)
def _no_cas_backoff(monkeypatch):
    # The CAS retry loop sleeps between attempts; no test needs to wait for real contention.
    # Stubbed on the method, not on time.sleep: walstore.time is the time module itself, so
    # patching its sleep would silently disarm every other sleep in the process.
    monkeypatch.setattr(walstore.WalStore, "_backoff", lambda self, attempt: None)


@pytest.fixture(autouse=True)
def _reset_bucket_region_cache():
    # resolve_bucket_region caches per process; isolate that state between tests
    # so a region resolved in one test does not suppress the HeadBucket probe in
    # the next.
    common._bucket_region_cache.clear()
    yield
    common._bucket_region_cache.clear()
