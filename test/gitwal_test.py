# SPDX-FileCopyrightText: 2026-present FullDuplex Media
#
# SPDX-License-Identifier: Apache-2.0

import json

import pytest

from git_remote_s3 import gitwal
from git_remote_s3.gitwal import Entry, Manifest, UnsupportedFormatError

SHA1_MAIN = "e3a1c0f6d2b48a1e9f37c5d0b6a2e814f9c37d5a"
SHA1_DEV = "7c14b9e0d8a3f5261c0b4e7a9d3f28c15b60e4a7"
SHA1_TAG = "9bd21f8c4e07a63d5b1f2e08c47a9d63e15b0c82"
SHA256_MAIN = "b" * 64


def manifest_doc():
    return {
        "format": 1,
        "seq": 43,
        "head": "refs/heads/main",
        "refs": {"refs/heads/main": SHA1_MAIN, "refs/heads/dev": SHA1_DEV},
        "protected": ["refs/heads/main"],
        "entries": [
            {
                "seq": 1,
                "kind": "base",
                "pack": "packs/4f1c8ab6d0e29537c14b8f60a2e7d9354c81b0f6.pack",
                "bytes": 734003200,
                "objects": 128412,
                "tips": {"refs/heads/main": SHA1_TAG},
                "by": "arn:aws:sts::000000000000:assumed-role/git-buckets-dev/vince",
                "at": "2026-08-20T18:03:11Z",
            },
            {
                "seq": 43,
                "kind": "incremental",
                "pack": "packs/b2d70e1c9a34f80625d1e7b0c39a4f86d215e0b7.pack",
                "bytes": 41983,
                "objects": 7,
                "tips": {"refs/heads/main": SHA1_MAIN},
                "by": "arn:aws:sts::000000000000:assumed-role/git-buckets-dev/vince",
                "at": "2026-08-20T18:41:52Z",
            },
        ],
    }


def new_entry(pack="packs/" + "a" * 40 + ".pack", tips=None, **kwargs):
    return Entry(pack=pack, bytes=1024, objects=3, tips=tips or {"refs/heads/main": SHA1_MAIN}, **kwargs)


def test_load_reads_every_documented_field():
    m = gitwal.load(json.dumps(manifest_doc()))

    assert m.format == 1
    assert m.seq == 43
    assert m.head == "refs/heads/main"
    assert m.refs["refs/heads/main"] == SHA1_MAIN
    assert m.protected == ["refs/heads/main"]
    assert len(m.entries) == 2
    assert m.entries[0].kind == "base"
    assert m.entries[1].objects == 7
    assert m.entries[1].tips == {"refs/heads/main": SHA1_MAIN}


def test_load_accepts_bytes_and_dict():
    doc = manifest_doc()
    assert gitwal.load(json.dumps(doc).encode("utf-8")) == gitwal.load(doc)


def test_load_rejects_non_json():
    with pytest.raises(gitwal.ManifestFormatError):
        gitwal.load("{not json")


def test_load_rejects_non_object():
    with pytest.raises(gitwal.ManifestFormatError):
        gitwal.load("[1, 2]")


def test_dump_round_trips_verbatim():
    doc = manifest_doc()
    m = gitwal.load(doc)

    assert json.loads(gitwal.dump(m)) == doc


def test_dump_is_stable_and_human_readable():
    m = gitwal.load(manifest_doc())
    text = gitwal.dump(m)

    assert text == gitwal.dump(gitwal.load(text))
    assert text.endswith("\n")
    assert "\n  " in text


def test_head_absent_stays_absent():
    m = Manifest(refs={"refs/heads/main": SHA1_MAIN})

    assert "head" not in json.loads(gitwal.dump(m))


def test_apply_push_appends_entry_and_bumps_seq_once():
    m = gitwal.load(manifest_doc())

    out = gitwal.apply_push(m, refs={"refs/heads/dev": SHA1_TAG}, entry=new_entry())

    assert out.seq == 44
    assert out.refs["refs/heads/dev"] == SHA1_TAG
    assert out.refs["refs/heads/main"] == SHA1_MAIN
    assert len(out.entries) == 3
    assert out.entries[-1].seq == 44
    assert m.seq == 43 and len(m.entries) == 2


def test_apply_push_with_empty_pack_is_refs_only():
    m = gitwal.load(manifest_doc())

    out = gitwal.apply_push(m, refs={"refs/tags/v1.4.0": SHA1_TAG})

    assert out.seq == 44
    assert out.entries == m.entries
    assert out.refs["refs/tags/v1.4.0"] == SHA1_TAG
    assert not gitwal.errors(gitwal.validate(out))


def test_apply_push_can_set_head_for_a_new_repo():
    out = gitwal.apply_push(Manifest(), refs={"refs/heads/main": SHA1_MAIN}, entry=new_entry(), head="refs/heads/main")

    assert out.seq == 1
    assert out.head == "refs/heads/main"
    assert out.entries[0].seq == 1


