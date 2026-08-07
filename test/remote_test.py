import contextlib
import datetime
import os
import subprocess
import tempfile
import threading
from io import StringIO, BytesIO
from unittest.mock import patch

import boto3.exceptions
import botocore
import botocore.client
import botocore.exceptions
import pytest
from botocore.exceptions import ClientError

from git_remote_s3 import S3Remote, UriScheme, git
from git_remote_s3.remote import NotAuthorizedError, TransferProgress

SHA1 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8a"
SHA2 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8b"
MOVED_SHA = "c105d19ba64965d2c9d3d3246e7269059ef8bb8c"
NULL_SHA = "0" * 40
INVALID_SHA = "z45"
BUNDLE_SUFFIX = ".bundle"
MOCK_BUNDLE_CONTENT = b"MOCK_BUNDLE_CONTENT"
ARCHIVE_SUFFIX = ".zip"
MOCK_ARCHIVE_CONTENT = b"MOCK_ARCHIVE_CONTENT"
BRANCH = "pytest"


@pytest.fixture(autouse=True)
def _not_shallow(request, monkeypatch):
    # This suite's cwd is the project's own checkout, whose shallowness depends on how it was
    # cloned (e.g. CI's fetch-depth:1). cmd_push's pre-flight shallow guard would otherwise make
    # every push test here environment-dependent. Tests that exercise the shallow-clone path
    # patch git.is_shallow_repository themselves and take precedence over this default.
    # test_is_shallow_repository_distinguishes_shallow_from_partial exercises the real function
    # against temp repos it controls and must see the genuine result, not this stub.
    if "real_git_shallow_check" in request.keywords:
        yield
        return
    monkeypatch.setattr(git, "is_shallow_repository", lambda: False)
    yield


