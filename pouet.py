#!/usr/bin/env python3
"""pouet -- read the extra numbers off pouet.net's production API.

demozoo.py already knows, for every release Demozoo links to pouet.net, which
prod id that is (the `PouetProduction` link class).  The toplists it fetches
only cover the top 64 per platform, so anything below that gets no pouet data
at all.  This module fills that gap: given a prod id it fetches

    https://api.pouet.net/v1/prod/?id=<id>

and pulls out what demarc cares about -- the CDC count, the popularity
percentage, the thumb-up/ok/down votes and any awards the prod won or was
nominated for.  The API answers with ~5KB of JSON where prod.php is ~100KB of
HTML for the same numbers, and it needs no html parser.

Responses are cached under .pouetcache/prod-<id>.json, so a rerun is offline
and pouet.net is only asked once per prod.  Pass refresh=True (or --refresh on
the command line) to refetch.

    >>> get_prod(981)
    PouetProd(id=981, name='Hardwired', cdc=24, popularity=84.6..., rulez=292,
              isok=16, sucks=0, rank=47, awards=[])

Run as a script to dump one or more prods:  ./pouet.py 981 51983
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.request import Request, urlopen

API_URL = "https://api.pouet.net/v1/prod/?id={id}"

# One file per prod; delete the directory (or pass refresh=True) to pick up
# newer vote counts.  Kept separate from demozoo.py's .pouet_cache, which holds
# the per-platform toplist pages.
CACHE_DIR = ".pouetcache"


@dataclass
class PouetAward:
    """One award row.  The API names awards by category id only -- there is no
    endpoint that maps those to 'The Meteoriks - Best Low-End Production', so
    the id is what we can honestly report.  `type` is pouet's own wording,
    'winner' or 'nominee'."""
    category_id: int
    type: str

    def __str__(self):
        return f"{self.type}:{self.category_id}"


@dataclass
class PouetProd:
    """What the API tells us about one prod.  `popularity` is the percentage
    the site rounds down to '84%' (see popularity_pct), `rank` its alltime-top
    position (0 if unranked), `awards` the award rows in API order."""
    id: int
    name: str = ""
    cdc: int = 0
    popularity: float = 0.0
    rulez: int = 0
    isok: int = 0
    sucks: int = 0
    rank: int = 0
    awards: list[PouetAward] = field(default_factory=list)

    @property
    def thumbs(self) -> int:
        """Net thumbs, the number demozoo.py's toplist rows call `thumbs`."""
        return self.rulez

    @property
    def popularity_pct(self) -> int:
        """Popularity as the site shows it: 73.945 -> 73."""
        return int(self.popularity)


_INT_RE = re.compile(r"-?\d+")


def _int(value, default=0):
    """`value` as an int, however the API spelled it ('109', 109, None).  Vote
    counts come back as strings, cdc as a number, absent fields as null."""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    m = _INT_RE.search(str(value))
    return int(m.group()) if m else default


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_page(url):
    """GET `url` as text.  Pouet 403s the default urllib agent, so pose as a
    browser the way demozoo.py and bitworld_scrape.py do."""
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def cache_path(prod_id, cache_dir=CACHE_DIR):
    """Where the response for `prod_id` is cached."""
    return os.path.join(cache_dir, f"prod-{int(prod_id)}.json")


def is_cached(prod_id, cache_dir=CACHE_DIR):
    """True if get_prod(`prod_id`) would answer without hitting the network.
    Callers pacing a long run use this to sleep only between real fetches."""
    return os.path.exists(cache_path(prod_id, cache_dir))


def prod_json(prod_id, cache_dir=CACHE_DIR, refresh=False):
    """The API response for `prod_id` as text, from the disk cache if we have
    it.  Error responses are cached too: pouet answers 200 with {"error":true}
    for a prod that does not exist, and asking again would not change that."""
    path = cache_path(prod_id, cache_dir)
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    text = fetch_page(API_URL.format(id=int(prod_id)))
    os.makedirs(cache_dir, exist_ok=True)
    # Write-then-rename, as elsewhere here: a half-written response left by an
    # interrupted fetch would otherwise be cached as if it were the real thing.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return text


