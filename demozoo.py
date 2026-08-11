#!/usr/bin/env python3
"""demozoo — populate a SQLite database from a Demozoo PostgreSQL dump and
export all releases in the named-field db format read by src/files.rs, the same
one bitworld.txt and csdb.txt use:

    id:1<TAB>title:Zentro 4<TAB>author:Zenith<TAB>date:1992-12-27<TAB>party:The
    Party 1992<TAB>platform:Amiga<TAB>category:Demo<TAB>tags:<TAB>download:http://...

Demozoo covers every platform, so unlike the single-platform bitworld.txt and
csdb.txt there is no `# Platform:` header — each line names its own platform.
Only the platforms in PLATFORM_WHITELIST are exported, and releases whose only
downloads have a blacklisted extension (see DOWNLOAD_BLACKLIST) are dropped.
Graphics and music for which Demozoo records no platform at all are still
exported, with an empty platform field (see PLATFORMLESS_SUPERTYPES).

Usage:
    python demozoo.py --sql demozoo-export.sql --db demozoo.sqlite \
        --out demozoo.txt

By default the database is rebuilt from the SQL file. Pass --skip-load to reuse
an existing SQLite database and only regenerate the export (much faster).
"""

import argparse
import os
import sqlite3
import sys
import time
from fnmatch import fnmatchcase

# ---------------------------------------------------------------------------
# Which tables (and which of their columns) we pull out of the pg_dump.
# We only load what the export actually needs.
# ---------------------------------------------------------------------------
TABLES = {
    "productions_production": [
        "id", "title", "release_date_date", "release_date_precision", "supertype",
    ],
    "productions_production_author_nicks": ["production_id", "nick_id"],
    "productions_production_author_affiliation_nicks": ["production_id", "nick_id"],
    "demoscene_nick": ["id", "releaser_id", "name"],
    "demoscene_releaser": ["id", "is_group"],
    "productions_production_types": ["production_id", "productiontype_id"],
    "productions_productiontype": ["id", "name"],
    "productions_production_platforms": ["production_id", "platform_id"],
    "platforms_platform": ["id", "name"],
    "parties_competitionplacing": ["competition_id", "production_id"],
    "parties_competition": ["id", "party_id"],
    "parties_party_releases": ["party_id", "production_id"],
    "parties_party": ["id", "name"],
    "productions_productionlink": [
        "production_id", "link_class", "parameter", "is_download_link",
    ],
    "taggit_tag": ["id", "name"],
    "taggit_taggeditem": ["tag_id", "object_id", "content_type_id"],
}

# django_content_type id of the 'production' model (productions app).
# Values load from the dump as TEXT, so keep this a string for comparison.
PRODUCTION_CONTENT_TYPE = "12"

# ---------------------------------------------------------------------------
# Demozoo spells its platforms out in full ("Commodore 64"); demarc names the
# same machines the way bitworld.txt and csdb.txt do, so that one filter
# (`-I platform:C64`) picks the same platform out of any of the three dbs.
# Platforms not listed here are exported under their Demozoo name.
# ---------------------------------------------------------------------------
PLATFORM_NAMES = {
    "Commodore 64": "C64",
    "Commodore 64-DTV": "C64 DTV",
    "Commodore 128": "C128",
    "Commodore 16/Plus 4": "C16",
    "Commodore VIC-20": "VIC-20",
    "Amiga OCS/ECS": "Amiga",
    "Amiga AGA": "Amiga AGA",
    "Amiga PPC/RTG": "Amiga PPC",
    "Atari ST/E": "Atari ST",
    "Atari 8 bit": "Atari XL",
    "Atari 2600 Video Computer System (VCS)": "Atari 2600",
    "Sega Megadrive/Genesis": "Megadrive",
    "Nintendo SNES/Super FamiCom": "SNES",
    "Nintendo Game Boy (GB)": "Gameboy",
    "Nintendo Game Boy Color (GBC)": "Gameboy Color",
    "Nintendo Game Boy Advance (GBA)": "GBA",
    "Sony Playstation 1 (PSX)": "PlayStation",
    "TIC-80": "Tic-80",
    "PICO-8": "Pico8",
}

