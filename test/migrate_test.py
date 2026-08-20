# SPDX-FileCopyrightText: 2026-present FullDuplex Media
#
# SPDX-License-Identifier: Apache-2.0

import datetime
import shutil
import subprocess
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from git_remote_s3 import UriScheme, git, gitwal
from git_remote_s3.manage import Migrate, main
from remote_test import NoSuchKey, ManifestStore, _clone, _git, _make_origin, client_error, wal_manifest

PREFIX = "repo"
MANIFEST_KEY = f"{PREFIX}/gitwal.json"
CALLER_ARN = "arn:aws:sts::000000000000:assumed-role/git-buckets-dev/tester"
LFS_OID = "094ae93548989b4173f5ed4d2fe90480b8b4db8b14e818935644a5ee896db3b7"
ABSENT_SHA = "1" * 40


def _listing(sizes):
    """A list_objects_v2 side effect over {key -> size}, honouring the scoped Prefix."""

    def side_effect(**kwargs):
        prefix = kwargs["Prefix"]
        return {
            "Contents": [
                {"Key": key, "Size": size, "LastModified": datetime.datetime.now(tz=datetime.timezone.utc)}
                for key, size in sorted(sizes.items())
                if key.startswith(prefix)
            ],
        }

    return side_effect


class _Legacy:
    """A repo in the old format: bundle keys in the listing, a HEAD object, no manifest."""

    def __init__(self, client, *, head=None, manifest=None, put_results=()):
        self.store = ManifestStore(client, manifest=manifest, put_results=put_results, key=MANIFEST_KEY)
        self.head = head
        manifest_get = client.get_object.side_effect

        def get_object(Bucket, Key):
            if Key == f"{PREFIX}/HEAD":
                if self.head is None:
                    raise client_error("NoSuchKey", NoSuchKey)
                return {"Body": BytesIO(self.head.encode("utf-8"))}
            return manifest_get(Bucket=Bucket, Key=Key)

        client.get_object.side_effect = get_object


def _legacy_sizes(refs, *, protected=(), head=True):
    sizes = {f"{PREFIX}/{ref}/{sha}.bundle": 2048 for ref, sha in refs.items()}
    sizes[f"{PREFIX}/refs/heads/main/LOCK#.lock"] = 0
    sizes[f"{PREFIX}/repo.zip"] = 1024
    sizes[f"{PREFIX}/lfs/{LFS_OID}"] = 900
    if head:
        sizes[f"{PREFIX}/HEAD"] = 16
    for ref in protected:
        sizes[f"{PREFIX}/{ref}/PROTECTED#"] = 0
    return sizes


def _migrate(tmp_path, sizes, *, head=None, manifest=None, put_results=(), finalize=False, yes=False, pack=None):
    """A Migrate over a stubbed client; the uploaded pack is what a later download returns."""
    client = MagicMock()
    saved = tmp_path / "uploaded.pack"

    def upload_file(Filename, Bucket, Key, **kwargs):
        shutil.copy(Filename, saved)

    def download_file(Bucket, Key, Filename, **kwargs):
        shutil.copy(pack or saved, Filename)

    client.upload_file.side_effect = upload_file
    client.download_file.side_effect = download_file
    client.list_objects_v2.side_effect = _listing(sizes)
    with (
        patch("boto3.Session") as session_cls,
        patch("git_remote_s3.manage.register_s3_access_grants", return_value=client),
        patch("git_remote_s3.manage.s3_region_kwargs", return_value={}),
    ):
        session_cls.return_value.client.return_value.get_caller_identity.return_value = {"Arn": CALLER_ARN}
        legacy = _Legacy(client, head=head, manifest=manifest, put_results=put_results)
        return Migrate(None, "bucket", PREFIX, finalize, yes), legacy, saved


def _local_repo(tmp_path, monkeypatch):
    """A full clone as cwd, plus the refs the legacy bundle keys would name."""
    origin = _make_origin(tmp_path)
    work = _clone(origin, tmp_path / "clone")
    monkeypatch.chdir(work)
    _git("tag", "v1", "HEAD~2", cwd=work)
    return work, {"refs/heads/main": git.rev_parse("refs/heads/main"), "refs/tags/v1": git.rev_parse("refs/tags/v1")}


def _checksum(path):
    """A pack's key is its own trailing SHA-1, which is the last 20 bytes git wrote into it."""
    with open(path, "rb") as f:
        f.seek(-20, 2)
        return f.read(20).hex()


