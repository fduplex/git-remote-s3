# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0
# Modifications Copyright 2026 FullDuplex Media
# Changed: Added LFS auto-install, DNS bucket-alias resolution, Access Grants, and scoped list prefixes.

import sys
import logging
import boto3
import boto3.exceptions
from botocore.exceptions import (
    ClientError,
    ProfileNotFound,
    CredentialRetrievalError,
    NoCredentialsError,
    UnknownCredentialError,
)
from boto3.s3.transfer import TransferConfig
import re
import shutil
import subprocess
import tempfile
import os
import concurrent.futures
from threading import Lock

import botocore.exceptions
from git_remote_s3 import git
from .enums import UriScheme
from .common import (
    parse_git_url,
    resolve_bucket_alias,
    register_s3_access_grants,
    s3_region_kwargs,
    scoped_list_prefix,
    BucketAliasError,
)
import botocore
import contextlib

logger = logging.getLogger(__name__)
if "remote" in __name__:
    # Check for early verbosity via environment variable
    verbose_env = os.environ.get("GIT_REMOTE_S3_VERBOSE", "").lower() in (
        "1",
        "true",
        "yes",
    )
    log_level = logging.INFO if verbose_env else logging.ERROR
    logging.basicConfig(
        level=log_level,
        stream=sys.stderr,
        format="%(name)s: %(levelname)s: %(message)s",
    )

DEFAULT_LOCK_TTL_SECONDS = 60

_KB = 1024
_MB = 1024**2

# 16 MB parts keep per-request overhead low while still giving enough chunks to spread over the
# 8 worker threads; going multipart above 25 MB also lifts the 5 GB single-PUT ceiling.
_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=25 * _MB,
    multipart_chunksize=16 * _MB,
    use_threads=True,
    max_concurrency=8,
)


class TransferProgress:
    """Renders S3 transfer progress on stderr, which git leaves attached to the user's terminal.

    boto3 invokes the callback from several transfer threads under multipart, so both the counter
    and the write have to be serialised.
    """

    def __init__(self, *, action: str, label: str, total_bytes: int | None = None):
        self.action = action
        self.label = label
        self.total_bytes = total_bytes
        self._seen_so_far = 0
        self._lock = Lock()
        self._rendered = False

    def __call__(self, bytes_amount: int) -> None:
        with self._lock:
            self._seen_so_far += bytes_amount
            if self.total_bytes:
                pct = min(100, int(self._seen_so_far * 100 / self.total_bytes))
                unit, divisor, decimals = self._unit_for(self.total_bytes)
                seen_disp = self._seen_so_far / divisor
                total_disp = self.total_bytes / divisor
                sys.stderr.write(
                    f"\r{self.action} {self.label}: {seen_disp:.{decimals}f} / {total_disp:.{decimals}f} "
                    f"{unit} ({pct}%)"
                )
            else:
                unit, divisor, decimals = self._unit_for(self._seen_so_far)
                seen_disp = self._seen_so_far / divisor
                sys.stderr.write(f"\r{self.action} {self.label}: {seen_disp:.{decimals}f} {unit}")
            sys.stderr.flush()
            self._rendered = True

    @staticmethod
    def _unit_for(reference_bytes: int) -> tuple[str, int, int]:
        """Picks the display unit off the given byte count: KiB below 1 MiB, MiB at or above."""
        if reference_bytes < _MB:
            return "KiB", _KB, 0
        return "MiB", _MB, 1

    def close(self) -> None:
        with self._lock:
            if self._rendered:
                sys.stderr.write("\n")
                sys.stderr.flush()
                self._rendered = False


class BucketNotFoundError(Exception):
    def __init__(self, bucket: str):
        self.bucket = bucket
        super().__init__(f"Bucket {bucket} not found.")


class NotAuthorizedError(Exception):
    def __init__(self, action: str, bucket: str):
        self.bucket = bucket
        self.action = action
        super().__init__(f"Not authorized to perform {action} on the S3 bucket {bucket}.")