def parse_prod(text, prod_id: int = 0) -> PouetProd | None:
    """Pull the stats out of an API response (json text, or the decoded dict).

    The fields we want sit at the top level of `prod`:

        "voteup": "109", "votepig": "3", "votedown": "0",
        "cdc": 1, "popularity": 73.945, "rank": "767",
        "awards": [{"categoryID": "40", "awardType": "nominee"}, ...]

    Returns None when pouet has no such prod -- it answers HTTP 200 with
    {"error": true} rather than a 404 -- or when the body is not the JSON we
    expect, so a caller can tell "no data" from a broken run either way.
    """
    if isinstance(text, (dict, list)):
        data = text
    else:
        try:
            data = json.loads(text)
        except ValueError:
            return None
    if not isinstance(data, dict) or not data.get("success"):
        return None
    p = data.get("prod")
    if not isinstance(p, dict):
        return None

    awards = []
    for a in p.get("awards") or []:
        if isinstance(a, dict):
            awards.append(PouetAward(category_id=_int(a.get("categoryID")),
                                     type=str(a.get("awardType") or "")))

    return PouetProd(
        id=_int(p.get("id"), int(prod_id)),
        name=str(p.get("name") or ""),
        cdc=_int(p.get("cdc")),
        popularity=_float(p.get("popularity")),
        rulez=_int(p.get("voteup")),
        isok=_int(p.get("votepig")),
        sucks=_int(p.get("votedown")),
        rank=_int(p.get("rank")),
        awards=awards,
    )


def get_prod(prod_id, cache_dir=CACHE_DIR, refresh=False) -> PouetProd | None:
    """Fetch (or read back) one prod and parse it.  None if pouet has no such
    prod; raises OSError (urllib's HTTPError included) if the fetch fails."""
    return parse_prod(prod_json(prod_id, cache_dir, refresh), prod_id)


def get_prods(prod_ids, cache_dir=CACHE_DIR, refresh=False,
              delay=0.0) -> dict[int, PouetProd]:
    """{prod id: PouetProd} for the ids we could read.  A prod that does not
    exist or whose fetch fails is warned about and skipped rather than failing
    the whole run -- this data is an extra, not the point of the export.

    `delay` seconds are slept after each *fetch*, so a long run does not hammer
    pouet.net; prods already cached go at full speed."""
    out = {}
    for prod_id in prod_ids:
        cached = not refresh and is_cached(prod_id, cache_dir)
        try:
            prod = get_prod(prod_id, cache_dir, refresh)
        except OSError as e:
            print(f"  WARNING: no pouet prod {prod_id}: {e}", file=sys.stderr)
            continue
        if prod is not None:
            out[int(prod_id)] = prod
        if delay and not cached:
            time.sleep(delay)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ids", nargs="+", type=int, help="pouet prod ids")
    ap.add_argument("--cache-dir", default=CACHE_DIR,
                    help=f"where responses are cached (default: {CACHE_DIR})")
    ap.add_argument("--refresh", action="store_true",
                    help="refetch instead of using the cache")
    args = ap.parse_args()

    for prod_id in args.ids:
        prod = get_prod(prod_id, args.cache_dir, args.refresh)
        if prod is None:
            print(f"{prod_id}: no such prod", file=sys.stderr)
            continue
        print(f"{prod.id} {prod.name}: cdc={prod.cdc} "
              f"popularity={prod.popularity_pct}% rulez={prod.rulez} "
              f"isok={prod.isok} sucks={prod.sucks} rank={prod.rank or '-'}")
        for award in prod.awards:
            print(f"  award: {award}")


if __name__ == "__main__":
    main()
