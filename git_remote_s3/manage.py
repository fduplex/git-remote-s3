# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0
# Modifications Copyright 2026 FullDuplex Media
# Changed: WAL manifest audit and compaction, Access Grants diagnostics, bucket-alias resolution,
# scoped list prefixes, prefix-relative key parsing.

import argparse
import contextlib
import datetime
import os
import re
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
    register_s3_access_grants_readwrite,
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


# The pre-manifest ref layout: <repo>/refs/heads|tags/<name>/<sha>.bundle, where <name> may itself
# contain slashes. Both hash algorithms are accepted; the old reader's sha1-only filter is a bug
# migration must not inherit.
_BUNDLE_RE = re.compile(r"^(refs/(?:heads|tags)/.+)/([0-9a-f]{40}|[0-9a-f]{64})\.bundle$")

_PROTECTED_MARKER = "PROTECTED#"

_ROLLBACK = "Rollback: delete gitwal.json and packs/ to roll back; legacy keys are untouched"


@contextlib.contextmanager
def _git_dir(path: str):
    """Points the git module's subprocesses at another repository for the duration."""
    previous = os.environ.get("GIT_DIR")
    os.environ["GIT_DIR"] = path
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GIT_DIR", None)
        else:
            os.environ["GIT_DIR"] = previous


class _Repo:
    """The S3 half every command shares: a client, the repo prefix, and the manifest store."""

    def __init__(self, profile, bucket, prefix) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.session = boto3.Session(profile_name=profile)
        region_kwargs = s3_region_kwargs(self.session, bucket)
        self.s3 = register_s3_access_grants(self.session.client("s3", **region_kwargs), self.session)
        # The manifest's conditional PUTs need a READWRITE-vended session, which S3 evaluates
        # s3:GetObject against; the shared client's per-operation WRITE session would be denied.
        self.write_s3 = register_s3_access_grants_readwrite(self.session.client("s3", **region_kwargs), self.session)
        self.wal = WalStore(self.s3, bucket=bucket, prefix=prefix, writer=lambda: self.write_s3)
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

    def list_packs(self) -> dict[str, int]:
        """{repo-relative pack key -> size in bytes} for everything under <repo>/packs/."""
        relative = f"{gitwal.PACKS_PREFIX}/"
        base = scoped_list_prefix(self.prefix)
        return {o["Key"].removeprefix(base): o["Size"] for o in self.list_objects(relative)}

    def incomplete_refs(self, refs: dict[str, str]) -> list[tuple[str, str]]:
        """The (ref, sha) pairs this clone cannot pack: absent object, or truncated history."""
        return [
            (ref, sha)
            for ref, sha in sorted(refs.items())
            if not git.has_object(sha) or not git.has_complete_history(sha)
        ]

    def upload_pack(self, pack: "git.Pack", tips: dict[str, str]) -> gitwal.Entry:
        """Writes a base pack to its content-addressed key. Not a commit point."""
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
            tips=dict(tips),
            by=self.caller_arn(),
            at=_now(),
        )


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
        incomplete = self.incomplete_refs(manifest.refs)
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
            entry = self.upload_pack(pack, manifest.refs)
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


