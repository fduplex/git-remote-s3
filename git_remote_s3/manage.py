# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0
# Modifications Copyright 2026 FullDuplex Media
# Changed: WAL manifest audit and compaction, Access Grants diagnostics, bucket-alias resolution,
# scoped list prefixes, prefix-relative key parsing.

import argparse
import datetime
import shutil
import sys
import tempfile
from typing import Any

import boto3
from botocore.exceptions import (
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    ProfileNotFound,
    UnknownCredentialError,
)

from . import git, gitwal
from .common import (
    BucketAliasError,
    TRANSFER_CONFIG,
    register_s3_access_grants,
    register_s3_access_grants_strict,
    resolve_bucket_alias,
    s3_region_kwargs,
    scoped_list_prefix,
)
from .git import GitError, get_remote_url
from .remote import parse_git_url
from .walstore import Reject, WalStore, WalStoreError

_ACCESS_GRANTS_HINTS = {
    "GetAccessGrantsInstanceForPrefix": ("caller role is missing s3:GetAccessGrantsInstanceForPrefix"),
    "GetDataAccess": ("caller role is missing s3:GetDataAccess or has no matching grant"),
}


def _access_grants_hint(operation, code):
    """Maps an (operation, error code) to an actionable IAM hint, or None."""
    if code != "AccessDenied":
        return None
    return _ACCESS_GRANTS_HINTS.get(operation)


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_legacy_key(rel: str) -> bool:
    """Whether a repo-relative key belongs to the pre-manifest format.

    Deliberately narrow: lfs/<oid>, packs/*.pack and gitwal.json must never match. This whole
    arm is deleted one release after the last repo is migrated.
    """
    leaf = rel.rpartition("/")[2]
    return rel == "HEAD" or leaf in ("PROTECTED#", "repo.zip") or rel.endswith((".lock", ".bundle"))


class _Repo:
    """The S3 half every command shares: a client, the repo prefix, and the manifest store."""

    def __init__(self, profile, bucket, prefix) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.session = boto3.Session(profile_name=profile)
        self.s3 = register_s3_access_grants(
            self.session.client("s3", **s3_region_kwargs(self.session, bucket)),
            self.session,
        )
        self.wal = WalStore(self.s3, bucket=bucket, prefix=prefix)
        self.name = prefix if prefix else "<bucket root>"

    def key(self, relative: str) -> str:
        return f"{self.prefix}/{relative}" if self.prefix else relative

    def list_objects(self, relative_prefix: str = "") -> list[dict]:
        list_prefix = f"{scoped_list_prefix(self.prefix)}{relative_prefix}"
        res = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=list_prefix)
        contents = res.get("Contents", [])
        next_token = res.get("NextContinuationToken", None)

        while next_token:
            res = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=list_prefix, ContinuationToken=next_token)
            contents.extend(res.get("Contents", []))
            next_token = res.get("NextContinuationToken", None)
        return contents

    def caller_arn(self) -> str | None:
        """The caller's STS ARN, for entry provenance. Best effort: never fails a command."""
        try:
            return self.session.client("sts").get_caller_identity()["Arn"]
        except Exception:
            return None


