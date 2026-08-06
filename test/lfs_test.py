import json
import os
import subprocess
import tempfile
from io import StringIO
from types import SimpleNamespace

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from git_remote_s3 import lfs
from git_remote_s3.common import TRANSFER_CONFIG


def test_resolve_git_dir_returns_absolute_path_in_repo():
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["git", "init", "--quiet", tmp],
            check=True,
        )
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            git_dir = lfs._resolve_git_dir()
        finally:
            os.chdir(cwd)
        assert os.path.isabs(git_dir)
        assert os.path.isdir(git_dir)
        assert os.path.samefile(git_dir, os.path.join(tmp, ".git"))


def test_resolve_git_dir_resolves_submodule_gitlink():
    """In a submodule worktree, .git is a file pointing at the parent's
    .git/modules/<path>/. _resolve_git_dir() must follow the gitlink instead
    of treating .git as a directory."""
    with tempfile.TemporaryDirectory() as tmp:
        sub_src = os.path.join(tmp, "sub-src")
        parent = os.path.join(tmp, "parent")
        for path in (sub_src, parent):
            subprocess.run(["git", "init", "--quiet", path], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    path,
                    "commit",
                    "--quiet",
                    "--allow-empty",
                    "-m",
                    "init",
                    "--no-gpg-sign",
                ],
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t",
                },
            )
        subprocess.run(
            [
                "git",
                "-C",
                parent,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "--quiet",
                sub_src,
                "sub",
            ],
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        sub_worktree = os.path.join(parent, "sub")
        assert os.path.isfile(os.path.join(sub_worktree, ".git")), (
            "submodule .git should be a gitlink file, not a directory"
        )

        cwd = os.getcwd()
        try:
            os.chdir(sub_worktree)
            git_dir = lfs._resolve_git_dir()
        finally:
            os.chdir(cwd)

        assert os.path.isabs(git_dir)
        assert os.path.isdir(git_dir), f"resolved gitdir must be a real directory, got {git_dir!r}"
        expected = os.path.join(parent, ".git", "modules", "sub")
        assert os.path.samefile(git_dir, expected)


@patch("git_remote_s3.lfs.subprocess.check_output")
def test_resolve_git_dir_invokes_rev_parse(check_output_mock):
    check_output_mock.return_value = "/fake/path/to/.git\n"
    result = lfs._resolve_git_dir()
    check_output_mock.assert_called_once_with(["git", "rev-parse", "--absolute-git-dir"], text=True)
    assert result == "/fake/path/to/.git"


@patch("git_remote_s3.lfs.os.makedirs")
@patch("git_remote_s3.lfs._resolve_git_dir")
def test_download_uses_resolved_gitdir_for_temp_dir(
    resolve_mock,
    makedirs_mock,
):
    resolve_mock.return_value = "/resolved/.git/modules/sub"

    proc = lfs.LFSProcess.__new__(lfs.LFSProcess)
    proc.prefix = "test_prefix"
    proc.bucket = "test_bucket"
    proc.profile = None
    proc.s3_bucket = SimpleNamespace()
    captured = {}

    def fake_download_file(Key, Filename, Callback, Config):
        captured["Key"] = Key
        captured["Filename"] = Filename
        captured["Config"] = Config

    proc.s3_bucket.download_file = fake_download_file
    proc.init_s3_bucket = lambda: None

    with patch("sys.stdout"):
        proc.download({"event": "download", "oid": "abc123", "size": 1})

    expected_dir = "/resolved/.git/modules/sub/lfs/tmp"
    assert captured["Filename"] == f"{expected_dir}/abc123"
    assert captured["Config"] is TRANSFER_CONFIG
    makedirs_mock.assert_called_once_with(expected_dir, exist_ok=True)


def _make_upload_proc(client):
    proc = lfs.LFSProcess.__new__(lfs.LFSProcess)
    proc.prefix = "test_prefix"
    proc.bucket = "test_bucket"
    proc.profile = None
    bucket = MagicMock()
    bucket.name = "test_bucket"
    bucket.meta.client = client
    proc.s3_bucket = bucket
    proc.init_s3_bucket = lambda: None
    return proc, bucket


def _head_object_error(code):
    return ClientError({"Error": {"Code": code}}, "HeadObject")


def test_upload_skips_when_object_exists_via_head_object():
    # The existence check must be a single HeadObject, not a ListObjectsV2 (which incurs
    # a listing-permission dependency and is slower under high object counts).
    client = MagicMock()
    client.exceptions.ClientError = ClientError
    client.head_object.return_value = {}
    proc, bucket = _make_upload_proc(client)

    with patch("sys.stdout", new_callable=StringIO) as stdout:
        proc.upload({"oid": "abc123", "path": "/tmp/whatever"})

    client.head_object.assert_called_once_with(Bucket="test_bucket", Key="test_prefix/lfs/abc123")
    bucket.objects.filter.assert_not_called()
    bucket.upload_file.assert_not_called()
    event = json.loads(stdout.getvalue().strip())
    assert event == {"event": "complete", "oid": "abc123"}


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_upload_proceeds_when_head_object_reports_not_found(code):
    # On AWS the HeadObject error body is empty, so botocore reports the raw HTTP status "404";
    # S3-compatible backends send a body and name the condition instead. All three mean the same
    # thing, and reading any of them as a hard failure would break every first upload.
    client = MagicMock()
    client.exceptions.ClientError = ClientError
    client.head_object.side_effect = _head_object_error(code)
    proc, bucket = _make_upload_proc(client)

    with patch("sys.stdout", new_callable=StringIO) as stdout:
        proc.upload({"oid": "abc123", "path": "/tmp/whatever"})

    bucket.upload_file.assert_called_once()
    assert bucket.upload_file.call_args.args[:2] == ("/tmp/whatever", "test_prefix/lfs/abc123")
    assert bucket.upload_file.call_args.kwargs["Config"] is TRANSFER_CONFIG
    event = json.loads(stdout.getvalue().strip())
    assert event == {"event": "complete", "oid": "abc123"}


def test_upload_reports_error_on_non_404_head_object_failure():
    # A 403 (e.g. missing s3:GetObject/HeadObject) must surface as an upload error, not be
    # mistaken for "object does not exist".
    client = MagicMock()
    client.exceptions.ClientError = ClientError
    client.head_object.side_effect = _head_object_error("403")
    proc, bucket = _make_upload_proc(client)

    with patch("sys.stdout", new_callable=StringIO) as stdout:
        proc.upload({"oid": "abc123", "path": "/tmp/whatever"})

    bucket.upload_file.assert_not_called()
    event = json.loads(stdout.getvalue().strip())
    assert event["event"] == "complete"
    assert event["error"]["code"] == 2