class Mode:
    FETCH = "fetch"
    PUSH = "push"


def maybe_install_lfs_agent(remote_name: str) -> None:
    """Install the git-lfs-s3 transfer agent in local git config if unset.

    Skipped when GIT_REMOTE_S3_AUTO_INSTALL_LFS is 0/false/no, or when
    lfs.standalonetransferagent or remote.<name>.lfsurl is already set.
    """
    if os.environ.get("GIT_REMOTE_S3_AUTO_INSTALL_LFS", "1").lower() in (
        "0",
        "false",
        "no",
    ):
        return

    if _git_config_get("lfs.standalonetransferagent"):
        return
    if _git_config_get(f"remote.{remote_name}.lfsurl"):
        return

    _git_config_add("lfs.customtransfer.git-lfs-s3.path", "git-lfs-s3")
    _git_config_add("lfs.standalonetransferagent", "git-lfs-s3")


def _git_config_get(key: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "config", "--get", key],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _git_config_add(key: str, value: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        subprocess.run(
            ["git", "config", "--add", key, value],
            check=False,
            stderr=subprocess.DEVNULL,
        )


class S3Remote:
    def __init__(self, uri_scheme, profile, bucket, prefix, *, remote_name=None):
        self.uri_scheme = uri_scheme
        self.profile = profile
        self.bucket = bucket
        self.prefix = prefix
        self.remote_name = remote_name
        if profile:
            self.session = boto3.Session(profile_name=profile)
        else:
            self.session = boto3.Session()
        self.s3 = register_s3_access_grants(
            self.session.client("s3", **s3_region_kwargs(self.session, bucket)),
            self.session,
        )
        try:
            # Scope to exactly this repo: a bare Prefix="core/cli" also matches
            # a sibling repo like "core/climate", so use the trailing-slash
            # scoped prefix instead.
            self.s3.list_objects_v2(Bucket=bucket, Prefix=scoped_list_prefix(prefix))
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchBucket":
                raise BucketNotFoundError(bucket) from e
            if e.response["Error"]["Code"] == "AccessDenied":
                raise NotAuthorizedError("ListObjectsV2", bucket) from e
            raise e

        self.bucket = bucket
        self.mode = None
        self.progress = False
        self.verbosity = 1
        self.fetched_refs = []
        self.fetched_refs_lock = Lock()  # Lock for thread-safe access to fetched_refs
        self.push_cmds = []
        self.fetch_cmds = []  # Store fetch commands for batch processing
        # <remote ref> -> sha git leased the ref against, from `option cas` (--force-with-lease).
        self.cas_refs: dict[str, str] = {}
        self._protected_cache: dict[str, list] = {}
        # Lock TTL (seconds); can be configured via env var
        try:
            self.lock_ttl_seconds = int(os.environ.get("GIT_REMOTE_S3_LOCK_TTL_SECONDS", DEFAULT_LOCK_TTL_SECONDS))
        except ValueError:
            self.lock_ttl_seconds = DEFAULT_LOCK_TTL_SECONDS

        # remote_name is only set when invoked via the remote-helper protocol;
        # gating on it keeps tests from writing to the project's own git config.
        if remote_name is not None:
            maybe_install_lfs_agent(remote_name)

    def list_refs(self, *, bucket: str, prefix: str) -> list:
        # Scoped to the refs/ subtree server-side: in an LFS-heavy repo the
        # "lfs/<oid>" keys vastly outnumber refs, so listing the bare repo
        # prefix (see __init__'s existence-check probe, which does need the
        # bare prefix) would paginate through every LFS object on every push
        # and fetch. For a bucket-root repo (prefix == "") this scopes to
        # "/refs"; keys there are written as f"{prefix}/{ref}/..." = "/refs/...",
        # carrying that same leading slash, so the scoped prefix still matches
        # them exactly as the bare-prefix filter below always did.
        list_prefix = f"{prefix}/refs"
        res = self.s3.list_objects_v2(Bucket=bucket, Prefix=list_prefix)
        contents = res.get("Contents", [])
        next_token = res.get("NextContinuationToken", None)

        while next_token:
            res = self.s3.list_objects_v2(Bucket=bucket, Prefix=list_prefix, ContinuationToken=next_token)
            contents.extend(res.get("Contents", []))
            next_token = res.get("NextContinuationToken", None)

        contents.sort(key=lambda x: x["LastModified"])
        contents.reverse()

        objs = [
            o["Key"].removeprefix(prefix)[1:]
            for o in contents
            if o["Key"].startswith(prefix + "/refs") and o["Key"].endswith(".bundle")
        ]
        return objs

    @contextlib.contextmanager
    def transfer_progress(self, *, action: str, label: str, total_bytes: int | None = None, show_progress: bool = True):
        """Yields a boto3 transfer Callback, or None when git did not ask for progress."""
        progress = (
            TransferProgress(action=action, label=label, total_bytes=total_bytes)
            if self.progress and show_progress
            else None
        )
        try:
            yield progress
        finally:
            if progress is not None:
                progress.close()

    def cmd_fetch(self, args: str, *, show_progress: bool = True):
        """Fetches a single ref's bundle and unbundles it.

        Args:
            args (str): the `fetch <sha> <ref>` command line from git
            show_progress (bool): render this fetch's transfer progress. Multiple concurrent
                fetches would otherwise interleave their own `\\r`-updating meters (both the S3
                download callback and `git bundle unbundle --progress`) into garbled output, so
                process_fetch_cmds disables it for every fetch in a multi-ref batch.
        """
        sha, ref = args.split(" ")[1:]
        with self.fetched_refs_lock:
            if sha in self.fetched_refs:
                return
        logger.info(f"fetch {sha} {ref}")
        temp_dir: str | None = None
        try:
            temp_dir = tempfile.mkdtemp(prefix="git_remote_s3_fetch_")
            bundle_path = f"{temp_dir}/{sha}.bundle"

            # The object size is unknown before the transfer starts, so the renderer reports
            # bytes transferred without a percentage rather than paying for a head_object.
            with self.transfer_progress(action="Downloading", label=ref, show_progress=show_progress) as progress:
                self.s3.download_file(
                    Bucket=self.bucket,
                    Key=f"{self.prefix}/{ref}/{sha}.bundle",
                    Filename=bundle_path,
                    Config=_TRANSFER_CONFIG,
                    Callback=progress,
                )

            logger.info(f"fetched {bundle_path} {ref}")

            git.unbundle(folder=temp_dir, sha=sha, ref=ref, progress=self.progress and show_progress)
            with self.fetched_refs_lock:
                self.fetched_refs.append(sha)
        except ClientError as e:
            if e.response["Error"]["Code"] == "AccessDenied":
                raise NotAuthorizedError("GetObject", self.bucket) from e
            raise e
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def remove_remote_ref(self, remote_ref: str) -> str:
        logger.info(f"Removing remote ref {remote_ref}")
        try:
            objects_to_delete = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=f"{self.prefix}/{remote_ref}/").get(
                "Contents", []
            )
            if (
                self.uri_scheme == UriScheme.S3
                and len(objects_to_delete) == 1
                or self.uri_scheme == UriScheme.S3_ZIP
                and len(objects_to_delete) == 2
            ):
                for object in objects_to_delete:
                    self.s3.delete_object(Bucket=self.bucket, Key=object["Key"])
                return f"ok {remote_ref}\n"
            elif len(objects_to_delete) == 0:
                return f"error {remote_ref} not found\n"
            else:
                return f'error {remote_ref} "multiple bundles exists on server. Run git-s3 doctor to fix."?\n'  # noqa: B950

        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                logger.info(f"fatal: {remote_ref} not found\n")
                return f"error {remote_ref} not found\n"
            raise e

    def cmd_push(self, args: str) -> str:
        force_push = False
        local_ref, remote_ref = args.split(" ")[1].split(":")
        if not local_ref:
            return self.remove_remote_ref(remote_ref)
        if local_ref.startswith("+"):
            force_push = True
            logger.info(f"Force push {force_push}")
            local_ref = local_ref[1:]

        logger.info(f"push !{local_ref}! !{remote_ref}!")

        # `git bundle create` in a shallow repo emits a bundle with truncated history and no
        # prerequisites that `git bundle verify` still calls complete, so the breakage would only
        # surface for whoever clones it next.
        if git.is_shallow_repository():
            return f'error {remote_ref} "cannot push from a shallow clone; run git fetch --unshallow first."?\n'

        contents = self.get_bundles_for_ref(remote_ref)
        if len(contents) > 1:
            return f'error {remote_ref} "multiple bundles exists on server. Run git-s3 doctor to fix."?\n'  # noqa: B950

        temp_dir = tempfile.mkdtemp(prefix="git_remote_s3_push_")

        # Best-effort fast-fail on a non-fast-forward push before we create the bundle and take the
        # lock. This is only an optimization; the authoritative reconcile happens under the lock
        # below, off the state read AFTER acquisition.
        pre_lock_bundle = contents[0]["Key"] if len(contents) == 1 else None
        sha: str | None = None
        lock_key: str | None = None
        result: str | None = None
        lock_release_error: str | None = None
        try:
            sha = git.rev_parse(local_ref)
            if pre_lock_bundle:
                remote_sha = pre_lock_bundle.split("/")[-1].split(".")[0]
                if not git.is_ancestor(remote_sha, sha):
                    error = self.non_fast_forward_error(
                        remote_ref=remote_ref,
                        local_ref=local_ref,
                        remote_sha=remote_sha,
                        force_push=force_push,
                    )
                    if error:
                        return error

            # Create the bundle before acquiring the lock (local operation)
            temp_file = git.bundle(
                folder=temp_dir,
                sha=sha,
                ref=local_ref,
                progress=self.progress,
                quiet=self.verbosity == 0,
            )

            # Acquire per-ref lock to avoid concurrent writes
            lock_key = self.acquire_lock(remote_ref)
            if not lock_key:
                # Provide clear guidance to the user; include lock path and TTL
                lock_path = f"{self.prefix}/{remote_ref}/LOCK#.lock"
                return (
                    f"error {remote_ref} "
                    f'"failed to acquire ref lock at {lock_path}. '
                    f"Another client may be pushing. If this persists beyond {self.lock_ttl_seconds}s, "
                    f"run git-remote-s3 doctor --lock-ttl {self.lock_ttl_seconds} to inspect and "
                    f'optionally clear stale locks."?\n'
                )

            # Authoritative view: reconcile against the bundles that exist NOW, under the lock, not
            # the pre-lock snapshot. A concurrent push whose pre-lock view was empty must still see
            # (and reconcile against) a bundle another pusher committed before we acquired the lock,
            # otherwise both writers would leave a bundle and the ref would end up with two.
            current_contents = self.get_bundles_for_ref(remote_ref)
            if len(current_contents) > 1:
                return (
                    f'error {remote_ref} "multiple bundles exists for the same ref on server. '
                    f"Run git-s3 doctor to fix. Upgrade git-remote-s3 to latest version to "
                    f'prevent this in the future."\n'
                )

            remote_to_remove = current_contents[0]["Key"] if len(current_contents) == 1 else None
            if remote_to_remove is not None:
                remote_sha = remote_to_remove.split("/")[-1].split(".")[0]
                if not git.is_ancestor(remote_sha, sha):
                    error = self.non_fast_forward_error(
                        remote_ref=remote_ref,
                        local_ref=local_ref,
                        remote_sha=remote_sha,
                        force_push=force_push,
                    )
                    if error:
                        return error

            with self.transfer_progress(
                action="Uploading",
                label=remote_ref,
                total_bytes=os.path.getsize(temp_file),
            ) as progress:
                self.s3.upload_file(
                    Filename=temp_file,
                    Bucket=self.bucket,
                    Key=f"{self.prefix}/{remote_ref}/{sha}.bundle",
                    Config=_TRANSFER_CONFIG,
                    Callback=progress,
                )

            # init_remote_head (a HeadObject + maybe PutObject) and removing the stale bundle
            # are independent of each other, so run them concurrently. Both futures are waited
            # on before either result is consulted, so a delete failure is never silently
            # dropped just because the HEAD-init result was checked first; checking
            # head_future first preserves the same error-priority the sequential code had.
            if remote_to_remove:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    head_future = executor.submit(self.init_remote_head, remote_ref)
                    delete_future = executor.submit(self.s3.delete_object, Bucket=self.bucket, Key=remote_to_remove)
                    concurrent.futures.wait([head_future, delete_future])
                    head_future.result()
                    delete_future.result()
            else:
                self.init_remote_head(remote_ref)
            logger.info(f"pushed {temp_file} to {remote_ref}")

            if self.uri_scheme == UriScheme.S3_ZIP:
                # Create and push a zip archive next to the bundle file
                # Example use-case: Repo on S3 as Source for AWS CodePipeline
                commit_msg = git.get_last_commit_message()
                temp_file_archive = git.archive(folder=temp_dir, ref=local_ref)
                with self.transfer_progress(
                    action="Uploading",
                    label=f"{remote_ref} (zip)",
                    total_bytes=os.path.getsize(temp_file_archive),
                ) as progress:
                    self.s3.upload_file(
                        Filename=temp_file_archive,
                        Bucket=self.bucket,
                        Key=f"{self.prefix}/{remote_ref}/repo.zip",
                        ExtraArgs={
                            "Metadata": {"codepipeline-artifact-revision-summary": commit_msg},
                            "ContentDisposition": f"attachment; filename=repo-{sha[:8]}.zip",
                        },
                        Config=_TRANSFER_CONFIG,
                        Callback=progress,
                    )
                logger.info(
                    f"pushed {temp_file_archive} to {self.prefix}/{remote_ref}/repo.zip with message {commit_msg}"
                )

            result = f"ok {remote_ref}\n"
        except git.GitError:
            logger.info(f"fatal: {local_ref} not found\n")
            return f'error {remote_ref} "{local_ref} not found"?\n'
        except boto3.exceptions.S3UploadFailedError as e:
            logger.info(f"fatal: {e}\n")
            return f'error {remote_ref} "{e}"?\n'
        except botocore.exceptions.ClientError as e:
            logger.info(f"fatal: {e}\n")
            return f'error {remote_ref} "{e}"?\n'
        finally:
            if lock_key:
                try:
                    self.release_lock(remote_ref, lock_key)
                except Exception as e:
                    logger.info(f"failed to release lock {lock_key} for {remote_ref}: {e}")
                    lock_release_error = (
                        f'error {remote_ref} "failed to release lock. You may need to '
                        f"manually remove the lock {lock_key} from the server or use "
                        f'git-s3 doctor to fix."?\n'
                    )
            shutil.rmtree(temp_dir, ignore_errors=True)

        return lock_release_error if lock_release_error else result

    def init_remote_head(self, ref: str) -> None:
        """Initialise the remote HEAD reference if it does not exist

        Args:
            ref (str): The ref to which the remote HEAD should point to
        """

        try:
            self.s3.head_object(Bucket=self.bucket, Key=f"{self.prefix}/HEAD")
        except ClientError:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=f"{self.prefix}/HEAD",
                Body=ref,
            )

    def get_bundles_for_ref(self, remote_ref: str) -> list[dict]:
        """Lists all the bundles for a given ref on the remote

        Args:
            remote_ref (str): the remote ref

        Returns:
            list[dict]: the list of bundles objects
        """

        # We are not implementing pagination since there can be few objects (bundles)
        # under a single Prefix
        return [
            c
            for c in self.s3.list_objects_v2(Bucket=self.bucket, Prefix=f"{self.prefix}/{remote_ref}/").get(
                "Contents", []
            )
            if "PROTECTED#" not in c["Key"]
            and ".zip" not in c["Key"]
            and "/LOCKS/" not in c["Key"]
            and not c["Key"].endswith(".lock")
        ]

    def is_protected(self, remote_ref):
        # cmd_push consults this on the pre-lock fast-fail and again under the lock; protection is
        # set out of band by an admin, so one ListObjectsV2 per ref per process is enough.
        if remote_ref not in self._protected_cache:
            self._protected_cache[remote_ref] = self.s3.list_objects_v2(
                Bucket=self.bucket, Prefix=f"{self.prefix}/{remote_ref}/PROTECTED#"
            ).get("Contents", [])
        return self._protected_cache[remote_ref]

    def non_fast_forward_error(
        self, *, remote_ref: str, local_ref: str, remote_sha: str, force_push: bool
    ) -> str | None:
        """Authorises replacing a remote sha that is not an ancestor of what is being pushed.

        Callers must only reach here for a genuinely non-fast-forward update, since the protection
        check costs an S3 round trip.

        Returns:
            str | None: the error line to send back to git, or None when the update may proceed
        """
        if not force_push:
            leased_sha = self.cas_refs.get(remote_ref)
            if leased_sha is None:
                return f'error {remote_ref} "remote ref is not ancestor of {local_ref}."?\n'
            if leased_sha != remote_sha:
                return (
                    f'error {remote_ref} "stale info: remote ref is at {remote_sha}, not the '
                    f'{leased_sha} it was leased against. Fetch first."?\n'
                )

        # A lease-approved update replaces history just as a `+` push does, so both answer to the
        # protected marker.
        if self.is_protected(remote_ref):
            return f'error {remote_ref} "remote ref is protected."?\n'
        return None

    def acquire_lock(self, remote_ref: str) -> str | None:
        """Acquire a per-ref lock using S3 conditional writes.

        Client attempts to create a single lock object under <prefix>/<ref>/ using
        S3's HTTP `If-None-Match` conditional header so that only one client can write the
        lock in case of acquisition races.
        If unable to acquire the lock, check for staleness of the lock and delete it if it is stale.
        Clients that lose the race will get a `412 PreconditionFailed` and should retry later.

        Returns the lock key if acquired, or None otherwise.
        """

        lock_key = f"{self.prefix}/{remote_ref}/LOCK#.lock"
        try:
            # Use conditional write to create the lock only if it does not exist
            self.s3.put_object(
                Bucket=self.bucket,
                Key=lock_key,
                Body=b"",
                IfNoneMatch="*",
            )
            return lock_key
        except botocore.exceptions.ClientError as e:
            # 412 PreconditionFailed when the lock already exists
            if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412 or e.response.get("Error", {}).get(
                "Code"
            ) in [
                "PreconditionFailed",
                "412",
            ]:
                # Check if the existing lock is stale; if so, try to clear and acquire
                try:
                    head = self.s3.head_object(Bucket=self.bucket, Key=lock_key)
                    last_modified = head.get("LastModified")
                    if last_modified is not None:
                        import datetime

                        now = datetime.datetime.now(tz=last_modified.tzinfo)
                        age = (now - last_modified).total_seconds()
                        if age > self.lock_ttl_seconds:
                            # Attempt to delete stale lock and re-acquire
                            self.s3.delete_object(Bucket=self.bucket, Key=lock_key)
                            # Retry conditional put
                            self.s3.put_object(
                                Bucket=self.bucket,
                                Key=lock_key,
                                Body=b"",
                                IfNoneMatch="*",
                            )
                            return lock_key
                except botocore.exceptions.ClientError as e:
                    logger.info(f"failed to check staleness of {lock_key} for {remote_ref}: {e}")
                    raise e
            raise

    def release_lock(self, remote_ref: str, lock_key: str) -> None:
        """Release a previously acquired lock for the given ref."""
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=lock_key)
        except botocore.exceptions.ClientError as e:
            if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                logger.info(f"lock {lock_key} already released")
            else:
                raise

    def cmd_option(self, arg: str):
        parts = arg.strip().split(" ")
        option = parts[1] if len(parts) > 1 else ""
        value = " ".join(parts[2:])
        answer = "unsupported\n"

        if option == "progress":
            self.progress = value.lower() == "true"
            answer = "ok\n"
        elif option == "cas":
            # git sends `option cas <ref>:<sha>` per ref of a --force-with-lease push, after
            # `list for-push` and before the push line, and the push line carries no leading `+`.
            ref, _, expected_sha = value.rpartition(":")
            if ref and expected_sha:
                self.cas_refs[ref] = expected_sha
            answer = "ok\n"
        elif option == "verbosity":
            try:
                self.verbosity = int(value)
            except ValueError:
                self.verbosity = 1
            else:
                # Only ever raises the level, so GIT_REMOTE_S3_VERBOSE keeps winning over
                # the default verbosity git sends on every invocation.
                if self.verbosity >= 2:
                    # Set both root logger and module logger for complete verbosity
                    logging.getLogger().setLevel(logging.INFO)
                    logger.setLevel(logging.INFO)
                answer = "ok\n"

        sys.stdout.write(answer)
        sys.stdout.flush()

    def cmd_list(self, *, for_push: bool = False):
        objs = self.list_refs(bucket=self.bucket, prefix=self.prefix)
        logger.info(objs)

        if not for_push:
            try:
                head = self.get_remote_head()
                logger.info(f"HEAD=[{head}]")
                for o in objs:
                    ref = "/".join(o.split("/")[:-1])
                    if ref == head:
                        logger.info(f"@{ref} HEAD\n")
                        sys.stdout.write(f"@{ref} HEAD\n")
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchKey":
                    pass  # ignoring missing HEAD on remote

        for o in [x for x in objs if re.match(".+/.+/.+/[a-f0-9]{40}.bundle", x)]:
            elements = o.split("/")
            sha = elements[-1].split(".")[0]
            sys.stdout.write(f"{sha} {'/'.join(elements[:-1])}\n")

        sys.stdout.write("\n")
        sys.stdout.flush()

    def get_remote_head(self) -> str:
        """Gets the remote head ref

        Returns:
            str: the remote head ref
        """
        head = (
            self.s3.get_object(Bucket=self.bucket, Key=f"{self.prefix}/HEAD").get("Body").read().decode("utf-8").strip()
        )

        return head

    def cmd_capabilities(self):
        sys.stdout.write("*push\n")
        sys.stdout.write("*fetch\n")
        sys.stdout.write("option\n")
        sys.stdout.write("\n")
        sys.stdout.flush()

    def process_fetch_cmds(self, cmds):
        """Process fetch commands in parallel using a thread pool.

        Args:
            cmds (list): List of fetch commands to process
        """
        if not cmds:
            return

        logger.info(f"Processing {len(cmds)} fetch commands in parallel")

        # Two or more concurrent fetches each render their own progress meter to the shared
        # stderr, which garbles into unreadable output, so progress is only shown when there is
        # exactly one fetch in the batch.
        show_progress = len(cmds) == 1

        # Use a thread pool to process fetch commands in parallel
        with concurrent.futures.ThreadPoolExecutor() as executor:
            # Submit all fetch commands to the thread pool
            futures = [executor.submit(self.cmd_fetch, cmd, show_progress=show_progress) for cmd in cmds]

            # Wait for all fetch commands to complete
            concurrent.futures.wait(futures)

        logger.info(f"Completed processing {len(cmds)} fetch commands in parallel")

    def process_cmd(self, cmd: str):  # noqa: C901
        if cmd.startswith("fetch"):
            if self.mode != Mode.FETCH:
                self.mode = Mode.FETCH
                self.fetch_cmds = []
            self.fetch_cmds.append(cmd.strip())
            # Don't process fetch commands immediately, collect them for batch processing
        elif cmd.startswith("push"):
            if self.mode != Mode.PUSH:
                self.mode = Mode.PUSH
                self.push_cmds = []
            self.push_cmds.append(cmd.strip())
            # self.cmd_push(cmd.strip())
        elif cmd.startswith("option"):
            self.cmd_option(cmd.strip())
        elif cmd.startswith("list for-push"):
            self.cmd_list(for_push=True)
        elif cmd.startswith("list"):
            self.cmd_list()
        elif cmd.startswith("capabilities"):
            self.cmd_capabilities()
        elif cmd == "\n":
            logger.info("empty line")
            if self.mode == Mode.PUSH and self.push_cmds:
                logger.info(f"pushing {self.push_cmds}")
                push_res = [self.cmd_push(c) for c in self.push_cmds]
                for res in push_res:
                    sys.stdout.write(res)
                self.push_cmds = []
                # git sends one batch per invocation today, but the protocol permits several; a
                # stale lease or protection verdict must not leak into a later batch.
                self.cas_refs = {}
                self._protected_cache = {}
            elif self.mode == Mode.FETCH and self.fetch_cmds:
                logger.info(f"fetching {len(self.fetch_cmds)} refs in parallel")
                self.process_fetch_cmds(self.fetch_cmds)
                self.fetch_cmds = []
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            sys.stderr.write(f"fatal: invalid command '{cmd}'\n")
            sys.stderr.flush()
            sys.exit(1)


