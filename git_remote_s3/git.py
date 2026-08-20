# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0

import os
import re
import struct
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

_PACK_HEADER_BYTES = 12
_PACK_CHECKSUM_BYTES = 20


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
    return _pack(folder=folder, revs=revs, subject=sha, progress=progress, quiet=quiet)


def pack_all(*, folder: str, shas: Iterable[str], progress: bool = False, quiet: bool = False) -> Pack:
    """Packs everything reachable from every sha, with no exclusions: compaction's base pack.

    The union of the refs is packed once rather than once per ref, so history shared between
    branches is stored a single time.

    Args:
        folder: directory to write the pack into
        shas: every tip the manifest names
        progress: let git render its own progress meter on the inherited stderr
        quiet: suppress git's output entirely
    """
    revs = sorted(set(shas))
    if not revs:
        raise GitError("cannot pack an empty ref set")
    return _pack(folder=folder, revs=revs, subject=" ".join(revs), progress=progress, quiet=quiet)


def _pack(*, folder: str, revs: list[str], subject: str, progress: bool, quiet: bool) -> Pack:
    # `--stdout` rather than a `<folder>/pack` prefix: in prefix mode git writes its temp pack
    # beside the repository and renames it onto the prefix, which fails with EXDEV whenever the
    # repo and the scratch folder sit on different filesystems (a bind-mounted repo plus /tmp).
    # Streaming into a file we open ourselves keeps the only rename inside the destination folder.
    args = ["git", "pack-objects", "--revs", "--stdout"]
    if quiet:
        args.append("-q")
    elif progress:
        args.append("--progress")

    incoming = f"{folder}/incoming.pack"
    # git's progress meter only reaches the user when stderr is inherited, so the captured text
    # is only available when progress is off. The pack itself is on stdout.
    with open(incoming, "wb") as out:
        result = subprocess.run(
            args,
            input="\n".join(revs).encode("utf8"),
            stdout=out,
            stderr=None if (progress and not quiet) else subprocess.PIPE,
        )
    if result.returncode != 0:
        os.unlink(incoming)
        raise GitError(result.stderr.decode("utf8") if result.stderr else f"failed to pack {subject}")

    size = _file_size(incoming)
    if size < _PACK_HEADER_BYTES + _PACK_CHECKSUM_BYTES:
        os.unlink(incoming)
        raise GitError(f"git pack-objects wrote no pack for {subject}")

    checksum = _pack_checksum(incoming)
    objects = _pack_object_count(incoming)
    path = f"{folder}/pack-{checksum}.pack"
    os.rename(incoming, path)
    return Pack(path=path, checksum=checksum, bytes=size, objects=objects)


def _pack_checksum(path: str) -> str:
    """Reads the trailing checksum git names the pack by."""
    with open(path, "rb") as f:
        f.seek(-_PACK_CHECKSUM_BYTES, 2)
        return f.read(_PACK_CHECKSUM_BYTES).hex()


def _file_size(path: str) -> int:
    with open(path, "rb") as f:
        f.seek(0, 2)
        return f.tell()


def _pack_object_count(path: str) -> int:
    """Reads the object count out of the 12-byte pack header."""
    with open(path, "rb") as f:
        signature, _version, objects = struct.unpack(">4sII", f.read(_PACK_HEADER_BYTES))
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


def init_bare(path: str) -> None:
    """Creates an empty bare repository, for checking a pack reconstructs its refs on its own."""
    result = subprocess.run(
        ["git", "init", "-q", "--bare", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise GitError(result.stderr.decode("utf8") if result.stderr else f"failed to init {path}")


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
