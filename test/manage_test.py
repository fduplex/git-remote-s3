# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0

import datetime
import shutil
import subprocess

import pytest
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError

from git_remote_s3 import UriScheme, git, gitwal
from git_remote_s3.manage import Compact, Doctor, ManageBranch, ManageHead, main
from remote_test import ManifestStore, _clone, _git, _make_origin, wal_manifest


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
        patch("git_remote_s3.manage.Compact") as compact_cls,
    ):
        get_remote_url.return_value = "s3://profile@bucket/repo"
        parse_git_url.return_value = (UriScheme.S3, "profile", "bucket", "repo")
        resolve_bucket_alias.return_value = "bucket"
        doctor_cls.return_value.run.return_value = 0
        compact_cls.return_value.run.return_value = 0
        yield doctor_cls, manage_branch_cls, compact_cls


def test_doctor_without_branch_parses_and_runs(mocked_cli_chain, monkeypatch):
    doctor_cls, manage_branch_cls, _ = mocked_cli_chain
    monkeypatch.setattr("sys.argv", ["git-s3", "doctor", "s3://profile@bucket/repo"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    doctor_cls.assert_called_once_with("profile", "bucket", "repo", False)
    doctor_cls.return_value.run.assert_called_once_with()
    manage_branch_cls.assert_not_called()


def test_doctor_exit_code_is_the_audit_verdict(mocked_cli_chain, monkeypatch):
    doctor_cls, _, _ = mocked_cli_chain
    doctor_cls.return_value.run.return_value = 1
    monkeypatch.setattr("sys.argv", ["git-s3", "doctor", "s3://profile@bucket/repo"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1


def test_doctor_takes_the_legacy_sweep_flag(mocked_cli_chain, monkeypatch):
    doctor_cls, _, _ = mocked_cli_chain
    monkeypatch.setattr(
        "sys.argv",
        ["git-s3", "doctor", "--delete-legacy", "s3://profile@bucket/repo"],
    )

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    doctor_cls.assert_called_once_with("profile", "bucket", "repo", True)


def test_compact_is_wired_to_the_remote(mocked_cli_chain, monkeypatch):
    _, _, compact_cls = mocked_cli_chain
    monkeypatch.setattr("sys.argv", ["git-s3", "compact", "s3://profile@bucket/repo"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    compact_cls.assert_called_once_with("profile", "bucket", "repo")
    compact_cls.return_value.run.assert_called_once_with()


def test_delete_branch_without_branch_still_errors(mocked_cli_chain, monkeypatch, capsys):
    _, manage_branch_cls, _ = mocked_cli_chain
    monkeypatch.setattr("sys.argv", ["git-s3", "delete-branch", "s3://profile@bucket/repo"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1
    assert "branch is required" in capsys.readouterr().err
    manage_branch_cls.assert_not_called()


def test_delete_branch_with_branch_still_works(mocked_cli_chain, monkeypatch):
    _, manage_branch_cls, _ = mocked_cli_chain
    monkeypatch.setattr(
        "sys.argv",
        ["git-s3", "delete-branch", "s3://profile@bucket/repo", "mybranch"],
    )

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    manage_branch_cls.assert_called_once_with("profile", "bucket", "repo", "mybranch")
    manage_branch_cls.return_value.process_cmd.assert_called_once_with("delete-branch")


NESTED_PREFIX = "vendors/extrahop"
SHA1 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8a"
SHA2 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8b"
PACK1 = "packs/4f1c8ab6d0e29537c14b8f60a2e7d9354c81b0f6.pack"
PACK2 = "packs/b2d70e1c9a34f80625d1e7b0c39a4f86d215e0b7.pack"
LFS_OID1 = "094ae93548989b4173f5ed4d2fe90480b8b4db8b14e818935644a5ee896db3b7"


def _entry(seq, pack, *, tips, kind=gitwal.KIND_INCREMENTAL, size=1024, objects=7):
    return gitwal.Entry(seq=seq, kind=kind, pack=pack, bytes=size, objects=objects, tips=dict(tips))


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


def _doctor(manifest, sizes=None, *, prefix=NESTED_PREFIX, delete_legacy=False):
    """A Doctor over a stubbed client holding real manifest bytes and a scripted listing."""
    client = MagicMock()
    with (
        patch("boto3.Session"),
        patch("git_remote_s3.manage.register_s3_access_grants", return_value=client),
        patch("git_remote_s3.manage.register_s3_access_grants_readwrite", return_value=client),
        patch("git_remote_s3.manage.s3_region_kwargs", return_value={}),
    ):
        store = ManifestStore(client, manifest=manifest, key=f"{prefix}/gitwal.json")
        client.list_objects_v2.side_effect = _listing(sizes or {})
        return Doctor(None, "bucket", prefix, delete_legacy), store


def _run_doctor(doctor):
    """Runs doctor with the Access Grants probe stubbed out; returns its exit code."""
    probe = MagicMock()
    probe.exceptions.ClientError = ClientError
    probe.list_objects_v2.return_value = {"Contents": []}
    with (
        patch("git_remote_s3.manage.register_s3_access_grants_strict", return_value=probe),
        patch("git_remote_s3.manage.s3_region_kwargs", return_value={}),
        patch("builtins.input", side_effect=AssertionError("doctor must never prompt")),
    ):
        return doctor.run()


def _healthy():
    manifest = wal_manifest(
        {"refs/heads/main": SHA1, "refs/tags/v1": SHA2},
        head="refs/heads/main",
        protected=["refs/heads/main"],
        entries=[_entry(7, PACK1, tips={"refs/heads/main": SHA1})],
        seq=7,
    )
    sizes = {
        f"{NESTED_PREFIX}/gitwal.json": 512,
        f"{NESTED_PREFIX}/{PACK1}": 4096,
        f"{NESTED_PREFIX}/lfs/{LFS_OID1}": 900,
    }
    return manifest, sizes


def test_doctor_reports_a_healthy_manifest_repo_clean(capsys):
    doctor, store = _doctor(*_healthy())

    code = _run_doctor(doctor)

    out = capsys.readouterr().out
    assert code == 0
    assert "schema: OK" in out
    assert "format: 1  seq: 7" in out
    assert " refs: 2" in out
    assert "HEAD: refs/heads/main (resolves)" in out
    assert "protected: refs/heads/main" in out
    assert "entries: 1" in out
    assert "orphan packs: none" in out
    assert "MISSING PACK" not in out
    assert "lfs" not in out
    # An auditor deletes nothing, and it rewrites nothing.
    store.client.delete_object.assert_not_called()
    assert store.puts == []


def test_doctor_reports_schema_findings(capsys):
    manifest = wal_manifest(
        {"refs/heads/main": "not-a-sha"},
        head="refs/heads/gone",
        entries=[_entry(9, PACK1, tips={"refs/heads/main": SHA1})],
        seq=2,
    )
    doctor, _ = _doctor(manifest, {f"{NESTED_PREFIX}/{PACK1}": 10})

    code = _run_doctor(doctor)

    out = capsys.readouterr().out
    assert code == 1
    assert "schema error: bad_sha" in out
    assert "schema error: seq_monotonic" in out
    assert "head_unresolved" in out
    assert "HEAD: refs/heads/gone (UNRESOLVED)" in out


def test_doctor_names_the_entry_whose_pack_is_missing(capsys):
    manifest = wal_manifest(
        {"refs/heads/main": SHA1},
        entries=[_entry(6, PACK1, tips={"refs/heads/main": SHA2}), _entry(7, PACK2, tips={"refs/heads/main": SHA1})],
        seq=7,
    )
    doctor, _ = _doctor(manifest, {f"{NESTED_PREFIX}/{PACK1}": 4096})

    code = _run_doctor(doctor)

    out = capsys.readouterr().out
    assert code == 1
    assert f"MISSING PACK: entry seq 7 names {PACK2}" in out


def test_doctor_counts_orphan_packs_and_their_bytes(capsys):
    manifest = wal_manifest({"refs/heads/main": SHA1}, entries=[_entry(7, PACK1, tips={"refs/heads/main": SHA1})])
    orphan_a = "packs/" + "a" * 40 + ".pack"
    orphan_b = "packs/" + "b" * 40 + ".pack"
    doctor, store = _doctor(
        manifest,
        {
            f"{NESTED_PREFIX}/{PACK1}": 4096,
            f"{NESTED_PREFIX}/{orphan_a}": 1024,
            f"{NESTED_PREFIX}/{orphan_b}": 2048,
        },
    )

    code = _run_doctor(doctor)

    out = capsys.readouterr().out
    # Orphans are inert: reported, never deleted, and never a non-zero exit.
    assert code == 0
    assert "orphan packs: 2 (3.0 KiB reclaimable by git-s3 compact)" in out
    store.client.delete_object.assert_not_called()


def test_doctor_advises_compaction_when_the_log_has_grown(capsys):
    manifest = wal_manifest(
        {"refs/heads/main": SHA1},
        entries=[_entry(6, PACK1, tips={"refs/heads/main": SHA2}), _entry(7, PACK2, tips={"refs/heads/main": SHA1})],
        seq=7,
    )
    doctor, _ = _doctor(manifest, {f"{NESTED_PREFIX}/{PACK1}": 10, f"{NESTED_PREFIX}/{PACK2}": 10})

    assert _run_doctor(doctor) == 0
    assert "compaction: 2 entries would collapse to 1" in capsys.readouterr().out


def test_doctor_reports_a_repo_with_no_manifest(capsys):
    doctor, _ = _doctor(None, {f"{NESTED_PREFIX}/refs/heads/main/{SHA1}.bundle": 100})

    code = _run_doctor(doctor)

    out = capsys.readouterr().out
    assert code == 0
    assert "gitwal.json: missing (this repo has not been migrated)" in out


def _legacy_sizes():
    manifest, sizes = _healthy()
    sizes.update(
        {
            f"{NESTED_PREFIX}/HEAD": 16,
            f"{NESTED_PREFIX}/refs/heads/main/{SHA1}.bundle": 2048,
            f"{NESTED_PREFIX}/refs/heads/main/LOCK#.lock": 0,
            f"{NESTED_PREFIX}/refs/heads/feature/PROTECTED#": 0,
            f"{NESTED_PREFIX}/repo.zip": 1024,
        }
    )
    return manifest, sizes


def test_doctor_reports_pre_migration_keys_without_deleting_them(capsys):
    doctor, store = _doctor(*_legacy_sizes())

    code = _run_doctor(doctor)

    out = capsys.readouterr().out
    assert code == 0
    assert "5 legacy objects (3.0 KiB)" in out
    assert f"{NESTED_PREFIX}/refs/heads/main/LOCK#.lock" in out
    assert "run with --delete-legacy to remove them" in out
    store.client.delete_object.assert_not_called()


def test_doctor_deletes_pre_migration_keys_under_the_flag():
    manifest, sizes = _legacy_sizes()
    doctor, store = _doctor(manifest, sizes, delete_legacy=True)

    assert _run_doctor(doctor) == 0

    deleted = {call.kwargs["Key"] for call in store.client.delete_object.call_args_list}
    assert deleted == {
        f"{NESTED_PREFIX}/HEAD",
        f"{NESTED_PREFIX}/refs/heads/main/{SHA1}.bundle",
        f"{NESTED_PREFIX}/refs/heads/main/LOCK#.lock",
        f"{NESTED_PREFIX}/refs/heads/feature/PROTECTED#",
        f"{NESTED_PREFIX}/repo.zip",
    }
    # The manifest, the live pack and the LFS store are not legacy.
    assert not any(k.endswith(("gitwal.json", ".pack")) or "/lfs/" in k for k in deleted)


PREFIX = "repo"
CALLER_ARN = "arn:aws:sts::000000000000:assumed-role/git-buckets-dev/tester"


def _compact(manifest, tmp_path, put_results=()):
    """A Compact over a stubbed client; uploaded packs are copied where the test can index them."""
    client = MagicMock()
    saved = tmp_path / "uploaded.pack"

    def upload_file(Filename, Bucket, Key, **kwargs):
        shutil.copy(Filename, saved)

    client.upload_file.side_effect = upload_file
    with (
        patch("boto3.Session") as session_cls,
        patch("git_remote_s3.manage.register_s3_access_grants", return_value=client),
        patch("git_remote_s3.manage.register_s3_access_grants_readwrite", return_value=client),
        patch("git_remote_s3.manage.s3_region_kwargs", return_value={}),
    ):
        session_cls.return_value.client.return_value.get_caller_identity.return_value = {"Arn": CALLER_ARN}
        store = ManifestStore(client, manifest=manifest, put_results=put_results, key=f"{PREFIX}/gitwal.json")
        return Compact(None, "bucket", PREFIX), store, saved


def _local_repo(tmp_path, monkeypatch):
    """A full clone as cwd, plus the refs a manifest would name."""
    origin = _make_origin(tmp_path)
    work = _clone(origin, tmp_path / "clone")
    monkeypatch.chdir(work)
    _git("tag", "v1", "HEAD~2", cwd=work)
    return work, {"refs/heads/main": git.rev_parse("refs/heads/main"), "refs/tags/v1": git.rev_parse("refs/tags/v1")}


def _log_of(refs, count=3):
    """A manifest whose entry log names ``count`` packs, as a long-lived repo's would."""
    packs = ["packs/" + chr(ord("a") + i) * 40 + ".pack" for i in range(count)]
    entries = [_entry(i + 1, pack, tips=refs, size=1000 * (i + 1)) for i, pack in enumerate(packs)]
    return wal_manifest(refs, head="refs/heads/main", entries=entries, seq=count), packs


def _rev_parse_all(refs, cwd):
    return {
        ref: subprocess.run(["git", "rev-parse", ref], cwd=cwd, capture_output=True, text=True).stdout.strip()
        for ref in refs
    }


def test_compact_collapses_the_log_to_one_base_pack(tmp_path, monkeypatch, capsys):
    work, refs = _local_repo(tmp_path, monkeypatch)
    before = _rev_parse_all(refs, work)
    manifest, packs = _log_of(refs)
    compact, store, uploaded = _compact(manifest, tmp_path)

    assert compact.run() == 0

    committed = store.manifest
    assert len(committed.entries) == 1
    entry = committed.entries[0]
    assert entry.kind == gitwal.KIND_BASE
    assert entry.tips == refs
    assert entry.by == CALLER_ARN
    assert entry.pack == f"packs/{_checksum(uploaded)}.pack"
    assert committed.refs == refs
    assert committed.seq == manifest.seq + 1
    assert gitwal.errors(gitwal.validate(committed)) == []

    # The one pack must reconstruct every ref: same rev-parse, and a clean fsck.
    restored = tmp_path / "restored.git"
    subprocess.run(["git", "init", "-q", "--bare", str(restored)], check=True, stdout=subprocess.DEVNULL)
    with open(uploaded, "rb") as pack:
        subprocess.run(
            ["git", "index-pack", "--stdin"], cwd=restored, stdin=pack, check=True, stdout=subprocess.DEVNULL
        )
    for ref, sha in refs.items():
        _git("update-ref", ref, sha, cwd=restored)
    assert _rev_parse_all(refs, restored) == before
    fsck = subprocess.run(["git", "fsck", "--full"], cwd=restored, capture_output=True, text=True)
    assert fsck.returncode == 0, fsck.stderr

    out = capsys.readouterr().out
    assert "3 entries (5.9 KiB) -> 1 entry" in out
    assert f"Deleted {len(packs)} superseded packs" in out


def _checksum(path):
    """A pack's key is its own trailing SHA-1, which is the last 20 bytes git wrote into it."""
    with open(path, "rb") as f:
        f.seek(-20, 2)
        return f.read(20).hex()


def test_compact_deletes_superseded_packs_only_after_the_cas(tmp_path, monkeypatch):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    manifest, packs = _log_of(refs)
    compact, store, _uploaded = _compact(manifest, tmp_path)

    assert compact.run() == 0

    deleted = [call.kwargs["Key"] for call in store.client.delete_object.call_args_list]
    assert deleted == [f"{PREFIX}/{pack}" for pack in packs]
    names = [call[0] for call in store.client.mock_calls if call[0] in ("put_object", "delete_object")]
    # Order is the safety property: until the manifest commits, those packs are still live.
    assert names == ["put_object", *["delete_object"] * len(packs)]


def test_compact_leaves_orphans_and_a_valid_manifest_when_it_dies_before_the_deletes(tmp_path, monkeypatch):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    manifest, _packs = _log_of(refs)
    compact, store, _uploaded = _compact(manifest, tmp_path)

    with patch.object(Compact, "delete_packs", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
        compact.run()

    committed = store.manifest
    assert gitwal.errors(gitwal.validate(committed)) == []
    assert len(committed.entries) == 1
    # The superseded packs survive the crash as orphans, which the format defines as harmless.
    store.client.delete_object.assert_not_called()


def test_compact_aborts_when_the_repo_changed_underneath_it(tmp_path, monkeypatch, capsys):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    manifest, packs = _log_of(refs)
    concurrent = wal_manifest(
        refs,
        head="refs/heads/main",
        entries=[*manifest.entries, _entry(9, PACK2, tips=refs)],
        seq=9,
    )
    compact, store, _uploaded = _compact(
        manifest, tmp_path, put_results=[("PreconditionFailed", lambda s: s.hold(concurrent))]
    )

    assert compact.run() == 1

    assert "repo changed during compaction; re-run" in capsys.readouterr().err
    # The loser's base pack is an orphan; nothing was deleted and the winner's log stands.
    store.client.delete_object.assert_not_called()
    assert [e.pack for e in store.manifest.entries] == [*packs, PACK2]


def test_compact_refuses_a_clone_that_does_not_hold_every_ref(tmp_path, monkeypatch, capsys):
    _work, refs = _local_repo(tmp_path, monkeypatch)
    manifest, _packs = _log_of({**refs, "refs/heads/absent": "1" * 40})
    compact, store, _uploaded = _compact(manifest, tmp_path)

    assert compact.run() == 1

    assert "does not hold every ref" in capsys.readouterr().err
    store.client.upload_file.assert_not_called()
    assert store.puts == []


def test_compact_refuses_a_repo_with_no_manifest(tmp_path, monkeypatch, capsys):
    _local_repo(tmp_path, monkeypatch)
    compact, store, _uploaded = _compact(None, tmp_path)

    assert compact.run() == 1

    assert "no gitwal.json" in capsys.readouterr().err
    assert store.puts == []


REF = "refs/heads/main"
OTHER_REF = "refs/heads/dev"


def _manage_branch(manifest, branch="main"):
    """A ManageBranch wired to a stubbed client holding real manifest bytes."""
    client = MagicMock()
    with (
        patch("boto3.Session"),
        patch("git_remote_s3.manage.register_s3_access_grants", return_value=client),
        patch("git_remote_s3.manage.register_s3_access_grants_readwrite", return_value=client),
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


def _manage_head(manifest, put_results=()):
    """A ManageHead wired to a stubbed client holding real manifest bytes."""
    client = MagicMock()
    with (
        patch("boto3.Session"),
        patch("git_remote_s3.manage.register_s3_access_grants", return_value=client),
        patch("git_remote_s3.manage.register_s3_access_grants_readwrite", return_value=client),
        patch("git_remote_s3.manage.s3_region_kwargs", return_value={}),
    ):
        store = ManifestStore(client, manifest=manifest, put_results=put_results, key="repo/gitwal.json")
        return ManageHead(None, "bucket", "repo"), store


def test_head_set_is_a_cas_update_naming_the_old_and_new_head(capsys):
    head, store = _manage_head(wal_manifest({REF: SHA1, OTHER_REF: SHA2}, head=OTHER_REF, seq=4))

    assert head.set("main") == 0

    assert len(store.puts) == 1
    assert store.puts[0]["IfMatch"] == '"etag-0"'
    assert store.manifest.head == REF
    assert store.manifest.seq == 5
    assert f"HEAD {OTHER_REF} -> {REF}" in capsys.readouterr().out


def test_head_set_accepts_a_full_refname(capsys):
    head, store = _manage_head(wal_manifest({REF: SHA1}, seq=4))

    assert head.set(REF) == 0

    assert store.manifest.head == REF
    assert "HEAD unset -> refs/heads/main" in capsys.readouterr().out


def test_head_set_refuses_a_branch_the_manifest_does_not_name(capsys):
    head, store = _manage_head(wal_manifest({REF: SHA1}, head=REF, seq=4))

    assert head.set("gone") == 1

    assert "refs/heads/gone does not exist in this repo" in capsys.readouterr().err
    # A Reject is a decision about state a retry cannot change: no PUT is issued at all.
    assert store.puts == []
    assert store.manifest.head == REF


def test_head_set_refuses_a_repo_with_no_manifest(capsys):
    head, store = _manage_head(None)

    assert head.set("main") == 1

    assert "no gitwal.json" in capsys.readouterr().err
    assert store.puts == []


def test_head_without_a_branch_reports_a_resolving_head(capsys):
    head, store = _manage_head(wal_manifest({REF: SHA1}, head=REF, seq=4))

    assert head.show() == 0

    assert f"HEAD {REF} (resolves)" in capsys.readouterr().out
    assert store.puts == []


def test_head_without_a_branch_reports_an_unresolved_head(capsys):
    head, _store = _manage_head(wal_manifest({REF: SHA1}, head=OTHER_REF, seq=4))

    assert head.show() == 0

    assert f"HEAD {OTHER_REF} (UNRESOLVED)" in capsys.readouterr().out


def test_head_without_a_branch_reports_an_unset_head(capsys):
    head, _store = _manage_head(wal_manifest({REF: SHA1}, seq=4))

    assert head.show() == 0

    assert "HEAD is unset" in capsys.readouterr().out


def test_head_is_wired_to_the_remote(mocked_cli_chain, monkeypatch):
    monkeypatch.setattr("sys.argv", ["git-s3", "head", "s3://profile@bucket/repo", "mybranch"])
    with patch("git_remote_s3.manage.ManageHead") as head_cls:
        head_cls.return_value.set.return_value = 0
        with pytest.raises(SystemExit) as excinfo:
            main()

    assert excinfo.value.code == 0
    head_cls.assert_called_once_with("profile", "bucket", "repo")
    head_cls.return_value.set.assert_called_once_with("mybranch")
    head_cls.return_value.show.assert_not_called()


def test_head_without_a_branch_is_a_read(mocked_cli_chain, monkeypatch):
    monkeypatch.setattr("sys.argv", ["git-s3", "head", "s3://profile@bucket/repo"])
    with patch("git_remote_s3.manage.ManageHead") as head_cls:
        head_cls.return_value.show.return_value = 0
        with pytest.raises(SystemExit) as excinfo:
            main()

    assert excinfo.value.code == 0
    head_cls.return_value.show.assert_called_once_with()
    head_cls.return_value.set.assert_not_called()
