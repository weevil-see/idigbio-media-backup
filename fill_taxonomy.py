#!/usr/bin/env python3
"""Resolve classifications against the TaxonWorks API for the gallery to use.

Writes taxonomy/taxonworks.csv, which make_gallery.py reads via --taxonomy. The
archive in dwca/ is opened read-only and never modified: everything learned here
lands in a separate file, and the gallery merges it at build time while keeping
each record's provenance (archive / mixed / external).

    python3 fill_taxonomy.py --missing-only     # prompts for the token
    python3 make_gallery.py --taxonomy taxonomy/taxonworks.csv

The token is prompted for, never stored: it is not in this file, not read from
the environment by default, and not written to disk. --project-token exists for
automation.

Why it is worth doing: dwc:tribe is absent from the archive entirely, and
iDigBio rewrites names to senior synonyms while dropping the epithet (Hadropoda
xanthoura is indexed as genus 'aedmon', no species). TaxonWorks can supply the
rank a name sits at plus its full ancestry.

API notes (https://api.taxonworks.org, spec in SpeciesFileGroup/taxonworks_api):
  * base https://sfg.taxonworks.org/api/v1, sandbox https://sandbox.taxonworks.org/api/v1
  * TaxonWorks is a general nomenclatural workbench: all taxonomic groups, all
    the codes (ICZN, ICN, ICNP). Coverage is bounded by the project a token
    belongs to, not by taxon.
  * every endpoint needs `project_token`, or `token` + `project_id`, as query
    parameters. A project_token is explicitly not secret -- it marks a project's
    data as public -- but it is per project, so results only cover that project's
    names. Get one from your TaxonWorks project preferences.
  * GET /taxon_names?name=&name_exact=&epithet_only=  -- search
  * GET /taxon_names/{id}                             -- one name, has parent_id
  * rank comes back as rank_string, e.g.
    "NomenclaturalRank::Iczn::GenusGroup::Genus" -> Genus
"""

import argparse
import csv
import getpass
import importlib.util
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location(
    "download_media", os.path.join(HERE, "download_media.py"))
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

DEFAULT_BASE = "https://sfg.taxonworks.org/api/v1"

# TaxonWorks rank_string tail -> the Darwin Core column the gallery expects.
RANK_MAP = {
    "kingdom": "dwc:kingdom",
    "phylum": "dwc:phylum",
    "class": "dwc:class",
    "order": "dwc:order",
    "family": "dwc:family",
    "tribe": "dwc:tribe",
    "genus": "dwc:genus",
    "subgenus": "dwc:subgenus",
    "species": "dwc:specificEpithet",
}
# Ranks TaxonWorks is allowed to supply: everything above the species epithet.
# The epithet and dwc:scientificName stay exactly as published -- an external
# database fills in the scaffold above a name, it does not rename the specimen.
FILLABLE_RANKS = [r for r in dl.TAXON_RANKS if r != "dwc:specificEpithet"]

# Keyed on coreid, one row per specimen. Keying on the name would be wrong:
# iDigBio shortens dwc:scientificName to the genus whenever it cannot match the
# species, so 'diabrotica' alone covers 98 distinct taxa across 5,634 records.
OUT_COLUMNS = (["coreid", "queried_name", "taxonworks_name", "taxonworks_rank",
                "taxonworks_id"]
               + [c.split(":", 1)[1] for c in FILLABLE_RANKS] + ["source"])


