"""pouet.get_prod against a saved API response (Megademica 4K, id 81065), so
the test neither hits the network nor depends on today's vote counts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import demozoo
import pouet

FIXTURE = Path(__file__).with_name("prod_81065.json")


def test_get_prod(monkeypatch, tmp_path):
    seen = []

    def fake_fetch(url):
        seen.append(url)
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(pouet, "fetch_page", fake_fetch)
    prod = pouet.get_prod(81065, cache_dir=str(tmp_path))

    assert seen == ["https://api.pouet.net/v1/prod/?id=81065"]
    assert (prod.id, prod.name, prod.cdc) == (81065, "Megademica 4K", 1)
    assert (prod.rulez, prod.isok, prod.sucks) == (109, 3, 0)
    # The site shows '73%' for this one: popularity is the unrounded number.
    assert prod.popularity_pct == 73 and 73.0 <= prod.popularity < 74.0
    assert prod.rank == 767
    assert [str(a) for a in prod.awards] == ["nominee:40", "nominee:44"]

    # Second call is served from the disk cache, without refetching.
    assert pouet.get_prod(81065, cache_dir=str(tmp_path)) == prod
    assert len(seen) == 1
    # ...unless asked to refresh.
    pouet.get_prod(81065, cache_dir=str(tmp_path), refresh=True)
    assert len(seen) == 2


def test_vote_counts_are_strings_in_the_api():
    """The API spells vote counts as strings and cdc as a number; both have to
    come back as ints."""
    prod = pouet.parse_prod({"success": True, "prod": {
        "id": "981", "name": "Hardwired", "voteup": "292", "votepig": "16",
        "votedown": "0", "cdc": 24, "popularity": 84.6, "rank": "47"}})
    assert (prod.id, prod.cdc, prod.rulez, prod.isok, prod.sucks) == (
        981, 24, 292, 16, 0)
    assert prod.popularity_pct == 84
    assert prod.awards == []


def test_no_such_prod():
    """pouet answers 200 with {"error": true} for a prod that is not there."""
    assert pouet.parse_prod('{"error": true}', 424242424) is None
    assert pouet.parse_prod('not json at all', 1) is None
    assert pouet.parse_prod('{"success": true}', 1) is None


def test_error_responses_are_cached(monkeypatch, tmp_path):
    """Asking again would not make the prod exist, so the miss is cached too."""
    seen = []
    monkeypatch.setattr(pouet, "fetch_page",
                        lambda url: seen.append(url) or '{"error": true}')
    assert pouet.get_prod(424242424, cache_dir=str(tmp_path)) is None
    assert pouet.get_prod(424242424, cache_dir=str(tmp_path)) is None
    assert len(seen) == 1


def test_demozoo_prod_lookup(monkeypatch, tmp_path):
    """demozoo --pouet-prods maps an API response onto the same PouetData the
    toplists produce, and asks pouet for each prod only once."""
    seen = []

    def fake_fetch(url):
        seen.append(url)
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(pouet, "fetch_page", fake_fetch)
    lookup = demozoo.pouet_prod_lookup(str(tmp_path), delay=0)

    assert lookup(81065) == demozoo.PouetData(id=81065, thumbs=109, cncd_count=1)
    assert lookup(81065).cncd_count == 1   # served from the cache
    assert len(seen) == 1
    # A release with no pouet link asks nothing.
    assert lookup(None) is None
    assert len(seen) == 1


def test_demozoo_prod_lookup_skips_dead_prods(monkeypatch, tmp_path):
    monkeypatch.setattr(pouet, "fetch_page", lambda url: '{"error": true}')
    assert demozoo.pouet_prod_lookup(str(tmp_path), delay=0)(1) is None

    def boom(url):
        raise OSError("connection reset")

    monkeypatch.setattr(pouet, "fetch_page", boom)
    assert demozoo.pouet_prod_lookup(str(tmp_path), delay=0)(2) is None


def test_demozoo_prod_lookup_limit(monkeypatch, tmp_path):
    """--pouet-limit caps the fetches, not the export: past the limit the
    releases just come out without a pouet field.  Cached prods are free."""
    seen = []

    def fake_fetch(url):
        seen.append(url)
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(pouet, "fetch_page", fake_fetch)
    lookup = demozoo.pouet_prod_lookup(str(tmp_path), delay=0, limit=2)

    assert lookup(1) is not None
    assert lookup(2) is not None
    assert lookup(3) is None          # over the limit: not even asked for
    assert len(seen) == 2
    assert lookup(1) is not None      # ...but the cache still answers
    assert len(seen) == 2
