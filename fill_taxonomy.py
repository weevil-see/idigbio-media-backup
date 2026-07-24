#!/usr/bin/env python3
"""Resolve classifications against the TaxonWorks API for the gallery to use.

Writes taxonomy/taxonworks.csv, which make_gallery.py reads via --taxonomy. The
archive in dwca/ is opened read-only and never modified: everything learned here
lands in a separate file, and the gallery merges it at build time while keeping
each record's provenance (archive / mixed / external).

    python3 fill_taxonomy.py --missing-only     # prompts for the token
    python3 make_gallery.py --taxonomy taxonomy/taxonworks.csv

Credentials are never hard-coded and never committed: they come from a
git-ignored api.yml if one exists, otherwise you are prompted (getpass, so it
does not echo) and the file is written for you. --project-token exists for
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
import collections
import csv
import re
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

# Latin gender agreement: an epithet takes the gender of its genus, so one name
# has up to three written forms. Each tuple is one declension class; a form is
# swapped for its siblings, leaving the stem untouched.
GENDER_CLASSES = [("us", "a", "um"), ("er", "era", "erum"), ("er", "ra", "rum"),
                  ("is", "e"), ("or", "rix")]


def gender_forms(epithet):
    """Every gendered spelling of one epithet, itself included."""
    forms = {epithet}
    for endings in GENDER_CLASSES:
        for ending in endings:
            if epithet.endswith(ending) and len(epithet) > len(ending) + 2:
                stem = epithet[: -len(ending)]
                forms.update(stem + other for other in endings)
    return forms

# Credentials live in a git-ignored api.yml, looked for in the dataset directory
# first and then next to the scripts, so one file can serve every dataset or a
# dataset can carry its own:
#
#     ---
#     url: https://sfg.taxonworks.org/api/v1
#     project_token: <token>
#
CONFIG_FILE = "api.yml"


def read_config(root):
    """{key: value} from the first api.yml found, plus where it came from.

    Deliberately a two-line parser rather than a YAML dependency: the file is a
    flat set of `key: value` pairs and nothing here needs more than that.
    """
    for directory in (root, HERE):
        path = os.path.join(directory, CONFIG_FILE)
        if not os.path.exists(path):
            continue
        config = {}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith(("#", "---")):
                    continue
                key, sep, value = line.partition(":")
                if sep:
                    config[key.strip()] = value.strip().strip("'\"")
        return config, path
    return {}, ""


def write_config(root, url, token):
    """Create api.yml, readable only by its owner."""
    path = os.path.join(root, CONFIG_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("---\n")
        fh.write(f"url: {url}\n")
        fh.write(f"project_token: {token}\n")
    os.chmod(path, 0o600)
    return path


# TaxonWorks rank_string tail -> the column the gallery expects. Derived from
# the rank ladder, so the two cannot drift apart: a rank named there is captured
# here automatically.
RANK_MAP = {rank.split(":", 1)[1].lower(): rank for rank in dl.TAXON_RANKS}
# TaxonWorks spells a few ranks differently from the ladder.
RANK_MAP["classrank"] = "dwc:class"
RANK_MAP["orderrank"] = "dwc:order"
RANK_MAP["species"] = "dwc:specificEpithet"
RANK_MAP["subspecies"] = "dwc:infraspecificEpithet"
RANK_MAP["variety"] = "dwc:infraspecificEpithet"
RANK_MAP["form"] = "dwc:infraspecificEpithet"

# Ranks TaxonWorks is allowed to supply: everything above the species epithet.
# The epithet and dwc:scientificName stay exactly as published -- an external
# database fills in the scaffold above a name, it does not rename the specimen.
FILLABLE_RANKS = [r for r in dl.TAXON_RANKS
                  if r not in ("dwc:specificEpithet",
                               "dwc:infraspecificEpithet")]

# Keyed on coreid, one row per specimen. Keying on the name would be wrong:
# iDigBio shortens dwc:scientificName to the genus whenever it cannot match the
# species, so 'diabrotica' alone covers 98 distinct taxa across 5,634 records.
OUT_COLUMNS = (["coreid", "queried_name", "taxonworks_name", "matched_name",
                "taxonworks_rank", "matched_via", "similarity", "taxonworks_id"]
               + [c.split(":", 1)[1] for c in FILLABLE_RANKS] + ["source"])


class Transient(Exception):
    """The API did not answer. The name is left unqueried, not marked absent."""


class TaxonWorks:
    def __init__(self, base, token, project_id=None, user_token=None, pause=0.2,
                 timeout=45.0, retries=4):
        self.base = base.rstrip("/")
        self.pause = pause
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = dl.USER_AGENT
        self.auth = {"project_token": token} if token else {}
        if user_token:
            self.auth = {"token": user_token, "project_id": project_id}
        self.names = {}      # id -> record, so a shared ancestry is fetched once
        self.new_names = 0   # how many of those were fetched this run
        self.calls = 0

    def get(self, path, **params):
        params.update(self.auth)
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(f"{self.base}{path}", params=params,
                                            timeout=self.timeout)
            except requests.RequestException as error:
                # A read timeout or dropped connection is not an answer about
                # the name -- back off and ask again rather than let one blip
                # end a run of thousands.
                if attempt == self.retries:
                    raise Transient(f"{type(error).__name__}: {error}") from error
                time.sleep(min(2 ** attempt, 30))
                continue
            self.calls += 1
            if response.status_code == 401:
                sys.exit("TaxonWorks returned 401 -- check the project token "
                         "(see --help for where to get one)")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.retries:
                    raise Transient(f"HTTP {response.status_code}")
                time.sleep(min(2 ** attempt, 30))
                continue
            if response.status_code == 404:
                return None
            response.raise_for_status()
            time.sleep(self.pause)
            return response.json()
        raise Transient("retries exhausted")

    def by_id(self, taxon_id):
        key = str(taxon_id)
        if key not in self.names:
            self.names[key] = self.get(f"/taxon_names/{taxon_id}") or {}
            self.new_names += 1
        return self.names[key]

    def search(self, name):
        """Best exact match for a name, plus what else it could have been."""
        found = self.get("/taxon_names", name=name, name_exact="true", per=10)
        if isinstance(found, dict):
            found = found.get("taxon_names") or found.get("data") or []
        if not found:
            return None, 0
        candidates = len(found)
        # Prefer a valid name over a synonym when the API tells us which is which.
        found.sort(key=lambda r: (bool(r.get("cached_is_valid") is False),
                                  len(r.get("cached") or "")))
        return found[0], candidates

    def gender_variant(self, name):
        """The same epithet agreeing with a different genus, or None.

        Moving a species to a genus of another gender changes the ending of the
        epithet and nothing else: albidus, albida, albidum are one name. That is
        a rule, not a resemblance, so matching on it cannot confuse two species
        the way a similarity score does -- carinatus and ecarinatus have
        different stems and never meet.
        """
        # The API matches substrings, so a misspelling returns nothing at all.
        # Probe with progressively shorter keys until some names come back, then
        # score those against the full string.
        probes = [name]
        first = name.split()[0]
        if first != name:
            probes.append(first)
        if len(first) > 5:
            probes.append(first[:5])
        found = []
        for probe in probes:
            found = self.get("/taxon_names", name=probe, per=50)
            if isinstance(found, dict):
                found = found.get("taxon_names") or found.get("data") or []
            if found:
                break
        parts = name.split()
        if len(parts) < 2:
            return None, ""
        wanted = set(gender_forms(parts[1].lower()))
        if not wanted:
            return None, ""
        for candidate in found or []:
            label = candidate.get("cached") or candidate.get("name") or ""
            words = label.split()
            if len(words) < 2:
                continue
            # The genus must still be the one asked about: a different genus
            # would replace every rank this fills, so it is never accepted.
            if words[0].lower() != parts[0].lower():
                continue
            if words[1].lower() in wanted and words[1].lower() != parts[1].lower():
                return candidate, words[1].lower()
        return None, ""

    def current(self, record):
        """The name in use now, when the match is a synonym or old combination.

        A published name is often not the current one -- Pandeleteius
        subtropicus Fall, 1907 is now Scalaventer subtropicus (Fall, 1907) --
        and the classification worth showing is the current one. The published
        name is never touched; this is recorded beside it.
        """
        if record.get("cached_is_valid") is False:
            valid_id = record.get("cached_valid_taxon_name_id")
            if valid_id and valid_id != record.get("id"):
                valid = self.by_id(valid_id)
                if valid:
                    return valid
        return record

    def lineage(self, record):
        """Walk parent_id upward, returning every (rank, name) pair found.

        The raw pairs are kept rather than only the mapped ones: RANK_MAP decides
        which ranks the gallery uses, and that list has grown before. Caching the
        lineage as returned means widening it later re-maps from cache instead of
        re-querying the API.
        """
        pairs, guard = [], 0
        while record and guard < 40:
            # A stall part-way up returns what was gathered so far, which is
            # still a valid partial lineage.
            guard += 1
            tail = (record.get("rank_string") or "").rsplit("::", 1)[-1].lower()
            name = (record.get("name") or "").strip()
            # A species' own `name` is the epithet; higher ranks are uninomial.
            if tail and name:
                pairs.append([tail, name])
            parent = record.get("parent_id")
            if not parent:
                break
            try:
                record = self.by_id(parent)
            except Transient:
                break
        return pairs


# Open nomenclature: a name deliberately left unresolved by the identifier.
# These cannot match anything and are not failures -- they are reported apart
# from names that are simply absent from the project.
OPEN_NOMENCLATURE = re.compile(
    r"(^|\s)(sp|spp|cf|aff|nr|indet|incertae|nov|near)\b\.?|[?]", re.IGNORECASE)

REPORT_COLUMNS = ["status", "queried_name", "sent_to_api", "matched_via",
                  "taxonworks_name", "matched_name", "is_synonym", "similarity",
                  "taxonworks_rank", "taxonworks_id", "candidates",
                  "ranks_filled", "lineage", "specimens", "media_files"]


UNMAPPED_RANKS = collections.Counter()


def save_json(path, data):
    """Checkpoint write: atomic, and tolerant of its directory having gone away.

    Written to a temporary file and renamed, so an interrupt mid-write leaves
    the previous complete file rather than a truncated one -- the whole point of
    checkpointing is that stopping never costs more than the last interval.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=0)
    os.replace(tmp, path)