class Doctor(_Repo):
    """An auditor, not a repair tool.

    The WAL format cannot be half-written, so there is nothing left to fix interactively: doctor
    reports what the manifest says, what S3 holds, and whether compaction is due. The one finding
    it treats as corruption is an entry naming a pack that is not there.
    """

    def __init__(self, profile, bucket, prefix, delete_legacy=False) -> None:
        super().__init__(profile, bucket, prefix)
        self.delete_legacy = delete_legacy

    def run(self) -> int:
        """Prints the report and returns the process exit code."""
        self.check_access_grants()
        manifest = self.load_manifest()
        errors = 0
        if manifest is None:
            print(f"\n{self.name}:")
            print(" gitwal.json: missing (this repo has not been migrated)")
        else:
            errors += self.report_manifest(manifest)
            errors += self.report_packs(manifest)
        self.report_legacy()
        return 1 if errors else 0

    def load_manifest(self) -> gitwal.Manifest | None:
        manifest, _etag = self.wal.load()
        return manifest

    def check_access_grants(self):
        """Diagnoses the S3 Access Grants path and names any missing permission.

        The production S3 client registers the plugin with fallback enabled, so a
        broken Access Grants setup silently drops to direct credentials. This probe
        registers a SEPARATE, fallback-disabled plugin and drives the full vend
        path (including the ``GetAccessGrantsInstanceForPrefix`` preflight) against
        the repo prefix, surfacing the real error rather than masking it.

        This is informational: an IAM-access-key caller with no grant will
        legitimately report "not available" and keep using direct credentials.
        """
        print("\nChecking S3 Access Grants entitlement...")
        probe = register_s3_access_grants_strict(
            self.session.client("s3", **s3_region_kwargs(self.session, self.bucket)),
            self.session,
        )
        # Scoped to exactly this repo, matching the S3Remote listing path (see
        # scoped_list_prefix): a bare prefix would also match a sibling repo.
        scoped_prefix = scoped_list_prefix(self.prefix)
        try:
            probe.list_objects_v2(Bucket=self.bucket, Prefix=scoped_prefix)
        except probe.exceptions.ClientError as x:
            operation = getattr(x, "operation_name", None) or "S3 request"
            error = x.response.get("Error", {})
            code = error.get("Code", "Unknown")
            message = error.get("Message", "")
            hint = _access_grants_hint(operation, code)
            if code == "AccessDenied":
                print(" Access Grants: not available (using direct S3 credentials)")
            else:
                print(" Access Grants: ERROR")
            print(f"  {operation} failed: {code}")
            if hint:
                print(f"  hint: {hint}")
            elif message:
                print(f"  {message}")
        except Exception as x:  # a failed diagnostic must not abort the doctor run
            print(f" Access Grants: could not be checked ({x})")
        else:
            print(f" Access Grants: OK (vended credentials for s3://{self.bucket}/{scoped_prefix})")

    def report_manifest(self, manifest: gitwal.Manifest) -> int:
        """Prints the schema audit and the manifest's own numbers. Returns the error count."""
        print(f"\n{self.name}:")
        print(f" format: {manifest.format}  seq: {manifest.seq}")
        findings = gitwal.validate(manifest)
        if not findings:
            print(" schema: OK")
        for finding in findings:
            print(f" schema {finding.level}: {finding.code}: {finding.message}")
        print(f" refs: {len(manifest.refs)}")
        head = manifest.head
        if head is None:
            print(" HEAD: unset")
        else:
            print(f" HEAD: {head} ({'resolves' if head in manifest.refs else 'UNRESOLVED'})")
        print(f" protected: {', '.join(sorted(manifest.protected)) if manifest.protected else 'none'}")
        live_bytes = sum(e.bytes for e in manifest.entries)
        print(f" entries: {len(manifest.entries)} ({_human_bytes(live_bytes)} of live packs)")
        return len(gitwal.errors(findings))

    def report_packs(self, manifest: gitwal.Manifest) -> int:
        """Diffs entries[].pack against the pack objects in S3. Returns the error count.

        An orphan is inert by definition and is collected by ``git-s3 compact``, never here:
        doctor deletes nothing. A missing pack is corruption, and it is the only finding in this
        format that exits non-zero.
        """
        stored = self.list_packs()
        named = {e.pack: e for e in manifest.entries}

        missing = [entry for pack, entry in sorted(named.items()) if pack not in stored]
        for entry in missing:
            print(f" MISSING PACK: entry seq {entry.seq} names {entry.pack}, which is not in the bucket")

        orphans = sorted(pack for pack in stored if pack not in named)
        reclaimable = sum(stored[pack] for pack in orphans)
        if orphans:
            print(f" orphan packs: {len(orphans)} ({_human_bytes(reclaimable)} reclaimable by git-s3 compact)")
        else:
            print(" orphan packs: none")
        if len(manifest.entries) > 1:
            print(f" compaction: {len(manifest.entries)} entries would collapse to 1")
        return len(missing)

    def list_packs(self) -> dict[str, int]:
        """{repo-relative pack key -> size in bytes} for everything under <repo>/packs/."""
        relative = f"{gitwal.PACKS_PREFIX}/"
        base = scoped_list_prefix(self.prefix)
        return {o["Key"].removeprefix(base): o["Size"] for o in self.list_objects(relative)}

    def list_repo_objects(self) -> list[dict]:
        return self.list_objects()

    def report_legacy(self) -> None:
        """Reports (and with --delete-legacy, removes) the pre-migration keys.

        Bundles, PROTECTED# markers, residual LOCK#.lock files, repo.zip and the <repo>/HEAD
        object are invisible to every reader of the new format, so this is housekeeping rather
        than a correctness matter. The whole arm goes away one release after the last migration.
        """
        print("\nScanning for pre-migration keys...")
        base = scoped_list_prefix(self.prefix)
        legacy = [
            (o["Key"], o.get("Size", 0)) for o in self.list_objects() if _is_legacy_key(o["Key"].removeprefix(base))
        ]
        if not legacy:
            print(" none found")
            return

        total = sum(size for _key, size in legacy)
        print(f" {len(legacy)} legacy objects ({_human_bytes(total)})")
        for key, _size in legacy:
            print(f"  - {key}")
        if not self.delete_legacy:
            print(" run with --delete-legacy to remove them")
            return

        print(" Deleting...")
        for key, _size in legacy:
            try:
                self.s3.delete_object(Bucket=self.bucket, Key=key)
                print(f"  deleted {key}")
            except ClientError as x:
                print(f"  failed to delete {key}: {x}")


