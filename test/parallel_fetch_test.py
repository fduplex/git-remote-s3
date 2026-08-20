# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0
# Modifications Copyright 2026 FullDuplex Media
# Changed: re-aimed at the WAL fetch, whose parallelism is over pack downloads, not over refs.

import threading
import time
from unittest.mock import patch

import pytest

from git_remote_s3 import S3Remote, UriScheme, gitwal
from git_remote_s3 import remote as remote_module
from remote_test import ManifestStore, wal_manifest

SHA1 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8a"
SHA2 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8b"
SHA3 = "c105d19ba64965d2c9d3d3246e7269059ef8bb8c"
BRANCH = "pytest"
REF = f"refs/heads/{BRANCH}"
URL = "s3://test_bucket/test_prefix"


@pytest.fixture(autouse=True)
def _no_git_config(monkeypatch):
    """Keeps the imported-seq and region caches off the project's own git config."""
    monkeypatch.setattr(remote_module, "_git_config_get", lambda key: None)
    monkeypatch.setattr(remote_module, "_git_config_run", lambda *args: None)
    monkeypatch.setattr(remote_module, "maybe_install_lfs_agent", lambda remote_name: None)


def _entries(*shas):
    return [gitwal.Entry(seq=i + 1, pack=f"packs/{sha}.pack", bytes=64, tips={REF: sha}) for i, sha in enumerate(shas)]


def _remote(client, *shas):
    ManifestStore(client, manifest=wal_manifest({REF: shas[-1]}, entries=_entries(*shas)))
    return S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix", remote_name="origin", remote_url=URL)


@patch("boto3.Session.client")
def test_process_fetch_cmds_empty_list(session_client_mock):
    S3Remote(UriScheme.S3, None, "test_bucket", "test_prefix").process_fetch_cmds([])

    session_client_mock.return_value.get_object.assert_not_called()


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_one_entry_is_one_download_and_one_index(session_client_mock, index_pack_mock, resolves_mock):
    client = session_client_mock.return_value

    _remote(client, SHA1).process_fetch_cmds([f"fetch {SHA1} {REF}"])

    client.download_file.assert_called_once()
    index_pack_mock.assert_called_once()


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_every_entry_of_the_log_is_downloaded_and_indexed(session_client_mock, index_pack_mock, resolves_mock):
    client = session_client_mock.return_value

    _remote(client, SHA1, SHA2, SHA3).process_fetch_cmds([f"fetch {SHA3} {REF}"])

    assert [c.kwargs["Key"] for c in client.download_file.call_args_list] == [
        f"test_prefix/packs/{sha}.pack" for sha in (SHA1, SHA2, SHA3)
    ]
    assert index_pack_mock.call_count == 3


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_pack_downloads_overlap(session_client_mock, index_pack_mock, resolves_mock):
    """The packs of one import are downloaded concurrently, which is where the wall time is."""
    client = session_client_mock.return_value
    in_flight = set()
    overlapped = threading.Event()
    lock = threading.Lock()

    def download_file(*, Key, **kwargs):
        with lock:
            in_flight.add(Key)
            if len(in_flight) > 1:
                overlapped.set()
        time.sleep(0.05)
        with lock:
            in_flight.discard(Key)

    client.download_file.side_effect = download_file

    _remote(client, SHA1, SHA2, SHA3).process_fetch_cmds([f"fetch {SHA3} {REF}"])

    assert overlapped.is_set()


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_indexing_is_serialised_after_the_downloads(session_client_mock, index_pack_mock, resolves_mock):
    # git index-pack writes into one object database; the packs are downloaded in parallel and
    # imported one at a time.
    client = session_client_mock.return_value
    concurrent_indexes = []
    active = []
    lock = threading.Lock()

    def index_pack(*, path, **kwargs):
        with lock:
            active.append(path)
            concurrent_indexes.append(len(active))
        time.sleep(0.01)
        with lock:
            active.remove(path)

    index_pack_mock.side_effect = index_pack

    _remote(client, SHA1, SHA2, SHA3).process_fetch_cmds([f"fetch {SHA3} {REF}"])

    assert concurrent_indexes == [1, 1, 1]


@patch("git_remote_s3.git.has_complete_history", return_value=True)
@patch("git_remote_s3.git.index_pack")
@patch("boto3.Session.client")
def test_process_cmd_batches_fetches_until_the_blank_line(session_client_mock, index_pack_mock, resolves_mock):
    client = session_client_mock.return_value
    s3_remote = _remote(client, SHA1, SHA2, SHA3)

    s3_remote.process_cmd(f"fetch {SHA1} {REF}")
    s3_remote.process_cmd(f"fetch {SHA2} {REF}")
    s3_remote.process_cmd(f"fetch {SHA3} {REF}")

    assert len(s3_remote.fetch_cmds) == 3
    client.download_file.assert_not_called()

    with patch("git_remote_s3.remote.S3Remote.process_fetch_cmds") as process_fetch_cmds:
        s3_remote.process_cmd("\n")

    process_fetch_cmds.assert_called_once()
    assert len(process_fetch_cmds.call_args[0][0]) == 3
    assert s3_remote.fetch_cmds == []
