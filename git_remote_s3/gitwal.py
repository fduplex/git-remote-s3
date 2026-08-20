# SPDX-FileCopyrightText: 2026-present FullDuplex Media
#
# SPDX-License-Identifier: Apache-2.0

"""The gitwal.json manifest: model, serialization, and state transitions.

This module is pure. It performs no I/O and imports nothing that does, so the
durable S3 format can be specified and tested independently of the client that
writes it. Every transition is a function of (manifest, intent) -> manifest and
bumps ``seq`` exactly once; the caller commits the result with a single
conditional PUT.
"""

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

SUPPORTED_FORMAT = 1

MANIFEST_KEY = "gitwal.json"
PACKS_PREFIX = "packs"

KIND_BASE = "base"
KIND_INCREMENTAL = "incremental"

_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

_MANIFEST_KEYS = ("format", "seq", "head", "refs", "protected", "entries")
_ENTRY_KEYS = ("seq", "kind", "pack", "bytes", "objects", "tips", "by", "at")

_KNOWN_KINDS = (KIND_BASE, KIND_INCREMENTAL)


def is_sha(value: Any) -> bool:
    """Returns whether a value is shaped like a git object id (sha1 or sha256)."""
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


class ManifestError(Exception):
    """Base class for every error this module raises."""


class ManifestFormatError(ManifestError):
    """The bytes on the wire are not a manifest document."""


class UnsupportedFormatError(ManifestError):
    """The manifest was written by a newer client and must not be rewritten here.

    Reads still work; only writes are refused, because a rewrite by this client
    would silently drop fields it does not model.
    """

    def __init__(self, manifest_format: int):
        self.manifest_format = manifest_format
        super().__init__(
            f"gitwal.json is format {manifest_format}, but this client supports format "
            f"{SUPPORTED_FORMAT}. Reading is allowed; writing is refused so unmodelled fields "
            f"are not dropped. Upgrade to a fduplex-git-remote-s3 release that supports manifest "
            f"format {manifest_format} before pushing to this repo."
        )


@dataclass
class Finding:
    """One structured result from :func:`validate`."""

    level: str
    code: str
    message: str


@dataclass
class Entry:
    """One append to the write-ahead log: a pack and the ref tips it established."""

    seq: int = 0
    kind: str = KIND_INCREMENTAL
    pack: str = ""
    bytes: int = 0
    objects: int = 0
    tips: dict[str, str] = field(default_factory=dict)
    by: str | None = None
    at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entry":
        return cls(
            seq=data.get("seq", 0),
            kind=data.get("kind", KIND_INCREMENTAL),
            pack=data.get("pack", ""),
            bytes=data.get("bytes", 0),
            objects=data.get("objects", 0),
            tips=dict(data.get("tips") or {}),
            by=data.get("by"),
            at=data.get("at"),
            extra={k: v for k, v in data.items() if k not in _ENTRY_KEYS},
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "seq": self.seq,
            "kind": self.kind,
            "pack": self.pack,
            "bytes": self.bytes,
            "objects": self.objects,
            "tips": {ref: self.tips[ref] for ref in sorted(self.tips)},
        }
        if self.by is not None:
            out["by"] = self.by
        if self.at is not None:
            out["at"] = self.at
        for key in sorted(self.extra):
            out[key] = self.extra[key]
        return out


@dataclass
class Manifest:
    """The sole authority on a repo's refs, HEAD, protection flags and entry log."""

    format: int = SUPPORTED_FORMAT
    seq: int = 0
    head: str | None = None
    refs: dict[str, str] = field(default_factory=dict)
    protected: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def writable(self) -> bool:
        return self.format <= SUPPORTED_FORMAT

    def require_writable(self) -> None:
        """Raises :class:`UnsupportedFormatError` when this client must not rewrite the manifest."""
        if not self.writable:
            raise UnsupportedFormatError(self.format)

    def is_protected(self, ref: str) -> bool:
        return ref in self.protected

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        if not isinstance(data, dict):
            raise ManifestFormatError("gitwal.json must contain a JSON object")
        return cls(
            format=data.get("format", SUPPORTED_FORMAT),
            seq=data.get("seq", 0),
            head=data.get("head"),
            refs=dict(data.get("refs") or {}),
            protected=list(data.get("protected") or []),
            entries=[Entry.from_dict(e) for e in (data.get("entries") or [])],
            extra={k: v for k, v in data.items() if k not in _MANIFEST_KEYS},
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"format": self.format, "seq": self.seq}
        if self.head is not None:
            out["head"] = self.head
        out["refs"] = {ref: self.refs[ref] for ref in sorted(self.refs)}
        out["protected"] = sorted(self.protected)
        out["entries"] = [e.to_dict() for e in self.entries]
        for key in sorted(self.extra):
            out[key] = self.extra[key]
        return out

    def copy(self) -> "Manifest":
        return replace(
            self,
            refs=dict(self.refs),
            protected=list(self.protected),
            entries=list(self.entries),
            extra=dict(self.extra),
        )


def load(source: str | bytes | dict[str, Any]) -> Manifest:
    """Builds a Manifest from JSON text, bytes, or an already-decoded dict."""
    if isinstance(source, str | bytes):
        try:
            source = json.loads(source)
        except ValueError as x:
            raise ManifestFormatError(f"gitwal.json is not valid JSON: {x}") from None
    return Manifest.from_dict(source)


def dump(manifest: Manifest) -> str:
    """Serializes a Manifest to stable, human-readable JSON text."""
    return json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n"


def _bumped(manifest: Manifest) -> Manifest:
    manifest.require_writable()
    out = manifest.copy()
    out.seq = manifest.seq + 1
    return out