class Compact(_Repo):
    """Collapses the entry log to a single base pack: one more CAS, plus deletes that follow it.

    The base pack is built from the operator's local clone, so the clone must hold every ref the
    manifest names -- a pack built from a partial clone would drop objects the manifest promises.
    """

    def run(self) -> int:  # noqa: C901
        manifest, _etag = self.wal.load()
        if manifest is None:
            sys.stderr.write("fatal: no gitwal.json in this repo; nothing to compact\n")
            return 1
        if not manifest.refs:
            print("Nothing to compact: the manifest names no refs")
            return 0
        if git.is_shallow_repository():
            sys.stderr.write("fatal: cannot compact from a shallow clone; run git fetch --unshallow first\n")
            return 1
        incomplete = self.incomplete_refs(manifest)
        if incomplete:
            sys.stderr.write("fatal: this clone does not hold every ref the manifest names:\n")
            for ref, sha in incomplete:
                sys.stderr.write(f"  {ref} {sha}\n")
            sys.stderr.write("run git fetch --all and re-run compact from a full clone\n")
            return 1

        before_entries = len(manifest.entries)
        before_bytes = sum(e.bytes for e in manifest.entries)
        baseline = _fingerprint(manifest)

        temp_dir = tempfile.mkdtemp(prefix="git_remote_s3_compact_")
        try:
            pack = git.pack_all(folder=temp_dir, shas=manifest.refs.values(), quiet=True)
            entry = self.upload(pack, manifest)
        except (GitError, ClientError) as x:
            sys.stderr.write(f"fatal: {x}\n")
            return 1
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        replaced: list[gitwal.Manifest] = []

        def mutate(current: gitwal.Manifest) -> gitwal.Manifest:
            # The base pack covers the refs as they were when it was built. Anything committed
            # since could name objects it does not carry, so the only safe answer is to abort;
            # the pack we uploaded becomes an orphan and nothing is damaged.
            if _fingerprint(current) != baseline:
                raise Reject("repo changed during compaction; re-run")
            replaced.append(current)
            return gitwal.apply_compaction(current, entry=entry)

        try:
            committed = self.wal.update(mutate)
        except Reject as x:
            sys.stderr.write(f"fatal: {x}\n")
            return 1
        except (WalStoreError, gitwal.ManifestError, ClientError) as x:
            sys.stderr.write(f"fatal: {x}\n")
            return 1

        # Strictly after the commit. Until the CAS landed these packs were still live, and a
        # crash before the deletes leaves orphans, which the format defines as harmless.
        superseded = gitwal.superseded_packs(replaced[-1], committed)
        deleted = self.delete_packs(superseded)

        after_bytes = sum(e.bytes for e in committed.entries)
        print(
            f"Compacted {self.name}: {before_entries} entries ({_human_bytes(before_bytes)}) "
            f"-> {len(committed.entries)} entry ({_human_bytes(after_bytes)}) at seq {committed.seq}"
        )
        print(f"Deleted {deleted} superseded pack{'' if deleted == 1 else 's'}")
        return 0

    def incomplete_refs(self, manifest: gitwal.Manifest) -> list[tuple[str, str]]:
        return [
            (ref, sha)
            for ref, sha in sorted(manifest.refs.items())
            if not git.has_object(sha) or not git.has_complete_history(sha)
        ]

    def upload(self, pack: "git.Pack", manifest: gitwal.Manifest) -> gitwal.Entry:
        """Writes the base pack to its content-addressed key. Not a commit point."""
        relative_key = f"{gitwal.PACKS_PREFIX}/{pack.checksum}.pack"
        print(f"Uploading base pack {relative_key} ({_human_bytes(pack.bytes)}, {pack.objects} objects)")
        self.s3.upload_file(
            Filename=pack.path,
            Bucket=self.bucket,
            Key=self.key(relative_key),
            Config=TRANSFER_CONFIG,
        )
        return gitwal.Entry(
            kind=gitwal.KIND_BASE,
            pack=relative_key,
            bytes=pack.bytes,
            objects=pack.objects,
            tips=dict(manifest.refs),
            by=self.caller_arn(),
            at=_now(),
        )

    def delete_packs(self, packs: list[str]) -> int:
        deleted = 0
        for pack in packs:
            try:
                self.s3.delete_object(Bucket=self.bucket, Key=self.key(pack))
                deleted += 1
            except ClientError as x:
                # A pack that outlives its entry is an orphan; the next compaction collects it.
                print(f"could not delete {pack}: {x}")
        return deleted


