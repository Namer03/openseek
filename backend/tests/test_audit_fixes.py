"""Regression tests for vulnerabilities found by the pre-release adversarial review."""
from __future__ import annotations

import pytest

from openseek.db import DB
from openseek.fetch import MAX_SOURCE_SIZE, _parse_cr
from openseek.library import Library


def _library(tmp_path):
    db = DB(tmp_path / "x.db")
    db.set_setting("metadata_index", "1")
    return db, Library(db, cache=None)


def _meta(db, fp):
    return db.q("SELECT imdb_id, tmdb_id, season, episode FROM release_meta WHERE fp=?", (fp,))


def test_a_contribution_cannot_relabel_an_existing_entry(tmp_path):
    """The owner's entry for a file is labelled; a guest who streams the same bytes must not rewrite the ids."""
    db, lib = _library(tmp_path)
    fp = "ab" * 32
    assert lib.record(fp, {"imdb_id": "tt0000001", "season": 0, "episode": 0}, 123)
    # contributed job: first writer wins
    lib.record(fp, {"imdb_id": "tt9999999", "season": 0, "episode": 0}, 123, replace=False)
    assert _meta(db, fp) == [("tt0000001", None, 0, 0)]


def test_a_contribution_still_labels_a_new_entry(tmp_path):
    db, lib = _library(tmp_path)
    fp = "cd" * 32
    lib.record(fp, {"imdb_id": "tt0000002", "season": 1, "episode": 2}, 5, replace=False)
    assert _meta(db, fp) == [("tt0000002", None, 1, 2)]


def test_the_owner_can_still_correct_a_label(tmp_path):
    db, lib = _library(tmp_path)
    fp = "ef" * 32
    lib.record(fp, {"imdb_id": "tt0000003", "season": 0, "episode": 0}, 5, replace=False)       # a guest's (maybe wrong) label
    lib.record(fp, {"imdb_id": "tt0000004", "season": 0, "episode": 0}, 5)                        # the owner's own job
    assert _meta(db, fp) == [("tt0000004", None, 0, 0)]


@pytest.mark.parametrize("total", [2 ** 64, 2 ** 64 + 5, MAX_SOURCE_SIZE + 1, 10 ** 30])
def test_an_absurd_content_range_total_is_rejected(total):
    assert _parse_cr(f"bytes 0-99/{total}") is None


def test_a_normal_content_range_is_still_accepted():
    assert _parse_cr("bytes 0-99/10000000000") == (0, 99, 10_000_000_000)