def test_migrate_creates_a_seq_one_manifest_from_the_legacy_keys(tmp_path, monkeypatch, capsys):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    sizes = _legacy_sizes(refs, protected=["refs/heads/main"])
    migrate, legacy, uploaded = _migrate(tmp_path, sizes, head="refs/heads/main")

    assert migrate.run() == 0

    assert len(legacy.store.puts) == 1
    # Must-create: a concurrent creation has to fail this PUT, never win it.
    assert legacy.store.puts[0]["IfNoneMatch"] == "*"
    assert "IfMatch" not in legacy.store.puts[0]

    committed = legacy.store.manifest
    assert committed.seq == 1
    assert committed.refs == refs
    assert committed.head == "refs/heads/main"
    assert committed.protected == ["refs/heads/main"]
    assert gitwal.errors(gitwal.validate(committed)) == []

    (entry,) = committed.entries
    assert entry.kind == gitwal.KIND_BASE
    assert entry.seq == 1
    assert entry.tips == refs
    assert entry.by == CALLER_ARN
    assert entry.at
    assert entry.pack == f"packs/{_checksum(uploaded)}.pack"

    # Verification is a real round trip: the pack is read back from S3 and re-indexed.
    assert legacy.store.client.download_file.call_args.kwargs["Key"] == f"{PREFIX}/{entry.pack}"
    # Nothing legacy is touched by phase 1: both formats stay live until finalize.
    legacy.store.client.delete_object.assert_not_called()
    out = capsys.readouterr().out
    assert "the legacy keys are still present" in out
    assert "delete gitwal.json and packs/ to roll back" in out


def test_migrate_reads_head_as_absent_when_the_repo_has_none(tmp_path, monkeypatch):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    migrate, legacy, _uploaded = _migrate(tmp_path, _legacy_sizes(refs, head=False))

    assert migrate.run() == 0

    assert legacy.store.manifest.head is None
    assert "head" not in gitwal.load(legacy.store.doc).to_dict()


def test_migrate_refuses_a_ref_carrying_more_than_one_bundle(tmp_path, monkeypatch, capsys):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    sizes = _legacy_sizes(refs)
    sizes[f"{PREFIX}/refs/heads/main/{ABSENT_SHA}.bundle"] = 2048
    migrate, legacy, _uploaded = _migrate(tmp_path, sizes, head="refs/heads/main")

    assert migrate.run() == 1

    err = capsys.readouterr().err
    assert "more than one bundle" in err
    assert "resolve duplicate bundles first" in err
    legacy.store.client.upload_file.assert_not_called()
    assert legacy.store.puts == []


def test_migrate_refuses_a_repo_that_already_has_a_manifest(tmp_path, monkeypatch, capsys):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    migrate, legacy, _uploaded = _migrate(
        tmp_path, _legacy_sizes(refs), head="refs/heads/main", manifest=wal_manifest(refs, seq=4)
    )

    assert migrate.run() == 1

    assert "already exists" in capsys.readouterr().err
    legacy.store.client.upload_file.assert_not_called()
    assert legacy.store.puts == []


def test_migrate_refuses_a_shallow_clone(tmp_path, monkeypatch, capsys):
    origin = _make_origin(tmp_path)
    shallow = _clone(origin, tmp_path / "shallow", "--depth", "1")
    monkeypatch.chdir(shallow)
    refs = {"refs/heads/main": git.rev_parse("refs/heads/main")}
    migrate, legacy, _uploaded = _migrate(tmp_path, _legacy_sizes(refs), head="refs/heads/main")

    assert migrate.run() == 1

    assert "shallow clone" in capsys.readouterr().err
    legacy.store.client.upload_file.assert_not_called()
    assert legacy.store.puts == []


def test_migrate_refuses_a_clone_that_does_not_hold_every_bundled_ref(tmp_path, monkeypatch, capsys):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    sizes = _legacy_sizes({**refs, "refs/heads/absent": ABSENT_SHA})
    migrate, legacy, _uploaded = _migrate(tmp_path, sizes, head="refs/heads/main")

    assert migrate.run() == 1

    err = capsys.readouterr().err
    assert "does not hold every ref" in err
    assert f"refs/heads/absent {ABSENT_SHA}" in err
    legacy.store.client.upload_file.assert_not_called()
    assert legacy.store.puts == []


