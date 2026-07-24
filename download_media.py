#!/usr/bin/env python3
"""Download media files from an iDigBio Darwin Core Archive.

Either every media record, or only those belonging to occurrences whose
dwc:typeStatus is not empty -- see --scope, which is asked for interactively
when it is not given.

The archive has no core file -- occurrence.csv and multimedia.csv are both
extensions keyed on `coreid` (the iDigBio occurrence UUID), so the two are
joined on that column.

Usage:
    python3 download_media.py                    # asks which scope
    python3 download_media.py --scope all        # every media record
    python3 download_media.py --dry-run          # just write the manifest
    python3 download_media.py --limit 20         # try a small batch first
"""

import argparse
import collections
import csv
import html
import os
import queue
import re
import sys
import threading
import time
from urllib.parse import urlparse

import requests

csv.field_size_limit(10 ** 9)

HERE = os.path.dirname(os.path.abspath(__file__))

OCCURRENCE_FILE = "occurrence.csv"
OCCURRENCE_RAW_FILE = "occurrence_raw.csv"
MULTIMEDIA_FILE = "multimedia.csv"

SCOPE_TYPES = "types"   # only occurrences with a non-empty dwc:typeStatus
SCOPE_ALL = "all"       # every media record in the archive

# Key under which the publisher's own scientificName is kept, separate from
# the indexed dwc:scientificName.
VERBATIM_NAME = "verbatim:scientificName"

# Taxonomic ranks, coarse to fine. Kept as an explicit ordered list because the
# gallery builds its tree from it, and because ranks left empty by the publisher
# are meant to be fillable later from an external name-parsing source.
# The full rank ladder, coarse to fine. Darwin Core has terms for only some of
# these; the rest are prefixed `tw:` and can only ever come from an external
# source. Listing a rank costs nothing -- the gallery drops any level nothing
# fills -- so the ladder is kept complete rather than trimmed to one archive.
TAXON_RANKS = [
    "dwc:kingdom",
    "tw:subkingdom",
    "tw:infrakingdom",
    "tw:superphylum",
    "dwc:phylum",
    "tw:subphylum",
    "tw:superclass",
    "dwc:class",
    "tw:subclass",
    "tw:infraclass",
    "tw:superorder",
    "dwc:order",
    "tw:suborder",
    "tw:infraorder",
    "dwc:superfamily",
    "tw:epifamily",
    "dwc:family",
    "dwc:subfamily",
    "tw:supertribe",
    "dwc:tribe",
    "dwc:subtribe",
    "dwc:genus",
    "dwc:subgenus",
    "tw:section",
    "tw:series",
    "dwc:specificEpithet",
    "dwc:infraspecificEpithet",
]

# Geographic hierarchy, coarse to fine -- the gallery browses it like the ranks.
GEO_RANKS = [
    "dwc:continent",
    "dwc:country",
    "dwc:stateProvince",
]

# Columns copied from occurrence.csv into the manifest for context.
OCCURRENCE_CONTEXT = [
    "dwc:typeStatus",
    "dwc:catalogNumber",
    "dwc:scientificName",
    "dwc:institutionCode",
] + TAXON_RANKS + GEO_RANKS

MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
}

USER_AGENT = "idigbio-archive-media-downloader/1.0"

# Consecutive connect failures before a host is set aside.
DEAD_HOST_STRIKES = 5
# ... and for how long. A host is never written off permanently: it may recover,
# and the run has no way to tell a dead server from a blip at this end.
DEAD_HOST_COOLDOWN = 300.0
# Distinct hosts failing to connect inside OUTAGE_WINDOW for the problem to be
# diagnosed as local. Remote hosts do not fail in unison; a dropped link does.
OUTAGE_HOSTS = 3
OUTAGE_WINDOW = 60.0
OUTAGE_PAUSE = 30.0

