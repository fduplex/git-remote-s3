import os
import subprocess
from io import BytesIO, StringIO
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from conftest import git_config_get as _config_get
from git_remote_s3 import S3Remote, UriScheme, gitwal
from git_remote_s3.remote import BucketNotFoundError, NotAuthorizedError
from remote_test import S3Exceptions

BUCKET = "test_bucket"
PREFIX = "test_prefix"
URL = f"s3://{BUCKET}/{PREFIX}"
REGION_KEY = "remote.origin.s3region"


class _TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def _remote(**kwargs) -> S3Remote:
    return S3Remote(UriScheme.S3, None, BUCKET, PREFIX, **kwargs)


@patch("boto3.Session")
def test_capabilities_and_options_build_no_aws_client(session_cls, capsys):
    remote = _remote(remote_name="origin", remote_url=URL)

    remote.process_cmd("capabilities\n")
    remote.process_cmd("option progress true\n")
    remote.process_cmd("option verbosity 2\n")

    session_cls.assert_not_called()
    assert remote._s3 is None
    out = capsys.readouterr().out
    assert out.startswith("*push\n*fetch\noption\n\n")
    assert out.endswith("ok\nok\n")
    # Options arriving before any S3 work must still be recorded on the instance.
    assert remote.progress is True
    assert remote.verbosity == 2


@patch("git_remote_s3.remote.maybe_install_lfs_agent")
@patch("git_remote_s3.remote._git_config_get", return_value="eu-west-1")
@patch("boto3.Session.client")
def test_cached_region_skips_head_bucket(client_mock, config_get, install_mock):
    client = client_mock.return_value

    _remote(remote_name="origin", remote_url=URL)._ensure_s3()

    config_get.assert_any_call(REGION_KEY)
    client_mock.assert_called_once_with("s3", region_name="eu-west-1")
    client.head_bucket.assert_not_called()


@patch("boto3.Session.client")
def test_detected_region_is_cached_in_repo_config(client_mock, temp_git_repo):
    client_mock.return_value.head_bucket.return_value = {"BucketRegion": "ap-south-1"}

    _remote(remote_name="origin", remote_url=URL)._ensure_s3()

    assert _config_get(REGION_KEY) == "ap-south-1"


@patch("boto3.Session.client")
def test_undetectable_region_is_not_cached(client_mock, temp_git_repo):
    client_mock.return_value.head_bucket.return_value = {}

    _remote(remote_name="origin", remote_url=URL)._ensure_s3()

    assert _config_get(REGION_KEY) is None