def test_apply_push_round_trips():
    out = gitwal.apply_push(gitwal.load(manifest_doc()), refs={"refs/heads/dev": SHA1_TAG}, entry=new_entry())

    assert gitwal.load(gitwal.dump(out)) == out


def test_apply_delete_drops_ref_and_protection():
    m = gitwal.load(manifest_doc())

    out = gitwal.apply_delete(m, ref="refs/heads/main")

    assert out.seq == 44
    assert "refs/heads/main" not in out.refs
    assert out.protected == []
    assert out.entries == m.entries
    assert gitwal.load(gitwal.dump(out)) == out


def test_apply_delete_of_absent_ref_still_bumps_seq():
    out = gitwal.apply_delete(gitwal.load(manifest_doc()), ref="refs/heads/nope")

    assert out.seq == 44


def test_apply_protect_and_unprotect_round_trip():
    m = gitwal.load(manifest_doc())

    protected = gitwal.apply_protect(m, ref="refs/heads/dev")
    assert protected.seq == 44
    assert protected.protected == ["refs/heads/main", "refs/heads/dev"]
    assert protected.is_protected("refs/heads/dev")

    unprotected = gitwal.apply_unprotect(protected, ref="refs/heads/dev")
    assert unprotected.seq == 45
    assert unprotected.protected == ["refs/heads/main"]
    assert gitwal.load(gitwal.dump(unprotected)) == unprotected


def test_apply_protect_is_idempotent_and_legal_for_absent_ref():
    m = gitwal.load(manifest_doc())

    once = gitwal.apply_protect(m, ref="refs/heads/future")
    twice = gitwal.apply_protect(once, ref="refs/heads/future")

    assert twice.protected == ["refs/heads/main", "refs/heads/future"]
    assert twice.seq == 45
    assert not gitwal.errors(gitwal.validate(twice))


def test_apply_compaction_collapses_to_one_base_entry():
    m = gitwal.load(manifest_doc())
    base = new_entry(pack="packs/" + "c" * 40 + ".pack")

    out = gitwal.apply_compaction(m, entry=base)

    assert out.seq == 44
    assert [e.kind for e in out.entries] == ["base"]
    assert out.entries[0].seq == 44
    assert out.refs == m.refs
    assert gitwal.load(gitwal.dump(out)) == out
    assert gitwal.superseded_packs(m, out) == [e.pack for e in m.entries]


def test_set_head_round_trips_and_clears():
    m = gitwal.load(manifest_doc())

    moved = gitwal.set_head(m, head="refs/heads/dev")
    assert moved.seq == 44
    assert moved.head == "refs/heads/dev"
    assert gitwal.load(gitwal.dump(moved)) == moved

    cleared = gitwal.set_head(moved, head=None)
    assert cleared.head is None
    assert cleared.seq == 45


def test_unknown_top_level_keys_survive_load_mutate_dump():
    doc = manifest_doc()
    doc["lfs"] = {"count": 3}
    doc["zzz_future"] = "keep me"

    out = gitwal.apply_push(gitwal.load(doc), refs={"refs/heads/dev": SHA1_TAG}, entry=new_entry())
    round_tripped = json.loads(gitwal.dump(out))

    assert round_tripped["lfs"] == {"count": 3}
    assert round_tripped["zzz_future"] == "keep me"
    assert round_tripped["seq"] == 44


def test_unknown_entry_keys_survive_load_mutate_dump():
    doc = manifest_doc()
    doc["entries"][0]["signature"] = "abc"

    out = gitwal.apply_delete(gitwal.load(doc), ref="refs/heads/dev")

    assert json.loads(gitwal.dump(out))["entries"][0]["signature"] == "abc"


def test_higher_format_reads_refs_but_refuses_writes():
    doc = manifest_doc()
    doc["format"] = gitwal.SUPPORTED_FORMAT + 1
    m = gitwal.load(doc)

    assert m.refs["refs/heads/main"] == SHA1_MAIN
    assert m.head == "refs/heads/main"
    assert not m.writable

    with pytest.raises(UnsupportedFormatError) as excinfo:
        gitwal.apply_push(m, refs={"refs/heads/dev": SHA1_TAG}, entry=new_entry())

    message = str(excinfo.value)
    assert "format 2" in message
    assert f"supports format {gitwal.SUPPORTED_FORMAT}" in message
    assert "fduplex-git-remote-s3" in message