# dwc:typeStatus is free text. Values that explicitly deny type status; kept in
# the download (they occasionally carry media) but flagged in the manifest.
NEGATIVE_TYPE_STATUS = {
    "no aplica", "no aplica.", "none", "ninguno", "nenhum", "non-type", "nontype",
    "no type", "no type!", "not a type", "not type", "no es tipo", "nao se aplica",
    "não se aplica", "not applicable", "n/a", "na", "null", "|null|", "no",
    "unknown", "-", "--",
}

TYPE_WORD = re.compile(
    r"\b("
    r"(?:holo|para|syn|lecto|para\s?lecto|neo|allo|iso|topo|co|hypo|epi|geno|plesio)?"
    r"types?|typus|tipos?|typen|t[ií]pico"
    r")\b",
    re.IGNORECASE,
)

ANCHOR = re.compile(r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
TAG = re.compile(r"<[^>]+>")
PAGE = re.compile(r"\bpage\s+([\w.-]+)", re.IGNORECASE)
ROLE_OF = re.compile(r"^(.+)\s+of\b\s*(.*)$", re.IGNORECASE)
UUID_SUFFIX = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)



def default_root():
    """Which dataset directory to work on.

    Every iDigBio DwC-A has the same layout, so one copy of these scripts serves
    any number of downloads. The dataset is whichever directory holds a dwca/ --
    the current one if it does, otherwise the one the scripts live in. --root
    overrides.
    """
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "dwca")):
        return cwd
    return HERE


def parse_type_status(value):
    """Split a dwc:typeStatus into a category plus any Arctos citation detail.

    Arctos publishes specimen citations through this field, e.g.
        voucher of <a href=".../name/bromius obscurus">bromius obscurus</a>
        in <a href=".../publication/10006537">bousquet et al. (2013)</a>
    and chains several of them with semicolons. A nomenclatural type can hide at
    the end of such a chain, so every segment is inspected, not just the first.
    """
    parsed = {"type_category": "", "citation_roles": "", "cited_taxa": "",
              "publications": "", "cited_pages": ""}
    text = html.unescape(value or "").strip()
    if not text:
        return parsed

    roles, taxa, pubs, pages = [], [], [], []
    for segment in text.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        anchors = ANCHOR.findall(segment)
        if not anchors:
            continue
        # Everything before the first link is "<role> of [<unlinked taxon>] in".
        # Split on the *last* " of " so multi-word roles survive intact
        # ("basis of illustration of x") while an unlinked taxon still separates
        # cleanly ("voucher of donacia subtilis group in <pub>").
        prefix = re.sub(r"\s+", " ",
                        TAG.sub("", segment[: segment.lower().find("<a")])).strip()
        # Greedy, so the role runs up to the *last* "of": "basis of illustration"
        # keeps its words, while "voucher of donacia subtilis group in" splits.
        match = ROLE_OF.match(prefix)
        role, trailing = match.groups() if match else (prefix, "")
        role = role.strip().lower()
        if role:
            roles.append(role)
        trailing = re.sub(r"\s+in\s*$", "", trailing, flags=re.IGNORECASE).strip()
        if trailing:
            taxa.append(trailing.lower())
        for href, label in anchors:
            label = TAG.sub("", label).strip()
            if not label:
                continue
            if "/publication/" in href.lower():
                pubs.append(label)
            elif "/name/" in href.lower():
                taxa.append(label)
        match = PAGE.search(segment)
        if match:
            pages.append(match.group(1))

    def dedupe(values):
        return " | ".join(dict.fromkeys(values))

    parsed["citation_roles"] = dedupe(roles)
    parsed["cited_taxa"] = dedupe(taxa)
    parsed["publications"] = dedupe(pubs)
    parsed["cited_pages"] = dedupe(pages)

    plain = TAG.sub(" ", text).strip().lower()
    if roles:
        # A citation record: it is type material only if a role says so.
        parsed["type_category"] = "type" if any(TYPE_WORD.search(r) for r in roles) \
            else "citation"
    elif plain in NEGATIVE_TYPE_STATUS:
        parsed["type_category"] = "negative"
    elif TYPE_WORD.search(plain):
        parsed["type_category"] = "type"
    else:
        parsed["type_category"] = "other"
    return parsed


