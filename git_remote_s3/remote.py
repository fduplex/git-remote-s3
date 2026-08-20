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
import shutil
import subprocess
import tempfile
import time
import os
import concurrent.futures
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, NoReturn

import botocore.exceptions
from git_remote_s3 import git
from . import gitwal
from .enums import UriScheme
from .common import (
    parse_git_url,
    resolve_bucket_alias,
    register_s3_access_grants,
    register_s3_access_grants_readwrite,
    resolve_bucket_region,
    synthetic_lfs_url,
    BucketAliasError,
    TRANSFER_CONFIG,
)
from .walstore import CasExhaustedError, Reject, WalStore
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

# Bucket region, cached per remote in the repo's local git config so the HeadBucket probe is only
# paid once per clone. Documented in the README, including how to drop it if the bucket moves.
_REGION_CONFIG_KEY = "remote.{remote_name}.s3region"

# Highest manifest entry seq imported into this clone, cached in the same repo-local git config.
# A hint only: every fetch verifies what it imported and pulls older entries when the tip does not
# resolve, so a stale, corrupt or post-compaction value costs a round trip, never correctness.
_SEQ_CONFIG_KEY = "remote.{remote_name}.gitwal-seq"

# S3 answers a request aimed at the wrong region with one of these. A region-agnostic client is
# redirected; a client pinned to the wrong region instead fails to verify the SigV4 scope and
# answers AuthorizationHeaderMalformed, which is what the cached region produces.
_REGION_REDIRECT_CODES = ("PermanentRedirect", "301", "AuthorizationHeaderMalformed")

_KB = 1024
_MB = 1024**2

# Rendering on every boto3 chunk (256 KiB) from up to 8 transfer threads costs far more writes
# than a terminal can show; a tenth of a second still reads as continuous motion.
_PROGRESS_MIN_INTERVAL_S = 0.1


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
        self._last_render = 0.0

    def __call__(self, bytes_amount: int) -> None:
        with self._lock:
            self._seen_so_far += bytes_amount
            now = time.monotonic()
            if self._rendered and now - self._last_render < _PROGRESS_MIN_INTERVAL_S:
                return
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
            self._last_render = now

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


class BucketNotFoundError(Exception):
    def __init__(self, bucket: str):
        self.bucket = bucket
        super().__init__(f"Bucket {bucket} not found.")


class NotAuthorizedError(Exception):
    def __init__(self, action: str, bucket: str):
        self.bucket = bucket
        self.action = action
        super().__init__(f"Not authorized to perform {action} on the S3 bucket {bucket}.")


class FetchIncompleteError(Exception):
    """Every entry in the log was imported and the wanted shas still do not resolve."""

    def __init__(self, shas: list[str]):
        self.shas = shas
        super().__init__(
            f"the remote's packs do not contain the whole history of {', '.join(shas)}. "
            "Run git-s3 doctor against the remote."
        )


class Mode:
    FETCH = "fetch"
    PUSH = "push"


_S3_ZIP_DEPRECATION = "git-remote-s3: s3+zip:// is deprecated and now behaves as s3://; no repo.zip archive is written."


@dataclass
class _PushRef:
    """One `push <src>:<dst>` line, carried from parsing through the batch's single CAS."""

    remote_ref: str
    local_ref: str | None
    force: bool
    sha: str | None = None
    entry: gitwal.Entry | None = None
    # The mutator's verdict on its last attempt; set on every attempt because a re-validation
    # against fresher refs can change it.
    rejection: str | None = None
    # The line sent back to git. Empty until this ref is decided, which is what marks a push as
    # still live after the pre-CAS phase.
    result: str = ""

    @property
    def delete(self) -> bool:
        return self.local_ref is None