def create_list_objects_v2_mock(
    *,
    protected=False,
    no_head=False,
    branch=BRANCH,
    shas,
):
    def s3_list_objects_v2_mock(Prefix, **kwargs):
        content = []
        for s in shas:
            content.append(
                {
                    "Key": f"test_prefix/refs/heads/{branch}/{s}.bundle",
                    "LastModified": datetime.datetime.now(),
                }
            )
        if protected:
            content.append(
                {
                    "Key": f"test_prefix/refs/heads/{branch}/PROTECTED#",
                    "LastModified": datetime.datetime.now(),
                }
            )
        if not no_head:
            content.append(
                {
                    "Key": "test_prefix/HEAD",
                    "LastModified": datetime.datetime.now(),
                }
            )
        return {
            # ty: heterogeneous mock dict; the "Key" value is always a str here
            "Contents": [c for c in content if c["Key"].startswith(Prefix)],  # ty: ignore[unresolved-attribute]
            "NextContinuationToken": None,
        }

    return s3_list_objects_v2_mock


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA1])
    assert s3_remote.bucket == "test_bucket"
    assert s3_remote.prefix == "test_prefix"
    assert s3_remote.s3 == session_client_mock.return_value
    session_client_mock.assert_any_call("s3")
    session_client_mock.return_value.get_object.return_value = {"Body": BytesIO(b"refs/heads/%b" % str.encode(BRANCH))}
    s3_remote.cmd_list()
    assert f"@refs/heads/{BRANCH} HEAD\n{SHA1} refs/heads/{BRANCH}\n\n" == stdout_mock.getvalue()


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_list_refs(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "nested/test_prefix")

    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"nested/test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            },
            {
                "Key": f"nested/test_prefix/refs/tags/v1/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            },
        ]
    }

    assert s3_remote.bucket == "test_bucket"
    assert s3_remote.prefix == "nested/test_prefix"
    assert s3_remote.s3 == session_client_mock.return_value
    session_client_mock.assert_any_call("s3")
    refs = s3_remote.list_refs(bucket=s3_remote.bucket, prefix=s3_remote.prefix)
    assert len(refs) == 2
    assert f"refs/heads/{BRANCH}/{SHA1}.bundle" in refs
    assert f"refs/tags/v1/{SHA1}.bundle" in refs
    refs_call = session_client_mock.return_value.list_objects_v2.call_args_list[-1]
    assert refs_call.kwargs["Prefix"] == "nested/test_prefix/refs"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_list_refs_scopes_listing_away_from_lfs_objects(session_client_mock, stdout_mock):
    # list_refs must narrow its ListObjectsV2 call to the refs/ subtree server-side, so an
    # LFS-heavy repo does not paginate through every "lfs/<oid>" object on every push/fetch.
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    def s3_list_objects_v2_mock(Prefix, **kwargs):
        content = [
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            },
            {
                "Key": "test_prefix/lfs/deadbeef",
                "LastModified": datetime.datetime.now(),
            },
        ]
        return {
            # ty: heterogeneous mock dict; the "Key" value is always a str here
            "Contents": [c for c in content if c["Key"].startswith(Prefix)],  # ty: ignore[unresolved-attribute]
            "NextContinuationToken": None,
        }

    session_client_mock.return_value.list_objects_v2.side_effect = s3_list_objects_v2_mock

    refs = s3_remote.list_refs(bucket=s3_remote.bucket, prefix=s3_remote.prefix)

    assert refs == [f"refs/heads/{BRANCH}/{SHA1}.bundle"]
    refs_call = session_client_mock.return_value.list_objects_v2.call_args_list[-1]
    assert refs_call.kwargs["Prefix"] == "test_prefix/refs"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_list_refs_empty_prefix_scopes_to_leading_slash_refs(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "")

    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            }
        ]
    }

    refs = s3_remote.list_refs(bucket=s3_remote.bucket, prefix=s3_remote.prefix)

    calls = session_client_mock.return_value.list_objects_v2.call_args_list
    # list_refs scopes server-side to the refs/ subtree ("<prefix>/refs"); for a
    # bucket-root repo (prefix=="") that is "/refs". Bundle keys for such a repo are written as
    # f"{prefix}/{ref}/..." = "/refs/...", carrying that same leading slash, so "/refs" still
    # matches them correctly -- this is unchanged behavior, not a quirk introduced by scoping.
    assert [c.kwargs["Prefix"] for c in calls] == ["/refs"]
    assert refs == [f"refs/heads/{BRANCH}/{SHA1}.bundle"]


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_nested_prefix(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "nested/test_prefix")

    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"nested/test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            },
            {
                "Key": "nested/test_prefix/HEAD",
                "LastModified": datetime.datetime.now(),
            },
        ]
    }
    assert s3_remote.bucket == "test_bucket"
    assert s3_remote.prefix == "nested/test_prefix"
    assert s3_remote.s3 == session_client_mock.return_value
    session_client_mock.assert_any_call("s3")
    session_client_mock.return_value.get_object.return_value = {"Body": BytesIO(b"refs/heads/%b" % str.encode(BRANCH))}
    s3_remote.cmd_list()
    assert f"@refs/heads/{BRANCH} HEAD\n{SHA1} refs/heads/{BRANCH}\n\n" == stdout_mock.getvalue()


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_no_head(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(
        shas=[SHA1], no_head=True
    )

    def error(**kwargs):
        raise botocore.exceptions.ClientError({"Error": {"Code": "NoSuchKey"}}, "get_object")

    session_client_mock.return_value.get_object.side_effect = error
    assert s3_remote.bucket == "test_bucket"
    assert s3_remote.prefix == "test_prefix"
    assert s3_remote.s3 == session_client_mock.return_value
    session_client_mock.assert_any_call("s3")
    s3_remote.cmd_list()
    assert f"{SHA1} refs/heads/{BRANCH}\n\n" == stdout_mock.getvalue()


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_with_head_not_exsting_ref(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA1])
    session_client_mock.return_value.get_object.return_value = {"Body": BytesIO(b"refs/heads/master")}
    assert s3_remote.bucket == "test_bucket"
    assert s3_remote.prefix == "test_prefix"
    assert s3_remote.s3 == session_client_mock.return_value
    session_client_mock.assert_any_call("s3")
    s3_remote.cmd_list()
    assert f"{SHA1} refs/heads/{BRANCH}\n\n" == stdout_mock.getvalue()


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_protected_branch(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(
        protected=True, shas=[SHA1]
    )

    session_client_mock.return_value.get_object.return_value = {"Body": BytesIO(b"refs/heads/%b" % str.encode(BRANCH))}
    assert s3_remote.bucket == "test_bucket"
    assert s3_remote.prefix == "test_prefix"
    assert s3_remote.s3 == session_client_mock.return_value
    session_client_mock.assert_any_call("s3")
    s3_remote.cmd_list()
    assert f"@refs/heads/{BRANCH} HEAD\n{SHA1} refs/heads/{BRANCH}\n\n" == stdout_mock.getvalue()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_no_force_unprotected_ancestor(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(
        protected=True, shas=[SHA1]
    )
    is_ancestor_mock.return_value = True
    assert s3_remote.s3 == session_client_mock.return_value
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    put_calls = [
        c for c in session_client_mock.return_value.put_object.call_args_list if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(put_calls) == 0
    upload_calls = session_client_mock.return_value.upload_file.call_args_list
    assert len(upload_calls) == 1
    assert upload_calls[0].kwargs["Key"].endswith(f"/{SHA1}.bundle")
    assert upload_calls[0].kwargs["Config"] is not None
    del_calls = [
        c
        for c in session_client_mock.return_value.delete_object.call_args_list
        if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(del_calls) == 1
    assert res == (f"ok refs/heads/{BRANCH}\n")


@patch("git_remote_s3.git.archive")
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_no_force_unprotected_ancestor_s3_zip(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock, archive_mock
):
    s3_remote = S3Remote(UriScheme.S3_ZIP, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1

    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name

    temp_file_archive = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=ARCHIVE_SUFFIX)
    with open(temp_file_archive.name, "wb") as f:
        f.write(MOCK_ARCHIVE_CONTENT)
    archive_mock.return_value = temp_file_archive.name

    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(
        protected=True, shas=[SHA1]
    )

    is_ancestor_mock.return_value = True

    assert s3_remote.s3 == session_client_mock.return_value

    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    put_calls = [
        c for c in session_client_mock.return_value.put_object.call_args_list if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(put_calls) == 0
    assert session_client_mock.return_value.upload_file.call_count == 2
    del_calls = [
        c
        for c in session_client_mock.return_value.delete_object.call_args_list
        if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(del_calls) == 1
    assert res == (f"ok refs/heads/{BRANCH}\n")


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_no_force_unprotected_no_ancestor(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])

    is_ancestor_mock.return_value = False
    assert s3_remote.s3 == session_client_mock.return_value
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    put_calls = [
        c
        for c in session_client_mock.return_value.put_object.call_args_list
        if not c.kwargs.get("Key", "").endswith(".lock")
    ]
    assert len(put_calls) == 0
    assert session_client_mock.return_value.delete_object.call_count == 0
    assert res.startswith("error")


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_force_no_ancestor(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    is_ancestor_mock.return_value = False
    assert s3_remote.s3 == session_client_mock.return_value
    res = s3_remote.cmd_push(f"push +refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    put_calls = [
        c for c in session_client_mock.return_value.put_object.call_args_list if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(put_calls) == 0
    assert session_client_mock.return_value.upload_file.call_count == 1
    del_calls = [
        c
        for c in session_client_mock.return_value.delete_object.call_args_list
        if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(del_calls) == 1
    assert res.startswith("ok")


@patch("git_remote_s3.git.archive")
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_force_no_ancestor_s3_zip(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock, archive_mock
):
    s3_remote = S3Remote(UriScheme.S3_ZIP, None, "test_bucket", "test_prefix")

    rev_parse_mock.return_value = SHA1

    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name

    temp_file_archive = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=ARCHIVE_SUFFIX)
    with open(temp_file_archive.name, "wb") as f:
        f.write(MOCK_ARCHIVE_CONTENT)
    archive_mock.return_value = temp_file_archive.name

    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])

    is_ancestor_mock.return_value = False

    assert s3_remote.s3 == session_client_mock.return_value

    res = s3_remote.cmd_push(f"push +refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    put_calls = [
        c for c in session_client_mock.return_value.put_object.call_args_list if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(put_calls) == 0
    assert session_client_mock.return_value.upload_file.call_count == 2
    del_calls = [
        c
        for c in session_client_mock.return_value.delete_object.call_args_list
        if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(del_calls) == 1
    assert res.startswith("ok")


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_force_no_ancestor_protected(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(
        protected=True, shas=[SHA2]
    )
    is_ancestor_mock.return_value = False
    assert s3_remote.s3 == session_client_mock.return_value
    res = s3_remote.cmd_push(f"push +refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    assert session_client_mock.return_value.put_object.call_count == 0
    assert session_client_mock.return_value.delete_object.call_count == 0
    assert res.startswith("error")


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_empty_bucket(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name

    session_client_mock.return_value.head_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "head_object"
    )

    is_ancestor_mock.return_value = False
    assert s3_remote.s3 == session_client_mock.return_value
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    put_calls = [
        c for c in session_client_mock.return_value.put_object.call_args_list if not c.kwargs["Key"].endswith(".lock")
    ]
    # Only the remote HEAD is written with put_object; the bundle goes through upload_file.
    assert len(put_calls) == 1
    assert put_calls[0].kwargs["Key"].endswith("/HEAD")
    assert session_client_mock.return_value.upload_file.call_count == 1
    del_calls = [
        c
        for c in session_client_mock.return_value.delete_object.call_args_list
        if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(del_calls) == 0
    assert res.startswith("ok")


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_head_init_and_stale_bundle_delete_both_run(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock
):
    # init_remote_head and the stale-bundle delete run concurrently (see cmd_push); both must
    # still take effect on an otherwise-successful push.
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    session_client_mock.return_value.head_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "head_object"
    )
    is_ancestor_mock.return_value = True

    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res == f"ok refs/heads/{BRANCH}\n"
    del_calls = [
        c
        for c in session_client_mock.return_value.delete_object.call_args_list
        if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(del_calls) == 1
    assert del_calls[0].kwargs["Key"] == f"test_prefix/refs/heads/{BRANCH}/{SHA2}.bundle"
    put_calls = [
        c for c in session_client_mock.return_value.put_object.call_args_list if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(put_calls) == 1
    assert put_calls[0].kwargs["Key"].endswith("/HEAD")


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_stale_bundle_delete_failure_still_errors(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock
):
    # Running init_remote_head and the stale-bundle delete concurrently must not swallow a
    # failure from either side: the error semantics stay the same as the sequential code.
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    session_client_mock.return_value.head_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "head_object"
    )
    is_ancestor_mock.return_value = True

    def delete_object_side_effect(Bucket, Key):
        if not Key.endswith(".lock"):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "delete_object")
        return {}

    session_client_mock.return_value.delete_object.side_effect = delete_object_side_effect

    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res.startswith("error")


@patch("git_remote_s3.git.archive")
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_empty_bucket_s3_zip(
    session_client_mock,
    bundle_mock,
    rev_parse_mock,
    is_ancestor_mock,
    archive_mock,
):
    s3_remote = S3Remote(UriScheme.S3_ZIP, None, "test_bucket", "test_prefix")

    rev_parse_mock.return_value = SHA1

    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name

    temp_file_archive = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=ARCHIVE_SUFFIX)
    with open(temp_file_archive.name, "wb") as f:
        f.write(MOCK_ARCHIVE_CONTENT)
    archive_mock.return_value = temp_file_archive.name

    session_client_mock.return_value.head_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "head_object"
    )

    is_ancestor_mock.return_value = False

    assert s3_remote.s3 == session_client_mock.return_value

    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    put_calls = [
        c for c in session_client_mock.return_value.put_object.call_args_list if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(put_calls) == 1
    assert put_calls[0].kwargs["Key"].endswith("/HEAD")
    assert session_client_mock.return_value.upload_file.call_count == 2
    del_calls = [
        c
        for c in session_client_mock.return_value.delete_object.call_args_list
        if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(del_calls) == 0
    assert res.startswith("ok")


@patch("git_remote_s3.git.archive")
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("git_remote_s3.git.get_last_commit_message")
@patch("boto3.Session.client")
def test_cmd_push_s3_zip_upload_file_params(
    session_client_mock,
    get_last_commit_message_mock,
    bundle_mock,
    rev_parse_mock,
    is_ancestor_mock,
    archive_mock,
):
    s3_remote = S3Remote(UriScheme.S3_ZIP, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    get_last_commit_message_mock.return_value = "test commit message"

    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name

    temp_file_archive = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=ARCHIVE_SUFFIX)
    with open(temp_file_archive.name, "wb") as f:
        f.write(MOCK_ARCHIVE_CONTENT)
    archive_mock.return_value = temp_file_archive.name

    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])

    is_ancestor_mock.return_value = True

    s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    upload_calls = session_client_mock.return_value.upload_file.call_args_list
    assert len(upload_calls) == 2

    # Check bundle upload
    bundle_call = upload_calls[0]
    assert bundle_call.kwargs["Bucket"] == "test_bucket"
    assert bundle_call.kwargs["Key"].endswith(".bundle")
    assert bundle_call.kwargs["Filename"] == temp_file.name
    assert bundle_call.kwargs["Config"].max_concurrency == 8

    # Check zip upload
    zip_call = upload_calls[1]
    assert zip_call.kwargs["Bucket"] == "test_bucket"
    assert zip_call.kwargs["Key"].endswith("repo.zip")
    assert zip_call.kwargs["Filename"] == temp_file_archive.name
    extra_args = zip_call.kwargs["ExtraArgs"]
    assert extra_args["Metadata"]["codepipeline-artifact-revision-summary"] == "test commit message"
    assert extra_args["ContentDisposition"] == f"attachment; filename=repo-{SHA1[:8]}.zip"


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_multiple_heads(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA1, SHA2])
    is_ancestor_mock.return_value = False
    assert s3_remote.s3 == session_client_mock.return_value
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    assert session_client_mock.return_value.put_object.call_count == 0
    assert session_client_mock.return_value.delete_object.call_count == 0
    assert res.startswith("error")


@patch("git_remote_s3.git.unbundle")
@patch("boto3.Session.client")
def test_cmd_fetch(session_client_mock, unbundle_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_fetch(f"fetch {SHA1} refs/heads/{BRANCH}")

    unbundle_mock.assert_called_once()
    assert session_client_mock.return_value.download_file.call_count == 1


@patch("git_remote_s3.git.unbundle")
@patch("boto3.Session.client")
def test_cmd_fetch_same_ref(session_client_mock, unbundle_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_fetch(f"fetch {SHA1} refs/heads/{BRANCH}")
    s3_remote.cmd_fetch(f"fetch {SHA1} refs/heads/{BRANCH}")
    unbundle_mock.assert_called_once()
    assert session_client_mock.return_value.download_file.call_count == 1


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_option("option verbosity 2")
    assert stdout_mock.getvalue().startswith("ok\n")
    s3_remote.cmd_option("option concurrency 1")
    assert stdout_mock.getvalue().endswith("unsupported\n")


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_capabilities(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_capabilities()
    assert "fetch" in stdout_mock.getvalue()
    assert "option" in stdout_mock.getvalue()
    assert "push" in stdout_mock.getvalue()


@patch("boto3.Session.client")
def test_cmd_push_delete(session_client_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            }
        ]
    }
    assert s3_remote.s3 == session_client_mock.return_value
    res = s3_remote.cmd_push(f"push :refs/heads/{BRANCH}")
    assert session_client_mock.return_value.delete_object.call_count == 1
    assert res == (f"ok refs/heads/{BRANCH}\n")


@patch("boto3.Session.client")
def test_cmd_push_delete_s3_zip(session_client_mock):
    s3_remote = S3Remote(UriScheme.S3_ZIP, None, "test_bucket", "test_prefix")

    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            },
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/repo.zip",
                "LastModified": datetime.datetime.now(),
            },
        ]
    }
    assert s3_remote.s3 == session_client_mock.return_value
    res = s3_remote.cmd_push(f"push :refs/heads/{BRANCH}")
    assert session_client_mock.return_value.delete_object.call_count == 2
    assert res == (f"ok refs/heads/{BRANCH}\n")


@patch("boto3.Session.client")
def test_cmd_push_delete_fails_with_multiple_heads(session_client_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            },
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA2}.bundle",
                "LastModified": datetime.datetime.now(),
            },
        ]
    }
    assert s3_remote.s3 == session_client_mock.return_value
    res = s3_remote.cmd_push(f"push :refs/heads/{BRANCH}")
    assert session_client_mock.return_value.delete_object.call_count == 0
    assert res.startswith("error")