# ---------------------------------------------------------------------------
# What gets exported at all.
#
# PLATFORM_WHITELIST is matched (case-sensitively, as a glob) against the
# *demarc* platform name, i.e. the right-hand side of PLATFORM_NAMES — so
# "Amiga*" covers Amiga, Amiga AGA and Amiga PPC in one pattern.  Platforms
# that match nothing are stripped from the platform field, and a release left
# without any platform is not exported — except for the platformless graphics
# and music described at PLATFORMLESS_SUPERTYPES.
#
# DOWNLOAD_BLACKLIST is matched against the lower-cased URL with any query
# string removed, so the patterns are effectively extensions.  ('#' is left
# alone: it is a literal character in plenty of these unencoded file names,
# e.g. modland's ".../XTD/## crimple ##.mod".)
# demarc runs what it downloads; a bare .mod or a video capture is not a
# release you can run.  Such urls are dropped, and a release whose downloads
# are *all* dropped goes with them.
# ---------------------------------------------------------------------------
PLATFORM_WHITELIST = [
    "Amiga*",
    "Atari*",
    "C64*",
    "C16",
    "Megadrive",
    "SNES",
    "Gameboy*",
    "GBA",
    "PlayStation",
    "Tic-80",
    "ZX Spectrum",
    "Amstrad*",
    "NEO GEO",

]

# Demozoo only really insists on a platform for executable prods; for graphics
# and music it is optional, and some 116k such entries carry no platform row at
# all (as opposed to one we filtered out).  Dropping everything platformless
# would therefore throw away most of Demozoo's pictures and tunes, so entries of
# these supertypes are kept when they have a usable download, and exported with
# an empty `platform:` field — a `-I platform:...` filter will not match them.
PLATFORMLESS_SUPERTYPES = {"graphics", "music"}

DOWNLOAD_BLACKLIST = [
    "*.php",
    "*.ogg",
    "*.mp4",
    "*.avi",
    "*.wmv",
    "*.pdf",
    "*.wav",
    "*.ogg",
    "*.4q",
]


def platform_allowed(name):
    return any(fnmatchcase(name, pat) for pat in PLATFORM_WHITELIST)


def download_allowed(url):
    path = url.split("?", 1)[0].lower()
    return not any(fnmatchcase(path, pat) for pat in DOWNLOAD_BLACKLIST)

# ---------------------------------------------------------------------------
# URL resolution.  A Demozoo productionlink stores a link_class plus a
# parameter; the real URL is reconstructed from a per-class template.  We
# implement the common ones.  `{p}` is the (stripped) parameter.
#
# One productionlink table holds both lists a Demozoo prod page shows: the
# download links and the external links (Pouet/csdb/Youtube/Bandcamp pages).
# demarc fetches the exported url and tries to *run* it, so only the former
# belong in `download:` — a prod whose only links are info pages exports an
# empty download rather than a web page demarc cannot load.  Hence only file
# classes are listed here; everything else resolves to None and is dropped.
# ---------------------------------------------------------------------------
URL_TEMPLATES = {
    "BaseUrl": "{p}",
     # https://files.scene.org/get:fi-ftp/mirrors/amigascne/Gfx/G/Gabi/1996/Floppy-Embraced%20(8bpl).png
    #"AmigascneFile": "http://ftp.amigascne.org/pub/amiga{p}",
    "AmigascneFile": "https://files.scene.org/get:fi-ftp/mirrors/amigascne{p}",
    "# SceneOrgFile": "https://files.scene.org/get{p}",
    "SceneOrgFile": "https://files.scene.org/get:de-https{p}",
    "ModlandFile": "https://ftp.modland.com{p}",
    "FujiologyFile": "https://ftp.untergrund.net/users/ltk_tscc/fujiology{p}",
    "UntergrundFile": "https://ftp.untergrund.net{p}",
    "PaduaOrgFile": "http://ftp.padua.org/pub/c64{p}",
    "Defacto2File": "https://defacto2.net/f/{p}",
    "ModarchiveModule": "https://modarchive.org/module.php?{p}",
    "SixteenColorsPack": "https://16colo.rs/pack/{p}",
}