def test_migrate_aborts_when_the_manifest_is_created_concurrently(tmp_path, monkeypatch, capsys):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    winner = wal_manifest(refs, head="refs/heads/main", seq=1)
    migrate, legacy, _uploaded = _migrate(
        tmp_path,
        _legacy_sizes(refs),
        head="refs/heads/main",
        put_results=[("PreconditionFailed", lambda s: s.hold(winner))],
    )

    assert migrate.run() == 1

    assert "already exists" in capsys.readouterr().err
    # One PUT and no retry: a must-create that lost must never fall through to an update.
    assert len(legacy.store.puts) == 1
    assert legacy.store.manifest.seq == winner.seq
    legacy.store.client.delete_object.assert_not_called()


def test_migrate_fails_loudly_when_the_stored_pack_does_not_rebuild_the_repo(tmp_path, monkeypatch, capsys):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    corrupt = tmp_path / "corrupt.pack"
    corrupt.write_bytes(b"PACK not really a packfile")
    migrate, legacy, _uploaded = _migrate(tmp_path, _legacy_sizes(refs), head="refs/heads/main", pack=str(corrupt))

    assert migrate.run() == 1

    err = capsys.readouterr().err
    assert "does not verify" in err
    assert "delete gitwal.json and packs/ to roll back" in err
    legacy.store.client.delete_object.assert_not_called()


def test_verify_reports_a_manifest_that_does_not_match_the_bundle_keys(tmp_path, monkeypatch):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    migrate, legacy, _uploaded = _migrate(tmp_path, _legacy_sizes(refs), head="refs/heads/main")
    assert migrate.run() == 0
    entry = legacy.store.manifest.entries[0]

    problems = migrate.verify({**refs, "refs/heads/other": ABSENT_SHA}, "refs/heads/other", entry, folder=str(tmp_path))

    assert any("do not match the bundle keys" in p for p in problems)
    assert any("manifest head is" in p for p in problems)


FINALIZE_PACK = "packs/" + "c" * 40 + ".pack"


def _migrated(tmp_path, refs, *, yes=True, packs=(FINALIZE_PACK,), manifest=None):
    sizes = _legacy_sizes(refs, protected=["refs/heads/main"])
    sizes[MANIFEST_KEY] = 512
    for pack in packs:
        sizes[f"{PREFIX}/{pack}"] = 4096
    if manifest is None:
        manifest = wal_manifest(
            refs,
            head="refs/heads/main",
            entries=[gitwal.Entry(seq=1, kind=gitwal.KIND_BASE, pack=FINALIZE_PACK, tips=refs)],
            seq=1,
        )
    return _migrate(tmp_path, sizes, head="refs/heads/main", manifest=manifest, finalize=True, yes=yes)


REFS = {"refs/heads/main": "c105d19ba64965d2c9d3d3246e7269059ef8bb8a"}


def test_finalize_refuses_without_the_confirmation_flag(tmp_path, capsys):
    migrate, legacy, _uploaded = _migrated(tmp_path, REFS, yes=False)

    assert migrate.run() == 1

    assert "re-run with --yes" in capsys.readouterr().err
    legacy.store.client.delete_object.assert_not_called()


def test_finalize_refuses_when_the_manifest_names_a_pack_that_is_not_there(tmp_path, capsys):
    migrate, legacy, _uploaded = _migrated(tmp_path, REFS, packs=())

    assert migrate.run() == 1

    err = capsys.readouterr().err
    assert "names packs that are not in the bucket" in err
    assert FINALIZE_PACK in err
    legacy.store.client.delete_object.assert_not_called()


def test_finalize_refuses_a_repo_with_no_manifest(tmp_path, capsys):
    migrate, legacy, _uploaded = _migrate(
        tmp_path, _legacy_sizes(REFS), head="refs/heads/main", finalize=True, yes=True
    )

    assert migrate.run() == 1

    assert "no gitwal.json" in capsys.readouterr().err
    legacy.store.client.delete_object.assert_not_called()


def test_finalize_refuses_a_manifest_that_does_not_validate(tmp_path, capsys):
    broken = wal_manifest(
        {"refs/heads/main": "not-a-sha"},
        entries=[gitwal.Entry(seq=1, kind=gitwal.KIND_BASE, pack=FINALIZE_PACK, tips=REFS)],
        seq=1,
    )
    migrate, legacy, _uploaded = _migrated(tmp_path, REFS, manifest=broken)

    assert migrate.run() == 1

    assert "does not validate" in capsys.readouterr().err
    legacy.store.client.delete_object.assert_not_called()