HIGHER_SPLIT = re.compile(r"[|;,>/]+")
# Animal tribes end in -ini (Chrysomelini), subfamilies in -inae, families in
# -idae, so -ini is unambiguous. Botanical tribes end in -eae, but so do family
# (-aceae) and subfamily (-oideae) endings, which are excluded.
TRIBE_LIKE = re.compile(r"^[a-z]{3,}(ini|(?<!ac)(?<!oid)eae)$", re.IGNORECASE)


def extract_tribe(higher_classification):
    """Pull a tribe out of dwc:higherClassification, which sometimes carries one.

    The field is an unranked, delimiter-separated lineage, so the rank has to be
    inferred from the name ending -- reliable for tribes, which have a reserved
    suffix under both the zoological and botanical codes.
    """
    for part in HIGHER_SPLIT.split(higher_classification or ""):
        part = part.strip()
        if part and TRIBE_LIKE.match(part):
            return part
    return ""


def load_occurrence_context(archive_dir, wanted):
    """coreid -> context dict, for the given coreids only."""
    path = os.path.join(archive_dir, OCCURRENCE_FILE)
    context = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if "dwc:typeStatus" not in reader.fieldnames:
            sys.exit(f"{OCCURRENCE_FILE} has no dwc:typeStatus column")
        for row in reader:
            coreid = row["coreid"]
            if coreid not in wanted:
                continue
            fields = {key: (row.get(key) or "").strip()
                      for key in OCCURRENCE_CONTEXT}
            if not fields["dwc:tribe"]:
                fields["dwc:tribe"] = extract_tribe(
                    row.get("dwc:higherClassification"))
            fields.update(parse_type_status(fields["dwc:typeStatus"]))
            context[coreid] = fields
    return context


def read_media_rows(archive_dir):
    """Every media record in the archive that has an accessURI."""
    path = os.path.join(archive_dir, MULTIMEDIA_FILE)
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            url = (row.get("ac:accessURI") or "").strip()
            if not url:
                continue
            rows.append({
                "coreid": row["coreid"],
                "media_uuid": (row.get("idigbio:uuid") or "").strip(),
                "url": url,
                "format": (row.get("dcterms:format") or "").strip().lower(),
                "media_type": (row.get("idigbio:mediaType") or "").strip(),
                # Licence terms travel with the media record, not the specimen.
                "rights": (row.get("dcterms:rights") or "").strip(),
                "rights_url": (row.get("xmpRights:WebStatement") or "").strip(),
            })
    return rows


def load_verbatim_ranks(archive_dir, wanted):
    """coreid -> publisher's own rank values, from occurrence_raw.csv.

    iDigBio's indexed occurrence.csv carries its *matched* classification: when
    it resolves a name to a senior synonym but cannot place the species under
    it, it keeps the accepted genus and drops dwc:specificEpithet. Hadropoda
    xanthoura, for instance, is indexed as genus 'aedmon' with no epithet. The
    verbatim file still has both, which is worth ~37 percentage points of
    species coverage.
    """
    path = os.path.join(archive_dir, OCCURRENCE_RAW_FILE)
    if not os.path.exists(path):
        return {}
    verbatim = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        available = [r for r in TAXON_RANKS if r in (reader.fieldnames or [])]
        for row in reader:
            if row["coreid"] not in wanted:
                continue
            values = {r: (row.get(r) or "").strip().lower() for r in available}
            # The publisher's own name string, kept under a separate key so it
            # never overwrites the indexed dwc:scientificName. iDigBio shortens
            # that to the genus when it cannot match the species, which makes it
            # useless as a lookup key -- 'diabrotica' alone stands for 98 taxa.
            name = (row.get("dwc:scientificName") or "").strip()
            if name:
                values[VERBATIM_NAME] = name
            if any(values.values()):
                verbatim[row["coreid"]] = values
    return verbatim


