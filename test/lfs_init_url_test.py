# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0

import json
import subprocess
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from git_remote_s3 import lfs

_FACADE_URL = "https://demos.git.example.com/core/cli"
_S3_URL = "s3://myprofile@demos.git.example.com/core/cli"


@pytest.fixture
def isolated_git_config(tmp_path, monkeypatch):
    """A repo with no remotes, and git config scoped to a throwaway global file.

    Mirrors uv's cache db dir: 'git init' with no 'git remote add' ever run.
    """
    repo = tmp_path / "db"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(repo)
    return config


def _add_insteadof(config, base, url):
    subprocess.run(
        ["git", "config", "--file", str(config), "--add", f"url.{base}.insteadOf", url],
        check=True,
    )


def test_insteadof_rewrites_facade_url(isolated_git_config):
    _add_insteadof(isolated_git_config, _S3_URL, _FACADE_URL)
    assert lfs._apply_url_insteadof(_FACADE_URL) == _S3_URL


def test_insteadof_longest_matching_prefix_wins(isolated_git_config):
    _add_insteadof(isolated_git_config, "s3://bucket/", "https://demos.git.example.com/")
    _add_insteadof(isolated_git_config, _S3_URL, _FACADE_URL)
    _add_insteadof(isolated_git_config, "s3://other/", "https://")

    assert lfs._apply_url_insteadof(_FACADE_URL) == _S3_URL
    assert lfs._apply_url_insteadof("https://demos.git.example.com/core/other") == "s3://bucket/core/other"


def test_insteadof_supports_multiple_values_for_one_base(isolated_git_config):
    _add_insteadof(isolated_git_config, _S3_URL, _FACADE_URL)
    _add_insteadof(isolated_git_config, _S3_URL, "ssh://git@demos.git.example.com/core/cli")

    assert lfs._apply_url_insteadof("ssh://git@demos.git.example.com/core/cli") == _S3_URL


def test_insteadof_leaves_unmatched_url_untouched(isolated_git_config):
    _add_insteadof(isolated_git_config, _S3_URL, _FACADE_URL)
    assert lfs._apply_url_insteadof("https://github.com/foo/bar") == "https://github.com/foo/bar"


def test_insteadof_with_no_rules_configured(isolated_git_config):
    assert lfs._git_url_insteadof_rules() == []
    assert lfs._apply_url_insteadof(_FACADE_URL) == _FACADE_URL


@pytest.mark.parametrize("url", ["s3://bucket/prefix", "s3+zip://profile@bucket/prefix"])
def test_resolve_s3_uri_passes_through_s3_urls(isolated_git_config, url):
    assert lfs._resolve_s3_uri_from_url(url) == url


def test_resolve_s3_uri_rewrites_facade_url(isolated_git_config):
    _add_insteadof(isolated_git_config, _S3_URL, _FACADE_URL)
    assert lfs._resolve_s3_uri_from_url(_FACADE_URL) == _S3_URL


def test_resolve_s3_uri_returns_none_when_rewrite_is_not_s3(isolated_git_config):
    _add_insteadof(isolated_git_config, "ssh://git@example.com/", _FACADE_URL)
    assert lfs._resolve_s3_uri_from_url(_FACADE_URL) is None


def _run_main_with_init(remote):
    """Drives main() through one init event; EOF ends it with a decode error."""
    stdin = StringIO(json.dumps({"event": "init", "operation": "download", "remote": remote}) + "\n")
    stdout = StringIO()
    with patch("sys.argv", ["git-lfs-s3"]), patch("sys.stdin", stdin), patch("sys.stdout", stdout):
        with patch("git_remote_s3.lfs.LFSProcess") as process:
            with pytest.raises((json.JSONDecodeError, SystemExit)) as exc:
                lfs.main()
    return process, stdout.getvalue(), exc.value


def test_init_with_facade_url_resolves_s3_uri_without_any_remote(isolated_git_config):
    _add_insteadof(isolated_git_config, _S3_URL, _FACADE_URL)

    process, _, _ = _run_main_with_init(_FACADE_URL)

    process.assert_called_once_with(s3uri=_S3_URL, remote_name=_FACADE_URL)


def test_init_with_unmappable_url_reports_a_protocol_error(isolated_git_config):
    process, output, exc = _run_main_with_init(_FACADE_URL)

    process.assert_not_called()
    assert isinstance(exc, SystemExit)
    assert exc.code == 1
    event = json.loads(output.strip())
    assert event["error"]["code"] == 2
    assert "insteadOf" in event["error"]["message"]
    assert _FACADE_URL in event["error"]["message"]


def test_init_with_remote_name_still_uses_git_remote_get_url(isolated_git_config):
    subprocess.run(["git", "remote", "add", "origin", "s3://bucket/repo"], check=True)

    process, _, _ = _run_main_with_init("origin")

    process.assert_called_once_with(s3uri="s3://bucket/repo", remote_name="origin")


def test_init_with_invalid_remote_name_is_rejected(isolated_git_config):
    _, _, exc = _run_main_with_init("bad~name")

    assert isinstance(exc, SystemExit)
    assert exc.code == 1


def test_init_with_facade_url_builds_a_working_process(isolated_git_config):
    """End to end through the real LFSProcess: bucket and prefix come out of the
    rewritten URL, the init ack goes out, and nothing reaches AWS."""
    _add_insteadof(isolated_git_config, "s3://myprofile@bucket/core/cli", _FACADE_URL)
    stdin = StringIO(json.dumps({"event": "init", "operation": "download", "remote": _FACADE_URL}) + "\n")
    stdout = StringIO()

    with patch("sys.argv", ["git-lfs-s3"]), patch("sys.stdin", stdin), patch("sys.stdout", stdout):
        with patch("git_remote_s3.lfs.boto3.Session", MagicMock()) as session:
            with patch.object(lfs.LFSProcess, "__init__", autospec=True, side_effect=lfs.LFSProcess.__init__) as init:
                with pytest.raises(json.JSONDecodeError):
                    lfs.main()

    process = init.call_args.args[0]
    assert (process.bucket, process.prefix, process.profile) == ("bucket", "core/cli", "myprofile")
    assert stdout.getvalue().strip() == "{}"
    session.assert_not_called()