# Order in which link classes are preferred when choosing THE url for a row.
# The dedicated file archives first (matches bitworld, which points at files);
# BaseUrl last, since it is the catch-all class and its is_download_link flag
# is the only thing telling a file apart from a home page.
URL_PRIORITY = [
    "AmigascneFile", "SceneOrgFile", "ModlandFile", "FujiologyFile",
    "UntergrundFile", "PaduaOrgFile", "Defacto2File", "ModarchiveModule",
    "SixteenColorsPack", "BaseUrl",
]
URL_RANK = {cls: i for i, cls in enumerate(URL_PRIORITY)}


# ---------------------------------------------------------------------------
# URL rewrites, applied to every resolved url as `(pattern, replacement)`
# pairs.  A trailing `*` in the pattern matches any suffix, which is then
# substituted for the `*` in the replacement; the first matching rule wins.
#
# These fix up links that are correct as Demozoo records them but awkward to
# download: they point at a redirect, a dead host name, or a doubled path
# prefix.  demarc used to do this at download time, so it also had to do it for
# urls it never generated; doing it here means the exported db already holds the
# url that works, at the cost of needing a regenerate if a mirror moves.
#
#   scene.org  a `/get/` link 302-redirects to a slow FTP mirror; the
#              `/get:de-https/` variant serves the file straight over HTTPS.
#   modland    some parameters already carry the `/pub/modules` prefix the
#              template adds, giving a doubled path.
#   untergrund the fujiology archive moved from the ltk_tscl user dir to
#              ltk_tscc.
#   sndh       plain http no longer serves files.
# ---------------------------------------------------------------------------
URL_REWRITES = [
    ("https://files.scene.org/get/*", "https://files.scene.org/get:de-https/*"),
    ("https://ftp.modland.com/pub/modules/pub/modules/*",
     "https://ftp.modland.com/pub/modules/*"),
    ("https://ftp.untergrund.net/users/ltk_tscl/*",
     "https://ftp.untergrund.net/users/ltk_tscc/*"),
    ("http://sndh.atari.org/*", "https://sndh.atari.org/*"),
]


def translate_url(url):
    """Apply the first matching URL_REWRITES rule, else return url unchanged."""
    for pattern, replacement in URL_REWRITES:
        if pattern.endswith("*") and replacement.endswith("*"):
            prefix = pattern[:-1]
            if url.startswith(prefix):
                return replacement[:-1] + url[len(prefix):]
    return url


def resolve_url(link_class, parameter):
    tmpl = URL_TEMPLATES.get(link_class)
    if tmpl is None:
        return None
    return translate_url(tmpl.replace("{p}", parameter.strip()))


# ---------------------------------------------------------------------------
# COPY-format parsing
# ---------------------------------------------------------------------------
_UNESCAPE = {
    "\\": "\\", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
}


