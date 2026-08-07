import subprocess
import pytest
from unittest.mock import patch
from conftest import git_config_get as _config_get
from git_remote_s3 import S3Remote, UriScheme
from git_remote_s3.common import synthetic_lfs_url
from git_remote_s3.remote import maybe_install_lfs_agent

S3_URL = "s3://my-bucket/my-prefix"
LFS_URL = synthetic_lfs_url("my-bucket", "my-prefix")
SCOPED_AGENT_KEY = f"lfs.{LFS_URL}.standalonetransferagent"


def _add_remote(name: str, url: str) -> None:
    subprocess.run(["git", "remote", "add", name, url], check=True)


def _config_get_all(key: str) -> list[str]:
    res = subprocess.run(
        ["git", "config", "--get-all", key],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return res.stdout.decode("utf-8").strip().splitlines()


def _git_config(*args: str) -> None:
    subprocess.run(["git", "config", *args], check=True)


def test_install_writes_the_three_scoped_keys(temp_git_repo):
    _add_remote("origin", S3_URL)
    maybe_install_lfs_agent("origin")
    assert _config_get_all("lfs.customtransfer.git-lfs-s3.path") == ["git-lfs-s3"]
    assert _config_get_all("remote.origin.lfsurl") == [LFS_URL]
    assert _config_get_all(SCOPED_AGENT_KEY) == ["git-lfs-s3"]
    assert _config_get_all("lfs.standalonetransferagent") == []


def test_preexisting_customtransfer_path_is_not_duplicated(temp_git_repo):
    """The hand-configured repo shape from the upstream README: the legacy path key and nothing else."""
    _add_remote("origin", S3_URL)
    _git_config("--add", "lfs.customtransfer.git-lfs-s3.path", "git-lfs-s3")
    maybe_install_lfs_agent("origin")
    assert _config_get_all("lfs.customtransfer.git-lfs-s3.path") == ["git-lfs-s3"]
    assert _config_get_all("remote.origin.lfsurl") == [LFS_URL]
    assert _config_get_all(SCOPED_AGENT_KEY) == ["git-lfs-s3"]
    assert _config_get_all("lfs.standalonetransferagent") == []


def test_install_is_idempotent(temp_git_repo):
    _add_remote("origin", S3_URL)
    maybe_install_lfs_agent("origin")
    maybe_install_lfs_agent("origin")
    assert _config_get_all("lfs.customtransfer.git-lfs-s3.path") == ["git-lfs-s3"]
    assert _config_get_all("remote.origin.lfsurl") == [LFS_URL]
    assert _config_get_all(SCOPED_AGENT_KEY) == ["git-lfs-s3"]


def test_legacy_unscoped_ours_clone_is_backfilled_without_churn(temp_git_repo):
    """A clone written by an older build: unscoped agent, no lfsurl, possibly a duplicated path key."""
    _add_remote("origin", S3_URL)
    _git_config("--add", "lfs.standalonetransferagent", "git-lfs-s3")
    _git_config("--add", "lfs.customtransfer.git-lfs-s3.path", "git-lfs-s3")
    _git_config("--add", "lfs.customtransfer.git-lfs-s3.path", "git-lfs-s3")
    maybe_install_lfs_agent("origin")
    assert _config_get_all("remote.origin.lfsurl") == [LFS_URL]
    assert _config_get_all(SCOPED_AGENT_KEY) == ["git-lfs-s3"]
    assert _config_get_all("lfs.customtransfer.git-lfs-s3.path") == ["git-lfs-s3"]
    # The legacy unscoped value is tolerated, not rewritten and not removed.
    assert _config_get_all("lfs.standalonetransferagent") == ["git-lfs-s3"]


def test_foreign_unscoped_agent_blocks_everything(temp_git_repo):
    _add_remote("origin", S3_URL)
    _git_config("--add", "lfs.standalonetransferagent", "some-other-agent")
    maybe_install_lfs_agent("origin")
    assert _config_get("lfs.standalonetransferagent") == "some-other-agent"
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") is None
    assert _config_get("remote.origin.lfsurl") is None
    assert _config_get(SCOPED_AGENT_KEY) is None


def test_per_remote_lfsurl_blocks_install(temp_git_repo):
    _add_remote("origin", S3_URL)
    _git_config("--add", "remote.origin.lfsurl", "https://lfs-alias.git-remote-s3.test/bucket/prefix")
    maybe_install_lfs_agent("origin")
    assert _config_get("lfs.standalonetransferagent") is None
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") is None
    assert _config_get(SCOPED_AGENT_KEY) is None


def test_user_set_lfsurl_is_not_stomped(temp_git_repo):
    _add_remote("origin", S3_URL)
    _git_config("--add", "remote.origin.lfsurl", "https://lfs.example.com/foo")
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") == "https://lfs.example.com/foo"


def test_per_remote_lfsurl_for_different_remote_does_not_block(temp_git_repo):
    _add_remote("origin", S3_URL)
    _add_remote("other", "s3://other-bucket/other-prefix")
    _git_config("--add", "remote.other.lfsurl", "https://lfs-alias.git-remote-s3.test/bucket/prefix")
    maybe_install_lfs_agent("origin")
    assert _config_get_all("remote.origin.lfsurl") == [LFS_URL]
    assert _config_get_all(SCOPED_AGENT_KEY) == ["git-lfs-s3"]


def test_nothing_is_written_for_a_non_s3_remote(temp_git_repo):
    _add_remote("origin", "https://github.com/example/repo.git")
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") is None
    assert _config_get("lfs.standalonetransferagent") is None
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") is None


def test_nothing_is_written_when_the_remote_has_no_url(temp_git_repo):
    maybe_install_lfs_agent("origin")
    assert _config_get("lfs.standalonetransferagent") is None
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") is None


def test_lfsurl_preserves_dns_bucket_alias(temp_git_repo):
    _add_remote("origin", "s3://profile@demos.git.example.com/vendors/extrahop")
    expected = synthetic_lfs_url("demos.git.example.com", "vendors/extrahop")
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") == expected
    assert _config_get(f"lfs.{expected}.standalonetransferagent") == "git-lfs-s3"


def test_lfsurl_percent_encodes_reserved_chars_in_prefix(temp_git_repo):
    """git-lfs resolves an unencoded '%' endpoint as <unknown>, so the auto-installer must write
    the encoded URL into both keys — they only pair up if they are byte-identical."""
    _add_remote("nasty", "s3://my-bucket/deep dir/repo%zz")
    expected = synthetic_lfs_url("my-bucket", "deep dir/repo%zz")
    assert expected.endswith("/my-bucket/deep%20dir/repo%25zz")

    maybe_install_lfs_agent("nasty")

    assert _config_get_all("remote.nasty.lfsurl") == [expected]
    assert _config_get_all(f"lfs.{expected}.standalonetransferagent") == ["git-lfs-s3"]


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "No"])
def test_opt_out_env_var_skips_install(temp_git_repo, monkeypatch, value):
    _add_remote("origin", S3_URL)
    monkeypatch.setenv("GIT_REMOTE_S3_AUTO_INSTALL_LFS", value)
    maybe_install_lfs_agent("origin")
    assert _config_get("remote.origin.lfsurl") is None
    assert _config_get("lfs.standalonetransferagent") is None
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") is None
    assert _config_get(SCOPED_AGENT_KEY) is None


def test_s3remote_installs_agent_on_first_s3_use(temp_git_repo):
    _add_remote("origin", "s3://test-bucket/test-prefix")
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
        assert _config_get("remote.origin.lfsurl") is None
        remote._ensure_s3()
    assert _config_get("remote.origin.lfsurl") == synthetic_lfs_url("test-bucket", "test-prefix")
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") == "git-lfs-s3"


def test_s3remote_without_remote_name_does_not_install(temp_git_repo):
    _add_remote("origin", "s3://test-bucket/test-prefix")
    with patch("boto3.Session.client") as client_mock:
        client_mock.return_value.head_bucket.return_value = {}
        client_mock.return_value.list_objects_v2.return_value = {"Contents": []}
        S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")._ensure_s3()
    assert _config_get("remote.origin.lfsurl") is None
    assert _config_get("lfs.customtransfer.git-lfs-s3.path") is None
