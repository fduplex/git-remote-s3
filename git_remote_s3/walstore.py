# SPDX-FileCopyrightText: 2026-present FullDuplex Media
#
# SPDX-License-Identifier: Apache-2.0

"""The compare-and-swap store for gitwal.json: the only code that writes the manifest.

Every write is conditional -- ``If-None-Match: *`` to create, ``If-Match: <etag>`` to update --
and every retry re-loads the manifest and re-runs the caller's mutator against the fresh state.
That re-run is the safety property: a 412 can never commit a decision made against stale refs.
"""

import logging
import random
import time
from collections.abc import Callable
from typing import Any

from . import gitwal
from .gitwal import Manifest

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 8

# Enough to let a competing push finish its PUT, short enough that a serial pusher never notices.
_BACKOFF_BASE_S = 0.15
_BACKOFF_CAP_S = 2.0

_STALE_CODES = ("PreconditionFailed", "412")
_CONFLICT_CODES = ("ConditionalRequestConflict", "409")
_ABSENT_CODES = ("NoSuchKey", "NotFound", "404")

_COMMITTED = "committed"
_STALE = "stale"
_CONFLICT = "conflict"
_ABSENT = "absent"


class WalStoreError(Exception):
    """Base class for every error this module raises."""


class Reject(WalStoreError):
    """Raised by a mutator to abort the CAS without retrying.

    A non-fast-forward, a broken lease or a protected ref is a decision about the state the
    mutator was just handed, and re-running it against newer state will not change the answer.
    Raising this stops immediately and issues no further PUTs.
    """


class CasExhaustedError(WalStoreError):
    """The manifest was contended for the whole attempt budget and never committed."""

    def __init__(self, key: str, attempts: int):
        self.key = key
        self.attempts = attempts
        super().__init__(f"{key} was updated by another client {attempts} times in a row; nothing was committed")


class WalStore:
    """Loads and conditionally rewrites a repo's gitwal.json.

    Composes keys the way the rest of the client does, from the bucket and repo prefix the remote
    was constructed with, and holds the caller's boto3 client rather than building its own.
    """

    def __init__(self, s3: Any, *, bucket: str, prefix: str, attempts: int = DEFAULT_ATTEMPTS):
        self.s3 = s3
        self.bucket = bucket
        self.prefix = prefix
        self.attempts = attempts
        self.key = f"{prefix}/{gitwal.MANIFEST_KEY}" if prefix else gitwal.MANIFEST_KEY

    def load(self) -> tuple[Manifest | None, str | None]:
        """Returns the manifest and its ETag, or (None, None) when the repo does not exist yet."""
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.key)
        except self.s3.exceptions.NoSuchKey:
            return None, None
        except self.s3.exceptions.ClientError as x:
            if _code(x) in _ABSENT_CODES:
                return None, None
            raise
        return gitwal.load(response["Body"].read()), response.get("ETag")

    def update(self, mutate: Callable[[Manifest], Manifest]) -> Manifest:
        """Runs ``mutate`` against the current manifest and commits it with a conditional PUT.

        On any conditional failure the manifest is re-loaded and ``mutate`` runs again against the
        new state, so every validation the mutator performs is performed against what it commits
        to. A mutator raising :class:`Reject` ends the loop at once.
        """
        for attempt in range(self.attempts):
            manifest, etag = self.load()
            if manifest is None or etag is None:
                committed = self._create(mutate)
            else:
                committed = self._replace(mutate, manifest, etag)
            if committed is not None:
                return committed
            self._backoff(attempt)
        raise CasExhaustedError(self.key, self.attempts)

    def _create(self, mutate: Callable[[Manifest], Manifest]) -> Manifest | None:
        candidate = mutate(Manifest())
        outcome = self._put(candidate, IfNoneMatch="*")
        if outcome == _STALE:
            logger.info(f"{self.key} was created concurrently; retrying as an update")
        return candidate if outcome == _COMMITTED else None

    def _replace(self, mutate: Callable[[Manifest], Manifest], manifest: Manifest, etag: str) -> Manifest | None:
        candidate = mutate(manifest)
        outcome = self._put(candidate, IfMatch=etag)
        if outcome == _ABSENT:
            # Only reachable by an out-of-band deletion between our GET and our PUT. The next
            # attempt loads nothing and takes the create-if-absent path.
            logger.warning(f"{self.key} disappeared between the read and the write; restarting from create-if-absent")
        return candidate if outcome == _COMMITTED else None

    def _put(self, manifest: Manifest, **condition: str) -> str:
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=gitwal.dump(manifest).encode("utf-8"),
                ContentType="application/json",
                **condition,
            )
        except self.s3.exceptions.ClientError as x:
            code = _code(x)
            if code in _STALE_CODES:
                return _STALE
            if code in _CONFLICT_CODES:
                # Request timing, not a logical conflict. Re-GET anyway rather than reason about it.
                return _CONFLICT
            if code in _ABSENT_CODES:
                return _ABSENT
            raise
        return _COMMITTED

    def _backoff(self, attempt: int) -> None:
        delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2**attempt))
        time.sleep(random.uniform(0, delay))


def _code(error: Any) -> str:
    response = getattr(error, "response", None) or {}
    code = response.get("Error", {}).get("Code")
    if code:
        return str(code)
    return str(response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