@patch("boto3.Session.client")
def test_cmd_push_delete_fails_with_multiple_heads_s3_zip(session_client_mock):
    s3_remote = S3Remote(UriScheme.S3_ZIP, None, "test_bucket", "test_prefix")

    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            },
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA2}.bundle",
                "LastModified": datetime.datetime.now(),
            },
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/repo.zip",
                "LastModified": datetime.datetime.now(),
            },
        ]
    }
    assert s3_remote.s3 == session_client_mock.return_value
    res = s3_remote.cmd_push(f"push :refs/heads/{BRANCH}")
    assert session_client_mock.return_value.delete_object.call_count == 0
    assert res.startswith("error")


@patch("git_remote_s3.git.bundle")
@patch("git_remote_s3.git.rev_parse")
@patch("boto3.Session.client")
def test_simultaneous_pushes_single_bundle_remains(session_client_mock, rev_parse_mock, bundle_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    storage = {}
    lock_keys = []
    storage_lock = threading.Lock()

    def list_objects_v2_side_effect(Bucket, Prefix, **kwargs):
        with storage_lock:
            if Prefix.endswith("/LOCKS/"):
                contents = [{"Key": k, "LastModified": datetime.datetime.now()} for k in lock_keys]
            else:
                contents = [
                    {"Key": k, "LastModified": datetime.datetime.now()} for k in storage if k.startswith(Prefix)
                ]
        return {"Contents": contents, "NextContinuationToken": None}

    def put_object_side_effect(Bucket, Key, Body=None, **kwargs):
        with storage_lock:
            # Simulate S3 conditional writes for lock creation using If-None-Match
            if Key.endswith(".lock"):
                if kwargs.get("IfNoneMatch") == "*":
                    if Key in lock_keys:
                        raise botocore.exceptions.ClientError(
                            {
                                "ResponseMetadata": {"HTTPStatusCode": 412},
                                "Error": {"Code": "PreconditionFailed"},
                            },
                            "put_object",
                        )
                    lock_keys.append(Key)
                else:
                    lock_keys.append(Key)
            else:
                data = Body.read() if hasattr(Body, "read") else Body or b""
                storage[Key] = data
        return {}

    def upload_file_side_effect(Filename, Bucket, Key, **kwargs):
        with open(Filename, "rb") as f:
            data = f.read()
        with storage_lock:
            storage[Key] = data

    def delete_object_side_effect(Bucket, Key):
        with storage_lock:
            storage.pop(Key, None)
            with contextlib.suppress(ValueError):
                lock_keys.remove(Key)
        return {}

    session_client_mock.return_value.list_objects_v2.side_effect = list_objects_v2_side_effect
    session_client_mock.return_value.put_object.side_effect = put_object_side_effect
    session_client_mock.return_value.upload_file.side_effect = upload_file_side_effect
    session_client_mock.return_value.delete_object.side_effect = delete_object_side_effect
    # Provide a concrete LastModified for lock head checks (non-stale)
    session_client_mock.return_value.head_object.side_effect = lambda Bucket, Key: {
        "LastModified": datetime.datetime.now()
    }

    def rev_parse_side_effect(local_ref: str):
        return SHA1 if "branch1" in local_ref else SHA2

    rev_parse_mock.side_effect = rev_parse_side_effect

    def bundle_side_effect(folder: str, sha: str, ref: str, **kwargs):
        temp_file = tempfile.NamedTemporaryFile(dir=folder, suffix=BUNDLE_SUFFIX, delete=False)
        with open(temp_file.name, "wb") as f:
            f.write(MOCK_BUNDLE_CONTENT)
        return temp_file.name

    bundle_mock.side_effect = bundle_side_effect

    remote_ref = f"refs/heads/{BRANCH}"

    t1 = threading.Thread(target=s3_remote.cmd_push, args=(f"push refs/heads/branch1:{remote_ref}",))
    t2 = threading.Thread(target=s3_remote.cmd_push, args=(f"push refs/heads/branch2:{remote_ref}",))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    with storage_lock:
        bundles = [k for k in storage if k.startswith(f"test_prefix/{remote_ref}/") and k.endswith(".bundle")]

    # Only one push should succeed due to per-ref locking; the other will fail to acquire lock
    assert len(bundles) == 1
    assert bundles[0].endswith(f"/{SHA1}.bundle") or bundles[0].endswith(f"/{SHA2}.bundle")


@patch("boto3.Session.client")
def test_acquire_lock_deletes_stale_and_reacquires(session_client_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    # Ensure initial list call in constructor succeeds
    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [],
        "NextContinuationToken": None,
    }

    # Simulate existing lock causing first put to fail with 412, then succeed after delete
    attempts = {"count": 0}

    def put_object_side_effect(Bucket, Key, Body=None, IfNoneMatch=None, **kwargs):
        if Key.endswith(".lock") and IfNoneMatch == "*" and attempts["count"] == 0:
            attempts["count"] += 1
            raise botocore.exceptions.ClientError(
                {
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                    "Error": {"Code": "PreconditionFailed"},
                },
                "put_object",
            )
        return {}

    # Stale lock: last_modified far in the past
    def head_object_side_effect(Bucket, Key):
        return {"LastModified": datetime.datetime.now() - datetime.timedelta(seconds=120)}

    session_client_mock.return_value.put_object.side_effect = put_object_side_effect
    session_client_mock.return_value.head_object.side_effect = head_object_side_effect
    session_client_mock.return_value.delete_object.return_value = {}

    # Make TTL small enough so 120s old is stale
    s3_remote.lock_ttl_seconds = 60

    remote_ref = f"refs/heads/{BRANCH}"
    lock_key = s3_remote.acquire_lock(remote_ref)

    expected_lock_key = f"test_prefix/{remote_ref}/LOCK#.lock"
    assert lock_key == expected_lock_key

    # Verify delete was called exactly once for the stale lock
    delete_calls = [
        c for c in session_client_mock.return_value.delete_object.call_args_list if c.kwargs["Key"].endswith(".lock")
    ]
    assert len(delete_calls) == 1

    # Verify put was attempted at least twice (initial fail + reacquire)
    put_lock_calls = [
        c
        for c in session_client_mock.return_value.put_object.call_args_list
        if c.kwargs.get("Key", "").endswith(".lock")
    ]
    assert len(put_lock_calls) >= 2


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_progress(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    assert s3_remote.progress is False

    s3_remote.cmd_option("option progress true")
    assert s3_remote.progress is True
    assert stdout_mock.getvalue() == "ok\n"

    s3_remote.cmd_option("option progress false")
    assert s3_remote.progress is False
    assert stdout_mock.getvalue() == "ok\nok\n"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_verbosity_is_stored_and_acked(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    assert s3_remote.verbosity == 1

    s3_remote.cmd_option("option verbosity 0")
    assert s3_remote.verbosity == 0
    s3_remote.cmd_option("option verbosity 1")
    assert s3_remote.verbosity == 1
    s3_remote.cmd_option("option verbosity 2")
    assert s3_remote.verbosity == 2

    assert stdout_mock.getvalue() == "ok\nok\nok\n"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_unknown_is_unsupported(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_option("option concurrency 1")
    s3_remote.cmd_option("option verbosity notanint")
    assert stdout_mock.getvalue() == "unsupported\nunsupported\n"


@patch("git_remote_s3.git.subprocess.run")
def test_bundle_default_captures_stderr(run_mock):
    run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    git.bundle(folder="/tmp/folder", sha=SHA1, ref=f"refs/heads/{BRANCH}")

    cmd = run_mock.call_args[0][0]
    assert cmd == ["git", "bundle", "create", f"/tmp/folder/{SHA1}.bundle", f"refs/heads/{BRANCH}"]
    assert run_mock.call_args.kwargs["stderr"] == subprocess.PIPE


@patch("git_remote_s3.git.subprocess.run")
def test_bundle_progress_inherits_stderr(run_mock):
    run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=None)
    git.bundle(folder="/tmp/folder", sha=SHA1, ref=f"refs/heads/{BRANCH}", progress=True)

    cmd = run_mock.call_args[0][0]
    assert cmd[:4] == ["git", "bundle", "create", "--progress"]
    assert run_mock.call_args.kwargs["stderr"] is None


@patch("git_remote_s3.git.subprocess.run")
def test_bundle_quiet_wins_over_progress(run_mock):
    run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    git.bundle(folder="/tmp/folder", sha=SHA1, ref=f"refs/heads/{BRANCH}", progress=True, quiet=True)

    cmd = run_mock.call_args[0][0]
    assert "-q" in cmd
    assert "--progress" not in cmd
    assert run_mock.call_args.kwargs["stderr"] == subprocess.PIPE


@patch("git_remote_s3.git.subprocess.run")
def test_unbundle_progress_flag(run_mock):
    run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    git.unbundle(folder="/tmp/folder", sha=SHA1, ref=f"refs/heads/{BRANCH}", progress=True)
    assert run_mock.call_args[0][0][:4] == ["git", "bundle", "unbundle", "--progress"]

    run_mock.reset_mock()
    git.unbundle(folder="/tmp/folder", sha=SHA1, ref=f"refs/heads/{BRANCH}")
    assert "--progress" not in run_mock.call_args[0][0]


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_threads_progress_options_to_bundle(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA1])
    is_ancestor_mock.return_value = True

    s3_remote.progress = True
    s3_remote.verbosity = 1
    s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    assert bundle_mock.call_args.kwargs["progress"] is True
    assert bundle_mock.call_args.kwargs["quiet"] is False

    s3_remote.progress = False
    s3_remote.verbosity = 0
    s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    assert bundle_mock.call_args.kwargs["progress"] is False
    assert bundle_mock.call_args.kwargs["quiet"] is True


@patch("git_remote_s3.git.unbundle")
@patch("boto3.Session.client")
def test_cmd_fetch_threads_progress_to_unbundle(session_client_mock, unbundle_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.progress = True
    s3_remote.cmd_fetch(f"fetch {SHA1} refs/heads/{BRANCH}")

    assert unbundle_mock.call_args.kwargs["progress"] is True
    assert session_client_mock.return_value.download_file.call_args.kwargs["Callback"] is not None


@patch("git_remote_s3.git.unbundle")
@patch("boto3.Session.client")
def test_cmd_fetch_no_progress_passes_no_callback(session_client_mock, unbundle_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_fetch(f"fetch {SHA1} refs/heads/{BRANCH}")

    assert unbundle_mock.call_args.kwargs["progress"] is False
    assert session_client_mock.return_value.download_file.call_args.kwargs["Callback"] is None


@patch("sys.stderr", new_callable=StringIO)
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_renders_progress_on_stderr(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock, stderr_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA1])
    is_ancestor_mock.return_value = True

    def upload_file_side_effect(Filename, Bucket, Key, Callback, **kwargs):
        Callback(os.path.getsize(Filename))

    session_client_mock.return_value.upload_file.side_effect = upload_file_side_effect

    s3_remote.progress = True
    s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    rendered = stderr_mock.getvalue()
    assert rendered.startswith(f"\rUploading refs/heads/{BRANCH}: ")
    assert "(100%)" in rendered
    assert rendered.endswith("\n")


@patch("sys.stderr", new_callable=StringIO)
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_is_silent_without_progress(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock, stderr_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA1])
    is_ancestor_mock.return_value = True

    s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert session_client_mock.return_value.upload_file.call_args.kwargs["Callback"] is None
    assert stderr_mock.getvalue() == ""


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_upload_failure_returns_error(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA1])
    is_ancestor_mock.return_value = True
    session_client_mock.return_value.upload_file.side_effect = boto3.exceptions.S3UploadFailedError(
        "Failed to upload bundle: AccessDenied"
    )

    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res.startswith(f"error refs/heads/{BRANCH} ")
    assert "AccessDenied" in res
    # The per-ref lock must still be released on a failed upload.
    lock_deletes = [
        c for c in session_client_mock.return_value.delete_object.call_args_list if c.kwargs["Key"].endswith(".lock")
    ]
    assert len(lock_deletes) == 1


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_upload_client_error_returns_error(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA1])
    is_ancestor_mock.return_value = True
    session_client_mock.return_value.upload_file.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "upload_file"
    )

    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    assert res.startswith(f"error refs/heads/{BRANCH} ")


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_removes_temp_dir(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA1])
    is_ancestor_mock.return_value = True

    created_dirs = []

    def bundle_side_effect(folder: str, sha: str, ref: str, **kwargs):
        created_dirs.append(folder)
        bundle_path = f"{folder}/{sha}.bundle"
        with open(bundle_path, "wb") as f:
            f.write(MOCK_BUNDLE_CONTENT)
        return bundle_path

    bundle_mock.side_effect = bundle_side_effect

    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res.startswith("ok")
    assert len(created_dirs) == 1
    assert not os.path.exists(created_dirs[0])