def _fingerprint(manifest: gitwal.Manifest) -> tuple[Any, ...]:
    """What compaction assumed about the repo when it built its base pack."""
    return (
        tuple((e.seq, e.pack) for e in manifest.entries),
        tuple(sorted(manifest.refs.items())),
    )


class ManageBranch(_Repo):
    """Branch operations that are all the same thing under the WAL: one conditional manifest PUT."""

    def __init__(self, profile, bucket, prefix, branch) -> None:
        super().__init__(profile, bucket, prefix)
        self.branch = branch
        self.ref = f"refs/heads/{branch}"
        manifest, _etag = self.wal.load()
        if manifest is None or self.ref not in manifest.refs:
            raise ValueError(f"Branch {self.branch} does not exist")

    def process_cmd(self, cmd):
        if cmd == "delete-branch":
            self.delete_branch()
        if cmd == "protect":
            self.protect_branch()
        if cmd == "unprotect":
            self.unprotect_branch()

    def delete_branch(self):
        resp = input(f"Delete {self.branch} branch [yes/no]: ")
        if resp.lower() != "yes":
            print("Aborted")
            return
        # Refs-only CAS: the objects this branch uniquely held stay in their packs until the repo
        # is compacted. Delete-branch no longer reclaims storage on its own.
        self.wal.update(lambda manifest: gitwal.apply_delete(manifest, ref=self.ref))
        print(f"Branch {self.branch} has been deleted")

    def protect_branch(self):
        self.wal.update(lambda manifest: gitwal.apply_protect(manifest, ref=self.ref))
        print(f"Branch {self.branch} is now protected")

    def unprotect_branch(self):
        self.wal.update(lambda manifest: gitwal.apply_unprotect(manifest, ref=self.ref))
        print(f"Branch {self.branch} is now unprotected")


def main():  # noqa: C901
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("remote", help="The remote s3 uri to analyze, including the AWS profile if used")
    parser.add_argument(
        "--delete-legacy",
        action="store_true",
        help="Delete the pre-migration keys doctor reports (bundles, PROTECTED#, locks, repo.zip, HEAD)",
    )
    # Optional: "doctor" and "compact" don't take a branch; delete-branch/protect/unprotect
    # validate it's present themselves (see the args.branch is None check below).
    parser.add_argument(
        "branch",
        type=str,
        nargs="?",
        default=None,
        help="Branch to delete from the remote",
    )
    args = parser.parse_args()
    remote = args.remote
    try:
        remote_url = get_remote_url(remote)
    except GitError as e:
        sys.stderr.write(f"fatal: {e}\n")
        sys.stderr.flush()
        sys.exit(1)

    uri_scheme, profile, bucket, prefix = parse_git_url(remote_url)
    try:
        bucket = resolve_bucket_alias(bucket)
    except BucketAliasError as e:
        sys.stderr.write(f"fatal: {e}\n")
        sys.stderr.flush()
        sys.exit(1)
    try:
        if args.command == "doctor":
            sys.exit(Doctor(profile, bucket, prefix, args.delete_legacy).run())
        if args.command == "compact":
            sys.exit(Compact(profile, bucket, prefix).run())
        if args.command == "delete-branch" or args.command == "protect" or args.command == "unprotect":
            if args.branch is None:
                sys.stderr.write("fatal: --branch is required\n")
                sys.stderr.flush()
                sys.exit(1)
            try:
                manage_branch = ManageBranch(profile, bucket, prefix, args.branch)
                manage_branch.process_cmd(args.command)
            except ValueError as e:
                sys.stderr.write(f"fatal: {e}\n")
                sys.stderr.flush()
                sys.exit(1)

        sys.exit(0)

    except (
        ClientError,
        ProfileNotFound,
        CredentialRetrievalError,
        NoCredentialsError,
        UnknownCredentialError,
    ) as e:
        sys.stderr.write(f"fatal: invalid credentials {e}\n")
        sys.stderr.flush()
        sys.exit(1)