class TaxonWorks:
    def __init__(self, base, token, project_id=None, user_token=None, pause=0.2):
        self.base = base.rstrip("/")
        self.pause = pause
        self.session = requests.Session()
        self.session.headers["User-Agent"] = dl.USER_AGENT
        self.auth = {"project_token": token} if token else {}
        if user_token:
            self.auth = {"token": user_token, "project_id": project_id}
        self.names = {}      # id -> record, so a shared ancestry is fetched once
        self.calls = 0

    def get(self, path, **params):
        params.update(self.auth)
        for attempt in range(1, 4):
            response = self.session.get(f"{self.base}{path}", params=params,
                                        timeout=45)
            self.calls += 1
            if response.status_code == 401:
                sys.exit("TaxonWorks returned 401 -- check the project token "
                         "(see --help for where to get one)")
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 404:
                return None
            response.raise_for_status()
            time.sleep(self.pause)
            return response.json()
        return None

    def by_id(self, taxon_id):
        if taxon_id not in self.names:
            self.names[taxon_id] = self.get(f"/taxon_names/{taxon_id}") or {}
        return self.names[taxon_id]

    def search(self, name):
        """Best exact match for a name, or None."""
        found = self.get("/taxon_names", name=name, name_exact="true", per=5)
        if isinstance(found, dict):
            found = found.get("taxon_names") or found.get("data") or []
        if not found:
            return None
        # Prefer a valid name over a synonym when the API tells us which is which.
        found.sort(key=lambda r: (bool(r.get("cached_is_valid") is False),
                                  len(r.get("cached") or "")))
        return found[0]

    def lineage(self, record):
        """Walk parent_id upward, returning {dwc column: value}."""
        ranks, guard = {}, 0
        while record and guard < 40:
            guard += 1
            tail = (record.get("rank_string") or "").rsplit("::", 1)[-1].lower()
            column = RANK_MAP.get(tail)
            if column and column not in ranks:
                # A species' own `name` is the epithet; higher ranks are uninomial.
                ranks[column] = (record.get("name") or "").strip()
            parent = record.get("parent_id")
            if not parent:
                break
            record = self.by_id(parent)
        return {k: v for k, v in ranks.items() if v}


def query_name(item):
    """The best name to ask TaxonWorks about, in order of trustworthiness.

    The publisher's verbatim name first (2,782 distinct here, 10 ambiguous),
    then a genus/epithet pair rebuilt from the ranks, and only as a last resort
    the indexed name, which may have been shortened to a bare genus.
    """
    verbatim = (item.get(dl.VERBATIM_NAME) or "").strip()
    if verbatim:
        return verbatim
    genus = (item.get("dwc:genus") or "").strip()
    epithet = (item.get("dwc:specificEpithet") or "").strip()
    if genus and epithet:
        return f"{genus} {epithet}"
    return (item.get("dwc:scientificName") or "").strip()