def write_output(path, cache, specimens):
    """Write taxonworks.csv from whatever is resolved so far.

    Called at every checkpoint, not only at the end: a resolve takes hours, and
    a run that is interrupted should still leave the gallery something to merge
    rather than nothing.
    """
    written = 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        for coreid, name in specimens:
            entry = cache.get(name)
            ranks = ranks_of(entry)
            if not ranks:
                continue
            row = {"coreid": coreid,
                   "queried_name": name,
                   # The name in use now where it differs from the one matched,
                   # both recorded. Neither replaces dwc:scientificName.
                   "taxonworks_name": (entry.get("current_name")
                                       or entry["matched_name"]),
                   "matched_name": entry["matched_name"],
                   "taxonworks_rank": entry["matched_rank"],
                   # 'genus': the species did not match and the record was placed
                   # by its genus alone -- said plainly rather than implying a
                   # species-level identification.
                   "matched_via": entry.get("via", ""),
                   "similarity": entry.get("ratio", ""),
                   "taxonworks_id": entry.get("current_id")
                                    or entry["taxonworks_id"],
                   "source": "taxonworks"}
            for column, value in ranks.items():
                if column in FILLABLE_RANKS:
                    row[column.split(":", 1)[1]] = value
            writer.writerow(row)
            written += 1
    os.replace(tmp, path)     # readers never see a half-written file
    return written