def unescape(value):
    """Un-escape a single field from a PostgreSQL COPY (text format) line."""
    if value == "\\N":
        return None
    if "\\" not in value:
        return value
    out = []
    i = 0
    n = len(value)
    while i < n:
        c = value[i]
        if c == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt in _UNESCAPE:
                out.append(_UNESCAPE[nxt])
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def load_database(sql_path, db_path):
    # Build into a scratch file and move it into place only once the load has
    # committed.  journal_mode=OFF below trades the rollback journal for speed,
    # so a load interrupted part-way (Ctrl-C, OOM) cannot be rolled back: were
    # we writing db_path directly it would be left with the previous build
    # dropped, half the tables empty and its freelist inconsistent -- and, being
    # a perfectly openable file, every later --skip-load run would read it as if
    # it were good and export nothing.  Renaming makes the db valid or absent.
    tmp_path = db_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    conn = sqlite3.connect(tmp_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    cur = conn.cursor()

    for table, cols in TABLES.items():
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.execute(f"CREATE TABLE {table} ({', '.join(cols)})")

    remaining = dict(TABLES)  # tables we still need to find in the dump
    current = None            # (table, [source col indices], cols)
    batch = []
    inserted = {t: 0 for t in TABLES}

    def flush():
        if current and batch:
            table = current[0]
            placeholders = ", ".join("?" * len(current[2]))
            cur.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})", batch
            )
            inserted[table] += len(batch)
        batch.clear()

    t0 = time.time()
    with open(sql_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if current is None:
                if line.startswith("COPY public.") and remaining:
                    # Is this one of the tables we care about?
                    name = line[len("COPY public."):line.index(" (")]
                    if name in remaining:
                        all_cols = line[line.index("(") + 1:line.index(")")].split(", ")
                        wanted = TABLES[name]
                        idx = [all_cols.index(c) for c in wanted]
                        current = (name, idx, wanted)
                continue

            # Inside a COPY block for a wanted table.
            if line.startswith("\\."):
                flush()
                del remaining[current[0]]
                current = None
                if not remaining:
                    break
                continue

            raw = line.rstrip("\n").split("\t")
            idx = current[1]
            batch.append(tuple(unescape(raw[i]) for i in idx))
            if len(batch) >= 20000:
                flush()

    flush()
    conn.commit()

    dt = time.time() - t0
    print(f"Loaded dump in {dt:.1f}s:", file=sys.stderr)
    for t, n in inserted.items():
        print(f"  {n:>8} {t}", file=sys.stderr)
    if remaining:
        print(f"  WARNING: tables not found in dump: {', '.join(remaining)}",
              file=sys.stderr)

    build_indexes(cur)
    conn.commit()
    conn.close()
    os.replace(tmp_path, db_path)
    return sqlite3.connect(db_path)


def build_indexes(cur):
    for stmt in [
        "CREATE INDEX ix_pan_prod ON productions_production_author_nicks(production_id)",
        "CREATE INDEX ix_paan_prod ON productions_production_author_affiliation_nicks(production_id)",
        "CREATE INDEX ix_nick_id ON demoscene_nick(id)",
        "CREATE INDEX ix_rel_id ON demoscene_releaser(id)",
        "CREATE INDEX ix_ptypes_prod ON productions_production_types(production_id)",
        "CREATE INDEX ix_ptype_id ON productions_productiontype(id)",
        "CREATE INDEX ix_pplat_prod ON productions_production_platforms(production_id)",
        "CREATE INDEX ix_plat_id ON platforms_platform(id)",
        "CREATE INDEX ix_cp_prod ON parties_competitionplacing(production_id)",
        "CREATE INDEX ix_comp_id ON parties_competition(id)",
        "CREATE INDEX ix_pr_prod ON parties_party_releases(production_id)",
        "CREATE INDEX ix_party_id ON parties_party(id)",
        "CREATE INDEX ix_link_prod ON productions_productionlink(production_id)",
        "CREATE INDEX ix_ti_obj ON taggit_taggeditem(object_id, content_type_id)",
        "CREATE INDEX ix_tag_id ON taggit_tag(id)",
    ]:
        cur.execute(stmt)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def fmt_date(date, precision):
    """Render a Demozoo (date, precision) pair the way the db format wants."""
    if not date:
        return ""
    # date is ISO 'YYYY-MM-DD'
    if precision == "y":
        return date[:4]
    if precision == "m":
        return date[:7]
    return date  # 'd' or anything else -> full date


def clean(value):
    """Strip the separators the db format reserves out of a free-text value."""
    return value.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def scalar_maps(cur):
    """Pre-load small lookup tables into dicts for fast per-row assembly."""
    nick_name = {}
    nick_releaser = {}
    for nid, rel, name in cur.execute(
            "SELECT id, releaser_id, name FROM demoscene_nick"):
        nick_name[nid] = name
        nick_releaser[nid] = rel
    is_group = {}
    for rid, g in cur.execute("SELECT id, is_group FROM demoscene_releaser"):
        is_group[rid] = (g == "t")
    ptype_name = {}
    for pid, name in cur.execute("SELECT id, name FROM productions_productiontype"):
        ptype_name[pid] = name
    platform_name = {}  # whitelisted platforms only; others are absent
    for pid, name in cur.execute("SELECT id, name FROM platforms_platform"):
        name = PLATFORM_NAMES.get(name, name)
        if platform_allowed(name):
            platform_name[pid] = name
    party_name = {}
    for pid, name in cur.execute("SELECT id, name FROM parties_party"):
        party_name[pid] = name
    tag_name = {}
    for tid, name in cur.execute("SELECT id, name FROM taggit_tag"):
        tag_name[tid] = name
    return (nick_name, nick_releaser, is_group, ptype_name, platform_name,
            party_name, tag_name)


def group_multimap(cur, table, key_col, val_col):
    """Return {key: [val, ...]} preserving insertion order."""
    d = {}
    for k, v in cur.execute(f"SELECT {key_col}, {val_col} FROM {table}"):
        d.setdefault(k, []).append(v)
    return d


def export(conn, out_path):
    cur = conn.cursor()
    (nick_name, nick_releaser, is_group, ptype_name,
     platform_name, party_name, tag_name) = scalar_maps(cur)

    author_nicks = group_multimap(
        cur, "productions_production_author_nicks", "production_id", "nick_id")
    affil_nicks = group_multimap(
        cur, "productions_production_author_affiliation_nicks",
        "production_id", "nick_id")
    prod_types = group_multimap(
        cur, "productions_production_types", "production_id", "productiontype_id")
    prod_platforms = group_multimap(
        cur, "productions_production_platforms", "production_id", "platform_id")

    # production -> party name (via competition placing, else party_releases)
    comp_party = {}  # competition_id -> party_id
    for cid, pid in cur.execute("SELECT id, party_id FROM parties_competition"):
        comp_party[cid] = pid
    prod_party = {}
    for comp_id, prod_id in cur.execute(
            "SELECT competition_id, production_id FROM parties_competitionplacing"):
        party_id = comp_party.get(comp_id)
        if party_id is not None:
            prod_party.setdefault(prod_id, party_id)
    for party_id, prod_id in cur.execute(
            "SELECT party_id, production_id FROM parties_party_releases"):
        prod_party.setdefault(prod_id, party_id)

    # production -> tags
    prod_tags = {}
    for tag_id, obj_id, ct in cur.execute(
            "SELECT tag_id, object_id, content_type_id FROM taggit_taggeditem"):
        if ct == PRODUCTION_CONTENT_TYPE:
            prod_tags.setdefault(obj_id, []).append(tag_id)

    # production -> every url we can resolve, best first.  demarc downloads the
    # first entry (or, for a disk-image set, all of them), so ordering decides
    # what a release actually launches; the rest are fallbacks.
    prod_links = {}
    for prod_id, link_class, parameter, is_dl in cur.execute(
            "SELECT production_id, link_class, parameter, is_download_link "
            "FROM productions_productionlink"):
        # Skip the external-link half of the table: those are pages about the
        # release (Pouet, csdb, Youtube, ...), not the release itself.
        if is_dl != "t":
            continue
        rank = URL_RANK.get(link_class)
        if rank is None:
            continue
        url = resolve_url(link_class, parameter)
        if url:
            prod_links.setdefault(prod_id, []).append((rank, url))

    prod_urls = {}
    blacklisted = set()  # had downloads, but every one of them was blacklisted
    for prod_id, links in prod_links.items():
        seen = set()
        urls = []
        for _, url in sorted(links, key=lambda l: l[0]):
            if url not in seen:
                seen.add(url)
                if download_allowed(url):
                    urls.append(url)
        if urls:
            prod_urls[prod_id] = ";".join(urls)
        else:
            blacklisted.add(prod_id)

    def join_names(names):
        # de-dupe, keep order; joined with ' & ' like bitworld bylines.
        seen = set()
        return " & ".join(n for n in names if not (n in seen or seen.add(n)))

    def group_string(prod_id):
        # Prefer explicit affiliation groups; otherwise author nicks that are
        # themselves groups.
        names = []
        for nid in affil_nicks.get(prod_id, []):
            nm = nick_name.get(nid)
            if nm:
                names.append(nm)
        if not names:
            for nid in author_nicks.get(prod_id, []):
                rel = nick_releaser.get(nid)
                if rel is not None and is_group.get(rel):
                    nm = nick_name.get(nid)
                    if nm:
                        names.append(nm)
        return join_names(names)

    def person_string(prod_id):
        # Author nicks belonging to a scener rather than a group.
        names = []
        for nid in author_nicks.get(prod_id, []):
            rel = nick_releaser.get(nid)
            if rel is not None and is_group.get(rel):
                continue
            nm = nick_name.get(nid)
            if nm:
                names.append(nm)
        return join_names(names)

    def author_string(prod_id, supertype):
        # A graphics entry is the work of the artist who drew it, so credit
        # them; demos and music keep the group byline.  An entry credited to
        # a group alone still falls back to that group.
        if supertype == "graphics" or supertype == "music" :
            return person_string(prod_id) or group_string(prod_id)
        return group_string(prod_id)

    n = 0
    skipped_platform = 0
    skipped_download = 0
    platformless = 0
    # Same reasoning as the db: write beside out_path and rename, so a run that
    # produces nothing (an empty or truncated db read back with --skip-load)
    # cannot replace a good export with an empty file for `just demozoo` to
    # gzip over the previous one.
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as out:
        # Prose comments, not a `key:value` header: Demozoo is multi-platform,
        # so the platform is per line rather than set once for the whole file.
        out.write("# Demozoo release database (https://demozoo.org/)\n")
        out.write("# id title author date party platform category tags download\n")
        for prod_id, title, date, precision, supertype in cur.execute(
                "SELECT id, title, release_date_date, release_date_precision, "
                "supertype FROM productions_production ORDER BY id"):
            raw_platforms = prod_platforms.get(prod_id, [])
            platforms = [platform_name[p] for p in raw_platforms
                         if p in platform_name]
            if not platforms:
                # No platform *at all* is Demozoo not recording one (normal for
                # graphics and music); a platform we filtered out is a machine
                # we deliberately do not export, so that one still goes.  The
                # url check keeps out the platformless entries demarc could not
                # do anything with anyway.
                if (raw_platforms or supertype not in PLATFORMLESS_SUPERTYPES
                        or prod_id not in prod_urls):
                    skipped_platform += 1
                    continue
                platformless += 1
            if prod_id in blacklisted:
                skipped_download += 1
                continue
            fields = [
                ("id", str(prod_id)),
                ("title", title or ""),
                ("author", author_string(prod_id, supertype)),
                ("date", fmt_date(date, precision)),
                ("party", party_name.get(prod_party.get(prod_id), "") or ""),
                ("platform", ";".join(platforms)),
                ("category", ";".join(
                    ptype_name.get(t, "") for t in prod_types.get(prod_id, [])
                    if ptype_name.get(t)
                )),
                ("tags", ";".join(
                    sorted(tag_name.get(t, "") for t in prod_tags.get(prod_id, [])
                           if tag_name.get(t))
                )),
                ("download", prod_urls.get(prod_id, "")),
            ]
            out.write("\t".join(f"{key}:{clean(val)}" for key, val in fields) + "\n")
            n += 1

    if n == 0:
        os.remove(tmp_path)
        raise SystemExit(
            f"error: no releases to export -- {skipped_platform} productions "
            f"were off-whitelist and {skipped_download} had only blacklisted "
            f"downloads.  If all three counts are zero the database is empty; "
            f"rebuild it without --skip-load."
        )
    os.replace(tmp_path, out_path)

    print(f"Wrote {n} releases to {out_path} "
          f"({platformless} of them without a platform; skipped "
          f"{skipped_platform} off-whitelist platforms, "
          f"{skipped_download} blacklisted downloads)", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sql", default="demozoo-export.sql",
                    help="Demozoo PostgreSQL dump (default: demozoo-export.sql)")
    ap.add_argument("--db", default="demozoo.sqlite",
                    help="SQLite database to build/use (default: demozoo.sqlite)")
    ap.add_argument("--out", default="demozoo.txt",
                    help="output file (default: demozoo.txt)")
    ap.add_argument("--skip-load", action="store_true",
                    help="reuse an existing --db instead of rebuilding it")
    args = ap.parse_args(argv)

    if args.skip_load:
        conn = sqlite3.connect(args.db)
    else:
        conn = load_database(args.sql, args.db)
    export(conn, args.out)
    conn.close()


if __name__ == "__main__":
    main()