def specimens_to_resolve(archive_dir, missing_only, verbatim):
    """[(coreid, name)] per specimen, plus the distinct names to query."""
    items = dl.gather(archive_dir, dl.SCOPE_TYPES, verbatim=verbatim)
    specimens, media = {}, {}
    for item in items:
        media[item["coreid"]] = media.get(item["coreid"], 0) + 1
        if item["coreid"] in specimens:
            continue
        if missing_only and all(item.get(r) for r in FILLABLE_RANKS):
            continue      # already fully placed; nothing to add
        name = query_name(item)
        if name:
            specimens[item["coreid"]] = name
    # Most-photographed specimens first, so an interrupted run has covered the
    # names that matter most to the gallery.
    order = sorted(specimens, key=lambda c: (-media[c], specimens[c]))
    names = sorted({specimens[c] for c in order})
    return [(c, specimens[c]) for c in order], names, len(items)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=dl.default_root(),
                        help="dataset directory holding dwca/ (default: the "
                             "current directory if it has one)")
    parser.add_argument("--archive", help="override the DwC-A directory")
    parser.add_argument("--out", help="override the output directory")
    parser.add_argument("--base-url", default=DEFAULT_BASE,
                        help=f"API base (default: {DEFAULT_BASE})")
    parser.add_argument("--project-token",
                        help="TaxonWorks project token. Omit it and you are "
                             "prompted; it is never written to disk or the code")
    parser.add_argument("--user-token",
                        help="user token instead of a project token; "
                             "requires --project-id")
    parser.add_argument("--project-id", help="project id, with --user-token")
    parser.add_argument("--missing-only", action="store_true",
                        help="only names lacking a tribe or epithet after the archive")
    parser.add_argument("--limit", type=int, help="stop after N names (for trying it)")
    parser.add_argument("--pause", type=float, default=0.2,
                        help="seconds between calls (default: 0.2)")
    parser.add_argument("--no-verbatim", action="store_true",
                        help="ignore occurrence_raw.csv when deciding what is missing")
    parser.add_argument("--list-only", action="store_true",
                        help="print what would be queried and exit; no network")
    args = parser.parse_args()
    args.archive = args.archive or os.path.join(args.root, "dwca")
    args.out = args.out or os.path.join(args.root, "taxonomy")

    specimens, names, total = specimens_to_resolve(
        args.archive, args.missing_only, not args.no_verbatim)
    if args.limit:
        specimens = specimens[: args.limit]
        names = sorted({n for _, n in specimens})
    print(f"{len(specimens):,} specimens, {len(names):,} distinct names "
          f"(covering {total:,} media records)")
    if args.list_only:
        for name in names[:40]:
            print("  ", name)
        return 0

    token = args.project_token
    if not token and not (args.user_token and args.project_id):
        # Prompted rather than kept in the file or an environment variable, so
        # it never lands in the repository or the shell history.
        print("\nA TaxonWorks project token is created in a project's "
              "preferences page.\nIt is project-scoped and not secret, but it is "
              "not stored here either.")
        try:
            token = getpass.getpass("TaxonWorks project token: ").strip()
        except (EOFError, KeyboardInterrupt):
            token = ""
        if not token:
            sys.exit("no token given -- nothing to do")

    os.makedirs(args.out, exist_ok=True)
    cache_path = os.path.join(args.out, "taxonworks_cache.json")
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
        print(f"  {len(cache):,} names already cached")

    api = TaxonWorks(args.base_url, token, args.project_id,
                     args.user_token, args.pause)
    resolved = 0
    try:
        for index, name in enumerate(names, 1):   # query each name once
            if name in cache:
                continue
            record = api.search(name)
            if record:
                cache[name] = {
                    "matched_name": record.get("cached") or record.get("name") or "",
                    "matched_rank": (record.get("rank_string") or "")
                                    .rsplit("::", 1)[-1],
                    "taxonworks_id": record.get("id"),
                    "ranks": api.lineage(record),
                }
                resolved += 1
            else:
                cache[name] = {"matched_name": "", "matched_rank": "",
                               "taxonworks_id": "", "ranks": {}}
            if index % 25 == 0 or index == len(names):
                print(f"  {index:,}/{len(names):,} queried, {resolved:,} matched",
                      flush=True)
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh, indent=0)
    except KeyboardInterrupt:
        print("\nInterrupted -- keeping what was resolved so far", flush=True)
    finally:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=0)

    out_path = os.path.join(args.out, "taxonworks.csv")
    written = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        for coreid, name in specimens:
            entry = cache.get(name)
            if not entry or not entry.get("ranks"):
                continue
            row = {"coreid": coreid,
                   "queried_name": name,
                   # Recorded so the gallery can show and search it. It is a
                   # voucher for how the placement was reached, never a
                   # replacement for the published dwc:scientificName.
                   "taxonworks_name": entry["matched_name"],
                   "taxonworks_rank": entry["matched_rank"],
                   "taxonworks_id": entry["taxonworks_id"],
                   "source": "taxonworks"}
            for column, value in entry["ranks"].items():
                if column in FILLABLE_RANKS:
                    row[column.split(":", 1)[1]] = value
            writer.writerow(row)
            written += 1

    print(f"\n{api.calls:,} API calls, {written:,} specimen rows -> {out_path}")
    print("Nothing in dwca/ was modified.")
    print(f"Next: python3 make_gallery.py --taxonomy {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