@pytest.mark.parametrize(
    "transition",
    [
        lambda m: gitwal.apply_push(m, refs={"refs/heads/dev": SHA1_TAG}),
        lambda m: gitwal.apply_delete(m, ref="refs/heads/dev"),
        lambda m: gitwal.apply_protect(m, ref="refs/heads/dev"),
        lambda m: gitwal.apply_unprotect(m, ref="refs/heads/main"),
        lambda m: gitwal.apply_compaction(m, entry=new_entry()),
        lambda m: gitwal.set_head(m, head="refs/heads/dev"),
    ],
)
def test_every_transition_is_gated_on_format(transition):
    doc = manifest_doc()
    doc["format"] = 99
    m = gitwal.load(doc)

    with pytest.raises(UnsupportedFormatError):
        transition(m)


def test_higher_format_dumps_unchanged():
    doc = manifest_doc()
    doc["format"] = 7
    doc["future_field"] = [1, 2]

    assert json.loads(gitwal.dump(gitwal.load(doc))) == doc


def test_sha1_and_sha256_are_both_accepted():
    assert gitwal.is_sha(SHA1_MAIN)
    assert gitwal.is_sha(SHA256_MAIN)
    assert not gitwal.is_sha(SHA1_MAIN[:39])
    assert not gitwal.is_sha(SHA1_MAIN.upper())
    assert not gitwal.is_sha("z" * 40)
    assert not gitwal.is_sha(None)


def test_sha256_refs_and_tips_validate_clean():
    m = Manifest(
        seq=1,
        head="refs/heads/main",
        refs={"refs/heads/main": SHA256_MAIN},
        entries=[new_entry(seq=1, tips={"refs/heads/main": SHA256_MAIN})],
    )

    assert gitwal.validate(m) == []


def test_validate_clean_manifest_has_no_findings():
    assert gitwal.validate(gitwal.load(manifest_doc())) == []


def test_validate_protected_ref_absent_from_refs_is_legal():
    m = gitwal.load(manifest_doc())
    m.protected.append("refs/heads/not-yet")

    assert gitwal.validate(m) == []


def test_validate_reports_seq_below_highest_entry():
    doc = manifest_doc()
    doc["seq"] = 2
    findings = gitwal.validate(gitwal.load(doc))

    assert [f.code for f in findings] == ["seq_monotonic"]
    assert "43" in findings[0].message


def test_validate_reports_non_increasing_entry_seq():
    doc = manifest_doc()
    doc["entries"][1]["seq"] = 1
    codes = [f.code for f in gitwal.validate(gitwal.load(doc))]

    assert "entry_seq_order" in codes


def test_validate_reports_bad_ref_sha_and_refname():
    doc = manifest_doc()
    doc["refs"]["refs/heads/dev"] = "not-a-sha"
    doc["refs"]["heads/loose"] = SHA1_TAG
    findings = gitwal.validate(gitwal.load(doc))
    codes = sorted(f.code for f in findings)

    assert codes == ["bad_refname", "bad_sha"]
    assert all(f.level == "error" for f in findings)


def test_validate_reports_bad_entry_tip_sha():
    doc = manifest_doc()
    doc["entries"][0]["tips"]["refs/heads/main"] = "nope"
    findings = gitwal.validate(gitwal.load(doc))

    assert [f.code for f in findings] == ["bad_sha"]
    assert "entry 1" in findings[0].message


def test_validate_reports_a_pack_named_twice():
    doc = manifest_doc()
    doc["entries"][1]["pack"] = doc["entries"][0]["pack"]
    findings = gitwal.validate(gitwal.load(doc))

    assert [f.code for f in findings] == ["duplicate_pack"]


def test_validate_reports_absolute_pack_key():
    doc = manifest_doc()
    doc["entries"][0]["pack"] = "repo/packs/deadbeef.pack"
    codes = [f.code for f in gitwal.validate(gitwal.load(doc))]

    assert codes == ["bad_pack_key"]


def test_validate_reports_unresolvable_head_as_a_warning():
    doc = manifest_doc()
    doc["head"] = "refs/heads/gone"
    findings = gitwal.validate(gitwal.load(doc))

    assert [(f.level, f.code) for f in findings] == [("warning", "head_unresolved")]
    assert gitwal.errors(findings) == []


def test_validate_reports_future_format_without_erroring():
    doc = manifest_doc()
    doc["format"] = 9
    findings = gitwal.validate(gitwal.load(doc))

    assert [f.code for f in findings] == ["future_format"]
    assert gitwal.errors(findings) == []


def test_validate_reports_unknown_entry_kind_as_a_warning():
    doc = manifest_doc()
    doc["entries"][1]["kind"] = "mystery"
    findings = gitwal.validate(gitwal.load(doc))

    assert [(f.level, f.code) for f in findings] == [("warning", "unknown_kind")]


def test_validate_accumulates_every_finding():
    doc = manifest_doc()
    doc["seq"] = 0
    doc["refs"]["refs/heads/main"] = "bad"
    doc["entries"][1]["pack"] = doc["entries"][0]["pack"]
    codes = sorted(f.code for f in gitwal.validate(gitwal.load(doc)))

    assert codes == ["bad_sha", "duplicate_pack", "seq_monotonic"]
