# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0

import datetime
from io import BytesIO

import pytest
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError

from git_remote_s3 import UriScheme
from git_remote_s3.manage import main, Doctor, ManageBranch
from remote_test import ManifestStore, wal_manifest


@pytest.fixture
def mocked_cli_chain():
    """Patches everything main() needs to reach argument-dependent branching
    without touching git, AWS, or a real bucket alias lookup."""
    with (
        patch("git_remote_s3.manage.get_remote_url") as get_remote_url,
        patch("git_remote_s3.manage.parse_git_url") as parse_git_url,
        patch("git_remote_s3.manage.resolve_bucket_alias") as resolve_bucket_alias,
        patch("git_remote_s3.manage.Doctor") as doctor_cls,
        patch("git_remote_s3.manage.ManageBranch") as manage_branch_cls,
    ):
        get_remote_url.return_value = "s3://profile@bucket/repo"
        parse_git_url.return_value = (UriScheme.S3, "profile", "bucket", "repo")
        resolve_bucket_alias.return_value = "bucket"
        yield doctor_cls, manage_branch_cls


def test_doctor_without_branch_parses_and_runs(mocked_cli_chain, monkeypatch):
    doctor_cls, manage_branch_cls = mocked_cli_chain
    monkeypatch.setattr("sys.argv", ["git-s3", "doctor", "s3://profile@bucket/repo"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    doctor_cls.assert_called_once_with("profile", "bucket", "repo", False, 60, False)
    doctor_cls.return_value.run.assert_called_once_with()
    manage_branch_cls.assert_not_called()


def test_delete_branch_without_branch_still_errors(mocked_cli_chain, monkeypatch, capsys):
    _, manage_branch_cls = mocked_cli_chain
    monkeypatch.setattr("sys.argv", ["git-s3", "delete-branch", "s3://profile@bucket/repo"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1
    assert "branch is required" in capsys.readouterr().err
    manage_branch_cls.assert_not_called()


def test_delete_branch_with_branch_still_works(mocked_cli_chain, monkeypatch):
    _, manage_branch_cls = mocked_cli_chain
    monkeypatch.setattr(
        "sys.argv",
        ["git-s3", "delete-branch", "s3://profile@bucket/repo", "mybranch"],
    )

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    manage_branch_cls.assert_called_once_with("profile", "bucket", "repo", "mybranch")
    manage_branch_cls.return_value.process_cmd.assert_called_once_with("delete-branch")


def test_doctor_accepts_options_before_and_after_uri(mocked_cli_chain, monkeypatch):
    doctor_cls, _ = mocked_cli_chain
    monkeypatch.setattr(
        "sys.argv",
        [
            "git-s3",
            "doctor",
            "--lock-ttl",
            "30",
            "s3://profile@bucket/repo",
            "--delete-stale-locks",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    doctor_cls.assert_called_once_with("profile", "bucket", "repo", False, 30, True)


NESTED_PREFIX = "vendors/extrahop"
SHA1 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8a"
SHA2 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8b"
LFS_OID1 = "094ae93548989b4173f5ed4d2fe90480b8b4db8b14e818935644a5ee896db3b7"
LFS_OID2 = "105b6d5b4ddc2b505c7fb8e405121aad67d440963b9dbd11eee2161403cf834d"


def _make_doctor(prefix, delete_bundle=False):
    with (
        patch("boto3.Session"),
        patch("git_remote_s3.manage.register_s3_access_grants"),
        patch("git_remote_s3.manage.s3_region_kwargs", return_value={}),
    ):
        return Doctor(None, "bucket", prefix, delete_bundle)


def _list_objects_mock(keys):
    def side_effect(**kwargs):
        return {
            "Contents": [
                {"Key": k, "LastModified": datetime.datetime.now(tz=datetime.timezone.utc)}
                for k in keys
                if k.startswith(kwargs["Prefix"])
            ],
        }

    return side_effect


def _repo_keys(prefix):
    p = f"{prefix}/" if prefix else ""
    return [
        f"{p}HEAD",
        f"{p}lfs/{LFS_OID1}",
        f"{p}lfs/{LFS_OID2}",
        f"{p}refs/heads/main/{SHA1}.bundle",
        f"{p}refs/heads/main/{SHA2}.bundle",
        f"{p}refs/heads/main/LOCK#.lock",
        f"{p}refs/heads/feature/PROTECTED#",
        f"{p}refs/heads/feature/{SHA1}.bundle",
    ]


@pytest.mark.parametrize("prefix", [NESTED_PREFIX, "test_prefix", ""])
def test_analyze_repo_parses_keys_relative_to_prefix(prefix):
    doctor = _make_doctor(prefix)
    doctor.s3.list_objects_v2.side_effect = _list_objects_mock(_repo_keys(prefix))
    doctor.s3.get_object.return_value = {"Body": BytesIO(b"refs/heads/main\n")}

    repos = doctor.analyze_repo()

    repo_name = prefix if prefix else "<bucket root>"
    assert set(repos) == {repo_name}
    refs = repos[repo_name]["refs"]
    assert set(refs) == {"refs/heads/main", "refs/heads/feature"}
    assert {b["sha"] for b in refs["refs/heads/main"]["bundles"]} == {SHA1, SHA2}
    assert not refs["refs/heads/main"]["protected"]
    assert refs["refs/heads/feature"]["protected"]
    assert [b["sha"] for b in refs["refs/heads/feature"]["bundles"]] == [SHA1]
    assert repos[repo_name]["HEAD"] == "refs/heads/main"
    head_key = f"{prefix}/HEAD" if prefix else "HEAD"
    doctor.s3.get_object.assert_called_once_with(Bucket="bucket", Key=head_key)


def test_analyze_repo_paginates_listing():
    doctor = _make_doctor(NESTED_PREFIX)

    def side_effect(**kwargs):
        if "ContinuationToken" not in kwargs:
            return {
                "Contents": [
                    {
                        "Key": f"{NESTED_PREFIX}/refs/heads/main/{SHA1}.bundle",
                        "LastModified": datetime.datetime.now(tz=datetime.timezone.utc),
                    }
                ],
                "NextContinuationToken": "token",
            }
        assert kwargs["ContinuationToken"] == "token"
        return {
            "Contents": [
                {
                    "Key": f"{NESTED_PREFIX}/refs/heads/main/{SHA2}.bundle",
                    "LastModified": datetime.datetime.now(tz=datetime.timezone.utc),
                }
            ],
        }

    doctor.s3.list_objects_v2.side_effect = side_effect

    repos = doctor.analyze_repo()

    bundles = repos[NESTED_PREFIX]["refs"]["refs/heads/main"]["bundles"]
    assert {b["sha"] for b in bundles} == {SHA1, SHA2}


def _repos_with_two_main_bundles():
    last_modified = datetime.datetime.now(tz=datetime.timezone.utc)
    return {
        NESTED_PREFIX: {
            "refs": {
                "refs/heads/main": {
                    "protected": False,
                    "bundles": [
                        {"sha": SHA1, "lastModified": last_modified},
                        {"sha": SHA2, "lastModified": last_modified},
                    ],
                }
            },
            "HEAD": "refs/heads/main",
        }
    }


def test_fix_multiple_bundles_deletes_stale_bundle_under_nested_prefix():
    doctor = _make_doctor(NESTED_PREFIX, delete_bundle=True)
    repos = _repos_with_two_main_bundles()

    with patch("builtins.input", side_effect=["1", ""]):
        doctor.fix_multiple_bundles(repos, NESTED_PREFIX, "refs/heads/main")

    doctor.s3.delete_object.assert_called_once_with(
        Bucket="bucket",
        Key=f"{NESTED_PREFIX}/refs/heads/main/{SHA2}.bundle",
    )
    doctor.s3.copy_object.assert_not_called()


def test_fix_multiple_bundles_moves_stale_bundle_under_nested_prefix():
    doctor = _make_doctor(NESTED_PREFIX, delete_bundle=False)
    repos = _repos_with_two_main_bundles()

    with patch("builtins.input", side_effect=["1", ""]):
        doctor.fix_multiple_bundles(repos, NESTED_PREFIX, "refs/heads/main")

    doctor.s3.copy_object.assert_called_once()
    copy_kwargs = doctor.s3.copy_object.call_args.kwargs
    source_key = f"{NESTED_PREFIX}/refs/heads/main/{SHA2}.bundle"
    assert copy_kwargs["CopySource"] == {"Bucket": "bucket", "Key": source_key}
    assert copy_kwargs["Key"].startswith(f"{NESTED_PREFIX}/refs/heads/main_")
    assert copy_kwargs["Key"].endswith(f"/{SHA2}.bundle")
    doctor.s3.delete_object.assert_called_once_with(Bucket="bucket", Key=source_key)


def test_fix_head_writes_prefix_relative_ref():
    doctor = _make_doctor(NESTED_PREFIX)
    repos = {
        NESTED_PREFIX: {
            "refs": {"refs/heads/main": {"protected": False, "bundles": []}},
            "HEAD": "Invalid",
        }
    }

    with patch("builtins.input", side_effect=["1"]):
        doctor.fix_head(repos, NESTED_PREFIX)

    doctor.s3.put_object.assert_called_once_with(
        Bucket="bucket",
        Key=f"{NESTED_PREFIX}/HEAD",
        Body="refs/heads/main",
    )


def test_doctor_run_healthy_nested_repo_never_prompts(capsys):
    """Regression for the incident where the LFS store was reported as a branch
    with multiple bundles and the interactive fixer offered to delete LFS objects."""
    doctor = _make_doctor(NESTED_PREFIX)
    keys = [
        f"{NESTED_PREFIX}/HEAD",
        f"{NESTED_PREFIX}/lfs/{LFS_OID1}",
        f"{NESTED_PREFIX}/lfs/{LFS_OID2}",
        f"{NESTED_PREFIX}/refs/heads/main/{SHA1}.bundle",
        f"{NESTED_PREFIX}/refs/heads/main/LOCK#.lock",
    ]
    doctor.s3.list_objects_v2.side_effect = _list_objects_mock(keys)
    doctor.s3.get_object.return_value = {"Body": BytesIO(b"refs/heads/main")}
    probe = MagicMock()
    probe.exceptions.ClientError = ClientError
    probe.list_objects_v2.return_value = {"Contents": []}

    with (
        patch("git_remote_s3.manage.register_s3_access_grants_strict", return_value=probe),
        patch("git_remote_s3.manage.s3_region_kwargs", return_value={}),
        patch("builtins.input", side_effect=AssertionError("interactive fixer must not trigger")),
    ):
        doctor.run()

    out = capsys.readouterr().out
    assert " refs/heads/main: Ok" in out
    assert "HEAD: refs/heads/main" in out
    assert "lfs" not in out
    assert "Multiple refs" not in out
    assert "No stale locks found." in out


REF = "refs/heads/main"
OTHER_REF = "refs/heads/dev"


def _manage_branch(manifest, branch="main"):
    """A ManageBranch wired to a stubbed client holding real manifest bytes."""
    client = MagicMock()
    with (
        patch("boto3.Session"),
        patch("git_remote_s3.manage.register_s3_access_grants", return_value=client),
        patch("git_remote_s3.manage.s3_region_kwargs", return_value={}),
    ):
        store = ManifestStore(client, manifest=manifest, key="repo/gitwal.json")
        return ManageBranch(None, "bucket", "repo", branch), store


def test_manage_branch_refuses_a_branch_the_manifest_does_not_name():
    with pytest.raises(ValueError, match="does not exist"):
        _manage_branch(wal_manifest({OTHER_REF: SHA1}))


def test_manage_branch_refuses_a_repo_with_no_manifest():
    with pytest.raises(ValueError, match="does not exist"):
        _manage_branch(None)


def test_protect_branch_is_a_cas_update_of_the_manifest():
    branch, store = _manage_branch(wal_manifest({REF: SHA1}, seq=4))

    branch.protect_branch()

    assert len(store.puts) == 1
    assert store.puts[0]["IfMatch"] == '"etag-0"'
    assert store.manifest.protected == [REF]
    assert store.manifest.seq == 5
    store.client.delete_object.assert_not_called()
    store.client.put_object.assert_called_once()


def test_unprotect_branch_is_a_cas_update_of_the_manifest():
    branch, store = _manage_branch(wal_manifest({REF: SHA1}, protected=[REF], seq=4))

    branch.unprotect_branch()

    assert len(store.puts) == 1
    assert store.manifest.protected == []
    assert store.manifest.seq == 5
    store.client.delete_object.assert_not_called()


def test_protect_retries_against_a_concurrent_commit():
    branch, store = _manage_branch(wal_manifest({REF: SHA1}, seq=4))
    store.put_results = [("PreconditionFailed", lambda s: s.hold(wal_manifest({REF: SHA2, OTHER_REF: SHA1}, seq=9)))]

    branch.protect_branch()

    assert len(store.puts) == 2
    # The retry protects the ref against whatever the winner committed, not against stale refs.
    assert store.manifest.refs == {REF: SHA2, OTHER_REF: SHA1}
    assert store.manifest.protected == [REF]
    assert store.manifest.seq == 10


def test_delete_branch_is_a_refs_only_cas():
    branch, store = _manage_branch(wal_manifest({REF: SHA1, OTHER_REF: SHA2}, protected=[REF], seq=4))

    with patch("builtins.input", return_value="yes"):
        branch.delete_branch()

    assert store.manifest.refs == {OTHER_REF: SHA2}
    assert store.manifest.protected == []
    # The bytes the branch uniquely held stay in their packs until the repo is compacted.
    store.client.delete_object.assert_not_called()


def test_delete_branch_aborted_writes_nothing():
    branch, store = _manage_branch(wal_manifest({REF: SHA1}, seq=4))

    with patch("builtins.input", return_value="no"):
        branch.delete_branch()

    assert store.puts == []
    assert store.manifest.refs == {REF: SHA1}