def gather(archive_dir, scope=SCOPE_TYPES, verbatim=False):
    """Media records to download, each with its occurrence context attached.

    Media is read first so that only the occurrences actually referenced by a
    media record are held in memory -- with scope=all that is 132k rows rather
    than the archive's 509k.
    """
    media = read_media_rows(archive_dir)
    coreids = {m["coreid"] for m in media}
    context = load_occurrence_context(archive_dir, coreids)
    if verbatim:
        for coreid, values in load_verbatim_ranks(archive_dir, coreids).items():
            fields = context.get(coreid)
            if fields:
                for rank, value in values.items():
                    if value and not fields.get(rank):
                        fields[rank] = value

    items = []
    for row in media:
        fields = context.get(row["coreid"])
        has_type = bool(fields and fields.get("dwc:typeStatus"))
        if scope == SCOPE_TYPES and not has_type:
            continue
        row.update(fields or dict.fromkeys(OCCURRENCE_CONTEXT, ""))
        if not has_type:
            row["type_category"] = "no-typestatus"
        items.append(row)
    return items


# Options are asked for at startup rather than only accepted as flags, since
# these are run bare far more often than not. A flag given on the command line
# always wins, and nothing is asked when there is no terminal to answer -- cron
# and pipes get the defaults instead of blocking forever.
def interactive():
    # Both ends matter: with stdout piped the question is swallowed by the pipe
    # buffer and the run looks hung while it waits for an answer nobody saw.
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(question, default_label):
    try:
        return input(f"  {question} [{default_label}]: ").strip()
    except EOFError:
        print()
        return ""          # no more input: take the default
    except KeyboardInterrupt:
        # Interrupting the questions means "do not run", not "use the defaults".
        sys.exit("\ncancelled")


def ask_yes_no(question, default=False):
    answer = _ask(question, "Y/n" if default else "y/N").lower()
    if not answer:
        return default
    return answer[0] == "y"


def ask_int(question, default=None):
    answer = _ask(question, str(default) if default is not None else "all")
    if not answer:
        return default
    try:
        return int(answer)
    except ValueError:
        print(f"    not a number, using {default}")
        return default


def ask_scope():
    """Prompt for the download scope; used when --scope is not given."""
    print("\nWhich media should be downloaded?")
    print("  [1] type specimens only  (dwc:typeStatus is not empty)  [default]")
    print("  [2] all media in the archive")
    try:
        answer = input("Choice [1]: ").strip()
    except EOFError:
        answer = ""
    if answer in ("2", "all", "a"):
        return SCOPE_ALL
    if answer in ("", "1", "types", "t"):
        return SCOPE_TYPES
    sys.exit(f"unrecognised choice: {answer!r}")