def apply_push(
    manifest: Manifest,
    *,
    refs: dict[str, str],
    entry: Entry | None = None,
    head: str | None = None,
) -> Manifest:
    """Commits a push: new ref values, and the entry whose pack carries their objects.

    ``entry`` is None for a refs-only push (an empty pack, e.g. tagging a commit the remote
    already holds); the seq still bumps. When given, the entry is stamped with the new seq.

    Args:
        refs: {full refname -> tip sha} for every ref this push updates.
        entry: the log entry to append, or None for a refs-only push.
        head: set the default branch at the same time, for a repo being created.
    """
    out = _bumped(manifest)
    out.refs.update(refs)
    if head is not None:
        out.head = head
    if entry is not None:
        out.entries = [*out.entries, replace(entry, seq=out.seq, tips=dict(entry.tips))]
    return out


def apply_delete(manifest: Manifest, *, ref: str) -> Manifest:
    """Drops a ref and its protection flag. Refs-only CAS; packs are untouched."""
    out = _bumped(manifest)
    out.refs.pop(ref, None)
    out.protected = [p for p in out.protected if p != ref]
    return out


def apply_protect(manifest: Manifest, *, ref: str) -> Manifest:
    """Marks a ref protected. Legal for a ref that does not exist yet."""
    out = _bumped(manifest)
    if ref not in out.protected:
        out.protected = [*out.protected, ref]
    return out


def apply_unprotect(manifest: Manifest, *, ref: str) -> Manifest:
    out = _bumped(manifest)
    out.protected = [p for p in out.protected if p != ref]
    return out


def apply_compaction(manifest: Manifest, *, entry: Entry) -> Manifest:
    """Replaces the whole entry log with one base entry covering every ref.

    The superseded pack objects are deleted by the caller strictly after the CAS commits;
    until then they are orphans, which the format defines as harmless.
    """
    out = _bumped(manifest)
    out.entries = [replace(entry, seq=out.seq, kind=KIND_BASE, tips=dict(entry.tips))]
    return out


def set_head(manifest: Manifest, *, head: str | None) -> Manifest:
    """Sets (or, with None, clears) the default branch."""
    out = _bumped(manifest)
    out.head = head
    return out


def superseded_packs(manifest: Manifest, compacted: Manifest) -> list[str]:
    """Returns the pack keys live before a compaction and no longer named after it."""
    kept = {e.pack for e in compacted.entries}
    return [e.pack for e in manifest.entries if e.pack not in kept]


def _validate_refs(manifest: Manifest, findings: list[Finding]) -> None:
    for ref in sorted(manifest.refs):
        sha = manifest.refs[ref]
        if not is_sha(sha):
            findings.append(Finding("error", "bad_sha", f"ref {ref} holds {sha!r}, which is not a sha"))
        if not ref.startswith("refs/"):
            findings.append(Finding("error", "bad_refname", f"ref {ref} is not a full refname"))
    if manifest.head is not None and manifest.head not in manifest.refs:
        findings.append(Finding("warning", "head_unresolved", f"head {manifest.head} names no ref in refs"))
    for ref in sorted(set(manifest.protected)):
        if manifest.protected.count(ref) > 1:
            findings.append(Finding("warning", "duplicate_protected", f"protected lists {ref} more than once"))


def _validate_entries(manifest: Manifest, findings: list[Finding]) -> None:
    packs: dict[str, int] = {}
    previous: int | None = None
    for entry in manifest.entries:
        if previous is not None and entry.seq <= previous:
            findings.append(Finding("error", "entry_seq_order", f"entry seq {entry.seq} does not follow {previous}"))
        previous = entry.seq
        if entry.pack in packs:
            findings.append(
                Finding(
                    "error",
                    "duplicate_pack",
                    f"pack {entry.pack} is named by entries {packs[entry.pack]} and {entry.seq}",
                )
            )
        else:
            packs[entry.pack] = entry.seq
        if not entry.pack.startswith(f"{PACKS_PREFIX}/") or not entry.pack.endswith(".pack"):
            findings.append(
                Finding(
                    "error",
                    "bad_pack_key",
                    f"entry {entry.seq} names pack {entry.pack!r}, not a repo-relative packs/ key",
                )
            )
        if entry.kind not in _KNOWN_KINDS:
            findings.append(Finding("warning", "unknown_kind", f"entry {entry.seq} has kind {entry.kind!r}"))
        for ref, sha in sorted(entry.tips.items()):
            if not is_sha(sha):
                findings.append(
                    Finding("error", "bad_sha", f"entry {entry.seq} tip {ref} holds {sha!r}, which is not a sha")
                )
    if manifest.entries:
        highest = max(e.seq for e in manifest.entries)
        if manifest.seq < highest:
            findings.append(
                Finding("error", "seq_monotonic", f"seq {manifest.seq} is below the highest entry seq {highest}")
            )


def validate(manifest: Manifest) -> list[Finding]:
    """Checks the format invariants and returns every finding, worst-first by nothing in particular.

    A protected ref that is absent from refs is legal: protection means "protected when it
    exists". Pack existence in S3 is not checked here; that needs I/O and belongs to doctor.
    """
    findings: list[Finding] = []
    if manifest.format > SUPPORTED_FORMAT:
        findings.append(
            Finding(
                "warning", "future_format", f"format {manifest.format} is newer than this client's {SUPPORTED_FORMAT}"
            )
        )
    if manifest.format < 1:
        findings.append(Finding("error", "bad_format", f"format {manifest.format!r} is not a valid format number"))
    if manifest.seq < 0:
        findings.append(Finding("error", "bad_seq", f"seq {manifest.seq!r} is negative"))
    _validate_refs(manifest, findings)
    _validate_entries(manifest, findings)
    return findings


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "error"]
