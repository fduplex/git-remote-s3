# SPDX-FileCopyrightText: 2026-present FullDuplex Media
#
# SPDX-License-Identifier: Apache-2.0

import json
from io import BytesIO

import pytest
from botocore.exceptions import ClientError

from git_remote_s3 import gitwal, walstore
from git_remote_s3.walstore import CasExhaustedError, Reject, WalStore

SHA_MAIN = "e3a1c0f6d2b48a1e9f37c5d0b6a2e814f9c37d5a"
SHA_MOVED = "7c14b9e0d8a3f5261c0b4e7a9d3f28c15b60e4a7"
SHA_MINE = "9bd21f8c4e07a63d5b1f2e08c47a9d63e15b0c82"

BUCKET = "test_bucket"
PREFIX = "test_prefix"
KEY = f"{PREFIX}/{gitwal.MANIFEST_KEY}"


class NoSuchKey(ClientError):
    pass


class Exceptions:
    NoSuchKey = NoSuchKey
    ClientError = ClientError


def client_error(code, error_class=ClientError):
    return error_class({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {}}, "PutObject")


class FakeS3:
    """A stubbed S3 client that answers get_object/put_object and scripts conditional failures.

    ``put_results`` is consumed one entry per PUT: None commits, a code string raises that error,
    and a (code, fn) pair raises it after fn has mutated the stored state the way a competing
    client would have.
    """

    exceptions = Exceptions

    def __init__(self, manifest=None, put_results=()):
        self.doc = gitwal.dump(manifest) if manifest is not None else None
        self.etag = '"etag-0"'
        self.put_results = list(put_results)
        self.puts = []
        self.gets = 0

    def get_object(self, Bucket, Key):
        assert (Bucket, Key) == (BUCKET, KEY)
        self.gets += 1
        if self.doc is None:
            raise client_error("NoSuchKey", NoSuchKey)
        return {"Body": BytesIO(self.doc.encode("utf-8")), "ETag": self.etag}

    def put_object(self, **kwargs):
        assert (kwargs["Bucket"], kwargs["Key"]) == (BUCKET, KEY)
        self.puts.append(kwargs)
        result = self.put_results.pop(0) if self.put_results else None
        if isinstance(result, tuple):
            code, then = result
            then(self)
            raise client_error(code)
        if result is not None:
            raise client_error(result)
        self.doc = kwargs["Body"].decode("utf-8")
        self.etag = f'"etag-{len(self.puts)}"'
        return {"ETag": self.etag}

    def stored(self):
        return json.loads(self.doc)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(walstore.time, "sleep", lambda _seconds: None)


def store(s3, **kwargs):
    return WalStore(s3, bucket=BUCKET, prefix=PREFIX, **kwargs)


def manifest_with(**refs):
    return gitwal.Manifest(seq=7, head="refs/heads/main", refs=dict(refs))


def push(ref, sha):
    """A mutator that records the state it saw, so a retry's re-run can be asserted on."""
    seen = []

    def mutate(manifest):
        seen.append(manifest.copy())
        return gitwal.apply_push(manifest, refs={ref: sha})

    mutate.seen = seen
    return mutate


def test_load_returns_none_when_the_repo_does_not_exist():
    assert store(FakeS3()).load() == (None, None)


def test_load_returns_the_manifest_and_its_etag():
    s3 = FakeS3(manifest_with(**{"refs/heads/main": SHA_MAIN}))
    manifest, etag = store(s3).load()
    assert manifest.refs == {"refs/heads/main": SHA_MAIN}
    assert etag == '"etag-0"'


def test_commits_on_the_first_put():
    s3 = FakeS3(manifest_with(**{"refs/heads/main": SHA_MAIN}))
    mutate = push("refs/heads/main", SHA_MINE)

    committed = store(s3).update(mutate)

    assert committed.seq == 8
    assert len(s3.puts) == 1
    assert s3.puts[0]["IfMatch"] == '"etag-0"'
    assert s3.puts[0]["ContentType"] == "application/json"
    assert s3.stored()["refs"]["refs/heads/main"] == SHA_MINE


def test_creates_with_if_none_match_when_the_repo_is_absent():
    s3 = FakeS3()
    mutate = push("refs/heads/main", SHA_MINE)

    committed = store(s3).update(mutate)

    assert mutate.seen[0].seq == 0 and mutate.seen[0].refs == {}
    assert committed.seq == 1
    assert s3.puts[0]["IfNoneMatch"] == "*"
    assert "IfMatch" not in s3.puts[0]
    assert s3.stored()["refs"] == {"refs/heads/main": SHA_MINE}