def ranks_of(entry, note_unmapped=False):
    """Darwin Core ranks for a cache entry, re-mapped from the raw lineage.

    `note_unmapped` is set only by the report, which passes over each name once.
    Counting on every call would multiply the tally by the number of checkpoints.
    """
    if not entry:
        return {}
    pairs = entry.get("lineage_raw")
    if pairs is None:                      # written before lineages were cached
        return entry.get("ranks") or {}
    ranks = {}
    for tail, value in pairs:
        column = RANK_MAP.get(tail)
        if column is None:
            if note_unmapped and tail not in ("", "nomenclaturalrank"):
                UNMAPPED_RANKS[tail] += 1      # reported, never dropped in silence
            continue
        if column not in ranks:
            ranks[column] = value
    return ranks


def classify(name, entry):
    """What 'matched' means for one queried name."""
    if entry is None:
        return "not-queried"
    if ranks_of(entry):
        return {"genus": "matched-via-genus",
                "gender": "matched-gender-variant"}.get(entry.get("via"), "matched")
    if OPEN_NOMENCLATURE.search(name):
        return "open-nomenclature"
    if not normalise_name(name):
        return "unparsable"
    return "absent-from-project"


def write_report(path, cache, specimens, media_counts):
    """Per-name account of what the API returned, and what it did not.

    A name absent from TaxonWorks is an ordinary outcome -- a project covers the
    groups it curates, not all of nomenclature -- so absence is reported as its
    own status rather than folded into failure.
    """
    UNMAPPED_RANKS.clear()
    by_name = {}
    for coreid, name in specimens:
        record = by_name.setdefault(name, {"specimens": 0, "media": 0})
        record["specimens"] += 1
        record["media"] += media_counts.get(coreid, 0)

    rows = []
    for name, counts in by_name.items():
        entry = cache.get(name)
        status = classify(name, entry)
        ranks = ranks_of(entry, note_unmapped=True)
        rows.append({
            "status": status,
            "queried_name": name,
            "sent_to_api": (entry or {}).get("sent", normalise_name(name)),
            "matched_via": (entry or {}).get("via", ""),
            "similarity": (entry or {}).get("ratio", ""),
            "taxonworks_name": ((entry or {}).get("current_name")
                                or (entry or {}).get("matched_name", "")),
            "matched_name": (entry or {}).get("matched_name", ""),
            "is_synonym": "yes" if (entry or {}).get("is_synonym") else "",
            "taxonworks_rank": (entry or {}).get("matched_rank", ""),
            "taxonworks_id": (entry or {}).get("taxonworks_id", ""),
            "candidates": (entry or {}).get("candidates", ""),
            "ranks_filled": " ".join(sorted(r.split(":", 1)[1] for r in ranks)),
            "lineage": " > ".join(
                ranks[r] for r in dl.TAXON_RANKS if ranks.get(r)),
            "specimens": counts["specimens"],
            "media_files": counts["media"],
        })
    order = {"matched": 0, "matched-gender-variant": 1, "matched-via-genus": 2,
             "absent-from-project": 3, "open-nomenclature": 4, "unparsable": 5,
             "not-queried": 6}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -r["media_files"],
                             r["queried_name"]))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def summarise(rows):
    """Print what the report says, so the run explains itself."""
    tally = {}
    for row in rows:
        key = row["status"]
        tally[key] = tally.get(key, [0, 0])
        tally[key][0] += 1
        tally[key][1] += row["media_files"]
    total_names = sum(v[0] for v in tally.values()) or 1
    print("\nMatching:")
    for status in sorted(tally, key=lambda s: -tally[s][0]):
        names, media = tally[status]
        print(f"  {status:22} {names:6,} names ({100 * names / total_names:4.1f}%)"
              f"  {media:7,} media files")

    matched = [r for r in rows if r["status"].startswith("matched")]
    variants = [r for r in rows if r["status"] == "matched-gender-variant"]
    if variants:
        print(f"\n  {len(variants):,} matched as a gender variant:")
        for row in variants[:5]:
            print(f"    {row['queried_name']} -> {row['taxonworks_name']}")
    renamed = [r for r in matched if r.get("is_synonym")]
    if renamed:
        print(f"  {len(renamed):,} names resolved to a different current "
              f"combination, e.g.")
        for row in renamed[:3]:
            print(f"    {row['matched_name']} -> {row['taxonworks_name']}")
    if matched:
        ranks = {}
        for row in matched:
            for rank in row["ranks_filled"].split():
                ranks[rank] = ranks.get(rank, 0) + 1
        print("  ranks supplied: " + ", ".join(
            f"{r} {n:,}" for r, n in sorted(ranks.items(), key=lambda kv: -kv[1])))
        ambiguous = [r for r in matched
                     if str(r["candidates"]).isdigit() and int(r["candidates"]) > 1]
        if ambiguous:
            print(f"  {len(ambiguous):,} matched names had more than one candidate; "
                  f"the valid one was preferred")

    if UNMAPPED_RANKS:
        print("\n  WARNING: ranks TaxonWorks returned that this ladder has no "
              "column for,\n           so they are not in the tree:")
        for tail, count in UNMAPPED_RANKS.most_common(8):
            print(f"    {count:5,}x  {tail}")

    missing = [r for r in rows if r["status"] == "absent-from-project"][:5]
    if missing:
        print("\n  most-photographed names not in the project:")
        for row in missing:
            print(f"    {row['media_files']:5,} files  {row['queried_name']}")