class Migrate(_Repo):
    """Moves one repo from the bundle format to the WAL manifest, under supervision.

    Phase 1 writes packs/<sha>.pack and gitwal.json alongside the legacy keys and touches nothing
    else. The two formats are mutually invisible -- the old helper lists only *.bundle under refs/,
    the new client reads only the manifest -- so both are live and correct at once and rollback is
    deleting what phase 1 wrote. Phase 2 (``--finalize``) deletes the legacy keys and is the point
    of no return, which is why it is a separate command behind its own flag.
    """

    def __init__(self, profile, bucket, prefix, finalize=False, yes=False) -> None:
        super().__init__(profile, bucket, prefix)
        self.finalize = finalize
        self.yes = yes

    def run(self) -> int:
        return self.run_finalize() if self.finalize else self.run_migrate()

    def run_migrate(self) -> int:  # noqa: C901
        refs = self.preflight()
        if refs is None:
            return 1

        head = self.read_head()
        protected = self.read_protected()
        print(f"Migrating {self.name}: {len(refs)} refs, HEAD {head or 'unset'}, {len(protected)} protected")

        temp_dir = tempfile.mkdtemp(prefix="git_remote_s3_migrate_")
        try:
            pack = git.pack_all(folder=temp_dir, shas=refs.values(), quiet=True)
            entry = self.upload_pack(pack, refs)
            manifest = gitwal.apply_push(gitwal.Manifest(), refs=refs, entry=entry, head=head)
            # Set inline rather than through apply_protect: every marker is part of the same
            # seq-1 creation, and each apply_ call would bump the seq again.
            manifest.protected = protected
            self.wal.create(manifest)
            print(f"Created gitwal.json at seq {manifest.seq}")
            problems = self.verify(refs, head, entry, folder=temp_dir)
        except (GitError, WalStoreError, gitwal.ManifestError, ClientError) as x:
            sys.stderr.write(f"fatal: {x}\n")
            return 1
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if problems:
            sys.stderr.write("fatal: the migrated repo does not verify:\n")
            for problem in problems:
                sys.stderr.write(f"  {problem}\n")
            sys.stderr.write(_ROLLBACK + "\n")
            return 1
        self.report_migrated()
        return 0

    def preflight(self) -> dict[str, str] | None:
        """Everything that must hold before a byte is written. None means refuse."""
        manifest, _etag = self.wal.load()
        if manifest is not None:
            sys.stderr.write("fatal: gitwal.json already exists; this repo has already been migrated\n")
            return None

        bundles = self.bundle_refs()
        duplicates = {ref: shas for ref, shas in bundles.items() if len(shas) > 1}
        if duplicates:
            sys.stderr.write("fatal: these refs carry more than one bundle:\n")
            for ref, shas in sorted(duplicates.items()):
                sys.stderr.write(f"  {ref}: {', '.join(sorted(shas))}\n")
            sys.stderr.write("resolve duplicate bundles first: keep the tip you want and delete the others\n")
            return None
        if not bundles:
            sys.stderr.write("fatal: no bundles under refs/; there is nothing to migrate\n")
            return None

        if git.is_shallow_repository():
            sys.stderr.write("fatal: cannot migrate from a shallow clone; run git fetch --unshallow first\n")
            return None
        refs = {ref: shas[0] for ref, shas in bundles.items()}
        incomplete = self.incomplete_refs(refs)
        if incomplete:
            sys.stderr.write("fatal: this clone does not hold every ref the bundles name:\n")
            for ref, sha in incomplete:
                sys.stderr.write(f"  {ref} {sha}\n")
            sys.stderr.write("run git fetch --all and re-run migrate from a full clone\n")
            return None
        return refs

    def bundle_refs(self) -> dict[str, list[str]]:
        """{full refname -> the shas its bundle keys name}, from the legacy layout."""
        base = scoped_list_prefix(self.prefix)
        found: dict[str, list[str]] = {}
        for obj in self.list_objects("refs/"):
            match = _BUNDLE_RE.match(obj["Key"].removeprefix(base))
            if match:
                found.setdefault(match.group(1), []).append(match.group(2))
        return found

    def read_head(self) -> str | None:
        """The refname in the <repo>/HEAD object, or None when the repo never had one."""
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.key("HEAD"))
        except self.s3.exceptions.NoSuchKey:
            return None
        except self.s3.exceptions.ClientError as x:
            if x.response.get("Error", {}).get("Code") in ("NoSuchKey", "NotFound", "404"):
                return None
            raise
        return response["Body"].read().decode("utf-8").strip() or None

    def read_protected(self) -> list[str]:
        """The refs carrying a PROTECTED# marker, derived from the marker keys."""
        base = scoped_list_prefix(self.prefix)
        marker = f"/{_PROTECTED_MARKER}"
        return sorted(
            obj["Key"].removeprefix(base).removesuffix(marker)
            for obj in self.list_objects("refs/")
            if obj["Key"].endswith(marker)
        )

    def verify(self, refs: dict[str, str], head: str | None, entry: gitwal.Entry, *, folder: str) -> list[str]:
        """Reads back what was written and rebuilds every ref from the stored pack alone.

        Cloning through the new path would need a real endpoint and the helper on PATH, so this
        checks the two properties a clone would have proved: the manifest S3 now serves names
        every ref at the sha its bundle key named, and the pack S3 now holds reconstructs each of
        those tips in a repository that starts empty. Ref names are not rev-parsed against the
        local clone -- a clone holds the remote's branches under refs/remotes/* -- so the local
        side is checked by sha, in preflight.
        """
        stored, _etag = self.wal.load()
        if stored is None:
            return ["gitwal.json cannot be read back from S3"]

        problems = [f"schema {f.code}: {f.message}" for f in gitwal.errors(gitwal.validate(stored))]
        if stored.seq != 1:
            problems.append(f"seq is {stored.seq}, not 1")
        if stored.refs != refs:
            problems.append(f"manifest refs {stored.refs} do not match the bundle keys {refs}")
        if stored.head != head:
            problems.append(f"manifest head is {stored.head!r}, not {head!r}")
        if [e.pack for e in stored.entries] != [entry.pack]:
            problems.append(f"manifest entries name {[e.pack for e in stored.entries]}, not [{entry.pack!r}]")

        restored = f"{folder}/verify.git"
        pack_path = f"{folder}/verify.pack"
        try:
            git.init_bare(restored)
            self.s3.download_file(Bucket=self.bucket, Key=self.key(entry.pack), Filename=pack_path)
            with _git_dir(restored):
                git.index_pack(path=pack_path)
                for ref, sha in sorted(refs.items()):
                    if not git.has_object(sha) or not git.has_complete_history(sha):
                        problems.append(f"{ref} {sha} is not reconstructible from {entry.pack}")
        except (GitError, ClientError, OSError) as x:
            problems.append(f"could not rebuild the repo from {entry.pack}: {x}")
        return problems

    def report_migrated(self) -> None:
        print(f"\nMigrated {self.name}. Observe before finalizing:")
        print("  - the materializer fires on the gitwal.json PUT: confirm this repo re-rendered")
        print("  - the SPA lists this repo's branches at their expected tips")
        print("  - clone and push once through the new client")
        print("  - the legacy keys are still present; nothing has been deleted")
        print(f"\n{_ROLLBACK}")
        print("When satisfied: git-s3 migrate --finalize --yes <remote>")

    def run_finalize(self) -> int:
        if not self.yes:
            sys.stderr.write(
                "fatal: --finalize deletes this repo's pre-migration keys and cannot be undone; "
                "re-run with --yes once the repo has been observed\n"
            )
            return 1

        manifest, _etag = self.wal.load()
        if manifest is None:
            sys.stderr.write("fatal: no gitwal.json in this repo; migrate it before finalizing\n")
            return 1
        findings = gitwal.errors(gitwal.validate(manifest))
        if findings:
            sys.stderr.write("fatal: gitwal.json does not validate; refusing to delete anything:\n")
            for finding in findings:
                sys.stderr.write(f"  {finding.code}: {finding.message}\n")
            return 1
        stored = self.list_packs()
        missing = [e.pack for e in manifest.entries if e.pack not in stored]
        if missing:
            sys.stderr.write("fatal: the manifest names packs that are not in the bucket:\n")
            for pack in missing:
                sys.stderr.write(f"  {pack}\n")
            return 1

        base = scoped_list_prefix(self.prefix)
        legacy = [o["Key"] for o in self.list_objects() if _is_legacy_key(o["Key"].removeprefix(base))]
        if not legacy:
            print(f"{self.name}: no pre-migration keys left; nothing to finalize")
            return 0

        failures = 0
        for key in legacy:
            try:
                self.s3.delete_object(Bucket=self.bucket, Key=key)
                print(f"deleted {key}")
            except ClientError as x:
                failures += 1
                print(f"could not delete {key}: {x}")
        print(f"Finalized {self.name}: {len(legacy) - failures} of {len(legacy)} pre-migration keys deleted")
        return 1 if failures else 0


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
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="migrate: delete the pre-migration keys, after the repo has been observed",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="migrate --finalize: confirm the deletion, which cannot be undone",
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
        if args.command == "migrate":
            sys.exit(Migrate(profile, bucket, prefix, args.finalize, args.yes).run())
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
