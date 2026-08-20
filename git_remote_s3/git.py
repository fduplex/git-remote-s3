# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0

import re
import struct
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass


class GitError(Exception):
    pass


@dataclass
class Pack:
    """A packfile built by :func:`pack_objects`, named by the checksum git gave it."""

    path: str
    checksum: str
    bytes: int
    objects: int


def has_object(sha: str) -> bool:
    """Whether the local repo holds this object.

    The remote may name a ref this clone never fetched, and a bare 40-hex string satisfies
    `rev-parse --verify` on its own, so the object is peeled to force an existence check.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--quiet", "--verify", f"{sha}^{{object}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def pack_objects(
    *, folder: str, sha: str, have: Iterable[str] = (), progress: bool = False, quiet: bool = False
) -> Pack:
    """Packs everything reachable from sha and not from the shas the remote already holds.

    Not `--thin`: a delta base outside the pack would make the pack unindexable on its own, and
    every reader of this format (the client, the materializer's dulwich reader) indexes each pack
    standalone. Shas the local repo does not hold are dropped from the exclusion set rather than
    failing the push.

    Args:
        folder: directory to write the pack into
        sha: the tip being pushed
        have: candidate exclusions, i.e. the tips the remote manifest already names
        progress: let git render its own progress meter on the inherited stderr
        quiet: suppress git's output entirely

    Returns:
        Pack: the written pack, with objects == 0 when the remote already holds everything.
    """
    revs = [sha, *(f"^{h}" for h in have if has_object(h))]
    args = ["git", "pack-objects", "--revs"]
    if quiet:
        args.append("-q")
    elif progress:
        args.append("--progress")
    args.append(f"{folder}/pack")

    # As in bundle(): git's progress meter only reaches the user when stderr is inherited, so the
    # captured text is only available when progress is off.
    result = subprocess.run(
        args,
        input="\n".join(revs).encode("utf8"),
        stdout=subprocess.PIPE,
        stderr=None if (progress and not quiet) else subprocess.PIPE,
    )
    if result.returncode != 0:
        raise GitError(result.stderr.decode("utf8") if result.stderr else f"failed to pack {sha}")

    lines = [line for line in result.stdout.decode("utf8").split("\n") if line.strip()]
    if not lines:
        raise GitError(f"git pack-objects wrote no pack for {sha}")
    checksum = lines[-1].strip()
    path = f"{folder}/pack-{checksum}.pack"
    return Pack(path=path, checksum=checksum, bytes=_file_size(path), objects=_pack_object_count(path))


def _file_size(path: str) -> int:
    with open(path, "rb") as f:
        f.seek(0, 2)
        return f.tell()


def _pack_object_count(path: str) -> int:
    """Reads the object count out of the 12-byte pack header."""
    with open(path, "rb") as f:
        signature, _version, objects = struct.unpack(">4sII", f.read(12))
    if signature != b"PACK":
        raise GitError(f"{path} is not a packfile")
    return objects


def index_pack(*, path: str, progress: bool = False) -> None:
    """Imports a fetched pack into the local object database.

    ``--stdin`` is what makes git own the pack: it writes both the .pack and its .idx under
    .git/objects/pack/. `git unpack-objects` would explode the same bytes into loose objects,
    which for a large pack is an order of magnitude more files and bytes on disk.

    Args:
        path: the downloaded packfile
        progress: let git render its own indexing meter on the inherited stderr
    """
    args = ["git", "index-pack", "--stdin"]
    if progress:
        args.append("-v")
    with open(path, "rb") as pack:
        result = subprocess.run(
            args,
            stdin=pack,
            stdout=subprocess.PIPE,
            stderr=None if progress else subprocess.PIPE,
        )
    if result.returncode != 0:
        raise GitError(result.stderr.decode("utf8") if result.stderr else f"failed to index {path}")


def has_complete_history(sha: str) -> bool:
    """Whether the whole commit and tree graph reachable from sha is present locally.

    This is the fetch's verification step: the imported-seq high-water mark is a hint, and this
    is the contradiction that makes the client pull older entries.
    """
    result = subprocess.run(
        ["git", "rev-list", "--objects", sha],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def archive(*, folder: str, ref: str) -> str:
    """Archive the content of the folder into a repo.zip file

    Args:
        folder (str): the folder to archive
        ref (str): the ref to archive

    Returns:
        str: the path to the archive file
    """

    file_path = f"{folder}/repo.zip"
    result = subprocess.run(
        ["git", "archive", "--format", "zip", "--output", file_path, ref],
        capture_output=True,
    )

    if result.returncode != 0:
        raise GitError(result.stderr.decode("utf8") if result.stderr else f"failed to archive {ref}")
    return file_path


def bundle(*, folder: str, sha: str, ref: str, progress: bool = False, quiet: bool = False) -> str:
    """Bundles the content of the folder into a sha.bundle file

    Args:
        folder (str): the folder to bundle
        sha (str): the sha of the bundle. A bundle is stored as sha.bundle
        ref (str): the ref to bundle
        progress (bool): let git render its own progress meter on the inherited stderr
        quiet (bool): suppress git's output entirely

    Returns:
        str: the path to the bundle file
    """
    file_path = f"{folder}/{sha}.bundle"
    args = ["git", "bundle", "create"]
    if quiet:
        args.append("-q")
    elif progress:
        args.append("--progress")
    args += [file_path, ref]

    # git's progress meter only reaches the user when stderr is inherited, so it cannot be
    # captured in that mode; the captured text below is therefore only available when progress
    # is off.
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=None if (progress and not quiet) else subprocess.PIPE,
    )

    if result.returncode != 0:
        raise GitError(result.stderr.decode("utf8") if result.stderr else f"failed to bundle {ref}")
    return file_path


def unbundle(*, folder: str, sha: str, ref: str, progress: bool = False):
    """Unbundles the content of the bundle referred by the sha

    Args:
        folder (str): the folder where the bundle is located
        sha (str): the sha of the bundle. A bundle is stored as sha.bundle
        ref (str): the ref to checkout after unbundling
        progress (bool): let git render its own progress meter on stderr

    Raises:
        GitError: if git could not apply the bundle
    """
    args = ["git", "bundle", "unbundle"]
    if progress:
        args.append("--progress")
    args += [f"{folder}/{sha}.bundle", ref]
    result = subprocess.run(
        args,
        stdout=sys.stderr,
    )

    if result.returncode != 0:
        raise GitError(f"failed to unbundle {sha} into {ref}")


def rev_parse(ref: str) -> str:
    """Gets the sha of a ref

    Args:
        ref (str): the ref to get the sha for

    Raises:
        Exception: if the ref is not found

    Returns:
        str: _description_
    """

    result = subprocess.run(["git", "rev-parse", ref], stdout=subprocess.PIPE)
    if result.returncode != 0:
        raise GitError(f"fatal: {ref} not found")
    sha = result.stdout.decode("utf8").strip()
    return sha


def is_ancestor(ancestor: str, descendant: str) -> bool:
    """Checks if the ancestor is an ancestor of the descendant

    Args:
        ancestor (str): the ancestor ref
        descendant (str): the descendant ref

    Returns:
        bool: true if the ancestor is an ancestor of the descendant
    """
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    return result.returncode == 0


def is_shallow_repository() -> bool:
    """Checks whether the local repository has a truncated history

    Returns:
        bool: true if the repository is shallow
    """
    result = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0 and result.stdout.decode("utf8").strip() == "true"


def get_remote_url(remote: str) -> str:
    result = subprocess.run(["git", "remote", "get-url", remote], stdout=subprocess.PIPE)
    if result.returncode != 0:
        raise GitError(f"fatal: {remote} not found")
    url = result.stdout.decode("utf8").strip()
    return url


# validate refname according to
# https://github.com/git/git/blob/406f326d271e0bacecdb00425422c5fa3f314930/refs.c#L170
def validate_ref_name(name: str) -> bool:
    return (
        re.search(
            r"(^\.)|(\.\.)|([:\?\[\\\^\~\s\*\]])|(\.lock$)|(/$)|(@\{)|([\x00-\x1f])",
            name,
        )
        is None
    )


def get_last_commit_message() -> str:
    result = subprocess.run(["git", "log", "-1", "--pretty=%h %s"], stdout=subprocess.PIPE)
    if result.returncode != 0:
        raise GitError("fatal: an error as occurred")
    message = result.stdout.decode("utf8").strip()
    return message
