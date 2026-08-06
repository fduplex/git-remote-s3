import subprocess
import pytest
from unittest.mock import patch
from conftest import git_config_get as _config_get
from git_remote_s3 import S3Remote, UriScheme
from git_remote_s3.common import synthetic_lfs_url
from git_remote_s3.remote import maybe_install_lfs_agent


def _add_remote(name: str, url: str) -> None:
    subprocess.run(["git", "remote", "add", name, url], check=True)


def test_install_writes_both_keys_when_unset(temp_git_repo):
    maybe_install_lfs_agent("origin")
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") == "git-lfs-s3"
    assert _config_get("lfs.standalonetransferagent") == "git-lfs-s3"


def test_install_is_idempotent(temp_git_repo):
    maybe_install_lfs_agent("origin")
    maybe_install_lfs_agent("origin")
    # --get returns the single configured value, not a duplicated one
    assert _config_get("lfs.standalonetransferagent") == "git-lfs-s3"
    res = subprocess.run(
        ["git", "config", "--get-all", "lfs.standalonetransferagent"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert res.stdout.decode("utf-8").strip().splitlines() == ["git-lfs-s3"]


def test_existing_unscoped_agent_is_not_stomped(temp_git_repo):
    subprocess.run(
        ["git", "config", "--add", "lfs.standalonetransferagent", "some-other-agent"],
        check=True,
    )
    maybe_install_lfs_agent("origin")
    assert _config_get("lfs.standalonetransferagent") == "some-other-agent"
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") is None


def test_per_remote_lfsurl_blocks_install(temp_git_repo):
    subprocess.run(
        [
            "git",
            "config",
            "--add",
            "remote.origin.lfsurl",
            "https://lfs-alias.git-remote-s3.test/bucket/prefix",
        ],
        check=True,
    )
    maybe_install_lfs_agent("origin")
    assert _config_get("lfs.standalonetransferagent") is None
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") is None


def test_per_remote_lfsurl_for_different_remote_does_not_block(temp_git_repo):
    subprocess.run(
        [
            "git",
            "config",
            "--add",
            "remote.other.lfsurl",
            "https://lfs-alias.git-remote-s3.test/bucket/prefix",
        ],
        check=True,
    )
    maybe_install_lfs_agent("origin")
    assert _config_get("lfs.standalonetransferagent") == "git-lfs-s3"


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "No"])
def test_opt_out_env_var_skips_install(temp_git_repo, monkeypatch, value):
    monkeypatch.setenv("GIT_REMOTE_S3_AUTO_INSTALL_LFS", value)
    maybe_install_lfs_agent("origin")
    assert _config_get("lfs.standalonetransferagent") is None
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") is None


def test_s3remote_installs_agent_on_first_s3_use(temp_git_repo):
    with patch("boto3.Session.client") as client_mock:
        client_mock.return_value.head_bucket.return_value = {}
        client_mock.return_value.list_objects_v2.return_value = {"Contents": []}
        remote = S3Remote(
            UriScheme.S3,
            None,
            "test_bucket",
            "test_prefix",
            remote_name="origin",
        )
        # The AWS setup, and the config writes that go with it, are deferred off the startup path.
        assert _config_get("lfs.standalonetransferagent") is None
        remote._ensure_s3()
    assert _config_get("lfs.standalonetransferagent") == "git-lfs-s3"


def test_s3remote_without_remote_name_does_not_install(temp_git_repo):
    with patch("boto3.Session.client") as client_mock:
        client_mock.return_value.head_bucket.return_value = {}
        client_mock.return_value.list_objects_v2.return_value = {"Contents": []}
        S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")._ensure_s3()
    assert _config_get("lfs.standalonetransferagent") is None
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") is None


def test_install_writes_lfsurl_for_s3_remote(temp_git_repo):
    _add_remote("origin", "s3://my-bucket/my-prefix")
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") == synthetic_lfs_url("my-bucket", "my-prefix")
    assert _config_get("lfs.standalonetransferagent") == "git-lfs-s3"


def test_lfsurl_preserves_dns_bucket_alias(temp_git_repo):
    _add_remote("origin", "s3://profile@demos.git.example.com/vendors/extrahop")
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") == synthetic_lfs_url("demos.git.example.com", "vendors/extrahop")


def test_lfsurl_added_to_a_clone_that_only_has_the_legacy_agent_keys(temp_git_repo):
    _add_remote("origin", "s3://my-bucket/my-prefix")
    subprocess.run(["git", "config", "--add", "lfs.standalonetransferagent", "git-lfs-s3"], check=True)
    subprocess.run(["git", "config", "--add", "lfs.customtransfer.git-lfs-s3.path", "git-lfs-s3"], check=True)
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") == synthetic_lfs_url("my-bucket", "my-prefix")
    res = subprocess.run(
        ["git", "config", "--get-all", "lfs.standalonetransferagent"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert res.stdout.decode("utf-8").strip().splitlines() == ["git-lfs-s3"]


def test_install_with_lfsurl_is_idempotent(temp_git_repo):
    _add_remote("origin", "s3://my-bucket/my-prefix")
    maybe_install_lfs_agent("origin")
    maybe_install_lfs_agent("origin")
    res = subprocess.run(
        ["git", "config", "--get-all", "remote.origin.lfsurl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert res.stdout.decode("utf-8").strip().splitlines() == [synthetic_lfs_url("my-bucket", "my-prefix")]


def test_user_set_lfsurl_is_not_stomped(temp_git_repo):
    _add_remote("origin", "s3://my-bucket/my-prefix")
    subprocess.run(["git", "config", "--add", "remote.origin.lfsurl", "https://lfs.example.com/foo"], check=True)
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") == "https://lfs.example.com/foo"


def test_foreign_unscoped_agent_blocks_the_lfsurl_write(temp_git_repo):
    _add_remote("origin", "s3://my-bucket/my-prefix")
    subprocess.run(["git", "config", "--add", "lfs.standalonetransferagent", "some-other-agent"], check=True)
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") is None


def test_no_lfsurl_for_a_non_s3_remote(temp_git_repo):
    _add_remote("origin", "https://github.com/example/repo.git")
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") is None
    assert _config_get("lfs.standalonetransferagent") == "git-lfs-s3"


@pytest.mark.parametrize("value", ["0", "false", "no"])
def test_opt_out_env_var_skips_the_lfsurl_write(temp_git_repo, monkeypatch, value):
    _add_remote("origin", "s3://my-bucket/my-prefix")
    monkeypatch.setenv("GIT_REMOTE_S3_AUTO_INSTALL_LFS", value)
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") is None
    assert _config_get("lfs.standalonetransferagent") is None