def slugify(value, limit=60):
    """Filesystem-safe fragment: 'UAM:Ento:320406' -> 'uam_ento_320406'."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:limit].strip("_")


def target_filename(item):
    """<catalog-number>__<media-uuid>.<ext>.

    The UUID suffix is what makes the name unique -- one specimen can carry a
    dozen images, and catalogue numbers repeat across institutions.
    """
    ext = MIME_EXTENSIONS.get(item["format"], "")
    if not ext:
        url_ext = os.path.splitext(urlparse(item["url"]).path)[1].lower()
        if re.fullmatch(r"\.[a-z0-9]{2,4}", url_ext):
            ext = url_ext
    uuid = item["media_uuid"]
    if not uuid:  # no UUID: fall back to the URL, which is at least distinct
        return re.sub(r"[^A-Za-z0-9._-]", "_", item["url"])[-100:] + ext
    # ~11% of records have no catalogue number. Without a specimen-level prefix
    # their several views would not sort together, so fall back to a short form
    # of the occurrence UUID, which identifies the specimen just as well.
    prefix = slugify(item.get("dwc:catalogNumber", ""))
    if not prefix and item["coreid"]:
        prefix = "occ-" + item["coreid"][:8]
    return (f"{prefix}__{uuid}" if prefix else uuid) + ext


def download(session, item, dest, timeout, retries):
    """Fetch one file; returns (status, detail)."""
    tmp = dest + ".part"
    for attempt in range(1, retries + 1):
        try:
            with session.get(item["url"], stream=True, timeout=timeout) as response:
                # A refusal (403 from a bot filter, 404 for a dead link) will
                # not become a success on attempt two; only back off when the
                # server says it is busy.
                if 400 <= response.status_code < 500 and \
                        response.status_code not in (408, 429):
                    return "failed", f"HTTP {response.status_code}"
                response.raise_for_status()
                size = 0
                with open(tmp, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
                            size += len(chunk)
            if size == 0:
                raise OSError("empty response body")
            # Extensionless downloads: name from the served content type.
            final = dest
            if not os.path.splitext(dest)[1]:
                ctype = response.headers.get("Content-Type", "").split(";")[0].strip()
                final = dest + MIME_EXTENSIONS.get(ctype.lower(), "")
            os.replace(tmp, final)
            return "ok", os.path.basename(final)
        except Exception as exc:  # network, HTTP and disk errors alike
            if attempt == retries:
                if os.path.exists(tmp):
                    os.remove(tmp)
                return "failed", f"{type(exc).__name__}: {exc}"
            time.sleep(min(2 ** attempt, 30))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=default_root(),
                        help="dataset directory holding dwca/ (default: the "
                             "current directory if it has one)")
    parser.add_argument("--archive", help="override the DwC-A directory")
    parser.add_argument("--out", help="override where media and the manifest go")
    parser.add_argument("--scope", choices=[SCOPE_TYPES, SCOPE_ALL],
                        help="which media to fetch: 'types' (dwc:typeStatus not "
                             "empty) or 'all'. Asked interactively if omitted")
    parser.add_argument("--workers", type=int,
                        help="parallel downloads (default: 8, asked if omitted)")
    parser.add_argument("--limit", type=int,
                        help="stop after N files, for testing")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="per-request timeout in seconds (default: 60)")
    parser.add_argument("--retries", type=int, default=3,
                        help="attempts per file (default: 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="write the manifest but download nothing")
    args = parser.parse_args()
    args.archive = args.archive or os.path.join(args.root, "dwca")
    args.out = args.out or os.path.join(args.root, "media")

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    scope = args.scope
    if scope is None:
        # Non-interactive (cron, piped): fall back to the safer, smaller set
        # rather than blocking on a prompt nobody can answer.
        scope = ask_scope() if interactive() else SCOPE_TYPES

    if interactive() and (args.workers is None or args.limit is None):
        print("\nOptions (press Enter to accept each default):")
        if args.workers is None:
            args.workers = ask_int("parallel downloads", 8)
        if args.limit is None:
            args.limit = ask_int("stop after how many files", None)
        print()
    if args.workers is None:
        args.workers = 8
    if args.workers < 1:
        sys.exit("--workers must be at least 1")

    label = "type specimens only" if scope == SCOPE_TYPES else "all media"
    print(f"\nReading archive ({label}) ...", flush=True)
    items = gather(args.archive, scope)
    print(f"  {len(items):,} media records to consider", flush=True)

    for item in items:
        item["filename"] = target_filename(item)

    # Resume: skip whatever is already on disk. Matching is by media UUID
    # rather than by full filename, so changing the naming scheme (or an
    # extension resolved from the Content-Type) never re-downloads a file.
    existing = set(os.listdir(out_dir))
    downloaded_uuids = set()
    for name in existing:
        match = UUID_SUFFIX.search(os.path.splitext(name)[0])
        if match:
            downloaded_uuids.add(match.group(0))
    pending = []
    skipped = 0
    unusable = 0
    for item in items:
        uuid = item["media_uuid"]
        if (uuid and uuid in downloaded_uuids) or item["filename"] in existing:
            item["status"], item["detail"] = "skipped", "already downloaded"
            skipped += 1
            continue
        # Some records publish a local staging path instead of a URL.
        parts = urlparse(item["url"])
        if parts.scheme not in ("http", "https") or not parts.netloc:
            item["status"] = "invalid-url"
            item["detail"] = "ac:accessURI is not an http(s) URL"
            unusable += 1
            continue
        pending.append(item)
    if args.limit:
        for item in pending[args.limit:]:
            item["status"], item["detail"] = "not-attempted", f"--limit {args.limit}"
        pending = pending[: args.limit]

    print(f"  {skipped:,} already present, {len(pending):,} to download"
          + (f", {unusable:,} with an unusable accessURI" if unusable else ""),
          flush=True)

    if args.dry_run:
        for item in pending:
            item.setdefault("status", "dry-run")
            item.setdefault("detail", "")
    elif pending:
        work = queue.Queue()
        for item in pending:
            work.put(item)
        counts = {"ok": 0, "failed": 0}
        lock = threading.Lock()
        done = [0]
        started = time.monotonic()
        # Report every file on short runs, otherwise often enough to show life.
        step = 1 if len(pending) <= 50 else 25
        workers = max(1, args.workers)
        print(f"Downloading with {workers} workers "
              f"(Ctrl-C is safe, progress is kept) ...", flush=True)

        stopping = threading.Event()
        # Append-only record of every attempt, flushed as it happens. manifest.csv
        # is rewritten each run and only describes the current one; this log
        # survives interruptions and accumulates across runs, so a file fetched
        # today is still documented in a run next week that merely skips it.
        log_path = os.path.join(out_dir, "download_log.csv")
        new_log = not os.path.exists(log_path)
        log_file = open(log_path, "a", newline="", encoding="utf-8")
        log_writer = csv.writer(log_file)
        if new_log:
            log_writer.writerow(["timestamp", "status", "media_uuid", "coreid",
                                 "filename", "url", "detail"])
            log_file.flush()

        # A host that refuses to connect will do so for every one of its files,
        # at three attempts times the connect timeout each, so one that keeps
        # timing out is set aside -- but only for DEAD_HOST_COOLDOWN, after which
        # one file is allowed through to test whether it is back.
        host_strikes = collections.Counter()
        dead_until = {}
        recent_failures = collections.deque()   # (when, host), for outage detection
        outage_until = [0.0]

        def worker():
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            while not stopping.is_set():
                try:
                    item = work.get_nowait()
                except queue.Empty:
                    return
                host = urlparse(item["url"]).netloc.lower()
                now = time.monotonic()
                with lock:
                    pause_until = outage_until[0]
                    resting = dead_until.get(host, 0.0)
                    skip = resting > now
                    if resting and not skip:
                        # Cool-down elapsed: let this one through as a probe.
                        dead_until.pop(host, None)
                        host_strikes.pop(host, None)
                if pause_until > now:
                    time.sleep(min(pause_until - now, OUTAGE_PAUSE))
                    work.put(item)          # nothing was wrong with this file
                    continue
                if skip and not item.get("requeued"):
                    # Try again later in this run rather than failing it now.
                    item["requeued"] = True
                    work.put(item)
                    time.sleep(0.05)        # do not spin if the queue is all one host
                    continue
                if skip:
                    status, detail = "failed", f"skipped: {host} not answering"
                    with lock:
                        counts[status] += 1
                        done[0] += 1
                        log_writer.writerow([
                            time.strftime("%Y-%m-%dT%H:%M:%S"), status,
                            item["media_uuid"], item["coreid"], item["filename"],
                            item["url"], detail])
                        log_file.flush()
                    item["status"], item["detail"] = status, detail
                    continue
                status, detail = download(
                    session,
                    item,
                    os.path.join(out_dir, item["filename"]),
                    args.timeout,
                    args.retries,
                )
                item["status"], item["detail"] = status, detail
                if status == "ok" and detail:
                    item["filename"] = detail
                with lock:
                    # Only connection-level failures count as a host being down;
                    # a 403 or 404 says the host is up and answering.
                    if status == "ok":
                        host_strikes.pop(host, None)
                        dead_until.pop(host, None)
                    elif detail.startswith(("ConnectTimeout", "ConnectionError")):
                        moment = time.monotonic()
                        recent_failures.append((moment, host))
                        while recent_failures and \
                                moment - recent_failures[0][0] > OUTAGE_WINDOW:
                            recent_failures.popleft()
                        if len({h for _, h in recent_failures}) >= OUTAGE_HOSTS:
                            # Several unrelated hosts at once: the link is down at
                            # this end. Blaming them would write off the whole run.
                            host_strikes.clear()
                            dead_until.clear()
                            recent_failures.clear()
                            if outage_until[0] < moment:
                                print(f"  network looks down here -- pausing "
                                      f"{OUTAGE_PAUSE:.0f}s, no hosts written off",
                                      flush=True)
                            outage_until[0] = moment + OUTAGE_PAUSE
                        else:
                            host_strikes[host] += 1
                            if host_strikes[host] >= DEAD_HOST_STRIKES:
                                dead_until[host] = moment + DEAD_HOST_COOLDOWN
                    counts[status] += 1
                    done[0] += 1
                    log_writer.writerow([
                        time.strftime("%Y-%m-%dT%H:%M:%S"), status,
                        item["media_uuid"], item["coreid"], item["filename"],
                        item["url"], detail,
                    ])
                    log_file.flush()
                    if done[0] % step == 0 or done[0] == len(pending):
                        # A refused host answers in milliseconds, so a burst of
                        # them makes the overall rate meaningless. Estimate from
                        # actual transfers, which is an upper bound: whatever
                        # remains that also fails will resolve far quicker.
                        elapsed = max(time.monotonic() - started, 1e-6)
                        ok_rate = counts["ok"] / elapsed
                        left = (len(pending) - done[0]) / ok_rate if ok_rate else 0
                        eta = f"<{left / 60:.0f} min left" if ok_rate else "ETA unknown"
                        print(f"  {done[0]:,}/{len(pending):,} "
                              f"(ok {counts['ok']:,}, failed {counts['failed']:,}) "
                              f"{ok_rate:.1f} ok/s, {eta}", flush=True)

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(workers)]
        for thread in threads:
            thread.start()
        try:
            for thread in threads:
                while thread.is_alive():  # plain join() swallows Ctrl-C
                    thread.join(timeout=0.5)
        except KeyboardInterrupt:
            stopping.set()
            print("\nInterrupted -- letting in-flight downloads finish, "
                  "then writing the manifest ...", flush=True)
            for thread in threads:
                thread.join(timeout=args.timeout + 5)
            interrupted = sum(1 for item in pending if not item.get("status"))
            for item in pending:
                if not item.get("status"):
                    item["status"], item["detail"] = "interrupted", "run stopped"
            print(f"  {interrupted:,} not attempted; re-run to continue",
                  flush=True)
        finally:
            log_file.close()

    manifest = os.path.join(out_dir, "manifest.csv")
    columns = (["status", "filename", "url", "coreid", "media_uuid", "media_type",
                "format", "rights", "rights_url", "type_category"]
               + OCCURRENCE_CONTEXT
               + ["citation_roles", "cited_taxa", "publications", "cited_pages",
                  "detail"])
    with open(manifest, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            item.setdefault("status", "")
            item.setdefault("detail", "")
            writer.writerow(item)

    tally = {}
    for item in items:
        tally[item["status"]] = tally.get(item["status"], 0) + 1
    print("\nSummary:")
    for status in sorted(tally):
        print(f"  {status:<14} {tally[status]:,}")

    categories = {}
    for item in items:
        key = item.get("type_category") or "unknown"
        categories[key] = categories.get(key, 0) + 1
    print("\nMedia by typeStatus category:")
    for category in sorted(categories, key=lambda c: -categories[c]):
        print(f"  {category:<14} {categories[category]:,}")
    print(f"\nFiles in {out_dir}")
    print(f"Manifest: {manifest}")

    failed = tally.get("failed", 0)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