@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_multiple_bundles_creates_no_temp_dir(session_client_mock, bundle_mock, rev_parse_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA1, SHA2])

    with patch("git_remote_s3.remote.tempfile.mkdtemp") as mkdtemp_mock:
        res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res.startswith("error")
    mkdtemp_mock.assert_not_called()


@patch("git_remote_s3.git.unbundle")
@patch("boto3.Session.client")
def test_cmd_fetch_removes_temp_dir(session_client_mock, unbundle_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    created_dirs = []

    def unbundle_side_effect(folder: str, sha: str, ref: str, **kwargs):
        created_dirs.append(folder)

    unbundle_mock.side_effect = unbundle_side_effect

    s3_remote.cmd_fetch(f"fetch {SHA1} refs/heads/{BRANCH}")

    assert len(created_dirs) == 1
    assert not os.path.exists(created_dirs[0])


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_cas_records_lease(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{SHA2}")
    assert stdout_mock.getvalue() == "ok\n"
    assert s3_remote.cas_refs == {f"refs/heads/{BRANCH}": SHA2}


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_lease_no_ancestor_accepted(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    is_ancestor_mock.return_value = False

    # git sends no leading "+" for --force-with-lease, only the leased sha via `option cas`.
    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{SHA2}")
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res.startswith("ok")
    assert session_client_mock.return_value.upload_file.call_count == 1
    del_calls = [
        c
        for c in session_client_mock.return_value.delete_object.call_args_list
        if not c.kwargs["Key"].endswith(".lock")
    ]
    assert len(del_calls) == 1
    assert del_calls[0].kwargs["Key"] == f"test_prefix/refs/heads/{BRANCH}/{SHA2}.bundle"


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_lease_no_ancestor_protected(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(
        protected=True, shas=[SHA2]
    )
    is_ancestor_mock.return_value = False

    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{SHA2}")
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res == f'error refs/heads/{BRANCH} "remote ref is protected."?\n'
    protected_calls = [
        c
        for c in session_client_mock.return_value.list_objects_v2.call_args_list
        if c.kwargs.get("Prefix", "").endswith("PROTECTED#")
    ]
    assert len(protected_calls) == 1
    assert session_client_mock.return_value.upload_file.call_count == 0
    assert session_client_mock.return_value.put_object.call_count == 0
    bundle_mock.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_lease_rejected_when_remote_moved(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    is_ancestor_mock.return_value = False

    # Lease taken against a sha the remote no longer holds: the ref moved after `list for-push`.
    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{MOVED_SHA}")
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res.startswith(f"error refs/heads/{BRANCH} ")
    assert "stale info" in res
    assert session_client_mock.return_value.upload_file.call_count == 0
    assert session_client_mock.return_value.put_object.call_count == 0
    bundle_mock.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_lease_for_other_ref_does_not_apply(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    is_ancestor_mock.return_value = False

    s3_remote.cmd_option(f"option cas refs/heads/other:{SHA2}")
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res == f'error refs/heads/{BRANCH} "remote ref is not ancestor of refs/heads/{BRANCH}."?\n'
    assert session_client_mock.return_value.upload_file.call_count == 0


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_fast_forward_never_checks_protection(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    is_ancestor_mock.return_value = True

    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res.startswith("ok")
    protected_calls = [
        c
        for c in session_client_mock.return_value.list_objects_v2.call_args_list
        if c.kwargs.get("Prefix", "").endswith("PROTECTED#")
    ]
    assert protected_calls == []


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_force_checks_protection_once(session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    is_ancestor_mock.return_value = False

    res = s3_remote.cmd_push(f"push +refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res.startswith("ok")
    # The pre-lock fast-fail and the authoritative check under the lock share one ListObjectsV2.
    protected_calls = [
        c
        for c in session_client_mock.return_value.list_objects_v2.call_args_list
        if c.kwargs.get("Prefix", "").endswith("PROTECTED#")
    ]
    assert len(protected_calls) == 1


@patch("git_remote_s3.git.is_shallow_repository")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_from_shallow_clone_rejected(
    session_client_mock, bundle_mock, rev_parse_mock, is_shallow_repository_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    is_shallow_repository_mock.return_value = True

    res = s3_remote.cmd_push(f"push +refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res == (f'error refs/heads/{BRANCH} "cannot push from a shallow clone; run git fetch --unshallow first."?\n')
    bundle_mock.assert_not_called()
    assert session_client_mock.return_value.upload_file.call_count == 0
    assert session_client_mock.return_value.put_object.call_count == 0


@patch("git_remote_s3.git.is_shallow_repository")
@patch("boto3.Session.client")
def test_cmd_push_delete_allowed_from_shallow_clone(session_client_mock, is_shallow_repository_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    is_shallow_repository_mock.return_value = True
    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            }
        ]
    }

    res = s3_remote.cmd_push(f"push :refs/heads/{BRANCH}")

    assert res == f"ok refs/heads/{BRANCH}\n"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_origin(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    _git("config", "uploadpack.allowFilter", "true", cwd=origin)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, stderr=subprocess.DEVNULL)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "test", cwd=work)
    for i in range(4):
        (work / "file.txt").write_text(f"revision-{i}\n")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", f"r{i}", cwd=work)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    return origin


def _clone(origin, dest, *extra):
    subprocess.run(
        ["git", "clone", "-q", *extra, f"file://{origin}", str(dest)],
        check=True,
        stderr=subprocess.DEVNULL,
    )
    _git("config", "user.email", "test@example.com", cwd=dest)
    _git("config", "user.name", "test", cwd=dest)
    return dest


@pytest.mark.real_git_shallow_check
def test_is_shallow_repository_distinguishes_shallow_from_partial(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)

    full = _clone(origin, tmp_path / "full")
    monkeypatch.chdir(full)
    assert git.is_shallow_repository() is False

    shallow = _clone(origin, tmp_path / "shallow", "--depth", "1")
    monkeypatch.chdir(shallow)
    assert git.is_shallow_repository() is True

    # Partial clones are not blocked: pack-objects faults the missing objects back in from the
    # promisor remote while bundling, so the bundle stays complete (asserted below).
    blobless = _clone(origin, tmp_path / "blobless", "--filter=blob:none")
    monkeypatch.chdir(blobless)
    assert git.is_shallow_repository() is False


def test_bundle_from_blobless_clone_is_complete(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    blobless = _clone(origin, tmp_path / "blobless", "--filter=blob:none")
    (blobless / "file.txt").write_text("revision-4\n")
    _git("commit", "-qam", "r4", cwd=blobless)

    monkeypatch.chdir(blobless)
    sha = git.rev_parse("refs/heads/main")
    bundle_path = git.bundle(folder=str(tmp_path), sha=sha, ref="refs/heads/main", quiet=True)

    restored = tmp_path / "restored.git"
    subprocess.run(["git", "init", "-q", "--bare", str(restored)], check=True, stdout=subprocess.DEVNULL)
    _git("fetch", "-q", bundle_path, "refs/heads/*:refs/heads/*", cwd=restored)

    fsck = subprocess.run(["git", "fsck", "--full"], cwd=restored, capture_output=True, text=True)
    assert fsck.returncode == 0, fsck.stderr
    assert "missing" not in fsck.stderr
    # Every object of the full history, including blobs the partial clone never held, made it over.
    objects = subprocess.run(
        ["git", "rev-list", "--objects", "--all"], cwd=restored, capture_output=True, text=True, check=True
    )
    assert objects.stdout.count("\n") == 5 * 3


@patch("sys.stderr", new_callable=StringIO)
def test_transfer_progress_uses_kib_below_one_mib(stderr_mock):
    progress = TransferProgress(action="Downloading", label="refs/heads/main", total_bytes=500 * 1024)
    progress(250 * 1024)

    rendered = stderr_mock.getvalue()
    assert "KiB" in rendered
    assert "MiB" not in rendered
    assert "250 / 500 KiB (50%)" in rendered


@patch("sys.stderr", new_callable=StringIO)
def test_transfer_progress_uses_mib_at_or_above_one_mib(stderr_mock):
    progress = TransferProgress(action="Uploading", label="refs/heads/main", total_bytes=4 * 1024 * 1024)
    progress(2 * 1024 * 1024)

    rendered = stderr_mock.getvalue()
    assert "MiB" in rendered
    assert "KiB" not in rendered
    assert "2.0 / 4.0 MiB (50%)" in rendered


@patch("sys.stderr", new_callable=StringIO)
def test_transfer_progress_without_total_adapts_to_seen_bytes(stderr_mock):
    progress = TransferProgress(action="Downloading", label="refs/heads/main")
    progress(10 * 1024)

    assert "10 KiB" in stderr_mock.getvalue()


@patch("git_remote_s3.git.unbundle")
@patch("boto3.Session.client")
def test_process_fetch_cmds_single_ref_shows_progress(session_client_mock, unbundle_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.progress = True

    s3_remote.process_fetch_cmds([f"fetch {SHA1} refs/heads/{BRANCH}"])

    assert unbundle_mock.call_args.kwargs["progress"] is True
    assert session_client_mock.return_value.download_file.call_args.kwargs["Callback"] is not None


@patch("git_remote_s3.git.unbundle")
@patch("boto3.Session.client")
def test_process_fetch_cmds_multi_ref_suppresses_progress(session_client_mock, unbundle_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.progress = True

    s3_remote.process_fetch_cmds(
        [
            f"fetch {SHA1} refs/heads/{BRANCH}",
            f"fetch {SHA2} refs/heads/other",
        ]
    )

    for call in unbundle_mock.call_args_list:
        assert call.kwargs["progress"] is False
    for call in session_client_mock.return_value.download_file.call_args_list:
        assert call.kwargs["Callback"] is None


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_process_cmd_clears_cas_and_protected_cache_after_push_batch(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            }
        ]
    }

    s3_remote.process_cmd(f"push :refs/heads/{BRANCH}\n")
    s3_remote.cas_refs[f"refs/heads/{BRANCH}"] = SHA1
    s3_remote._protected_cache[f"refs/heads/{BRANCH}"] = []

    s3_remote.process_cmd("\n")

    assert s3_remote.cas_refs == {}
    assert s3_remote._protected_cache == {}


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_cas_records_an_expect_absent_lease(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    # git spells "the ref must not exist" as an all-zero sha; an empty value means the same.
    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{NULL_SHA}")
    s3_remote.cmd_option("option cas refs/heads/other:")

    assert stdout_mock.getvalue() == "ok\nok\n"
    assert s3_remote.cas_refs == {f"refs/heads/{BRANCH}": "", "refs/heads/other": ""}


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_lease_rejected_even_when_fast_forward(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    # The ref moved after `list for-push`, but to a tip our push still fast-forwards from: the
    # lease has to be enforced independently of the ancestry check.
    is_ancestor_mock.return_value = True

    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{MOVED_SHA}")
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res.startswith(f"error refs/heads/{BRANCH} ")
    assert "stale info" in res
    assert session_client_mock.return_value.upload_file.call_count == 0
    bundle_mock.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_lease_expecting_absent_rejects_an_existing_ref(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[SHA2])
    is_ancestor_mock.return_value = True

    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{NULL_SHA}")
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert "stale info" in res
    assert "absent" in res
    assert session_client_mock.return_value.upload_file.call_count == 0
    bundle_mock.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_lease_naming_a_sha_rejects_an_absent_ref(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[])

    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{SHA2}")
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert "stale info" in res
    assert "absent" in res
    assert session_client_mock.return_value.upload_file.call_count == 0
    bundle_mock.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_lease_expecting_absent_accepts_a_new_ref(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name
    session_client_mock.return_value.list_objects_v2.side_effect = create_list_objects_v2_mock(shas=[])

    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{NULL_SHA}")
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert res == f"ok refs/heads/{BRANCH}\n"
    assert session_client_mock.return_value.upload_file.call_count == 1


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_lease_is_re_checked_under_the_lock(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    is_ancestor_mock.return_value = True
    ref_prefix = f"test_prefix/refs/heads/{BRANCH}/"
    # Another client committed a bundle between our pre-lock snapshot and the lock acquisition.
    views = [
        [],
        [{"Key": f"{ref_prefix}{SHA2}.bundle", "LastModified": datetime.datetime.now()}],
    ]

    def list_objects_v2(Prefix, **kwargs):
        if Prefix != ref_prefix:
            return {"Contents": []}
        return {"Contents": views.pop(0) if len(views) > 1 else views[0]}

    session_client_mock.return_value.list_objects_v2.side_effect = list_objects_v2

    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{NULL_SHA}")
    res = s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}")

    assert "stale info" in res
    assert session_client_mock.return_value.upload_file.call_count == 0


@patch("sys.stdout", new_callable=StringIO)
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_push_batch_survives_a_bundle_failure(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock, stdout_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    session_client_mock.return_value.list_objects_v2.return_value = {"Contents": []}
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.side_effect = [
        git.GitError('fatal: bad object\nnot "ok"\n'),
        temp_file.name,
    ]

    s3_remote.process_cmd(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}\n")
    s3_remote.process_cmd("push refs/heads/other:refs/heads/other\n")
    s3_remote.process_cmd("\n")

    failed, succeeded = stdout_mock.getvalue().splitlines()[:2]
    # git's stderr has to be flattened onto one line, with no bare quote to end the message early.
    assert failed == f"""error refs/heads/{BRANCH} "fatal: bad object not 'ok'"?"""
    assert succeeded == "ok refs/heads/other"
    assert session_client_mock.return_value.upload_file.call_count == 1


@patch("git_remote_s3.git.unbundle")
@patch("boto3.Session.client")
def test_process_fetch_cmds_raises_the_first_failure_after_the_batch(session_client_mock, unbundle_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    def download_file(*, Key, **kwargs):
        if SHA2 in Key:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    session_client_mock.return_value.download_file.side_effect = download_file

    with pytest.raises(NotAuthorizedError):
        s3_remote.process_fetch_cmds(
            [
                f"fetch {SHA1} refs/heads/{BRANCH}",
                f"fetch {SHA2} refs/heads/other",
                f"fetch {MOVED_SHA} refs/heads/third",
            ]
        )

    # The healthy fetches are not cancelled; only the batch's verdict changes.
    assert SHA1 in s3_remote.fetched_refs
    assert MOVED_SHA in s3_remote.fetched_refs
    assert SHA2 not in s3_remote.fetched_refs


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_bad_verbosity_leaves_the_level_alone(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    s3_remote.cmd_option("option verbosity 0")
    s3_remote.cmd_option("option verbosity notanint")

    assert s3_remote.verbosity == 0
    assert stdout_mock.getvalue() == "ok\nunsupported\n"


@patch("git_remote_s3.git.is_shallow_repository")
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.bundle")
@patch("boto3.Session.client")
def test_cmd_push_probes_shallowness_once_per_process(
    session_client_mock, bundle_mock, rev_parse_mock, is_ancestor_mock, is_shallow_repository_mock
):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    is_shallow_repository_mock.return_value = False
    session_client_mock.return_value.list_objects_v2.return_value = {"Contents": []}
    temp_dir = tempfile.mkdtemp("test_temp")
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=BUNDLE_SUFFIX)
    with open(temp_file.name, "wb") as f:
        f.write(MOCK_BUNDLE_CONTENT)
    bundle_mock.return_value = temp_file.name

    assert s3_remote.cmd_push(f"push refs/heads/{BRANCH}:refs/heads/{BRANCH}").startswith("ok")
    assert s3_remote.cmd_push("push refs/heads/other:refs/heads/other").startswith("ok")

    is_shallow_repository_mock.assert_called_once_with()


@patch("git_remote_s3.remote.time.monotonic")
@patch("sys.stderr", new_callable=StringIO)
def test_transfer_progress_throttles_rapid_updates(stderr_mock, monotonic_mock):
    monotonic_mock.side_effect = [0.0, 0.01, 0.02, 0.03, 0.2, 0.21]
    progress = TransferProgress(action="Downloading", label="refs/heads/main")

    for _ in range(6):
        progress(1024)

    # The first update always renders; after that only one per throttle window gets through.
    assert stderr_mock.getvalue().count("\r") == 2


@patch("sys.stderr", new_callable=StringIO)
def test_transfer_progress_close_newlines_only_after_a_render(stderr_mock):
    progress = TransferProgress(action="Downloading", label="refs/heads/main")

    progress.close()
    assert stderr_mock.getvalue() == ""

    progress(1024)
    progress.close()
    assert stderr_mock.getvalue().endswith("\n")


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_push_delete_with_matching_lease_proceeds(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            }
        ]
    }

    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{SHA1}")
    res = s3_remote.cmd_push(f"push :refs/heads/{BRANCH}")

    assert res == f"ok refs/heads/{BRANCH}\n"
    assert session_client_mock.return_value.delete_object.call_count == 1


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_push_delete_with_stale_lease_deletes_nothing(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            }
        ]
    }

    # The ref moved after `list for-push`; a delete replaces history just as a force push does.
    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{MOVED_SHA}")
    res = s3_remote.cmd_push(f"push :refs/heads/{BRANCH}")

    assert res.startswith(f"error refs/heads/{BRANCH} ")
    assert "stale info" in res
    session_client_mock.return_value.delete_object.assert_not_called()


@patch("boto3.Session.client")
def test_cmd_push_delete_without_a_lease_is_unchanged(session_client_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    session_client_mock.return_value.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": f"test_prefix/refs/heads/{BRANCH}/{SHA1}.bundle",
                "LastModified": datetime.datetime.now(),
            }
        ]
    }

    res = s3_remote.cmd_push(f"push :refs/heads/{BRANCH}")

    assert res == f"ok refs/heads/{BRANCH}\n"
    assert session_client_mock.return_value.delete_object.call_count == 1
    # An unleased delete must not pay for the lease probe's extra listing.
    assert session_client_mock.return_value.list_objects_v2.call_count == 1