def normalise_name(name):
    """Strip authorities, leaving the bare binomial/trinomial.

    Publishers write 'Acalles basalis LeConte 1876' or 'Alluria spinosa
    (Fabricius, 1801)'; TaxonWorks matches on the name alone, so the authority
    has to come off or nothing matches. Under both codes the genus is
    capitalised and every epithet is lower case, while an author is
    capitalised -- so keep the leading capitalised word plus the lower-case
    words that follow it, and stop at the next capital.
    """
    text = re.sub(r"[(\[].*?[)\]]", " ", name or "")   # parenthetical authors
    tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    if not tokens:
        return ""
    kept = [tokens[0]]
    for token in tokens[1:]:
        # An epithet is lower case; anything capitalised starts the authority.
        if token[:1].isupper() or not token[:1].isalpha():
            break
        kept.append(token)
    return " ".join(kept[:3])       # genus + species + subspecies at most


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


def specimens_to_resolve(archive_dir, missing_only, verbatim, required=()):
    """[(coreid, name)] per specimen, plus the distinct names to query."""
    items = dl.gather(archive_dir, dl.SCOPE_TYPES, verbatim=verbatim)
    specimens, media = {}, {}
    for item in items:
        media[item["coreid"]] = media.get(item["coreid"], 0) + 1
        if item["coreid"] in specimens:
            continue
        if missing_only and all(item.get(r) for r in required):
            continue      # already placed at every rank asked for
        name = query_name(item)
        if name:
            specimens[item["coreid"]] = name
    # Most-photographed specimens first, so an interrupted run has covered the
    # names that matter most to the gallery.
    order = sorted(specimens, key=lambda c: (-media[c], specimens[c]))
    names = sorted({specimens[c] for c in order})
    return [(c, specimens[c]) for c in order], names, len(items), media


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=dl.default_root(),
                        help="dataset directory holding dwca/ (default: the "
                             "current directory if it has one)")
    parser.add_argument("--archive", help="override the DwC-A directory")
    parser.add_argument("--out", help="override the output directory")
    parser.add_argument("--base-url",
                        help=f"API base; overrides api.yml "
                             f"(default: {DEFAULT_BASE})")
    parser.add_argument("--project-token",
                        help=f"TaxonWorks project token; overrides {CONFIG_FILE}. "
                             f"Omit it and {CONFIG_FILE} is used if present, "
                             f"otherwise you are prompted and it is written")
    parser.add_argument("--user-token",
                        help="user token instead of a project token; "
                             "requires --project-id")
    parser.add_argument("--project-id", help="project id, with --user-token")
    parser.add_argument("--missing-only", action="store_true",
                        help="skip specimens that already have every rank in "
                             "--missing-ranks")
    parser.add_argument("--missing-ranks", default="family,genus,tribe,subfamily",
                        help="ranks --missing-only checks for "
                             "(default: family,genus,tribe,subfamily)")
    parser.add_argument("--limit", type=int, help="stop after N names (for trying it)")
    parser.add_argument("--pause", type=float, default=0.2,
                        help="seconds between calls (default: 0.2)")
    parser.add_argument("--timeout", type=float, default=45.0,
                        help="per-request timeout in seconds (default: 45)")
    parser.add_argument("--retries", type=int, default=4,
                        help="attempts per request before giving up (default: 4)")
    parser.add_argument("--no-verbatim", action="store_true",
                        help="ignore occurrence_raw.csv when deciding what is missing")
    parser.add_argument("--gender-variants", action="store_true",
                        help="when an exact match fails, accept the same epithet "
                             "spelled for a genus of another gender (albidus / "
                             "albida / albidum). Same genus only")
    parser.add_argument("--retry-misses", action="store_true",
                        help="re-query names cached as unmatched (after a "
                             "matching improvement), keeping the hits")
    parser.add_argument("--report-only", action="store_true",
                        help="write the match report from the existing cache "
                             "and exit; makes no API calls, so it can be run "
                             "while a resolve is still going")
    parser.add_argument("--list-only", action="store_true",
                        help="print what would be queried and exit; no network")
    args = parser.parse_args()
    args.archive = args.archive or os.path.join(args.root, "dwca")
    args.out = args.out or os.path.join(args.root, "taxonomy")

    required = [r for r in dl.TAXON_RANKS
                if r.split(":", 1)[1].lower()
                in {x.strip().lower() for x in args.missing_ranks.split(",") if x.strip()}]
    if dl.interactive() and not (args.report_only or args.list_only):
        # Only ask about what was not already decided on the command line.
        print("Options (press Enter to accept each default):")
        args.report_only = dl.ask_yes_no(
            "only report on the existing cache, without querying", False)
        if not args.report_only:
            if not args.retry_misses:
                args.retry_misses = dl.ask_yes_no(
                    "re-query names previously found absent", False)
            if args.limit is None:
                args.limit = dl.ask_int("stop after how many specimens", None)
        print()

    specimens, names, total, media_counts = specimens_to_resolve(
        args.archive, args.missing_only, not args.no_verbatim, required)
    if args.limit:
        specimens = specimens[: args.limit]
        names = sorted({n for _, n in specimens})
    print(f"{len(specimens):,} specimens, {len(names):,} distinct names "
          f"(covering {total:,} media records)")
    if args.list_only:
        for name in names[:40]:
            print("  ", name)
        return 0

    cache_path_early = os.path.join(args.out, "taxonworks_cache.json")
    if args.report_only:
        if not os.path.exists(cache_path_early):
            sys.exit(f"no cache at {cache_path_early} -- nothing to report on")
        with open(cache_path_early, encoding="utf-8") as fh:
            cached = json.load(fh)
        report_path = os.path.join(args.out, "match_report.csv")
        summarise(write_report(report_path, cached, specimens, media_counts))
        print(f"\nReport -> {report_path}  (from {len(cached):,} cached names, "
              f"no API calls)")
        return 0

    config, config_path = read_config(args.root)
    base_url = args.base_url or config.get("url") or DEFAULT_BASE
    token = args.project_token or config.get("project_token", "")
    if token and not args.project_token:
        print(f"Using the credentials in {config_path}")
    if not token and not (args.user_token and args.project_id):
        # Prompted rather than kept in the file or an environment variable, so
        # it never lands in the repository or the shell history.
        print(f"\nA TaxonWorks project token is created in a project's "
              f"preferences page.\nIt will be saved to {CONFIG_FILE} "
              f"(git-ignored) so this is asked once.")
        try:
            token = getpass.getpass("TaxonWorks project token: ").strip()
        except (EOFError, KeyboardInterrupt):
            token = ""
        if not token:
            sys.exit("no token given -- nothing to do")
        try:
            print(f"  saved to {write_config(args.root, base_url, token)} "
                  f"(mode 600)")
        except OSError as error:
            print(f"  could not save {CONFIG_FILE}: {error}", file=sys.stderr)

    os.makedirs(args.out, exist_ok=True)
    cache_path = os.path.join(args.out, "taxonworks_cache.json")
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
        print(f"  {len(cache):,} names already cached")
    ancestors_path = os.path.join(args.out, "taxonworks_ancestors.json")
    ancestors = {}
    if os.path.exists(ancestors_path):
        with open(ancestors_path, encoding="utf-8") as fh:
            ancestors = json.load(fh)
        print(f"  {len(ancestors):,} ancestor records already cached")

    api = TaxonWorks(base_url, token, args.project_id,
                     args.user_token, args.pause, args.timeout, args.retries)
    api.names = ancestors
    resolved, stalled = 0, 0
    MAX_STALLED = 25
    try:
        for index, name in enumerate(names, 1):   # query each name once
            known = cache.get(name)
            if known is not None and (ranks_of(known) or not args.retry_misses):
                continue
            sent = normalise_name(name)
            try:
                record, candidates = api.search(sent) if sent else (None, 0)
            except Transient as error:
                # Leave it uncached so a later run retries it, rather than
                # recording "absent" for something the API never answered.
                stalled += 1
                print(f"  [{stalled}] {name}: {error}", flush=True)
                if stalled >= MAX_STALLED:
                    print(f"  giving up after {MAX_STALLED} unanswered requests; "
                          f"progress is saved, re-run to continue", flush=True)
                    break
                continue
            via = "name"
            stalled = 0
            if not record and sent:
                # No species-level match: place the record by its genus instead.
                # 'Curculio sp.' and 'Larinus cf. obtusus' can never match as
                # written, and a species absent from the project usually has its
                # genus present -- either way the higher ranks are recoverable,
                # which is all this fills anyway.
                genus = sent.split()[0]
                if genus and genus != sent:
                    try:
                        record, candidates = api.search(genus)
                    except Transient:
                        continue          # retried next run
                    via = "genus" if record else via
            if record:
                # Classify by the name in use now, but remember what was hit.
                accepted = api.current(record)
                cache[name] = {
                    "sent": sent,
                    "via": via,
                    "ratio": ratio,
                    "candidates": candidates,
                    "matched_name": record.get("cached") or record.get("name") or "",
                    "matched_rank": (record.get("rank_string") or "")
                                    .rsplit("::", 1)[-1],
                    "taxonworks_id": record.get("id"),
                    "current_name": (accepted.get("cached")
                                     or accepted.get("name") or ""),
                    "current_id": accepted.get("id"),
                    "is_synonym": accepted.get("id") != record.get("id"),
                    "lineage_raw": api.lineage(accepted),
                }
                resolved += 1
            else:
                cache[name] = {"sent": sent, "via": "", "ratio": ratio,
                               "candidates": candidates,
                               "matched_name": "", "matched_rank": "",
                               "taxonworks_id": "", "lineage_raw": []}
            if index % 25 == 0 or index == len(names):
                checkpoint = index % 250 == 0 or index == len(names)
                print(f"  {index:,}/{len(names):,} queried, {resolved:,} matched",
                      flush=True)
                save_json(cache_path, cache)
                if checkpoint:
                    # The CSV is regenerated whole, so do it on a coarser
                    # interval than the cache, which is cheap to rewrite.
                    write_output(os.path.join(args.out, "taxonworks.csv"),
                                 cache, specimens)
    except KeyboardInterrupt:
        print("\nInterrupted -- keeping what was resolved so far", flush=True)
    finally:
        save_json(cache_path, cache)
        save_json(ancestors_path, api.names)

    out_path = os.path.join(args.out, "taxonworks.csv")
    written = write_output(out_path, cache, specimens)

    report_path = os.path.join(args.out, "match_report.csv")
    summarise(write_report(report_path, cache, specimens, media_counts))

    print(f"\n{api.calls:,} API calls, {written:,} specimen rows -> {out_path}")
    print(f"Per-name report -> {report_path}")
    print("Nothing in dwca/ was modified.")
    print(f"Next: python3 make_gallery.py --taxonomy {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