def test_finalize_deletes_exactly_the_pre_migration_keys(tmp_path, capsys):
    migrate, legacy, _uploaded = _migrated(tmp_path, REFS)

    assert migrate.run() == 0

    deleted = {call.kwargs["Key"] for call in legacy.store.client.delete_object.call_args_list}
    assert deleted == {
        f"{PREFIX}/HEAD",
        f"{PREFIX}/refs/heads/main/{REFS['refs/heads/main']}.bundle",
        f"{PREFIX}/refs/heads/main/LOCK#.lock",
        f"{PREFIX}/refs/heads/main/PROTECTED#",
        f"{PREFIX}/repo.zip",
    }
    # The manifest, the live pack and the LFS store survive the point of no return.
    assert MANIFEST_KEY not in deleted
    assert f"{PREFIX}/{FINALIZE_PACK}" not in deleted
    assert f"{PREFIX}/lfs/{LFS_OID}" not in deleted
    assert legacy.store.puts == []
    assert "5 of 5 pre-migration keys deleted" in capsys.readouterr().out


def test_finalize_on_an_already_finalized_repo_is_a_no_op(tmp_path, capsys):
    manifest = wal_manifest(
        REFS,
        head="refs/heads/main",
        entries=[gitwal.Entry(seq=1, kind=gitwal.KIND_BASE, pack=FINALIZE_PACK, tips=REFS)],
        seq=1,
    )
    sizes = {MANIFEST_KEY: 512, f"{PREFIX}/{FINALIZE_PACK}": 4096, f"{PREFIX}/lfs/{LFS_OID}": 900}
    migrate, legacy, _uploaded = _migrate(tmp_path, sizes, manifest=manifest, finalize=True, yes=True)

    assert migrate.run() == 0

    assert "no pre-migration keys left" in capsys.readouterr().out
    legacy.store.client.delete_object.assert_not_called()


@pytest.fixture
def mocked_cli_chain():
    with (
        patch("git_remote_s3.manage.get_remote_url") as get_remote_url,
        patch("git_remote_s3.manage.parse_git_url") as parse_git_url,
        patch("git_remote_s3.manage.resolve_bucket_alias") as resolve_bucket_alias,
        patch("git_remote_s3.manage.Migrate") as migrate_cls,
    ):
        get_remote_url.return_value = "s3://profile@bucket/repo"
        parse_git_url.return_value = (UriScheme.S3, "profile", "bucket", "repo")
        resolve_bucket_alias.return_value = "bucket"
        migrate_cls.return_value.run.return_value = 0
        yield migrate_cls


def test_migrate_is_wired_to_the_remote(mocked_cli_chain, monkeypatch):
    monkeypatch.setattr("sys.argv", ["git-s3", "migrate", "s3://profile@bucket/repo"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    mocked_cli_chain.assert_called_once_with("profile", "bucket", "repo", False, False)


def test_finalize_takes_both_flags_and_returns_its_verdict(mocked_cli_chain, monkeypatch):
    mocked_cli_chain.return_value.run.return_value = 1
    monkeypatch.setattr("sys.argv", ["git-s3", "migrate", "--finalize", "--yes", "s3://profile@bucket/repo"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1
    mocked_cli_chain.assert_called_once_with("profile", "bucket", "repo", True, True)


def test_migration_round_trip_rebuilds_every_ref_from_the_stored_pack(tmp_path, monkeypatch):
    """The property a verifying clone would have proved, asserted independently of verify()."""
    work, refs = _local_repo(tmp_path, monkeypatch)
    before = {ref: git.rev_parse(sha) for ref, sha in refs.items()}
    migrate, _legacy, uploaded = _migrate(tmp_path, _legacy_sizes(refs), head="refs/heads/main")

    assert migrate.run() == 0

    restored = tmp_path / "restored.git"
    subprocess.run(["git", "init", "-q", "--bare", str(restored)], check=True, stdout=subprocess.DEVNULL)
    with open(uploaded, "rb") as pack:
        subprocess.run(
            ["git", "index-pack", "--stdin"], cwd=restored, stdin=pack, check=True, stdout=subprocess.DEVNULL
        )
    for ref, sha in refs.items():
        _git("update-ref", ref, sha, cwd=restored)
    rebuilt = {
        ref: subprocess.run(["git", "rev-parse", ref], cwd=restored, capture_output=True, text=True).stdout.strip()
        for ref in refs
    }
    assert rebuilt == before
    fsck = subprocess.run(["git", "fsck", "--full"], cwd=restored, capture_output=True, text=True)
    assert fsck.returncode == 0, fsck.stderr
    assert work.exists()