@patch("boto3.Session.client")
def test_detected_region_is_cached_in_the_submodules_own_config(client_mock, temp_git_repo, monkeypatch):
    # A submodule's config lives under .git/modules/<name>/; writing through `git config` rather
    # than by touching a config file directly lets git resolve that redirection for us.
    os.makedirs(f"{temp_git_repo}/.git/modules")
    subprocess.run(
        ["git", "init", "-q", "--separate-git-dir", f"{temp_git_repo}/.git/modules/sub", f"{temp_git_repo}/sub"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    monkeypatch.chdir(f"{temp_git_repo}/sub")
    client_mock.return_value.head_bucket.return_value = {"BucketRegion": "ap-south-1"}

    _remote(remote_name="origin", remote_url=URL)._ensure_s3()

    assert _config_get(REGION_KEY) == "ap-south-1"
    monkeypatch.chdir(temp_git_repo)
    assert _config_get(REGION_KEY) is None


@patch("boto3.Session.client")
def test_url_as_remote_name_is_not_cached(client_mock, temp_git_repo):
    # git invokes the helper with argv[1] == the URL for a push to a raw URL; there is no remote
    # section to cache under, so the region is detected and dropped.
    client_mock.return_value.head_bucket.return_value = {"BucketRegion": "ap-south-1"}

    _remote(remote_name=URL, remote_url=URL)._ensure_s3()

    assert _config_get(f"remote.{URL}.s3region") is None
    client_mock.assert_any_call("s3", region_name="ap-south-1")


@pytest.mark.parametrize("code", ["PermanentRedirect", "301", "AuthorizationHeaderMalformed"])
@patch("git_remote_s3.remote.maybe_install_lfs_agent")
@patch("git_remote_s3.remote._git_config_run")
@patch("git_remote_s3.remote._git_config_get")
@patch("boto3.Session.client")
def test_stale_cached_region_is_dropped_and_the_call_retried(client_mock, config_get, config_run, install_mock, code):
    # A region-pinned client fails SigV4 scope verification with AuthorizationHeaderMalformed
    # rather than being redirected, so that code has to drop the cache too.
    config = {REGION_KEY: "us-east-1"}
    config_get.side_effect = config.get
    config_run.side_effect = lambda *args: config.pop(args[-1], None) if "--unset" in args else None
    client = client_mock.return_value
    client.exceptions = S3Exceptions
    client.head_bucket.return_value = {"BucketRegion": "eu-west-1"}
    client.get_object.side_effect = [
        ClientError({"Error": {"Code": code}}, "GetObject"),
        {"Body": BytesIO(gitwal.dump(gitwal.Manifest()).encode("utf-8")), "ETag": '"etag"'},
    ]

    remote = _remote(remote_name="origin", remote_url=URL)

    refs = remote.list_refs()
    assert refs is not None
    assert refs.refs == {}
    assert config_run.call_args_list[0].args == ("--local", "--unset", REGION_KEY)
    client_mock.assert_any_call("s3", region_name="eu-west-1")


@patch("git_remote_s3.remote.maybe_install_lfs_agent")
@patch("git_remote_s3.remote._git_config_run")
@patch("git_remote_s3.remote._git_config_get", return_value="us-east-1")
@patch("boto3.Session.client")
def test_unrelated_client_error_is_not_retried(client_mock, config_get, config_run, install_mock):
    client = client_mock.return_value
    client.exceptions = S3Exceptions
    client.get_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    with pytest.raises(NotAuthorizedError):
        _remote(remote_name="origin", remote_url=URL).list_refs()

    config_run.assert_not_called()
    assert client.get_object.call_count == 1


@patch("boto3.Session.client")
def test_no_authz_probe_before_the_first_list(client_mock, capsys):
    client = client_mock.return_value
    client.exceptions = S3Exceptions
    client.get_object.return_value = {
        "Body": BytesIO(gitwal.dump(gitwal.Manifest()).encode("utf-8")),
        "ETag": '"etag"',
    }

    remote = _remote()
    remote._ensure_s3()
    client.get_object.assert_not_called()

    remote.cmd_list()

    # One GET of the manifest is the whole ref inventory.
    assert [c.kwargs["Key"] for c in client.get_object.call_args_list] == [f"{PREFIX}/gitwal.json"]


@patch("boto3.Session.client")
def test_missing_bucket_reported_from_the_list_path(client_mock):
    client = client_mock.return_value
    client.exceptions = S3Exceptions
    client.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchBucket"}}, "GetObject")

    with pytest.raises(BucketNotFoundError) as e:
        _remote().cmd_list()

    assert str(e.value) == f"Bucket {BUCKET} not found."


@patch("boto3.Session.client")
def test_missing_permission_reported_from_the_list_path(client_mock):
    client = client_mock.return_value
    client.exceptions = S3Exceptions
    client.get_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    with pytest.raises(NotAuthorizedError) as e:
        _remote().cmd_list()

    assert str(e.value) == f"Not authorized to perform GetObject on the S3 bucket {BUCKET}."


@patch("boto3.Session.client")
def test_connecting_notice_is_shown_on_a_tty(client_mock):
    stderr = _TtyStringIO()

    with patch("sys.stderr", stderr):
        _remote()._ensure_s3()

    rendered = stderr.getvalue()
    assert rendered.startswith(f"\rgit-remote-s3: connecting to {BUCKET}...")
    assert rendered.endswith("\r")
    assert "\n" not in rendered


@patch("boto3.Session.client")
def test_connecting_notice_is_suppressed_off_a_tty(client_mock):
    stderr = StringIO()

    with patch("sys.stderr", stderr):
        _remote()._ensure_s3()

    assert stderr.getvalue() == ""


@patch("boto3.Session.client")
def test_connecting_notice_is_suppressed_when_git_asked_for_silence(client_mock):
    stderr = _TtyStringIO()
    remote = _remote()
    remote.verbosity = 0

    with patch("sys.stderr", stderr):
        remote._ensure_s3()

    assert stderr.getvalue() == ""