def maybe_install_lfs_agent(remote_name: str) -> None:
    """Wires the git-lfs-s3 transfer agent into local git config for an s3:// remote.

    Writes three keys, all with --replace-all so a repeat run is a no-op and any duplicates left by
    hand-editing or by older builds collapse to a single value:

    - lfs.customtransfer.git-lfs-s3.path = git-lfs-s3
    - remote.<name>.lfsurl = <synthetic endpoint>
    - lfs.<that-url>.standalonetransferagent = git-lfs-s3

    The agent binding is the URL-scoped form, matching `git-lfs-s3 install --remote`: an unscoped
    lfs.standalonetransferagent hijacks every remote in the repo, including GitHub ones. Legacy
    unscoped values written by older builds are tolerated (the scoped key wins for a matching
    endpoint) but never created, and never rewritten here.

    remote.<name>.lfsurl exists because git-lfs parses an `s3://` remote as an SSH-style URL with
    hostname "s3" and probes it with `ssh s3 git-lfs-transfer ...` on every push, blocking ~10s on
    the name resolution failure before falling back. A standalone transfer agent does not suppress
    that probe; an HTTPS-shaped endpoint does. The URL is a never-contacted match key only.

    Nothing is written when GIT_REMOTE_S3_AUTO_INSTALL_LFS is 0/false/no, when remote.<name>.lfsurl
    is already set (fully configured, or user-managed either way), when lfs.standalonetransferagent
    names an agent other than ours, or when the remote has no resolvable s3:// URL to scope to.
    """
    if os.environ.get("GIT_REMOTE_S3_AUTO_INSTALL_LFS", "1").lower() in (
        "0",
        "false",
        "no",
    ):
        return

    existing_agent = _git_config_get("lfs.standalonetransferagent")
    if existing_agent is not None and existing_agent != "git-lfs-s3":
        return
    if _git_config_get(f"remote.{remote_name}.lfsurl"):
        return

    lfs_url = _remote_lfs_url(remote_name)
    if lfs_url is None:
        return

    _git_config_run("--replace-all", "lfs.customtransfer.git-lfs-s3.path", "git-lfs-s3")
    _git_config_run("--replace-all", f"remote.{remote_name}.lfsurl", lfs_url)
    _git_config_run("--replace-all", f"lfs.{lfs_url}.standalonetransferagent", "git-lfs-s3")


def _remote_lfs_url(remote_name: str) -> str | None:
    """Renders the synthetic LFS endpoint for a configured s3:// remote, or None.

    The URL is built from the remote's bucket component verbatim so a DNS bucket alias stays an
    alias, matching what `git-lfs-s3 install --remote` writes.
    """
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", remote_name],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    _, _, bucket, prefix = parse_git_url(url)
    if bucket is None or prefix is None:
        return None
    return synthetic_lfs_url(bucket, prefix)


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


def _git_config_run(*args: str) -> None:
    """Applies a best-effort `git config` mutation, e.g. ("--local", key, value).

    Run as a subprocess rather than by editing a config file so gitdir redirection is handled by
    git itself: in a submodule the local config lives under .git/modules/<name>/config, which git
    resolves from the environment the helper inherited.
    """
    with contextlib.suppress(FileNotFoundError):
        subprocess.run(
            ["git", "config", *args],
            check=False,
            stderr=subprocess.DEVNULL,
        )


def _single_line(message: str) -> str:
    """Flattens git's stderr so it can ride inside a single `error <ref> "..."` protocol line."""
    return " ".join(message.replace('"', "'").split()) or "unknown git error"


