import os
import re
import subprocess
from io import StringIO, BytesIO
from unittest.mock import patch

import boto3.exceptions
import pytest
from botocore.exceptions import ClientError

from git_remote_s3 import S3Remote, UriScheme, git, gitwal, walstore
from git_remote_s3 import remote as remote_module
from git_remote_s3.remote import FetchIncompleteError, NotAuthorizedError, TransferProgress

SHA1 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8a"
SHA2 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8b"
MOVED_SHA = "c105d19ba64965d2c9d3d3246e7269059ef8bb8c"
NULL_SHA = "0" * 40
BRANCH = "pytest"


@pytest.fixture(autouse=True)
def _not_shallow(request, monkeypatch):
    # This suite's cwd is the project's own checkout, whose shallowness depends on how it was
    # cloned (e.g. CI's fetch-depth:1). cmd_push's pre-flight shallow guard would otherwise make
    # every push test here environment-dependent. Tests that exercise the shallow-clone path
    # patch git.is_shallow_repository themselves and take precedence over this default.
    # test_is_shallow_repository_distinguishes_shallow_from_partial exercises the real function
    # against temp repos it controls and must see the genuine result, not this stub.
    if "real_git_shallow_check" in request.keywords:
        yield
        return
    monkeypatch.setattr(git, "is_shallow_repository", lambda: False)
    yield


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_advertises_the_manifests_refs_and_head(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    install_wal(
        session_client_mock.return_value,
        wal_manifest({REF: SHA1, "refs/tags/v1": SHA2}, head=REF),
    )

    s3_remote.cmd_list()

    assert stdout_mock.getvalue() == f"@{REF} HEAD\n{SHA1} {REF}\n{SHA2} refs/tags/v1\n\n"
    # One GET of gitwal.json; the paginated listing over <prefix>/refs is gone.
    session_client_mock.return_value.list_objects_v2.assert_not_called()
    assert session_client_mock.return_value.get_object.call_count == 1


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_nested_prefix_reads_the_nested_manifest(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "nested/test_prefix")
    install_wal(
        session_client_mock.return_value,
        wal_manifest({REF: SHA1}, head=REF),
        key="nested/test_prefix/gitwal.json",
    )

    s3_remote.cmd_list()

    assert stdout_mock.getvalue() == f"@{REF} HEAD\n{SHA1} {REF}\n\n"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_of_a_repo_that_does_not_exist_lists_nothing(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    install_wal(session_client_mock.return_value)

    s3_remote.cmd_list()

    assert stdout_mock.getvalue() == "\n"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_without_a_head_advertises_refs_only(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    install_wal(session_client_mock.return_value, wal_manifest({REF: SHA1}))

    s3_remote.cmd_list()

    assert stdout_mock.getvalue() == f"{SHA1} {REF}\n\n"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_with_a_head_naming_no_ref_advertises_refs_only(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    install_wal(session_client_mock.return_value, wal_manifest({REF: SHA1}, head="refs/heads/master"))

    s3_remote.cmd_list()

    assert stdout_mock.getvalue() == f"{SHA1} {REF}\n\n"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_for_push_reads_the_same_manifest_without_the_symref(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    install_wal(session_client_mock.return_value, wal_manifest({REF: SHA1}, head=REF))

    s3_remote.cmd_list(for_push=True)

    assert stdout_mock.getvalue() == f"{SHA1} {REF}\n\n"
    assert session_client_mock.return_value.get_object.call_count == 1


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_list_advertises_a_protected_ref_like_any_other(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    install_wal(session_client_mock.return_value, wal_manifest({REF: SHA1}, head=REF, protected=[REF]))

    s3_remote.cmd_list()

    assert stdout_mock.getvalue() == f"@{REF} HEAD\n{SHA1} {REF}\n\n"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_option("option verbosity 2")
    assert stdout_mock.getvalue().startswith("ok\n")
    s3_remote.cmd_option("option concurrency 1")
    assert stdout_mock.getvalue().endswith("unsupported\n")


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_capabilities(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_capabilities()
    assert "fetch" in stdout_mock.getvalue()
    assert "option" in stdout_mock.getvalue()
    assert "push" in stdout_mock.getvalue()


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_progress(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    assert s3_remote.progress is False

    s3_remote.cmd_option("option progress true")
    assert s3_remote.progress is True
    assert stdout_mock.getvalue() == "ok\n"

    s3_remote.cmd_option("option progress false")
    assert s3_remote.progress is False
    assert stdout_mock.getvalue() == "ok\nok\n"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_verbosity_is_stored_and_acked(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    assert s3_remote.verbosity == 1

    s3_remote.cmd_option("option verbosity 0")
    assert s3_remote.verbosity == 0
    s3_remote.cmd_option("option verbosity 1")
    assert s3_remote.verbosity == 1
    s3_remote.cmd_option("option verbosity 2")
    assert s3_remote.verbosity == 2

    assert stdout_mock.getvalue() == "ok\nok\nok\n"


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_unknown_is_unsupported(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_option("option concurrency 1")
    s3_remote.cmd_option("option verbosity notanint")
    assert stdout_mock.getvalue() == "unsupported\nunsupported\n"


@patch("git_remote_s3.git.subprocess.run")
def test_bundle_default_captures_stderr(run_mock):
    run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    git.bundle(folder="/tmp/folder", sha=SHA1, ref=f"refs/heads/{BRANCH}")

    cmd = run_mock.call_args[0][0]
    assert cmd == ["git", "bundle", "create", f"/tmp/folder/{SHA1}.bundle", f"refs/heads/{BRANCH}"]
    assert run_mock.call_args.kwargs["stderr"] == subprocess.PIPE


@patch("git_remote_s3.git.subprocess.run")
def test_bundle_progress_inherits_stderr(run_mock):
    run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=None)
    git.bundle(folder="/tmp/folder", sha=SHA1, ref=f"refs/heads/{BRANCH}", progress=True)

    cmd = run_mock.call_args[0][0]
    assert cmd[:4] == ["git", "bundle", "create", "--progress"]
    assert run_mock.call_args.kwargs["stderr"] is None


@patch("git_remote_s3.git.subprocess.run")
def test_bundle_quiet_wins_over_progress(run_mock):
    run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    git.bundle(folder="/tmp/folder", sha=SHA1, ref=f"refs/heads/{BRANCH}", progress=True, quiet=True)

    cmd = run_mock.call_args[0][0]
    assert "-q" in cmd
    assert "--progress" not in cmd
    assert run_mock.call_args.kwargs["stderr"] == subprocess.PIPE


@patch("git_remote_s3.git.subprocess.run")
def test_unbundle_progress_flag(run_mock):
    run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    git.unbundle(folder="/tmp/folder", sha=SHA1, ref=f"refs/heads/{BRANCH}", progress=True)
    assert run_mock.call_args[0][0][:4] == ["git", "bundle", "unbundle", "--progress"]

    run_mock.reset_mock()
    git.unbundle(folder="/tmp/folder", sha=SHA1, ref=f"refs/heads/{BRANCH}")
    assert "--progress" not in run_mock.call_args[0][0]


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_cas_records_lease(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{SHA2}")
    assert stdout_mock.getvalue() == "ok\n"
    assert s3_remote.cas_refs == {f"refs/heads/{BRANCH}": SHA2}


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_origin(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    _git("config", "uploadpack.allowFilter", "true", cwd=origin)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, stderr=subprocess.DEVNULL)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "test", cwd=work)
    for i in range(4):
        (work / "file.txt").write_text(f"revision-{i}\n")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", f"r{i}", cwd=work)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    return origin


def _clone(origin, dest, *extra):
    subprocess.run(
        ["git", "clone", "-q", *extra, f"file://{origin}", str(dest)],
        check=True,
        stderr=subprocess.DEVNULL,
    )
    _git("config", "user.email", "test@example.com", cwd=dest)
    _git("config", "user.name", "test", cwd=dest)
    return dest


@pytest.mark.real_git_shallow_check
def test_is_shallow_repository_distinguishes_shallow_from_partial(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)

    full = _clone(origin, tmp_path / "full")
    monkeypatch.chdir(full)
    assert git.is_shallow_repository() is False

    shallow = _clone(origin, tmp_path / "shallow", "--depth", "1")
    monkeypatch.chdir(shallow)
    assert git.is_shallow_repository() is True

    # Partial clones are not blocked: pack-objects faults the missing objects back in from the
    # promisor remote while bundling, so the bundle stays complete (asserted below).
    blobless = _clone(origin, tmp_path / "blobless", "--filter=blob:none")
    monkeypatch.chdir(blobless)
    assert git.is_shallow_repository() is False


def test_bundle_from_blobless_clone_is_complete(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    blobless = _clone(origin, tmp_path / "blobless", "--filter=blob:none")
    (blobless / "file.txt").write_text("revision-4\n")
    _git("commit", "-qam", "r4", cwd=blobless)

    monkeypatch.chdir(blobless)
    sha = git.rev_parse("refs/heads/main")
    bundle_path = git.bundle(folder=str(tmp_path), sha=sha, ref="refs/heads/main", quiet=True)

    restored = tmp_path / "restored.git"
    subprocess.run(["git", "init", "-q", "--bare", str(restored)], check=True, stdout=subprocess.DEVNULL)
    _git("fetch", "-q", bundle_path, "refs/heads/*:refs/heads/*", cwd=restored)

    fsck = subprocess.run(["git", "fsck", "--full"], cwd=restored, capture_output=True, text=True)
    assert fsck.returncode == 0, fsck.stderr
    assert "missing" not in fsck.stderr
    # Every object of the full history, including blobs the partial clone never held, made it over.
    objects = subprocess.run(
        ["git", "rev-list", "--objects", "--all"], cwd=restored, capture_output=True, text=True, check=True
    )
    assert objects.stdout.count("\n") == 5 * 3


@patch("sys.stderr", new_callable=StringIO)
def test_transfer_progress_uses_kib_below_one_mib(stderr_mock):
    progress = TransferProgress(action="Downloading", label="refs/heads/main", total_bytes=500 * 1024)
    progress(250 * 1024)

    rendered = stderr_mock.getvalue()
    assert "KiB" in rendered
    assert "MiB" not in rendered
    assert "250 / 500 KiB (50%)" in rendered


@patch("sys.stderr", new_callable=StringIO)
def test_transfer_progress_uses_mib_at_or_above_one_mib(stderr_mock):
    progress = TransferProgress(action="Uploading", label="refs/heads/main", total_bytes=4 * 1024 * 1024)
    progress(2 * 1024 * 1024)

    rendered = stderr_mock.getvalue()
    assert "MiB" in rendered
    assert "KiB" not in rendered
    assert "2.0 / 4.0 MiB (50%)" in rendered


@patch("sys.stderr", new_callable=StringIO)
def test_transfer_progress_without_total_adapts_to_seen_bytes(stderr_mock):
    progress = TransferProgress(action="Downloading", label="refs/heads/main")
    progress(10 * 1024)

    assert "10 KiB" in stderr_mock.getvalue()


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_cas_records_an_expect_absent_lease(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    # git spells "the ref must not exist" as an all-zero sha; an empty value means the same.
    s3_remote.cmd_option(f"option cas refs/heads/{BRANCH}:{NULL_SHA}")
    s3_remote.cmd_option("option cas refs/heads/other:")

    assert stdout_mock.getvalue() == "ok\nok\n"
    assert s3_remote.cas_refs == {f"refs/heads/{BRANCH}": "", "refs/heads/other": ""}


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_cmd_option_bad_verbosity_leaves_the_level_alone(session_client_mock, stdout_mock):
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")

    s3_remote.cmd_option("option verbosity 0")
    s3_remote.cmd_option("option verbosity notanint")

    assert s3_remote.verbosity == 0
    assert stdout_mock.getvalue() == "ok\nunsupported\n"


@patch("git_remote_s3.remote.time.monotonic")
@patch("sys.stderr", new_callable=StringIO)
def test_transfer_progress_throttles_rapid_updates(stderr_mock, monotonic_mock):
    monotonic_mock.side_effect = [0.0, 0.01, 0.02, 0.03, 0.2, 0.21]
    progress = TransferProgress(action="Downloading", label="refs/heads/main")

    for _ in range(6):
        progress(1024)

    # The first update always renders; after that only one per throttle window gets through.
    assert stderr_mock.getvalue().count("\r") == 2


@patch("sys.stderr", new_callable=StringIO)
def test_transfer_progress_close_newlines_only_after_a_render(stderr_mock):
    progress = TransferProgress(action="Downloading", label="refs/heads/main")

    progress.close()
    assert stderr_mock.getvalue() == ""

    progress(1024)
    progress.close()
    assert stderr_mock.getvalue().endswith("\n")


REF = f"refs/heads/{BRANCH}"
OTHER_REF = "refs/heads/other"
PACK_KEY = f"test_prefix/packs/{SHA1}.pack"
CALLER_ARN = "arn:aws:sts::000000000000:assumed-role/git-buckets-dev/tester"
MOCK_PACK_CONTENT = b"MOCK_PACK_CONTENT"


class NoSuchKey(ClientError):
    pass


class S3Exceptions:
    NoSuchKey = NoSuchKey
    ClientError = ClientError


def client_error(code, error_class=ClientError):
    return error_class({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {}}, "PutObject")


class ManifestStore:
    """The gitwal.json half of a stubbed S3 client: real manifest bytes, scripted CAS failures.

    ``put_results`` is consumed one entry per manifest PUT: None commits, a code string raises that
    error, and a (code, fn) pair raises it after fn has mutated the stored document the way a
    competing client would have.
    """

    def __init__(self, client, manifest=None, put_results=(), key="test_prefix/gitwal.json"):
        self.client = client
        self.key = key
        self.doc = gitwal.dump(manifest) if manifest is not None else None
        self.etag = '"etag-0"'
        self.put_results = list(put_results)
        self.puts = []
        client.exceptions = S3Exceptions
        # No STS identity by default: entry provenance is best-effort, and a test that cares
        # about it says so.
        client.get_caller_identity.return_value = {}
        client.get_object.side_effect = self._get_object
        client.put_object.side_effect = self._put_object

    def _get_object(self, Bucket, Key):
        assert Key == self.key
        if self.doc is None:
            raise client_error("NoSuchKey", NoSuchKey)
        return {"Body": BytesIO(self.doc.encode("utf-8")), "ETag": self.etag}

    def _put_object(self, **kwargs):
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

    def hold(self, manifest):
        """Replaces the stored document, standing in for a competing client's commit."""
        self.doc = gitwal.dump(manifest)
        self.etag = f'"etag-{len(self.puts)}-other"'

    @property
    def manifest(self):
        return gitwal.load(self.doc)


def wal_manifest(refs=None, *, head=None, protected=(), entries=(), seq=7):
    return gitwal.Manifest(seq=seq, head=head, refs=dict(refs or {}), protected=list(protected), entries=list(entries))


def install_wal(client, manifest=None, put_results=(), key="test_prefix/gitwal.json"):
    return ManifestStore(client, manifest=manifest, put_results=put_results, key=key)


def fake_pack(objects=3):
    """A pack_objects side effect writing a real file, named by the sha it packs."""

    def build(*, folder, sha, **kwargs):
        path = f"{folder}/pack-{sha}.pack"
        with open(path, "wb") as f:
            f.write(MOCK_PACK_CONTENT)
        return git.Pack(path=path, checksum=sha, bytes=os.path.getsize(path), objects=objects)

    return build


def uploads(client_mock):
    return client_mock.upload_file.call_args_list


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_creates_the_repo_with_one_pack_and_one_cas(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack(objects=7)
    store = install_wal(client)

    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res == f"ok {REF}\n"
    assert len(uploads(client)) == 1
    assert uploads(client)[0].kwargs["Key"] == PACK_KEY
    assert uploads(client)[0].kwargs["Config"] is not None
    assert len(store.puts) == 1
    assert store.puts[0]["IfNoneMatch"] == "*"
    manifest = store.manifest
    assert manifest.refs == {REF: SHA1}
    # init_remote_head's rule, kept: the first ref pushed to a repo with no default branch names it.
    assert manifest.head == REF
    entry = manifest.entries[0]
    assert (entry.pack, entry.objects, entry.bytes) == (f"packs/{SHA1}.pack", 7, len(MOCK_PACK_CONTENT))
    assert entry.tips == {REF: SHA1}
    assert entry.seq == manifest.seq == 1
    client.delete_object.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_fast_forward_appends_an_entry(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_ancestor_mock.return_value = True
    store = install_wal(client, wal_manifest({REF: SHA2, OTHER_REF: MOVED_SHA}, head=REF))

    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res == f"ok {REF}\n"
    # The pack excludes every tip the manifest already names, which is what makes it O(delta).
    assert pack_objects_mock.call_args.kwargs["have"] == sorted({SHA2, MOVED_SHA})
    assert store.puts[0]["IfMatch"] == '"etag-0"'
    manifest = store.manifest
    assert manifest.seq == 8
    assert manifest.refs == {REF: SHA1, OTHER_REF: MOVED_SHA}
    assert [e.seq for e in manifest.entries] == [8]
    client.delete_object.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_no_force_no_ancestor(session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    is_ancestor_mock.return_value = False
    store = install_wal(client, wal_manifest({REF: SHA2}))

    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res == f'error {REF} "remote ref is not ancestor of {REF}."?\n'
    # The doomed push is caught before it builds a pack, let alone uploads one.
    pack_objects_mock.assert_not_called()
    assert uploads(client) == []
    assert store.puts == []


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_force_no_ancestor(session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_ancestor_mock.return_value = False
    store = install_wal(client, wal_manifest({REF: SHA2}))

    res = s3_remote.cmd_push(f"push +{REF}:{REF}")

    assert res == f"ok {REF}\n"
    assert len(uploads(client)) == 1
    assert store.manifest.refs == {REF: SHA1}


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_force_no_ancestor_protected(session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    is_ancestor_mock.return_value = False
    store = install_wal(client, wal_manifest({REF: SHA2}, protected=[REF]))

    res = s3_remote.cmd_push(f"push +{REF}:{REF}")

    assert res == f'error {REF} "remote ref is protected."?\n'
    pack_objects_mock.assert_not_called()
    assert store.puts == []


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_fast_forward_to_a_protected_ref_is_allowed(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    # Protection blocks history rewrites only, which is exactly what the PROTECTED# marker did.
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_ancestor_mock.return_value = True
    store = install_wal(client, wal_manifest({REF: SHA2}, protected=[REF]))

    assert s3_remote.cmd_push(f"push {REF}:{REF}") == f"ok {REF}\n"
    assert store.manifest.protected == [REF]


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_with_nothing_to_pack_is_a_refs_only_cas(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    # Tagging a commit the remote already holds: no pack object, no entry, but the ref still moves.
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack(objects=0)
    is_ancestor_mock.return_value = True
    entry = gitwal.Entry(seq=7, pack=f"packs/{SHA2}.pack", objects=4, tips={REF: SHA2})
    store = install_wal(client, wal_manifest({REF: SHA2}, entries=[entry]))

    res = s3_remote.cmd_push("push refs/tags/v1:refs/tags/v1")

    assert res == "ok refs/tags/v1\n"
    assert uploads(client) == []
    manifest = store.manifest
    assert manifest.refs == {REF: SHA2, "refs/tags/v1": SHA1}
    assert [e.seq for e in manifest.entries] == [7]
    assert manifest.seq == 8


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_push_batch_commits_every_ref_in_one_cas(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.side_effect = lambda ref: SHA1 if ref == REF else SHA2
    pack_objects_mock.side_effect = fake_pack()
    store = install_wal(client)

    res = s3_remote.process_push_cmds([f"push {REF}:{REF}", f"push {OTHER_REF}:{OTHER_REF}"])

    assert res == [f"ok {REF}\n", f"ok {OTHER_REF}\n"]
    assert sorted(c.kwargs["Key"] for c in uploads(client)) == sorted(
        [f"test_prefix/packs/{SHA1}.pack", f"test_prefix/packs/{SHA2}.pack"]
    )
    assert len(store.puts) == 1
    manifest = store.manifest
    assert manifest.refs == {REF: SHA1, OTHER_REF: SHA2}
    assert [e.seq for e in manifest.entries] == [1, 2]
    assert manifest.seq == 2


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_push_batch_of_two_refs_at_one_tip_names_the_shared_pack_once(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    # Both refs pack the same objects, so both packs are the same object; only one entry may name it.
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    store = install_wal(client)

    res = s3_remote.process_push_cmds([f"push {REF}:{REF}", f"push {OTHER_REF}:{OTHER_REF}"])

    assert res == [f"ok {REF}\n", f"ok {OTHER_REF}\n"]
    manifest = store.manifest
    assert manifest.refs == {REF: SHA1, OTHER_REF: SHA1}
    assert [e.pack for e in manifest.entries] == [f"packs/{SHA1}.pack"]
    assert gitwal.errors(gitwal.validate(manifest)) == []


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_a_concurrent_push_to_another_ref_re_appends_at_the_new_seq(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_ancestor_mock.return_value = True

    def someone_else_pushed_another_ref(store):
        store.hold(
            wal_manifest(
                {REF: SHA2, OTHER_REF: MOVED_SHA},
                seq=8,
                entries=[gitwal.Entry(seq=8, pack=f"packs/{MOVED_SHA}.pack", tips={OTHER_REF: MOVED_SHA})],
            )
        )

    store = install_wal(
        client,
        wal_manifest({REF: SHA2}),
        put_results=[("PreconditionFailed", someone_else_pushed_another_ref)],
    )

    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res == f"ok {REF}\n"
    # Our ref did not move, so the pack and the entry are still valid: only the seq is rebased.
    assert len(uploads(client)) == 1
    assert len(store.puts) == 2
    manifest = store.manifest
    assert manifest.refs == {REF: SHA1, OTHER_REF: MOVED_SHA}
    assert [e.seq for e in manifest.entries] == [8, 9]
    assert manifest.seq == 9


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_a_concurrent_push_to_the_same_ref_fails_the_re_validation(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    # Fast-forward from the tip we read, not from the one the winner left behind.
    is_ancestor_mock.side_effect = lambda ancestor, descendant: ancestor == SHA2

    def someone_else_pushed_our_ref(store):
        store.hold(wal_manifest({REF: MOVED_SHA}, seq=8))

    store = install_wal(
        client,
        wal_manifest({REF: SHA2}),
        put_results=[("PreconditionFailed", someone_else_pushed_our_ref)],
    )

    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res == f'error {REF} "remote ref is not ancestor of {REF}."?\n'
    # The loser stops on the re-validated state instead of spending its retry budget.
    assert len(store.puts) == 1
    assert store.manifest.refs == {REF: MOVED_SHA}


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_perpetual_contention_renders_an_error_line(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_ancestor_mock.return_value = True
    store = install_wal(client, wal_manifest({REF: SHA2}), put_results=["PreconditionFailed"] * 50)

    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res.startswith(f"error {REF} ")
    assert "gitwal.json" in res
    assert len(store.puts) == walstore.DEFAULT_ATTEMPTS


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_a_push_after_a_failed_cas_succeeds(session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock):
    # A crash between the pack upload and the manifest PUT leaves an orphan pack, which is inert.
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_ancestor_mock.return_value = True
    store = install_wal(client, wal_manifest({REF: SHA2}), put_results=["AccessDenied"])

    crashed = s3_remote.cmd_push(f"push {REF}:{REF}")
    retried = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert crashed.startswith(f"error {REF} ")
    assert retried == f"ok {REF}\n"
    # The retry re-uploads identical bytes to the identical key, so the orphan is simply reused.
    assert [c.kwargs["Key"] for c in uploads(client)] == [PACK_KEY, PACK_KEY]
    manifest = store.manifest
    assert manifest.refs == {REF: SHA1}
    assert [e.pack for e in manifest.entries] == [f"packs/{SHA1}.pack"]


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_records_the_callers_arn_and_a_utc_timestamp(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    store = install_wal(client)
    client.get_caller_identity.return_value = {"Arn": CALLER_ARN}

    s3_remote.cmd_push(f"push {REF}:{REF}")

    entry = store.manifest.entries[0]
    assert entry.by == CALLER_ARN
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry.at)


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_provenance_is_best_effort(session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    store = install_wal(client)
    client.get_caller_identity.side_effect = client_error("AccessDenied")

    assert s3_remote.cmd_push(f"push {REF}:{REF}") == f"ok {REF}\n"
    assert store.manifest.entries[0].by is None


@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_delete_drops_the_ref_in_a_refs_only_cas(session_client_mock, pack_objects_mock, rev_parse_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    entry = gitwal.Entry(seq=7, pack=f"packs/{SHA1}.pack", tips={REF: SHA1})
    store = install_wal(client, wal_manifest({REF: SHA1, OTHER_REF: SHA2}, protected=[REF], entries=[entry]))

    res = s3_remote.cmd_push(f"push :{REF}")

    assert res == f"ok {REF}\n"
    manifest = store.manifest
    assert manifest.refs == {OTHER_REF: SHA2}
    assert manifest.protected == []
    # A delete reclaims no storage: the pack it uniquely held stays until compaction.
    assert [e.pack for e in manifest.entries] == [f"packs/{SHA1}.pack"]
    # One CAS, one seq bump: a delete-only batch must not also apply an empty push.
    assert len(store.puts) == 1
    assert manifest.seq == 8
    client.delete_object.assert_not_called()
    pack_objects_mock.assert_not_called()


@patch("boto3.Session.client")
def test_cmd_push_delete_of_an_absent_ref_is_not_found(session_client_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    store = install_wal(client, wal_manifest({OTHER_REF: SHA2}))

    res = s3_remote.cmd_push(f"push :{REF}")

    assert res == f"error {REF} not found\n"
    assert store.puts == []


@patch("boto3.Session.client")
def test_cmd_push_delete_with_matching_lease_proceeds(session_client_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    store = install_wal(client, wal_manifest({REF: SHA1}))

    s3_remote.cmd_option(f"option cas {REF}:{SHA1}")
    res = s3_remote.cmd_push(f"push :{REF}")

    assert res == f"ok {REF}\n"
    assert store.manifest.refs == {}


@patch("boto3.Session.client")
def test_cmd_push_delete_with_stale_lease_deletes_nothing(session_client_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    store = install_wal(client, wal_manifest({REF: SHA1}))

    # The ref moved after `list for-push`; a delete replaces history just as a force push does.
    s3_remote.cmd_option(f"option cas {REF}:{MOVED_SHA}")
    res = s3_remote.cmd_push(f"push :{REF}")

    assert res.startswith(f"error {REF} ")
    assert "stale info" in res
    assert store.puts == []


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_lease_no_ancestor_accepted(session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_ancestor_mock.return_value = False
    store = install_wal(client, wal_manifest({REF: SHA2}))

    # git sends no leading "+" for --force-with-lease, only the leased sha via `option cas`.
    s3_remote.cmd_option(f"option cas {REF}:{SHA2}")
    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res == f"ok {REF}\n"
    assert len(uploads(client)) == 1
    assert store.manifest.refs == {REF: SHA1}


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_lease_no_ancestor_protected(session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    is_ancestor_mock.return_value = False
    store = install_wal(client, wal_manifest({REF: SHA2}, protected=[REF]))

    s3_remote.cmd_option(f"option cas {REF}:{SHA2}")
    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res == f'error {REF} "remote ref is protected."?\n'
    assert uploads(client) == []
    assert store.puts == []
    pack_objects_mock.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_lease_rejected_when_remote_moved(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    is_ancestor_mock.return_value = False
    install_wal(client, wal_manifest({REF: SHA2}))

    # Lease taken against a sha the remote no longer holds: the ref moved after `list for-push`.
    s3_remote.cmd_option(f"option cas {REF}:{MOVED_SHA}")
    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res == (
        f'error {REF} "stale info: remote ref is at {SHA2}, not the {MOVED_SHA} it was leased against. Fetch first."?\n'
    )
    assert uploads(client) == []
    pack_objects_mock.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_lease_for_other_ref_does_not_apply(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    is_ancestor_mock.return_value = False
    install_wal(client, wal_manifest({REF: SHA2}))

    s3_remote.cmd_option(f"option cas {OTHER_REF}:{SHA2}")
    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res == f'error {REF} "remote ref is not ancestor of {REF}."?\n'
    assert uploads(client) == []


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_lease_rejected_even_when_fast_forward(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    # The ref moved after `list for-push`, but to a tip our push still fast-forwards from: the
    # lease has to be enforced independently of the ancestry check.
    is_ancestor_mock.return_value = True
    install_wal(client, wal_manifest({REF: SHA2}))

    s3_remote.cmd_option(f"option cas {REF}:{MOVED_SHA}")
    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res.startswith(f"error {REF} ")
    assert "stale info" in res
    assert uploads(client) == []
    pack_objects_mock.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_lease_expecting_absent_rejects_an_existing_ref(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    is_ancestor_mock.return_value = True
    install_wal(client, wal_manifest({REF: SHA2}))

    s3_remote.cmd_option(f"option cas {REF}:{NULL_SHA}")
    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert "stale info" in res
    assert "absent" in res
    assert uploads(client) == []
    pack_objects_mock.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_lease_naming_a_sha_rejects_an_absent_ref(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    install_wal(client, wal_manifest())

    s3_remote.cmd_option(f"option cas {REF}:{SHA2}")
    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert "stale info" in res
    assert "absent" in res
    assert uploads(client) == []
    pack_objects_mock.assert_not_called()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_lease_expecting_absent_accepts_a_new_ref(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    install_wal(client, wal_manifest({OTHER_REF: SHA2}))

    s3_remote.cmd_option(f"option cas {REF}:{NULL_SHA}")
    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res == f"ok {REF}\n"
    assert len(uploads(client)) == 1


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_lease_is_re_checked_inside_the_cas(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    # Another client committed between our pre-flight read and our conditional PUT.
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_ancestor_mock.return_value = True

    def someone_else_created_the_ref(store):
        store.hold(wal_manifest({REF: SHA2}, seq=8))

    store = install_wal(
        client,
        wal_manifest(),
        put_results=[("PreconditionFailed", someone_else_created_the_ref)],
    )

    s3_remote.cmd_option(f"option cas {REF}:{NULL_SHA}")
    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert "stale info" in res
    assert store.manifest.refs == {REF: SHA2}


@patch("git_remote_s3.git.is_shallow_repository")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_from_shallow_clone_rejected(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_shallow_repository_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    is_shallow_repository_mock.return_value = True
    store = install_wal(client, wal_manifest({REF: SHA2}))

    res = s3_remote.cmd_push(f"push +{REF}:{REF}")

    assert res == (f'error {REF} "cannot push from a shallow clone; run git fetch --unshallow first."?\n')
    pack_objects_mock.assert_not_called()
    assert uploads(client) == []
    assert store.puts == []


@patch("git_remote_s3.git.is_shallow_repository")
@patch("boto3.Session.client")
def test_cmd_push_delete_allowed_from_shallow_clone(session_client_mock, is_shallow_repository_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    is_shallow_repository_mock.return_value = True
    install_wal(client, wal_manifest({REF: SHA1}))

    assert s3_remote.cmd_push(f"push :{REF}") == f"ok {REF}\n"


@patch("git_remote_s3.git.is_shallow_repository")
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_probes_shallowness_once_per_process(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock, is_shallow_repository_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_shallow_repository_mock.return_value = False
    install_wal(client)

    assert s3_remote.cmd_push(f"push {REF}:{REF}").startswith("ok")
    assert s3_remote.cmd_push(f"push {OTHER_REF}:{OTHER_REF}").startswith("ok")

    is_shallow_repository_mock.assert_called_once_with()


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_threads_progress_options_to_pack_objects(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    install_wal(client)

    s3_remote.progress = True
    s3_remote.verbosity = 1
    s3_remote.cmd_push(f"push {REF}:{REF}")
    assert pack_objects_mock.call_args.kwargs["progress"] is True
    assert pack_objects_mock.call_args.kwargs["quiet"] is False

    s3_remote.progress = False
    s3_remote.verbosity = 0
    s3_remote.cmd_push(f"push {REF}:{REF}")
    assert pack_objects_mock.call_args.kwargs["progress"] is False
    assert pack_objects_mock.call_args.kwargs["quiet"] is True


@patch("sys.stderr", new_callable=StringIO)
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_renders_progress_on_stderr(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock, stderr_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    install_wal(client)

    def upload_file_side_effect(Filename, Bucket, Key, Callback, **kwargs):
        Callback(os.path.getsize(Filename))

    client.upload_file.side_effect = upload_file_side_effect

    s3_remote.progress = True
    s3_remote.cmd_push(f"push {REF}:{REF}")

    rendered = stderr_mock.getvalue()
    assert rendered.startswith(f"\rUploading {REF}: ")
    assert "(100%)" in rendered
    assert rendered.endswith("\n")


@patch("sys.stderr", new_callable=StringIO)
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_is_silent_without_progress(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock, stderr_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    install_wal(client)

    s3_remote.cmd_push(f"push {REF}:{REF}")

    assert client.upload_file.call_args.kwargs["Callback"] is None
    assert stderr_mock.getvalue() == ""


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_upload_failure_returns_error(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_ancestor_mock.return_value = True
    store = install_wal(client, wal_manifest({REF: SHA2}))
    client.upload_file.side_effect = boto3.exceptions.S3UploadFailedError("Failed to upload pack: AccessDenied")

    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res.startswith(f"error {REF} ")
    assert "AccessDenied" in res
    # A ref whose data never landed must not reach the commit point.
    assert store.puts == []


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_upload_client_error_returns_error(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    pack_objects_mock.side_effect = fake_pack()
    is_ancestor_mock.return_value = True
    install_wal(client, wal_manifest({REF: SHA2}))
    client.upload_file.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "upload_file")

    assert s3_remote.cmd_push(f"push {REF}:{REF}").startswith(f"error {REF} ")


@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_cmd_push_removes_temp_dir(session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    install_wal(client)

    created_dirs = []
    build = fake_pack()

    def pack_side_effect(*, folder, **kwargs):
        created_dirs.append(folder)
        return build(folder=folder, **kwargs)

    pack_objects_mock.side_effect = pack_side_effect

    res = s3_remote.cmd_push(f"push {REF}:{REF}")

    assert res.startswith("ok")
    assert len(created_dirs) == 1
    assert not os.path.exists(created_dirs[0])


@patch("sys.stdout", new_callable=StringIO)
@patch("git_remote_s3.git.is_ancestor")
@patch("git_remote_s3.git.rev_parse")
@patch("git_remote_s3.git.pack_objects")
@patch("boto3.Session.client")
def test_push_batch_survives_a_pack_failure(
    session_client_mock, pack_objects_mock, rev_parse_mock, is_ancestor_mock, stdout_mock
):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    rev_parse_mock.return_value = SHA1
    store = install_wal(client)
    build = fake_pack()
    failed = []

    def pack_side_effect(**kwargs):
        if not failed:
            failed.append(True)
            raise git.GitError('fatal: bad object\nnot "ok"\n')
        return build(**kwargs)

    pack_objects_mock.side_effect = pack_side_effect

    s3_remote.process_cmd(f"push {REF}:{REF}\n")
    s3_remote.process_cmd(f"push {OTHER_REF}:{OTHER_REF}\n")
    s3_remote.process_cmd("\n")

    failing, succeeded = stdout_mock.getvalue().splitlines()[:2]
    # git's stderr has to be flattened onto one line, with no bare quote to end the message early.
    assert failing == f"""error {REF} "fatal: bad object not 'ok'"?"""
    assert succeeded == f"ok {OTHER_REF}"
    assert len(uploads(client)) == 1
    # The ref that could not be packed never reaches the commit point; the other still commits.
    assert store.manifest.refs == {OTHER_REF: SHA1}


@patch("sys.stdout", new_callable=StringIO)
@patch("boto3.Session.client")
def test_process_cmd_clears_cas_after_a_push_batch(session_client_mock, stdout_mock):
    client = session_client_mock.return_value
    s3_remote = S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix")
    install_wal(client, wal_manifest({REF: SHA1}))

    s3_remote.process_cmd(f"push :{REF}\n")
    s3_remote.cas_refs[REF] = SHA1

    s3_remote.process_cmd("\n")

    assert s3_remote.cas_refs == {}


@patch("sys.stderr", new_callable=StringIO)
@patch("boto3.Session.client")
def test_s3_zip_is_accepted_and_deprecated(session_client_mock, stderr_mock):
    S3Remote(UriScheme.S3_ZIP, None, "test_bucket", "test_prefix")

    assert "s3+zip:// is deprecated" in stderr_mock.getvalue()


def test_pack_objects_packs_only_what_the_remote_lacks(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    work = _clone(origin, tmp_path / "clone")
    monkeypatch.chdir(work)
    base = git.rev_parse("refs/heads/main")
    (work / "file.txt").write_text("revision-4\n")
    _git("commit", "-qam", "r4", cwd=work)
    tip = git.rev_parse("refs/heads/main")

    folder = tmp_path / "packs"
    folder.mkdir()
    # An unknown sha is a ref this clone never fetched; it is dropped, not fatal.
    pack = git.pack_objects(folder=str(folder), sha=tip, have=[base, "1" * 40], quiet=True)

    assert pack.objects == 3
    assert pack.path == f"{folder}/pack-{pack.checksum}.pack"
    assert pack.bytes == os.path.getsize(pack.path)
    # Self-contained, not thin: the pack indexes standalone against no object database.
    subprocess.run(["git", "index-pack", pack.path], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)


def test_pack_objects_reports_an_empty_pack(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    work = _clone(origin, tmp_path / "clone")
    monkeypatch.chdir(work)
    tip = git.rev_parse("refs/heads/main")

    folder = tmp_path / "packs"
    folder.mkdir()
    pack = git.pack_objects(folder=str(folder), sha=tip, have=[tip], quiet=True)

    assert pack.objects == 0
    assert pack.bytes == 32


REMOTE_URL = "s3://test_bucket/test_prefix"
SEQ_KEY = "remote.origin.gitwal-seq"
REGION_KEY = "remote.origin.s3region"


@pytest.fixture
def git_config(monkeypatch):
    """Stands in for the repo-local git config the region and imported-seq caches live in."""
    values = {REGION_KEY: "us-east-1"}
    monkeypatch.setattr(remote_module, "_git_config_get", values.get)

    def run(*args):
        if "--unset" in args:
            values.pop(args[-1], None)
        else:
            values[args[-2]] = args[-1]

    monkeypatch.setattr(remote_module, "_git_config_run", run)
    monkeypatch.setattr(remote_module, "maybe_install_lfs_agent", lambda remote_name: None)
    return values


def log_entry(seq, sha, *, size=100):
    return gitwal.Entry(seq=seq, pack=f"packs/{sha}.pack", bytes=size, objects=2, tips={REF: sha})


def fetch_remote(remote_name="origin"):
    return S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix", remote_name=remote_name, remote_url=REMOTE_URL)


def downloaded(client):
    return [call.kwargs["Key"] for call in client.download_file.call_args_list]


THREE_ENTRIES = [log_entry(1, SHA1), log_entry(2, SHA2), log_entry(3, MOVED_SHA)]


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_fetch_imports_only_the_entries_above_the_high_water_mark(
    session_client_mock, index_pack_mock, resolves_mock, git_config
):
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: MOVED_SHA}, entries=THREE_ENTRIES))
    git_config[SEQ_KEY] = "2"

    fetch_remote().process_fetch_cmds([f"fetch {MOVED_SHA} {REF}"])

    assert downloaded(client) == [f"test_prefix/packs/{MOVED_SHA}.pack"]
    assert index_pack_mock.call_count == 1
    assert git_config[SEQ_KEY] == "3"


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_a_fresh_clone_imports_every_entry(session_client_mock, index_pack_mock, resolves_mock, git_config):
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: MOVED_SHA}, entries=THREE_ENTRIES))

    fetch_remote().process_fetch_cmds([f"fetch {MOVED_SHA} {REF}"])

    assert downloaded(client) == [
        f"test_prefix/packs/{SHA1}.pack",
        f"test_prefix/packs/{SHA2}.pack",
        f"test_prefix/packs/{MOVED_SHA}.pack",
    ]
    assert index_pack_mock.call_count == 3
    assert git_config[SEQ_KEY] == "3"


@patch("git_remote_s3.git.has_complete_history")
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_a_stale_mark_self_corrects_by_pulling_older_entries(
    session_client_mock, index_pack_mock, resolves_mock, git_config
):
    # `git gc --prune` dropped objects the mark says are already here, so nothing is above it and
    # the first verification fails. The fallback walks the log backwards until the tip resolves.
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: MOVED_SHA}, entries=THREE_ENTRIES))
    git_config[SEQ_KEY] = "3"
    resolves_mock.side_effect = [False, False, True]

    fetch_remote().process_fetch_cmds([f"fetch {MOVED_SHA} {REF}"])

    assert downloaded(client) == [
        f"test_prefix/packs/{MOVED_SHA}.pack",
        f"test_prefix/packs/{SHA2}.pack",
    ]
    assert git_config[SEQ_KEY] == "3"


@patch("git_remote_s3.git.has_complete_history")
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_a_mark_above_a_compacted_log_still_imports_the_base_entry(
    session_client_mock, index_pack_mock, resolves_mock, git_config
):
    # Compaction replaced the log with one base entry, so the recorded seq names an entry that no
    # longer exists and nothing is newer than it.
    base = gitwal.Entry(seq=9, kind=gitwal.KIND_BASE, pack=f"packs/{SHA1}.pack", bytes=500, tips={REF: MOVED_SHA})
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: MOVED_SHA}, entries=[base], seq=9))
    git_config[SEQ_KEY] = "42"
    resolves_mock.side_effect = [False, True]

    fetch_remote().process_fetch_cmds([f"fetch {MOVED_SHA} {REF}"])

    assert downloaded(client) == [f"test_prefix/packs/{SHA1}.pack"]
    assert git_config[SEQ_KEY] == "9"


@patch("git_remote_s3.git.has_complete_history", return_value=False)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_a_tip_that_never_resolves_fails_and_records_no_mark(
    session_client_mock, index_pack_mock, resolves_mock, git_config
):
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: MOVED_SHA}, entries=THREE_ENTRIES))

    with pytest.raises(FetchIncompleteError) as e:
        fetch_remote().process_fetch_cmds([f"fetch {MOVED_SHA} {REF}"])

    assert MOVED_SHA in str(e.value)
    # Every entry was pulled before giving up, and the mark stays unwritten.
    assert len(downloaded(client)) == 3
    assert SEQ_KEY not in git_config


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_the_batch_imports_the_union_of_the_wanted_shas_once(
    session_client_mock, index_pack_mock, resolves_mock, git_config
):
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: MOVED_SHA, OTHER_REF: SHA2}, entries=THREE_ENTRIES))

    fetch_remote().process_fetch_cmds(
        [f"fetch {MOVED_SHA} {REF}", f"fetch {SHA2} {OTHER_REF}", f"fetch {MOVED_SHA} refs/heads/third"]
    )

    # One manifest read and one import over the whole batch, not one per ref.
    assert len(downloaded(client)) == 3
    assert resolves_mock.call_args_list[0].args == (MOVED_SHA,)
    assert [c.args[0] for c in resolves_mock.call_args_list] == [MOVED_SHA, SHA2]


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_the_batch_renders_one_progress_meter_over_every_pack(
    session_client_mock, index_pack_mock, resolves_mock, git_config
):
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: MOVED_SHA}, entries=THREE_ENTRIES))
    s3_remote = fetch_remote()
    s3_remote.progress = True

    s3_remote.process_fetch_cmds([f"fetch {MOVED_SHA} {REF}"])

    callbacks = {call.kwargs["Callback"] for call in client.download_file.call_args_list}
    assert len(callbacks) == 1
    meter = callbacks.pop()
    assert meter is not None
    # The entries carry their own byte counts, so the meter has a total without a HEAD per pack.
    assert meter.total_bytes == 300
    assert index_pack_mock.call_args.kwargs["progress"] is True


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_fetch_without_progress_passes_no_callback(session_client_mock, index_pack_mock, resolves_mock, git_config):
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: SHA1}, entries=[log_entry(1, SHA1)]))

    fetch_remote().cmd_fetch(f"fetch {SHA1} {REF}")

    assert client.download_file.call_args.kwargs["Callback"] is None
    assert index_pack_mock.call_args.kwargs["progress"] is False


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_fetch_from_a_repo_with_no_manifest_does_nothing(
    session_client_mock, index_pack_mock, resolves_mock, git_config
):
    client = session_client_mock.return_value
    install_wal(client)

    fetch_remote().process_fetch_cmds([f"fetch {SHA1} {REF}"])

    client.download_file.assert_not_called()
    index_pack_mock.assert_not_called()
    assert SEQ_KEY not in git_config


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_fetch_reports_a_missing_read_permission(session_client_mock, index_pack_mock, resolves_mock, git_config):
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: SHA1}, entries=[log_entry(1, SHA1)]))
    client.download_file.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    with pytest.raises(NotAuthorizedError):
        fetch_remote().process_fetch_cmds([f"fetch {SHA1} {REF}"])


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_a_url_only_invocation_imports_but_caches_no_mark(
    session_client_mock, index_pack_mock, resolves_mock, git_config
):
    # git passes the URL as argv[1] for a push or fetch against a raw URL; there is no remote
    # section to cache the mark under, so the import runs every time from seq 0.
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: SHA1}, entries=[log_entry(1, SHA1)]))

    fetch_remote(remote_name=REMOTE_URL).process_fetch_cmds([f"fetch {SHA1} {REF}"])

    assert len(downloaded(client)) == 1
    assert SEQ_KEY not in git_config


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_an_unreadable_mark_is_treated_as_a_fresh_clone(
    session_client_mock, index_pack_mock, resolves_mock, git_config
):
    client = session_client_mock.return_value
    install_wal(client, wal_manifest({REF: MOVED_SHA}, entries=THREE_ENTRIES))
    git_config[SEQ_KEY] = "not-a-number"

    fetch_remote().process_fetch_cmds([f"fetch {MOVED_SHA} {REF}"])

    assert len(downloaded(client)) == 3
    assert git_config[SEQ_KEY] == "3"


@patch("boto3.Session.client")
def test_fetch_of_an_empty_batch_touches_nothing(session_client_mock, git_config):
    client = session_client_mock.return_value

    fetch_remote().process_fetch_cmds([])

    client.get_object.assert_not_called()
    client.download_file.assert_not_called()


def test_index_pack_imports_a_real_pack_into_the_object_database(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    source = _clone(origin, tmp_path / "source")
    monkeypatch.chdir(source)
    tip = git.rev_parse("refs/heads/main")
    folder = tmp_path / "packs"
    folder.mkdir()
    pack = git.pack_objects(folder=str(folder), sha=tip, quiet=True)

    empty = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(empty)], check=True, stdout=subprocess.DEVNULL)
    monkeypatch.chdir(empty)
    assert git.has_complete_history(tip) is False

    git.index_pack(path=pack.path)

    assert git.has_complete_history(tip) is True
    # A pack, not an explosion of loose objects.
    assert os.listdir(".git/objects/pack")
    assert sorted(os.listdir(".git/objects")) == ["info", "pack"]