def main():
    logger.info(sys.argv)
    remote_name = sys.argv[1] if len(sys.argv) > 1 else None
    remote = sys.argv[2]
    uri_scheme, profile, bucket, prefix = parse_git_url(remote)
    if bucket is None or prefix is None:
        sys.stderr.write(f"fatal: invalid remote '{remote}'. You need to have a bucket and a prefix.\n")
        sys.exit(1)
    try:
        bucket = resolve_bucket_alias(bucket, remote_name)
    except BucketAliasError as e:
        sys.stderr.write(f"fatal: {e}\n")
        sys.stderr.flush()
        sys.exit(1)
    try:
        s3remote = S3Remote(
            uri_scheme=uri_scheme,
            profile=profile,
            bucket=bucket,
            prefix=prefix,
            remote_name=remote_name,
        )
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            logger.info(f"cmd: {line}")
            s3remote.process_cmd(line)

    except BrokenPipeError:
        logger.info("BrokenPipeError")
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(0)
    except OSError as err:
        # Broken pipe error on Windows
        # see https://stackoverflow.com/questions/23688492/oserror-errno-22-invalid-argument-in-subprocess # noqa: B950
        if err.errno == 22:
            logger.info("BrokenPipeError")
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            sys.exit(0)
        else:
            raise err
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
    except BucketNotFoundError as e:
        sys.stderr.write(f"fatal: bucket not found {e.bucket}\n")
        sys.stderr.flush()
        sys.exit(1)
    except NotAuthorizedError as e:
        sys.stderr.write(f"fatal: user not authorized to perform {e.action} on {e.bucket}\n")
        sys.stderr.flush()
        sys.exit(1)
    except Exception as e:
        logger.info(e)
        sys.stderr.write("fatal: unknown error. Run with --verbose flag to get full log\n")
        sys.stderr.flush()
        sys.exit(1)