class S3Remote:
    def __init__(self, uri_scheme, profile, bucket, prefix, *, remote_name=None, remote_url=None):
        """Prepares the helper without touching AWS.

        git wants `capabilities` answered — and the `option` lines that follow it acknowledged —
        before it sends the first command that needs the remote, so nothing here may resolve
        credentials, do DNS or call S3; all of that is deferred to _ensure_s3.
        """
        self.uri_scheme = uri_scheme
        if uri_scheme == UriScheme.S3_ZIP:
            sys.stderr.write(f"{_S3_ZIP_DEPRECATION}\n")
        self.profile = profile
        self.bucket = bucket
        self.prefix = prefix
        self.remote_name = remote_name
        # boto3 clients are dynamically typed; there are no first-party stubs.
        self._s3: Any = None
        self._setup_lock = Lock()
        self._region_from_config = False
        self._region: str | None = None
        # git passes the URL as argv[1] when pushing to a raw URL instead of to a configured
        # remote; only a real remote name has a config section to cache the bucket region under.
        named_remote = remote_name is not None and remote_name != remote_url and "://" not in remote_name
        self._region_config_key = _REGION_CONFIG_KEY.format(remote_name=remote_name) if named_remote else None
        self._seq_config_key = _SEQ_CONFIG_KEY.format(remote_name=remote_name) if named_remote else None
        self._is_shallow: bool | None = None
        self.mode = None
        self.progress = False
        self.verbosity = 1
        self.push_cmds = []
        self.fetch_cmds = []  # Store fetch commands for batch processing
        # <remote ref> -> sha git leased the ref against, from `option cas` (--force-with-lease).
        self.cas_refs: dict[str, str] = {}
        self._wal: WalStore | None = None
        self._caller_arn: str | None = None
        self._caller_arn_probed = False

    @property
    def s3(self) -> Any:
        """The S3 client, built on first use along with the rest of the AWS setup."""
        self._ensure_s3()
        return self._s3

    @property
    def wal(self) -> WalStore:
        """The manifest's CAS store, built once the alias-resolved bucket name is known."""
        self._ensure_s3()
        if self._wal is None:
            self._wal = WalStore(
                self._s3,
                bucket=self.bucket,
                prefix=self.prefix,
                writer=self._build_manifest_writer_client,
            )
        return self._wal

    def _build_manifest_writer_client(self) -> Any:
        """Builds the READWRITE-vending client the manifest's conditional PUTs go through."""
        return register_s3_access_grants_readwrite(
            self.session.client("s3", **({"region_name": self._region} if self._region else {})),
            self.session,
        )

    def _ensure_s3(self) -> None:
        """Pays the one-off AWS setup cost: alias resolution, session, region and client.

        Idempotent, and locked because a multi-ref fetch reaches S3 from several threads at once.
        """
        if self._s3 is not None:
            return
        with self._setup_lock:
            if self._s3 is not None:
                return
            with self._connecting_notice():
                self.bucket = resolve_bucket_alias(self.bucket, self.remote_name)
                self.session = boto3.Session(profile_name=self.profile) if self.profile else boto3.Session()
                self._s3 = self._build_s3_client()
                # remote_name is only set when invoked via the remote-helper protocol;
                # gating on it keeps tests from writing to the project's own git config.
                if self.remote_name is not None:
                    maybe_install_lfs_agent(self.remote_name)

    @contextlib.contextmanager
    def _connecting_notice(self):
        """Shows one status line on the terminal while the fixed setup cost is paid.

        stdout carries the remote-helper protocol, so this can only go to stderr, which git leaves
        attached to the user's terminal. `option verbosity` may not have arrived yet (git sends no
        options at all for an ls-remote), so a tty is the primary gate.
        """
        notice = f"git-remote-s3: connecting to {self.bucket}..."
        show = sys.stderr.isatty() and self.verbosity != 0
        if show:
            sys.stderr.write(f"\r{notice}")
            sys.stderr.flush()
        try:
            yield
        finally:
            if show:
                sys.stderr.write(f"\r{' ' * len(notice)}\r")
                sys.stderr.flush()

    def _build_s3_client(self) -> Any:
        """Builds the S3 client pinned to the bucket's region, detecting it only when not cached."""
        region = _git_config_get(self._region_config_key) if self._region_config_key else None
        self._region_from_config = region is not None
        if region is None:
            region = resolve_bucket_region(self.session, self.bucket)
            if region and self._region_config_key:
                _git_config_run("--local", self._region_config_key, region)
        self._region = region
        return register_s3_access_grants(
            self.session.client("s3", **({"region_name": region} if region else {})),
            self.session,
        )

    def _retry_without_cached_region(self, error: ClientError) -> bool:
        """Drops a stale cached region and rebuilds the client, so the caller can retry once.

        Only a bucket that moved region can make the cache wrong, and only S3 can tell us so.

        list_refs is deliberately the only caller: git always sends `list` or `list for-push`
        before anything else in a helper session, so the stale cache is corrected there before any
        other S3 call can be issued against it, and no other call site needs its own retry.
        """
        if error.response["Error"]["Code"] not in _REGION_REDIRECT_CODES or not self._region_from_config:
            return False
        if self._region_config_key:
            _git_config_run("--local", "--unset", self._region_config_key)
        self._s3 = self._build_s3_client()
        # The store holds the client it was handed, so it has to be rebuilt around the new one.
        self._wal = None
        return True

    @staticmethod
    def _raise_list_error(error: ClientError, bucket: str) -> NoReturn:
        code = error.response["Error"]["Code"]
        if code == "NoSuchBucket":
            raise BucketNotFoundError(bucket) from error
        if code == "AccessDenied":
            raise NotAuthorizedError("GetObject", bucket) from error
        raise error

    def list_refs(self) -> gitwal.Manifest | None:
        """Reads the manifest, and is where a bad bucket or missing permission is reported.

        git sends `list` (or `list for-push`) before any fetch or push, so this one GET is the
        first S3 call of every invocation. It carries the friendly error mapping that a dedicated
        construction-time probe used to do at the cost of an extra round trip, and the one-shot
        retry that corrects a stale cached bucket region.

        Returns:
            The manifest, or None for a repo that does not exist yet.
        """
        for attempt in range(2):
            try:
                manifest, _etag = self.wal.load()
                return manifest
            except ClientError as e:
                if attempt == 0 and self._retry_without_cached_region(e):
                    continue
                self._raise_list_error(e, self.bucket)
        return None

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

    def cmd_fetch(self, args: str) -> None:
        """Imports one `fetch <sha> <ref>` line. git batches these; process_fetch_cmds is the path."""
        self.process_fetch_cmds([args])

    def cmd_push(self, args: str) -> str:
        return self.process_push_cmds([args])[0]

    def process_push_cmds(self, cmds: list[str]) -> list[str]:
        """Runs a whole push batch: build and upload every pack, then commit every ref in one CAS.

        Packs are immutable and content-addressed, so uploading them ahead of the commit point can
        never conflict with anything: a pack whose CAS never lands is an orphan, which the format
        defines as inert. The single manifest PUT is the commit point, which is what finally makes
        a multi-ref push atomic.
        """
        self._ensure_s3()
        pushes = [self._parse_push_cmd(cmd) for cmd in cmds]
        manifest, _etag = self.wal.load()
        snapshot = manifest if manifest is not None else gitwal.Manifest()

        temp_dir = tempfile.mkdtemp(prefix="git_remote_s3_push_")
        try:
            for push in pushes:
                self._prepare_push(push, snapshot, temp_dir)
            self._commit_pushes([p for p in pushes if not p.result])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        return [p.result for p in pushes]

    @staticmethod
    def _parse_push_cmd(cmd: str) -> _PushRef:
        local_ref, remote_ref = cmd.split(" ")[1].split(":")
        force = local_ref.startswith("+")
        if force:
            local_ref = local_ref[1:]
        logger.info(f"push !{local_ref}! !{remote_ref}!")
        return _PushRef(remote_ref=remote_ref, local_ref=local_ref or None, force=force)

    def _prepare_push(self, push: _PushRef, manifest: gitwal.Manifest, temp_dir: str) -> None:
        """Resolves the local sha and uploads this ref's pack. Nothing here is a commit point."""
        local_ref = push.local_ref
        if local_ref is None:
            return

        # `git pack-objects` in a shallow repo emits a pack that omits history the receiver may not
        # have, and the breakage only surfaces for whoever clones it next.
        if self._is_shallow_repository():
            push.result = (
                f'error {push.remote_ref} "cannot push from a shallow clone; run git fetch --unshallow first."?\n'
            )
            return

        try:
            push.sha = git.rev_parse(local_ref)
            # Fast-fail against the manifest we have already read, so a doomed push does not build
            # a pack. The authoritative verdict is the identical check inside the CAS mutator.
            rejection = self._push_rejection(manifest, push)
            if rejection:
                push.result = rejection
                return
            pack = git.pack_objects(
                folder=temp_dir,
                sha=push.sha,
                have=sorted(set(manifest.refs.values())),
                progress=self.progress,
                quiet=self.verbosity == 0,
            )
            if pack.objects == 0:
                # Everything this ref names is already on the remote (a tag on an existing commit,
                # or a ref re-pointed at a sha some other ref already carries): refs-only CAS.
                logger.info(f"nothing new to pack for {push.remote_ref}")
                return
            push.entry = self._upload_pack(push, pack)
        except git.GitError as e:
            # A pack git refuses to build must fail this ref only; letting it escape would kill the
            # helper and abandon the rest of the batch.
            logger.info(f"fatal: {e}\n")
            push.result = f'error {push.remote_ref} "{_single_line(str(e))}"?\n'
        except boto3.exceptions.S3UploadFailedError as e:
            logger.info(f"fatal: {e}\n")
            push.result = f'error {push.remote_ref} "{e}"?\n'
        except botocore.exceptions.ClientError as e:
            logger.info(f"fatal: {e}\n")
            push.result = f'error {push.remote_ref} "{e}"?\n'

    def _upload_pack(self, push: _PushRef, pack: git.Pack) -> gitwal.Entry:
        """Writes the pack to its content-addressed key and describes it as a log entry.

        The PUT is unconditional on purpose: the key is the pack's own checksum, so a retry writes
        identical bytes to an identical key and can never clobber anything.
        """
        relative_key = f"{gitwal.PACKS_PREFIX}/{pack.checksum}.pack"
        with self.transfer_progress(action="Uploading", label=push.remote_ref, total_bytes=pack.bytes) as progress:
            self.s3.upload_file(
                Filename=pack.path,
                Bucket=self.bucket,
                Key=f"{self.prefix}/{relative_key}",
                Config=TRANSFER_CONFIG,
                Callback=progress,
            )
        logger.info(f"pushed {pack.path} to {relative_key}")
        return gitwal.Entry(
            kind=gitwal.KIND_INCREMENTAL,
            pack=relative_key,
            bytes=pack.bytes,
            objects=pack.objects,
            tips={push.remote_ref: push.sha} if push.sha else {},
            by=self._push_identity(),
            at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def _commit_pushes(self, pushes: list[_PushRef]) -> None:
        """Commits every surviving ref of the batch in one compare-and-swap.

        The mutator re-runs all three validations on every attempt, so a 412 can never commit a
        decision taken against refs that have since moved. A ref the mutator rejects is dropped
        from that attempt and reported to git on its own `error <ref>` line, exactly as it was
        before the CAS existed; the batch as a whole still commits in a single PUT.
        """
        if not pushes:
            return

        def mutate(manifest: gitwal.Manifest) -> gitwal.Manifest:
            accepted = []
            for push in pushes:
                push.rejection = self._push_rejection(manifest, push)
                if push.rejection is None:
                    accepted.append(push)
            if not accepted:
                raise Reject(f"no ref of this push passed validation against gitwal.json seq {manifest.seq}")
            return self._apply_pushes(manifest, accepted)

        failure: str | None = None
        try:
            self.wal.update(mutate)
        except Reject:
            # Every ref carries the mutator's verdict from the attempt that rejected it.
            pass
        except (CasExhaustedError, gitwal.ManifestError, botocore.exceptions.ClientError) as e:
            logger.info(f"fatal: {e}\n")
            failure = _single_line(str(e))

        for push in pushes:
            if failure is not None:
                push.result = f'error {push.remote_ref} "{failure}"?\n'
            else:
                push.result = push.rejection or f"ok {push.remote_ref}\n"

    def _apply_pushes(self, manifest: gitwal.Manifest, accepted: list[_PushRef]) -> gitwal.Manifest:
        """Folds every accepted ref update, entry and deletion of the batch into one manifest."""
        refs = {p.remote_ref: p.sha for p in accepted if not p.delete and p.sha}
        # init_remote_head's rule, kept: the first ref pushed to a repo with no default branch
        # names it, and a repo that already has one is never re-pointed by a push.
        head = next(iter(refs), None) if manifest.head is None else None

        # Two refs pushed at the same tip produce byte-identical packs, and a pack may only be
        # named by one entry; the second ref still commits, it just names no new data.
        claimed = {e.pack for e in manifest.entries}
        entries = []
        for push in accepted:
            if push.entry is not None and push.entry.pack not in claimed:
                claimed.add(push.entry.pack)
                entries.append(push.entry)

        # A delete-only batch has neither refs nor entries; applying a push there would be an
        # empty transition that bumps seq for nothing.
        out = manifest
        if refs or entries:
            out = gitwal.apply_push(manifest, refs=refs, entry=entries[0] if entries else None, head=head)
        for entry in entries[1:]:
            out = gitwal.apply_push(out, refs={}, entry=entry)
        for push in accepted:
            if push.delete:
                out = gitwal.apply_delete(out, ref=push.remote_ref)
        return out

    def _push_rejection(self, manifest: gitwal.Manifest, push: _PushRef) -> str | None:
        """The lease, fast-forward and protection verdicts for one ref against one manifest.

        Run pre-flight against the manifest read at the start of the batch, and again inside the
        CAS mutator against whatever state the commit will actually replace.
        """
        remote_sha = manifest.refs.get(push.remote_ref)
        lease_error = self.lease_violation_error(remote_ref=push.remote_ref, remote_sha=remote_sha)
        if lease_error:
            return lease_error
        if push.delete:
            return None if remote_sha else f"error {push.remote_ref} not found\n"
        if remote_sha is not None and push.sha is not None and not git.is_ancestor(remote_sha, push.sha):
            return self.non_fast_forward_error(
                remote_ref=push.remote_ref,
                local_ref=push.local_ref or "",
                remote_sha=remote_sha,
                force_push=push.force,
                protected=manifest.is_protected(push.remote_ref),
            )
        return None

    def _push_identity(self) -> str | None:
        """The caller's STS ARN, for entry provenance. Best effort: never fails a push."""
        if not self._caller_arn_probed:
            self._caller_arn_probed = True
            try:
                self._caller_arn = self.session.client("sts").get_caller_identity()["Arn"]
            except Exception as x:
                logger.info(f"could not resolve the caller identity for the manifest entry: {x}")
        return self._caller_arn

    def _is_shallow_repository(self) -> bool:
        """Caches git's answer, which cannot change while the helper process is alive."""
        if self._is_shallow is None:
            self._is_shallow = git.is_shallow_repository()
        return self._is_shallow

    def lease_violation_error(self, *, remote_ref: str, remote_sha: str | None) -> str | None:
        """Enforces a --force-with-lease claim against the remote state we are about to replace.

        git checks the lease against what `list for-push` advertised, but the ref can move between
        that advertisement and the push, so the comparison is redone here. It is deliberately
        independent of the fast-forward check: a ref that moved to a new tip our push still
        fast-forwards from would otherwise slip past the lease entirely.

        Args:
            remote_sha (str | None): the sha the remote holds now, or None when the ref is absent

        Returns:
            str | None: the error line to send back to git, or None when the lease holds
        """
        if remote_ref not in self.cas_refs:
            return None
        leased_sha = self.cas_refs[remote_ref]
        actual = remote_sha or ""
        if actual == leased_sha:
            return None
        return (
            f'error {remote_ref} "stale info: remote ref is at {actual or "absent"}, not the '
            f'{leased_sha or "absent"} it was leased against. Fetch first."?\n'
        )

    def non_fast_forward_error(
        self, *, remote_ref: str, local_ref: str, remote_sha: str, force_push: bool, protected: bool
    ) -> str | None:
        """Authorises replacing a remote sha that is not an ancestor of what is being pushed.

        Callers must only reach here for a genuinely non-fast-forward update.

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
        # manifest's protected list.
        if protected:
            return f'error {remote_ref} "remote ref is protected."?\n'
        return None

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
            # A lease that the ref must NOT exist arrives as an all-zero sha (verified against git
            # 2.47, which sends 40 zeros; sha256 repos send 64). An empty value is accepted for the
            # same meaning, and both normalise to "".
            ref, separator, expected_sha = value.rpartition(":")
            if ref and separator:
                self.cas_refs[ref] = "" if not expected_sha.strip("0") else expected_sha
            answer = "ok\n"
        elif option == "verbosity":
            try:
                self.verbosity = int(value)
            except ValueError:
                # An unparseable level is answered `unsupported`; clobbering the level git already
                # set would be a side effect of rejecting it.
                pass
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
        """Advertises the manifest's refs, and its head as the symref, in one GET.

        A repo with no manifest lists nothing, exactly as an empty ref listing did: that is what
        git reads as "not created yet" on the first push.
        """
        # Before reading self.bucket: the alias-resolved name is only known once setup has run.
        self._ensure_s3()
        manifest = self.list_refs()
        logger.info(f"list: {manifest.refs if manifest else 'no manifest'}")

        if manifest is not None:
            head = manifest.head
            if not for_push and head is not None and head in manifest.refs:
                sys.stdout.write(f"@{head} HEAD\n")
            for ref in sorted(manifest.refs):
                sys.stdout.write(f"{manifest.refs[ref]} {ref}\n")

        sys.stdout.write("\n")
        sys.stdout.flush()

    def cmd_capabilities(self):
        sys.stdout.write("*push\n")
        sys.stdout.write("*fetch\n")
        sys.stdout.write("option\n")
        sys.stdout.write("\n")
        sys.stdout.flush()

    def process_fetch_cmds(self, cmds: list[str]) -> None:
        """Imports a whole fetch batch: one manifest read, one import over the union of the shas.

        The refs git asks for are satisfied by the same entry log, so there is nothing to
        parallelise per ref; the parallelism that matters is over the pack downloads, and one
        batch means one progress meter rather than N interleaving on the same stderr.
        """
        if not cmds:
            return
        self._ensure_s3()
        wanted = list(dict.fromkeys(cmd.split(" ")[1] for cmd in cmds))
        logger.info(f"fetching {len(wanted)} shas from the manifest")

        manifest, _etag = self.wal.load()
        if manifest is None:
            # Nothing has ever been pushed here; git asked for shas the remote cannot hold.
            return
        self._import_entries_for(manifest, wanted)

    def _import_entries_for(self, manifest: gitwal.Manifest, wanted: list[str]) -> None:
        """Imports the entries above the high-water mark, then older ones until the shas resolve.

        The mark is never trusted. A fresh clone (no mark), a `git gc --prune` that dropped
        objects, and a compaction that renumbered the log all land in the same fallback: keep
        pulling entries newest to oldest until `git rev-list --objects` is happy. Only then is the
        mark written, so an interrupted fetch cannot record history it does not hold.
        """
        entries = sorted(manifest.entries, key=lambda e: e.seq)
        mark = self._imported_seq()
        older = [e for e in entries if e.seq <= mark]

        self._import_entries([e for e in entries if e.seq > mark])
        missing = self._unresolved(wanted)
        while missing:
            if not older:
                raise FetchIncompleteError(missing)
            entry = older.pop()
            logger.info(f"{missing[0]} does not resolve yet; falling back to entry {entry.seq}")
            self._import_entries([entry])
            missing = self._unresolved(wanted)

        if entries:
            self._record_imported_seq(entries[-1].seq)

    def _import_entries(self, entries: list[gitwal.Entry]) -> None:
        """Downloads every entry's pack in parallel, then indexes each into .git/objects/pack/."""
        if not entries:
            return
        temp_dir = tempfile.mkdtemp(prefix="git_remote_s3_fetch_")
        try:
            label = f"{len(entries)} pack{'' if len(entries) == 1 else 's'}"
            with self.transfer_progress(
                action="Downloading", label=label, total_bytes=sum(e.bytes for e in entries) or None
            ) as progress:
                paths = self._download_packs(entries, temp_dir, progress)
            for path in paths:
                git.index_pack(path=path, progress=self.progress)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _download_packs(self, entries: list[gitwal.Entry], temp_dir: str, progress: Any) -> list[str]:
        def download(entry: gitwal.Entry) -> str:
            path = os.path.join(temp_dir, os.path.basename(entry.pack))
            self.s3.download_file(
                Bucket=self.bucket,
                Key=f"{self.prefix}/{entry.pack}",
                Filename=path,
                Config=TRANSFER_CONFIG,
                Callback=progress,
            )
            logger.info(f"fetched {entry.pack}")
            return path

        try:
            with concurrent.futures.ThreadPoolExecutor() as executor:
                return list(executor.map(download, entries))
        except ClientError as e:
            if e.response["Error"]["Code"] == "AccessDenied":
                raise NotAuthorizedError("GetObject", self.bucket) from e
            raise

    @staticmethod
    def _unresolved(wanted: list[str]) -> list[str]:
        return [sha for sha in wanted if not git.has_complete_history(sha)]

    def _imported_seq(self) -> int:
        """The high-water mark from git config; 0 for a fresh clone or a URL-only invocation."""
        value = _git_config_get(self._seq_config_key) if self._seq_config_key else None
        try:
            return max(0, int(value)) if value else 0
        except ValueError:
            return 0

    def _record_imported_seq(self, seq: int) -> None:
        if self._seq_config_key:
            _git_config_run("--local", self._seq_config_key, str(seq))

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
                for res in self.process_push_cmds(self.push_cmds):
                    sys.stdout.write(res)
                self.push_cmds = []
                # git sends one batch per invocation today, but the protocol permits several; a
                # stale lease must not leak into a later batch.
                self.cas_refs = {}
            elif self.mode == Mode.FETCH and self.fetch_cmds:
                logger.info(f"fetching {len(self.fetch_cmds)} refs in one batch")
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
        s3remote = S3Remote(
            uri_scheme=uri_scheme,
            profile=profile,
            bucket=bucket,
            prefix=prefix,
            remote_name=remote_name,
            remote_url=remote,
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
    except BucketAliasError as e:
        sys.stderr.write(f"fatal: {e}\n")
        sys.stderr.flush()
        sys.exit(1)
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
    except (FetchIncompleteError, git.GitError) as e:
        sys.stderr.write(f"fatal: {e}\n")
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