def test_precondition_failed_reloads_and_reruns_the_mutator_against_fresh_state():
    def someone_else_pushed(s3):
        s3.doc = gitwal.dump(manifest_with(**{"refs/heads/main": SHA_MOVED, "refs/heads/dev": SHA_MOVED}))
        s3.etag = '"etag-fresh"'

    s3 = FakeS3(
        manifest_with(**{"refs/heads/main": SHA_MAIN}),
        put_results=[("PreconditionFailed", someone_else_pushed)],
    )
    mutate = push("refs/heads/dev", SHA_MINE)

    committed = store(s3).update(mutate)

    assert [m.refs for m in mutate.seen] == [
        {"refs/heads/main": SHA_MAIN},
        {"refs/heads/main": SHA_MOVED, "refs/heads/dev": SHA_MOVED},
    ]
    assert s3.gets == 2
    assert [p["IfMatch"] for p in s3.puts] == ['"etag-0"', '"etag-fresh"']
    assert committed.refs["refs/heads/main"] == SHA_MOVED
    assert committed.refs["refs/heads/dev"] == SHA_MINE


def test_conditional_request_conflict_retries():
    s3 = FakeS3(manifest_with(**{"refs/heads/main": SHA_MAIN}), put_results=["ConditionalRequestConflict"])
    mutate = push("refs/heads/main", SHA_MINE)

    committed = store(s3).update(mutate)

    assert len(mutate.seen) == 2
    assert s3.gets == 2
    assert len(s3.puts) == 2
    assert committed.refs["refs/heads/main"] == SHA_MINE


def test_not_found_on_an_if_match_put_restarts_from_create_if_absent():
    def deleted_out_of_band(s3):
        s3.doc = None

    s3 = FakeS3(
        manifest_with(**{"refs/heads/main": SHA_MAIN}),
        put_results=[("NoSuchKey", deleted_out_of_band)],
    )
    mutate = push("refs/heads/main", SHA_MINE)

    committed = store(s3).update(mutate)

    assert "IfMatch" in s3.puts[0]
    assert s3.puts[1]["IfNoneMatch"] == "*"
    assert mutate.seen[1].refs == {}
    assert committed.seq == 1


def test_create_conflict_falls_through_to_the_update_path():
    def someone_else_created(s3):
        s3.doc = gitwal.dump(manifest_with(**{"refs/heads/main": SHA_MOVED}))
        s3.etag = '"etag-created"'

    s3 = FakeS3(put_results=[("PreconditionFailed", someone_else_created)])
    mutate = push("refs/heads/dev", SHA_MINE)

    committed = store(s3).update(mutate)

    assert s3.puts[0]["IfNoneMatch"] == "*"
    assert s3.puts[1]["IfMatch"] == '"etag-created"'
    assert committed.refs == {"refs/heads/main": SHA_MOVED, "refs/heads/dev": SHA_MINE}


def test_perpetual_contention_fails_cleanly_after_the_bound():
    s3 = FakeS3(manifest_with(**{"refs/heads/main": SHA_MAIN}), put_results=["PreconditionFailed"] * 50)
    mutate = push("refs/heads/main", SHA_MINE)

    with pytest.raises(CasExhaustedError) as failure:
        store(s3).update(mutate)

    assert len(s3.puts) == walstore.DEFAULT_ATTEMPTS
    assert KEY in str(failure.value)


def test_a_bounded_store_stops_at_its_own_attempt_budget():
    s3 = FakeS3(manifest_with(**{"refs/heads/main": SHA_MAIN}), put_results=["PreconditionFailed"] * 50)

    with pytest.raises(CasExhaustedError):
        store(s3, attempts=3).update(push("refs/heads/main", SHA_MINE))

    assert len(s3.puts) == 3


def test_a_rejecting_mutator_issues_no_further_puts():
    attempts = []

    def mutate(manifest):
        attempts.append(manifest)
        if len(attempts) == 3:
            raise Reject("non-fast-forward")
        return gitwal.apply_push(manifest, refs={"refs/heads/main": SHA_MINE})

    s3 = FakeS3(manifest_with(**{"refs/heads/main": SHA_MAIN}), put_results=["PreconditionFailed"] * 50)

    with pytest.raises(Reject):
        store(s3).update(mutate)

    assert len(attempts) == 3
    assert len(s3.puts) == 2


def test_an_unrelated_error_is_not_retried():
    s3 = FakeS3(manifest_with(**{"refs/heads/main": SHA_MAIN}), put_results=["AccessDenied"])

    with pytest.raises(ClientError):
        store(s3).update(push("refs/heads/main", SHA_MINE))

    assert len(s3.puts) == 1


def test_a_bucket_root_repo_keys_the_manifest_at_the_root():
    assert WalStore(FakeS3(), bucket=BUCKET, prefix="").key == gitwal.MANIFEST_KEY
