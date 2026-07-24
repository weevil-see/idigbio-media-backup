#!/usr/bin/env python3
"""Build a browsable HTML gallery of the media downloaded by download_media.py.

Reads the archive CSVs and whatever is currently in the media folder, so it can
be run at any point -- including while a download is still in progress. Writes
gallery/gallery.html, which references ../media/<file>; both live under this
project directory, so the page works straight off the filesystem (no server).

Taxonomy
--------
The tree in the sidebar is built from the seven Darwin Core ranks in
download_media.TAXON_RANKS. Ranks the publisher left empty (notably
dwc:specificEpithet, absent from ~45% of records) can be supplied from an
external source -- a name-parsing service, the GBIF backbone, a local checklist
-- via --taxonomy, without touching the archive:

    python3 make_gallery.py --taxonomy names.csv

names.csv is matched on `coreid` if that column is present, otherwise on
`scientificName` (case-insensitive), and may carry any subset of the rank
columns, with or without the `dwc:` prefix:

    scientificName,family,genus,specificEpithet
    Genus species,Familyidae,Genus,species

By default it only fills ranks the archive left empty; --taxonomy-authoritative
lets it overwrite. Every record carries the resulting provenance (archive /
mixed / external) so a tree built from inferred names is never mistaken for one
that came out of the archive.

Usage:
    python3 make_gallery.py
    python3 make_gallery.py --taxonomy names.csv --open
"""

import argparse
import csv
import html
import importlib.util
import json
import os
import re
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))

# Reuse the downloader's parsing so names and metadata cannot drift apart.
_spec = importlib.util.spec_from_file_location(
    "download_media", os.path.join(HERE, "download_media.py"))
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

csv.field_size_limit(10 ** 9)

IDIGBIO_RECORD = "https://www.idigbio.org/portal/records/"
MEDIA_PREFIX = "../media/"  # gallery.html lives one directory over from the files

# Name an external source matched, carried alongside the ranks. It is a voucher
# for how a record was placed and is searchable, but it never replaces the
# published dwc:scientificName.
TW_NAME = "taxonworks:name"

RANKS = dl.TAXON_RANKS
RANK_LABELS = [r.split(":", 1)[1] for r in RANKS]

# Browsable hierarchies, each rendered as a tree in the sidebar. Only the
# taxonomic one can be filled from --taxonomy; geography comes from the archive.
TREES = [
    ("Taxonomy", dl.TAXON_RANKS),
    ("Geography", dl.GEO_RANKS),
]

# Entry layout, positional to keep the embedded JSON small. Each tree gets an
# array of indices into a shared vocabulary; -1 means that level is unknown.
FIELDS = ["file", "catalog", "name", "status", "category", "institution",
          "coreid", "url", "publication", "levels", "source", "matched",
          "licence", "licence_url", "pending"]


# iDigBio lower-cases everything it indexes, so the archive yields 'animalia',
# 'usnm', 'united states'. Values from an external source already carry proper
# case, so every rule below applies only to an all-lower-case string and leaves
# anything already capitalised alone. Presentation only -- the stored data and
# the search index are untouched.
LOWER_WORDS = {"of", "the", "and", "de", "del", "da", "do", "des", "du", "la",
               "le", "las", "los", "van", "von", "y"}
EPITHET_FIELDS = {"dwc:specificEpithet", "dwc:infraspecificEpithet"}


def capitalise(value):
    """Animalia, Curculionidae -- a taxon name above species rank."""
    if not value or not value.islower():
        return value
    return value[0].upper() + value[1:]


def title_place(value):
    """United States, Democratic Republic of the Congo."""
    if not value or not value.islower():
        return value
    words = []
    for index, word in enumerate(value.split()):
        if index and word in LOWER_WORDS:
            words.append(word)
        else:
            words.append(word[0].upper() + word[1:] if word else word)
    return " ".join(words)


def present_rank(field, value):
    """A rank value as it should read: epithets stay lower, the rest capitalise."""
    if field in EPITHET_FIELDS:
        return value.lower() if value.isupper() else value
    if field in set(dl.GEO_RANKS):
        return title_place(value)
    return capitalise(value)


def present_name(value):
    """'profidia nitida' -> 'Profidia nitida'; the epithet stays lower case."""
    return capitalise(value)


def present_code(value):
    """Institution codes are acronyms: usnm -> USNM. Left alone if not one."""
    if value and value.islower() and value.isalnum() and len(value) <= 8:
        return value.upper()
    return value


def tidy(value, limit=180):
    """Strip Arctos' HTML out of a field and collapse whitespace."""
    text = html.unescape(value or "")
    text = dl.TAG.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def load_taxonomy(path):
    """External rank data: {'coreid'|'name': {key: {rank: value}}}."""
    table = {"coreid": {}, "name": {}}
    if not path:
        return table
    if not os.path.exists(path):
        sys.exit(f"--taxonomy {path} does not exist.\n"
                 f"Omit the flag to build the gallery from the archive alone.")
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        columns = {(c or "").strip().lower(): c for c in reader.fieldnames or []}
        # Accept 'family' or 'dwc:family' for each rank.
        rank_columns = {}
        for rank, label in zip(RANKS, RANK_LABELS):
            source = columns.get(label.lower()) or columns.get(rank.lower())
            if source:
                rank_columns[rank] = source
        if not rank_columns:
            sys.exit(f"{path}: no rank columns found "
                     f"(expected any of {', '.join(RANK_LABELS)})")
        key_column = columns.get("coreid")
        name_column = columns.get("scientificname")
        if not key_column and not name_column:
            sys.exit(f"{path}: needs a 'coreid' or 'scientificName' column")
        matched_column = (columns.get("taxonworks_name")
                          or columns.get("matched_name"))
        via_column = columns.get("matched_via")
        for row in reader:
            values = {rank: (row.get(src) or "").strip()
                      for rank, src in rank_columns.items()}
            values = {k: v for k, v in values.items() if v}
            if not values:
                continue
            if matched_column and (row.get(matched_column) or "").strip():
                name = row[matched_column].strip()
                if via_column and (row.get(via_column) or "").strip() == "genus":
                    name += "  (placed by genus)"
                values[TW_NAME] = name
            if key_column and (row.get(key_column) or "").strip():
                table["coreid"][row[key_column].strip()] = values
            elif name_column and (row.get(name_column) or "").strip():
                table["name"][row[name_column].strip().lower()] = values
    return table


def resolve_ranks(item, taxonomy, authoritative):
    """Merge archive ranks with external ones; returns (values, provenance)."""
    from_archive = {r: (item.get(r) or "").strip() for r in RANKS}
    external = (taxonomy["coreid"].get(item["coreid"])
                or taxonomy["name"].get((item.get("dwc:scientificName") or "")
                                        .strip().lower())
                or {})
    matched = external.get(TW_NAME, "")
    resolved, used_external = {}, False
    # Values the archive already placed, so an external source cannot repeat one
    # at a different rank: sources disagree about which rank a name sits at, and
    # honouring both puts the same name on two levels of the tree.
    placed = {v.lower() for v in from_archive.values() if v}
    for rank in RANKS:
        archive_value = from_archive[rank]
        outside_value = external.get(rank, "")
        if outside_value and outside_value.lower() in placed and not archive_value:
            outside_value = ""
        if outside_value and (authoritative or not archive_value):
            resolved[rank] = outside_value
            used_external = used_external or outside_value != archive_value
        else:
            resolved[rank] = archive_value
    if not used_external:
        source = "archive"
    elif any(from_archive.values()):
        source = "mixed"
    else:
        source = "external"
    return resolved, source, matched


ANCHOR_RANK = "dwc:family"      # the level a tree must not fragment


def trim_tree(fields, resolved, value_of, tree_from):
    """Drop leading ranks until each family is reached by exactly one route.

    Ranks above family are the ones sources disagree about -- one archive puts
    Coleoptera under Insecta and another under Hexapoda, and an external
    nomenclator may rank the same name differently again. Every such
    disagreement splits the family into a separate branch. Rather than pick a
    winner, the coarsest levels are dropped until the family sits in the tree
    once; they remain in the data and in each record's lineage.
    """
    if tree_from:
        wanted = tree_from.lower()
        for index, field in enumerate(fields):
            if field.split(":", 1)[1].lower() == wanted:
                return fields[index:]
        sys.exit(f"--tree-from {tree_from}: not a rank in this tree "
                 f"({', '.join(f.split(':', 1)[1] for f in fields)})")

    if ANCHOR_RANK not in fields:
        return fields
    anchor = fields.index(ANCHOR_RANK)

    def shown(item, values, field):
        # Compare what the tree will display, not the raw value: sources differ
        # in capitalisation, and 'curculionidae' and 'Curculionidae' become one
        # node. Comparing raw values would call them two families with one route
        # each and leave the split in place.
        return present_rank(field, value_of(item, values, field))

    for start in range(anchor + 1):
        routes = {}
        for _, item, values, _, _ in resolved:
            family = shown(item, values, ANCHOR_RANK)
            if not family:
                continue
            path = tuple(shown(item, values, f) for f in fields[start:anchor])
            routes.setdefault(family, set()).add(path)
        if all(len(paths) == 1 for paths in routes.values()):
            return fields[start:]
    return fields[anchor:]


def build_entries(archive_dir, media_dir, taxonomy, authoritative, verbatim,
                  tree_from=None):
    items = dl.gather(archive_dir, dl.SCOPE_ALL, verbatim=verbatim)
    by_uuid = {i["media_uuid"]: i for i in items if i["media_uuid"]}

    resolved, unnamed, unmatched = [], [], []
    for name in sorted(os.listdir(media_dir)):
        if name.endswith((".csv", ".html", ".part", ".log")):
            continue
        match = dl.UUID_SUFFIX.search(os.path.splitext(name)[0])
        if not match:
            unnamed.append(name)
            continue
        item = by_uuid.get(match.group(0))
        if not item:
            # A media UUID in no record of this archive -- almost always another
            # download sharing the folder. It cannot be described, so it cannot
            # be shown, but it must not disappear quietly either.
            unmatched.append(name)
            continue
        resolved.append((name, item) + resolve_ranks(item, taxonomy, authoritative))

    # A specimen's remaining views, not downloaded yet. Only for specimens that
    # already appear, so the page does not grow by the whole archive: the point
    # is to show that a specimen is incomplete, not to list everything unfetched.
    have = {item["media_uuid"] for _, item, _, _ in
            ((n, i, v, sc) for n, i, v, sc, _ in resolved)}
    shown_specimens = {item["coreid"] for _, item, _, _, _ in resolved}
    for item in items:
        if item["coreid"] in shown_specimens and item["media_uuid"] not in have:
            resolved.append((None, item) + resolve_ranks(item, taxonomy,
                                                         authoritative))

    # A level nothing populates would add an "unplaced" step to every branch for
    # no information, so drop it. dwc:tribe reappears by itself the moment a
    # --taxonomy file supplies it.
    def value_of(item, values, field):
        return values[field] if field in values else (item.get(field) or "").strip()

    trees = []
    for label, fields in TREES:
        active = [f for f in fields
                  if any(value_of(item, values, f)
                         for _, item, values, _, _ in resolved)]
        if label == "Taxonomy":
            active = trim_tree(active, resolved, value_of, tree_from)
        trees.append({"label": label,
                      "fields": active,
                      "labels": [f.split(":", 1)[1] for f in active]})

    vocabulary, vocab_index = [], {}

    def intern(value):
        if not value:
            return -1
        if value not in vocab_index:
            vocab_index[value] = len(vocabulary)
            vocabulary.append(value)
        return vocab_index[value]

    entries = []
    for name, item, values, source, matched in resolved:
        entries.append([
            name or "",
            item.get("dwc:catalogNumber", ""),
            present_name(item.get("dwc:scientificName", "")),
            capitalise(tidy(item.get("dwc:typeStatus", ""))),
            item.get("type_category", ""),
            present_code(item.get("dwc:institutionCode", "")),
            item["coreid"],
            item["url"],
            tidy(item.get("publications", ""), 90),
            [[intern(present_rank(f, value_of(item, values, f)))
              for f in tree["fields"]] for tree in trees],
            source,
            matched,
            # Stated explicitly rather than left blank: whether a file may be
            # reused is exactly what someone filters on, and an empty cell reads
            # as "unknown" when it means "the publisher said nothing".
            item.get("rights") or "(none stated)",
            item.get("rights_url", ""),
            0 if name else 1,          # pending: known to the archive, not on disk
        ])
    # Group a specimen's several views together. coreid (the occurrence UUID) is
    # the identity -- a catalogue number is only unique within an institution, so
    # it orders the grid for readability but never decides what belongs together.
    catalog_i, coreid_i = FIELDS.index("catalog"), FIELDS.index("coreid")
    entries.sort(key=lambda e: (e[catalog_i] or "\uffff" + e[coreid_i],
                                e[coreid_i], e[0]))
    return entries, len(items), vocabulary, trees, unnamed, unmatched


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Type specimen media — iDigBio archive</title>
<style>
  :root {
    --bg: #fbfaf8; --panel: #fff; --ink: #1c1a17; --muted: #6d675e;
    --line: #e3ded5; --accent: #8a5a2b; --chip: #f0ebe2; --sel: #f3e7d5;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16150f; --panel: #211f18; --ink: #ece7dc; --muted: #9b9587;
      --line: #33302a; --accent: #d8a55f; --chip: #2c2921; --sel: #3a3122;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header {
    position: sticky; top: 0; z-index: 20; background: var(--panel);
    border-bottom: 1px solid var(--line); padding: 13px 20px;
  }
  h1 { margin: 0 0 2px; font-size: 17px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 11px; }
  input[type=search], select, button.plain {
    background: var(--bg); color: var(--ink); border: 1px solid var(--line);
    border-radius: 7px; padding: 7px 10px; font: inherit; font-size: 14px;
  }
  input[type=search] { flex: 1 1 240px; min-width: 170px; }
  input[type=search]:focus, select:focus { outline: 2px solid var(--accent);
    outline-offset: -1px; }
  .shell { display: grid; grid-template-columns: 268px minmax(0, 1fr); }
  @media (max-width: 820px) { .shell { grid-template-columns: 1fr; }
    aside { position: static !important; max-height: 300px; } }

  aside {
    position: sticky; top: 106px; align-self: start; padding: 16px 8px 40px 18px;
    max-height: calc(100vh - 106px); overflow-y: auto;
    border-right: 1px solid var(--line);
  }
  .aside-head { display: flex; align-items: baseline; justify-content: space-between;
    gap: 8px; margin-bottom: 8px; }
  .aside-head strong { font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
  .reset { background: none; border: none; color: var(--accent); cursor: pointer;
    font: inherit; font-size: 12px; padding: 0; }
  .node { display: flex; align-items: center; gap: 3px; border-radius: 6px;
    padding: 1px 4px; }
  .node:hover { background: var(--chip); }
  .node.on { background: var(--sel); }
  .caret { width: 15px; flex: 0 0 15px; border: none; background: none;
    color: var(--muted); cursor: pointer; font-size: 10px; padding: 0;
    line-height: 1; }
  .caret.leaf { visibility: hidden; }
  .label { flex: 1 1 auto; background: none; border: none; color: inherit;
    font: inherit; font-size: 13.5px; text-align: left; cursor: pointer;
    padding: 2px 0; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; }
  .node.on .label { font-weight: 600; }
  .label.unknown { color: var(--muted); font-style: italic; }
  .n { color: var(--muted); font-size: 11.5px; font-variant-numeric: tabular-nums; }
  .kids { margin-left: 11px; border-left: 1px solid var(--line);
    padding-left: 4px; }
  .ext { color: var(--accent); font-size: 10px; }
  .find { width: 100%; margin-bottom: 7px; font-size: 13px !important;
    padding: 5px 8px !important; }
  /* Each tree scrolls in its own right, so a long genus list cannot
     push the geography tree off the bottom of the sidebar. */
  .treebox { margin-bottom: 20px; max-height: 40vh; overflow-y: auto;
    overscroll-behavior: contain; }
  #chips { display: none; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
  .chip { background: var(--sel); border: 1px solid var(--line); color: var(--ink);
    border-radius: 999px; padding: 3px 10px; font: inherit; font-size: 12.5px;
    cursor: pointer; }
  .chip:hover { border-color: var(--accent); }
  .chip.reset-all { background: none; color: var(--muted); }
  .reveal-btn { display: block; margin-top: 6px; background: var(--chip);
    border: 1px solid var(--line); color: var(--accent); border-radius: 7px;
    padding: 4px 9px; font: inherit; font-size: 12.5px; cursor: pointer; }
  .reveal-btn:hover { border-color: var(--accent); }
  .reset:disabled { opacity: .35; cursor: default; }
  .chip span { color: var(--muted); font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.04em; margin-right: 3px; }

  main {
    padding: 18px 20px 60px;
    display: grid; gap: 15px; align-content: start;
    grid-template-columns: repeat(auto-fill, minmax(205px, 1fr));
  }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    overflow: hidden; cursor: zoom-in; display: flex; flex-direction: column;
  }
  .card:hover { border-color: var(--accent); }
  .thumb { width: 100%; aspect-ratio: 4 / 3; object-fit: cover;
    background: var(--chip); display: block; }
  .meta { padding: 9px 11px 11px; font-size: 13px; }
  .cat { font-weight: 600; word-break: break-word; }
  .sci { color: var(--muted); font-style: italic; margin-top: 1px; }
  .badge { display: inline-block; margin-top: 7px; padding: 2px 7px;
    border-radius: 999px; background: var(--chip); color: var(--muted);
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  .badge.citation { color: var(--accent); }
  #empty, #more { grid-column: 1 / -1; color: var(--muted); padding: 30px 0;
    text-align: center; }

  dialog { border: none; border-radius: 12px; padding: 0;
    max-width: min(1100px, 94vw); background: var(--panel); color: var(--ink); }
  dialog::backdrop { background: rgba(0,0,0,.72); }
  .viewer { display: grid; grid-template-columns: minmax(0,1fr) 310px; }
  @media (max-width: 760px) { .viewer { grid-template-columns: 1fr; } }
  .stage { display: flex; flex-direction: column; min-width: 0; background: #000; }
  .stage img { width: 100%; max-height: 74vh; object-fit: contain; }
  .strip { display: flex; gap: 6px; padding: 8px; overflow-x: auto;
    background: rgba(0,0,0,.55); }
  .strip:empty { display: none; }
  .strip button { border: 2px solid transparent; border-radius: 6px; padding: 0;
    background: none; cursor: pointer; flex: 0 0 auto; line-height: 0; }
  .strip img { width: 68px; height: 68px; object-fit: cover; border-radius: 4px;
    opacity: .55; }
  .strip button.on { border-color: var(--accent); }
  .strip button.on img { opacity: 1; }
  .strip button.hidden img { opacity: .28; }
  .strip .n { color: #bbb; font-size: 11px; align-self: center; padding: 0 6px;
    white-space: nowrap; }
  .pending { display: none; min-height: 40vh; align-items: center;
    justify-content: center; color: #999; font-size: 14px; text-align: center;
    background: repeating-linear-gradient(45deg, #1a1a1a 0 10px, #151515 10px 20px); }
  #vpending[style*="none"] { display: none !important; }
  .strip button.pending-thumb img { opacity: .3; }
  .strip button.pending-thumb { position: relative; }
  .strip button.pending-thumb::after { content: "⬇"; position: absolute;
    inset: 0; display: flex; align-items: center; justify-content: center;
    color: #fff; font-size: 20px; }
  .info { padding: 18px 20px; overflow-y: auto; max-height: 84vh; }
  .info h2 { margin: 0 0 12px; font-size: 15px; word-break: break-word; }
  .info dt { color: var(--muted); font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.04em; margin-top: 12px; }
  .info dd { margin: 2px 0 0; word-break: break-word; }
  .info a { color: var(--accent); }
  .lineage { font-size: 13px; line-height: 1.7; }
  .lineage span { color: var(--muted); }
  .close { position: absolute; top: 8px; right: 13px; background: none;
    border: none; color: #fff; font-size: 28px; cursor: pointer; }
</style>
</head>
<body>
<header>
  <h1>Type specimen media</h1>
  <div class="sub" id="count"></div>
  <div class="sub" id="filehint" style="display:none">
    opened from a file — dragging an image gives a link, not a file.
    Re-run with <code>--serve</code> to drag images into other sites.</div>
  <div id="chips"></div>
  <div class="controls">
    <input type="search" id="q" placeholder="Search catalogue no., taxon, typeStatus, publication…">
    <select id="cat"></select>
    <select id="inst"></select>
    <select id="lic"></select>
  </div>
</header>

<div class="shell">
  <aside id="aside"></aside>
  <main id="grid"></main>
</div>

<dialog id="viewer">
  <button class="close" onclick="viewer.close()" aria-label="Close">&times;</button>
  <div class="viewer">
    <div class="stage">
      <img id="vimg" alt="">
      <div id="vpending" class="pending"></div>
      <div id="vstrip" class="strip"></div>
    </div>
    <div class="info" id="vinfo"></div>
  </div>
</dialog>

<script>
const FIELDS = __FIELDS__;
const DATA = __DATA__;
const VOCAB = __VOCAB__;
const TREES = __TREES__;
const TOTAL = __TOTAL__;
const RECORD_URL = "__RECORD__";
const MEDIA = "__MEDIA__";

const rows = DATA.map(a => {
  const o = {};
  FIELDS.forEach((f, i) => o[f] = a[i]);
  o.paths = o.levels.map(ix => ix.map(i => i < 0 ? "" : VOCAB[i]));
  o._hay = [o.catalog, o.name, o.status, o.institution, o.publication, o.matched,
            o.licence,
            ...o.paths.flat()].join(" ").toLowerCase();
  return o;
});

/* ---- hierarchy trees ------------------------------------------------ */
/* One tree per entry in TREES (taxonomy, geography). Nodes are keyed by depth,
   so a selection is just the first N level values. A record missing a level
   sits under an explicit "unplaced" node rather than being dropped, which keeps
   the counts honest. */
const UNKNOWN = "\\u0000";
const state = TREES.map(() => ({ path: [], opened: new Set([""]), find: "" }));

function buildTree(records, t) {
  const root = { kids: new Map(), n: 0 };
  for (const r of records) {
    let node = root;
    for (let d = 0; d < TREES[t].labels.length; d++) {
      const key = r.paths[t][d] || UNKNOWN;
      if (!node.kids.has(key)) node.kids.set(key, { kids: new Map(), n: 0 });
      node = node.kids.get(key);
      node.n++;
    }
  }
  return root;
}

/* A download confined to one lineage would otherwise open on a single
   "animalia" row; walk past every level that does not branch. */
function openTrunk(t) {
  let node = buildTree(GRID, t), here = [];
  for (;;) {
    // An "unplaced" sibling is not a branch worth stopping at, so only count
    // named children when deciding whether the lineage still runs straight.
    const named = [...node.kids.entries()].filter(([k]) => k !== UNKNOWN);
    if (named.length !== 1) return;
    const [key, child] = named[0];
    here = here.concat(key);
    state[t].opened.add(pathKey(here));
    node = child;
  }
}

function pathKey(p) { return p.join("\\u001f"); }

/* Type-ahead: keep a node when it matches, or when a descendant does -- so the
   ancestors needed to reach a hit stay visible and are opened automatically. */
function findMatch(node, t, term, prefix) {
  if (!term) return true;
  let hit = false;
  for (const [key, child] of node.kids) {
    const here = prefix.concat(key);
    const self = key !== UNKNOWN && key.toLowerCase().includes(term);
    const below = findMatch(child, t, term, here);
    child._show = self || below;
    if (child._show && below) state[t].opened.add(pathKey(here));
    hit = hit || child._show;
  }
  return hit;
}

function renderTrees() {
  const host = document.getElementById("aside");
  host.innerHTML = "";
  TREES.forEach((tree, t) => {
    const root = buildTree(GRID.filter(r => matchesExcept(r, t)), t);
    const term = state[t].find.trim().toLowerCase();
    findMatch(root, t, term, []);

    const head = document.createElement("div");
    head.className = "aside-head";
    if (t) head.style.borderTop = "1px solid var(--line)";
    const title = document.createElement("strong");
    title.textContent = tree.label;
    const clear = document.createElement("button");
    clear.className = "reset";
    clear.textContent = "clear";
    // Each tree clears only itself; greyed out when there is nothing to clear,
    // so it never looks like a button that did nothing.
    clear.disabled = !state[t].path.length;
    clear.onclick = () => { state[t].path = []; apply(); };
    head.append(title, clear);

    const find = document.createElement("input");
    find.type = "search";
    find.className = "find";
    find.placeholder = "find " + tree.labels.join("/");
    find.value = state[t].find;
    find.oninput = () => { state[t].find = find.value; renderTrees(); };

    const box = document.createElement("div");
    box.className = "treebox";
    box.appendChild(renderLevel(root, [], t, term));

    host.append(head, find, box);
    if (find.value) { find.focus(); find.setSelectionRange(9e9, 9e9); }
  });
}

function renderLevel(node, prefix, t, term) {
  const box = document.createElement("div");
  const st = state[t];
  const kids = [...node.kids.entries()]
    .filter(([, child]) => !term || child._show)
    .sort((a, b) =>
      a[0] === UNKNOWN ? 1 : b[0] === UNKNOWN ? -1 : a[0].localeCompare(b[0]));
  for (const [key, child] of kids) {
    const here = prefix.concat(key);
    const id = pathKey(here);
    /* A level nobody filled at this branch (sparse dwc:tribe, say) would cost a
       click and show nothing, so render its children in its place. The value
       stays in the path, so filtering by depth still lines up. */
    if (key === UNKNOWN && kids.length === 1 && child.kids.size) {
      box.appendChild(renderLevel(child, here, t, term));
      continue;
    }
    const row = document.createElement("div");
    row.className = "node" + (pathKey(st.path) === id ? " on" : "");

    const caret = document.createElement("button");
    caret.className = "caret" + (child.kids.size ? "" : " leaf");
    caret.textContent = st.opened.has(id) ? "▼" : "▶";
    caret.onclick = () => { st.opened.has(id) ? st.opened.delete(id)
                                              : st.opened.add(id);
                            renderTrees(); };

    const label = document.createElement("button");
    label.className = "label" + (key === UNKNOWN ? " unknown" : "");
    label.textContent = key === UNKNOWN
      ? `unplaced (${TREES[t].labels[here.length - 1]})` : key;
    label.title = `${TREES[t].labels[here.length - 1]}: ${
      key === UNKNOWN ? "not given" : key}`;
    label.onclick = () => {
      st.path = pathKey(st.path) === id ? here.slice(0, -1) : here;
      st.opened.add(id);
      apply();
    };

    const count = document.createElement("span");
    count.className = "n";
    count.textContent = child.n.toLocaleString();

    row.append(caret, label, count);
    box.appendChild(row);
    if (child.kids.size && st.opened.has(id)) {
      const kidBox = renderLevel(child, here, t, term);
      kidBox.className = "kids";
      box.appendChild(kidBox);
    }
  }
  return box;
}

/* ---- filtering ------------------------------------------------------ */
// Every image of a specimen, in file order. A specimen is its coreid: catalogue
// numbers repeat across institutions and are often absent.
/* Dragging an <img> offers only its URL, so a drop target treats it as a link
   and navigates instead of uploading. Attaching a real File makes it a file
   drop -- but the bytes can only be read when the page is served over http,
   since a file:// page may not fetch its own images. Serve with --serve. */
const CAN_READ_FILES = location.protocol !== "file:";
const REVEAL_LABEL = "show these files in the file manager";
const FILE_CACHE = new Map();

async function fileFor(r) {
  if (!CAN_READ_FILES || r.pending) return null;
  if (!FILE_CACHE.has(r.file)) {
    FILE_CACHE.set(r.file, (async () => {
      try {
        const response = await fetch(MEDIA + r.file);
        const blob = await response.blob();
        return new File([blob], r.file, { type: blob.type || "image/jpeg" });
      } catch { return null; }
    })());
    FILE_CACHE.get(r.file).then(file => {
      const slot = FILE_CACHE.get(r.file);
      if (slot) slot.file = file;      // dragstart is synchronous; keep it ready
    });
  }
  return FILE_CACHE.get(r.file);
}

function armDrag(img, r) {
  // The bytes must be in hand before dragstart fires, which cannot await.
  const warm = () => { fileFor(r); };
  img.addEventListener("pointerdown", warm);
  img.addEventListener("mouseenter", warm);
  img.addEventListener("dragstart", event => {
    const url = new URL(MEDIA + r.file, location.href).href;
    // Lets a drag to the desktop or a file manager save the file (Chromium).
    event.dataTransfer.setData(
      "DownloadURL", `${r.licence_url ? "image/jpeg" : "image/jpeg"}:${r.file}:${url}`);
    event.dataTransfer.setData("text/uri-list", url);
    const pending = FILE_CACHE.get(r.file);
    if (pending && pending.file) {
      event.dataTransfer.items.add(pending.file);   // a genuine file drop
    }
  });
}

const GRID = rows.filter(r => !r.pending);   // pending files have nothing to show
const BY_SPECIMEN = new Map();
rows.forEach((r, i) => {
  if (!BY_SPECIMEN.has(r.coreid)) BY_SPECIMEN.set(r.coreid, []);
  BY_SPECIMEN.get(r.coreid).push(i);
});

const grid = document.getElementById("grid");
const q = document.getElementById("q");
const catSel = document.getElementById("cat");
const instSel = document.getElementById("inst");
const licSel = document.getElementById("lic");

function fill(sel, label, values) {
  sel.innerHTML = `<option value="">${label} (all)</option>` +
    [...new Set(values)].filter(Boolean).sort()
      .map(v => `<option>${v}</option>`).join("");
}
fill(catSel, "Category", GRID.map(r => r.category));
fill(instSel, "Institution", GRID.map(r => r.institution));
fill(licSel, "Licence", GRID.map(r => r.licence));

function matchesFields(r) {
  const term = q.value.trim().toLowerCase();
  return (!term || r._hay.includes(term)) &&
         (!catSel.value || r.category === catSel.value) &&
         (!instSel.value || r.institution === instSel.value) &&
         (!licSel.value || r.licence === licSel.value);
}
function matchesTree(r, t) {
  return state[t].path.every((v, d) => (r.paths[t][d] || UNKNOWN) === v);
}
/* Counts inside a tree must ignore that tree's own selection -- otherwise
   picking a genus would collapse its siblings' counts to zero. */
function matchesExcept(r, skip) {
  return matchesFields(r) && TREES.every((_, t) => t === skip || matchesTree(r, t));
}
function matchesAll(r) {
  return matchesFields(r) && TREES.every((_, t) => matchesTree(r, t));
}

let shown = [], drawn = 0;
const PAGE_SIZE = 120;

function esc(s) {
  return (s || "").replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function apply() {
  shown = GRID.filter(matchesAll);
  grid.innerHTML = "";
  drawn = 0;
  const hint = document.getElementById("filehint");
  if (hint) hint.style.display = CAN_READ_FILES ? "none" : "";
  document.getElementById("count").textContent =
    `${shown.length.toLocaleString()} images` +
    (shown.length !== GRID.length ? ` of ${GRID.length.toLocaleString()} on disk` : "") +
    ` — ${TOTAL.toLocaleString()} media records in the archive`;
  renderChips();
  renderTrees();
  draw();
}

/* One removable chip per selected level, so what is filtering the grid stays
   visible without having to read it back out of two trees. */
function renderChips() {
  const host = document.getElementById("chips");
  host.innerHTML = "";
  TREES.forEach((tree, t) => {
    state[t].path.forEach((value, d) => {
      const chip = document.createElement("button");
      chip.className = "chip";
      chip.innerHTML = `<span>${tree.labels[d]}</span> ${
        value === UNKNOWN ? "unplaced" : esc(value)} &times;`;
      chip.title = `remove ${tree.labels[d]} filter`;
      chip.onclick = () => { state[t].path = state[t].path.slice(0, d); apply(); };
      host.appendChild(chip);
    });
  });
  const anyField = q.value || catSel.value || instSel.value || licSel.value;
  if (host.children.length || anyField) {
    const all = document.createElement("button");
    all.className = "chip reset-all";
    all.textContent = "reset all filters";
    all.onclick = () => {
      TREES.forEach((_, t) => state[t].path = []);
      q.value = ""; catSel.value = ""; instSel.value = ""; licSel.value = "";
      apply();
    };
    host.appendChild(all);
  }
  host.style.display = host.children.length ? "flex" : "none";
}

function draw() {
  const slice = shown.slice(drawn, drawn + PAGE_SIZE);
  const frag = document.createDocumentFragment();
  slice.forEach((r, i) => {
    const el = document.createElement("article");
    el.className = "card";
    el.dataset.i = drawn + i;
    el.innerHTML =
      `<img class="thumb" loading="lazy" src="${MEDIA}${esc(r.file)}" alt="">
       <div class="meta">
         <div class="cat">${esc(r.catalog) || "—"}</div>
         <div class="sci">${esc(r.name)}</div>
         <span class="badge ${esc(r.category)}">${esc(r.status) || r.category}</span>
       </div>`;
    armDrag(el.querySelector("img"), r);   // cards are dragged far more often
    frag.appendChild(el);                  // than the modal image
  });
  grid.appendChild(frag);
  drawn += slice.length;

  const old = document.getElementById("more");
  if (old) old.remove();
  if (drawn < shown.length) {
    const s = document.createElement("div");
    s.id = "more";
    s.textContent = "Loading more…";
    grid.appendChild(s);
    io.observe(s);
  } else if (!shown.length) {
    const s = document.createElement("div");
    s.id = "empty";
    s.textContent = "Nothing matches those filters.";
    grid.appendChild(s);
  }
}

const io = new IntersectionObserver(es => {
  if (es.some(e => e.isIntersecting)) draw();
}, { rootMargin: "600px" });

grid.addEventListener("click", e => {
  const card = e.target.closest(".card");
  if (card) open(+card.dataset.i);
});

let current = -1;          // index into `shown`, for arrow-key stepping
function open(i) {
  current = i;
  const r = shown[i];
  if (!r) return;
  show(rows.indexOf(r));
  viewer.showModal();
}

/* Show one image plus every other view of the same specimen, so a specimen is
   examined as a whole rather than one file at a time. Views filtered out of the
   grid are still offered here, dimmed -- they belong to the specimen whether or
   not the current filter matches them. */
function show(rowIndex) {
  const r = rows[rowIndex];
  if (!r) return;
  const image = document.getElementById("vimg");
  const note = document.getElementById("vpending");
  image.style.display = r.pending ? "none" : "";
  note.style.display = r.pending ? "" : "none";
  if (r.pending) {
    note.innerHTML = `not downloaded yet<br>
      <a href="${esc(r.url)}" target="_blank">open the original</a>`;
  } else {
    image.src = MEDIA + r.file;
    armDrag(image, r);
    fileFor(r);                      // warm it while the image is being looked at
  }
  const lineage = TREES.map((tree, t) => tree.labels.map((label, d) =>
    r.paths[t][d] ? `<span>${label}</span> ${esc(r.paths[t][d])}` : ""
  ).filter(Boolean).join("<br>")).filter(Boolean).join("<br><br>");
  document.getElementById("vinfo").innerHTML = `
    <h2>${esc(r.catalog) || "(no catalogue number)"}</h2>
    <dl>
      <dt>Scientific name</dt><dd><em>${esc(r.name) || "—"}</em></dd>
      ${r.matched ? `<dt>TaxonWorks match</dt><dd><em>${esc(r.matched)}</em>
        <span class="ext">· placement only, name unchanged</span></dd>` : ""}
      <dt>typeStatus</dt><dd>${esc(r.status) || "—"}</dd>
      <dt>Category</dt><dd>${esc(r.category)}</dd>
      <dt>Institution</dt><dd>${esc(r.institution) || "—"}</dd>
      <dt>Licence</dt><dd>${r.licence_url
        ? `<a href="${esc(r.licence_url)}" target="_blank">${esc(r.licence)}</a>`
        : esc(r.licence)}</dd>
      <dt>Classification <span class="ext">${
        r.source === "archive" ? "" : "· " + esc(r.source)}</span></dt>
      <dd class="lineage">${lineage || "—"}</dd>
      ${r.publication ? `<dt>Cited in</dt><dd>${esc(r.publication)}</dd>` : ""}
      <dt>File</dt><dd>${esc(r.file)}
        <button id="vreveal" class="reveal-btn" style="display:none">
          ${REVEAL_LABEL}</button></dd>
      <dt>Links</dt><dd>
        <a href="${RECORD_URL}${esc(r.coreid)}" target="_blank">iDigBio record</a><br>
        <a href="${esc(r.url)}" target="_blank">original media URL</a></dd>
    </dl>`;

  // A drop target that refuses a browser drag will still accept files dragged
  // from a file manager, so offer to open the folder with this specimen in it.
  const reveal = document.getElementById("vreveal");
  const onDisk = (BY_SPECIMEN.get(r.coreid) || []).filter(j => !rows[j].pending);
  reveal.style.display = CAN_READ_FILES && onDisk.length ? "" : "none";
  reveal.onclick = async () => {
    const names = onDisk.map(j => "f=" + encodeURIComponent(rows[j].file));
    reveal.textContent = "opening…";
    try {
      const response = await fetch("/__reveal?" + names.join("&"));
      reveal.textContent = response.ok ? "opened in file manager"
                                       : "no file manager found";
    } catch {
      reveal.textContent = "only works via serve.py";
    }
    setTimeout(() => { reveal.textContent = REVEAL_LABEL; }, 2500);
  };

  const siblings = BY_SPECIMEN.get(r.coreid) || [rowIndex];
  const strip = document.getElementById("vstrip");
  strip.innerHTML = "";
  if (siblings.length > 1) {
    const count = document.createElement("span");
    count.className = "n";
    const missing = siblings.filter(j => rows[j].pending).length;
    count.textContent = missing
      ? `${siblings.length} views, ${missing} not downloaded`
      : `${siblings.length} views`;
    strip.appendChild(count);
    const visible = new Set(shown);
    siblings.forEach(j => {
      const sib = rows[j];
      const button = document.createElement("button");
      button.className = (j === rowIndex ? "on " : "") +
                         (sib.pending ? "pending-thumb"
                                      : visible.has(sib) ? "" : "hidden");
      button.title = sib.pending ? "not downloaded yet"
                   : visible.has(sib) ? sib.file
                   : sib.file + " (hidden by the current filter)";
      button.innerHTML = sib.pending
        ? `<img alt="">`
        : `<img loading="lazy" src="${MEDIA}${esc(sib.file)}" alt="">`;
      button.onclick = () => show(j);
      if (!sib.pending) armDrag(button.querySelector("img"), sib);
      strip.appendChild(button);
    });
  }
}

addEventListener("keydown", e => {
  if (!viewer.open) return;
  if (e.key === "ArrowRight" && current < shown.length - 1) open(current + 1);
  if (e.key === "ArrowLeft" && current > 0) open(current - 1);
});

[q, catSel, instSel, licSel].forEach(el => el.addEventListener("input", apply));
TREES.forEach((_, t) => openTrunk(t));
apply();
</script>
</body>
</html>
"""


# Written next to gallery.html on every build. Standalone on purpose: it must
# keep working if the gallery folder is copied elsewhere, or if the scripts
# that produced it are not around.
LAUNCHER = '#!/usr/bin/env python3\n# Open this gallery, served over localhost.\n#\n# Serving matters for more than tidiness: a page opened straight from disk is a\n# file:// document and may not read its own images, so dragging one into another\n# site hands over a link instead of a file. Over http it hands over the image.\n#\n# It also answers /__reveal, which opens the media folder in a file manager with\n# a specimen\'s files selected -- handy when a drop target refuses a browser drag.\n#\n# Regenerated by make_gallery.py on every build -- local edits will be lost.\n#\n#     python3 serve.py          # port {port}\n#     python3 serve.py 8123     # another port; 0 picks a free one\n\nimport functools\nimport http.server\nimport os\nimport shutil\nimport socketserver\nimport subprocess\nimport sys\nimport urllib.parse\nimport webbrowser\n\nHERE = os.path.dirname(os.path.abspath(__file__))\nROOT = os.path.dirname(HERE)              # the dataset, so ../media resolves\nMEDIA = os.path.join(ROOT, "media")\nPAGE = os.path.relpath(os.path.join(HERE, "gallery.html"), ROOT)\n\n# --select takes one file in most managers; the folder still opens for the rest.\nSELECTORS = [("nautilus", ["--select"]), ("dolphin", ["--select"]),\n             ("nemo", []), ("thunar", []), ("caja", ["--select"]),\n             ("pcmanfm", []), ("explorer.exe", ["/select,"]), ("open", ["-R"])]\n\n\ndef reveal(names):\n    # Only files that really sit in media/, so a crafted request cannot point\n    # the file manager somewhere else.\n    paths = []\n    for name in names:\n        candidate = os.path.realpath(os.path.join(MEDIA, name))\n        if os.path.commonpath([candidate, os.path.realpath(MEDIA)]) == \\\n                os.path.realpath(MEDIA) and os.path.isfile(candidate):\n            paths.append(candidate)\n    if not paths:\n        return False\n    for program, flags in SELECTORS:\n        found = shutil.which(program)\n        if found:\n            try:\n                subprocess.Popen([found] + flags + paths[:1] if flags\n                                 else [found, MEDIA])\n                return True\n            except OSError:\n                continue\n    opener = shutil.which("xdg-open")\n    if opener:\n        subprocess.Popen([opener, MEDIA])\n        return True\n    return False\n\n\nclass Handler(http.server.SimpleHTTPRequestHandler):\n    def log_message(self, *args):\n        pass                                  # a served page should be quiet\n\n    def do_GET(self):\n        parsed = urllib.parse.urlparse(self.path)\n        if parsed.path == "/__reveal":\n            query = urllib.parse.parse_qs(parsed.query)\n            ok = reveal(query.get("f", []))\n            self.send_response(200 if ok else 404)\n            self.send_header("Content-Type", "text/plain")\n            self.end_headers()\n            self.wfile.write(b"ok" if ok else b"no file manager")\n            return\n        super().do_GET()\n\n\ndef main():\n    port = int(sys.argv[1]) if len(sys.argv) > 1 else {port}\n    handler = functools.partial(Handler, directory=ROOT)\n    socketserver.ThreadingTCPServer.allow_reuse_address = True\n    with socketserver.ThreadingTCPServer(("127.0.0.1", port), handler) as httpd:\n        url = "http://127.0.0.1:%d/%s" % (httpd.server_address[1], PAGE)\n        print("Gallery at", url, flush=True)\n        print("Ctrl-C to stop.", flush=True)\n        webbrowser.open(url)\n        try:\n            httpd.serve_forever()\n        except KeyboardInterrupt:\n            print("\\nstopped")\n\n\nif __name__ == "__main__":\n    main()\n'


# A shell wrapper as well, because file managers open .py files in an editor
# while a .sh can be marked runnable and started with a double-click.
LAUNCHER_SH = '#!/bin/sh\n# Double-click this to open the gallery.\n#\n# Written by make_gallery.py next to serve.py -- local edits will be lost.\n#\n# A double-click gives no working directory and no terminal, so this moves to\n# its own folder first, and reopens itself in a terminal when it can, to show\n# the address and let Ctrl-C stop it. Failing that it runs quietly and the\n# browser still opens; stop it from the system monitor.\n#\n# If double-clicking opens this in an editor instead, the file manager needs\n# telling to run executable text files (Dolphin: Settings > Configure Dolphin >\n# General > Confirmations; GNOME Files: Preferences > Executable Text Files).\n\ncd "$(dirname "$0")" || exit 1\n\nif [ -t 1 ]; then\n    exec python3 serve.py "$@"\nfi\n\nfor term in konsole gnome-terminal xfce4-terminal mate-terminal xterm; do\n    command -v "$term" >/dev/null 2>&1 || continue\n    case "$term" in\n        konsole)        exec "$term" --hold -e python3 serve.py "$@" ;;\n        gnome-terminal) exec "$term" -- python3 serve.py "$@" ;;\n        *)              exec "$term" -e python3 serve.py "$@" ;;\n    esac\ndone\n\nexec python3 serve.py "$@"\n'


def write_launcher(out_dir, port):
    """Emit serve.py and serve.sh beside gallery.html."""
    path = os.path.join(out_dir, "serve.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(LAUNCHER.replace("{port}", str(port)))
    os.chmod(path, 0o755)

    shell_path = os.path.join(out_dir, "serve.sh")
    with open(shell_path, "w", encoding="utf-8") as fh:
        fh.write(LAUNCHER_SH)
    os.chmod(shell_path, 0o755)
    return path


def serve(root, page, port, open_browser):
    """Serve the dataset over localhost until interrupted.

    A file:// page may not read its own images, so dragging one out offers only
    its URL and a drop target follows it as a link. Served over http the page
    can read the bytes and attach a real file to the drag.
    """
    import functools
    import http.server
    import socketserver

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=root)
    handler.log_message = lambda *args, **kwargs: None      # no request spam
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), handler) as httpd:
        chosen = httpd.server_address[1]
        url = f"http://127.0.0.1:{chosen}/{os.path.relpath(page, root)}"
        print(f"\nServing {root} at {url}")
        print("Images can now be dragged into other sites. Ctrl-C to stop.")
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=dl.default_root(),
                        help="dataset directory holding dwca/ (default: the "
                             "current directory if it has one)")
    parser.add_argument("--archive", help="override the DwC-A directory")
    parser.add_argument("--media", help="override the media folder")
    parser.add_argument("--out", help="override where gallery.html is written")
    parser.add_argument("--taxonomy",
                        help="CSV of externally resolved ranks (see module docs)")
    parser.add_argument("--taxonomy-authoritative", action="store_true",
                        help="let --taxonomy overwrite ranks present in the archive")
    parser.add_argument("--no-verbatim", action="store_true",
                        help="skip occurrence_raw.csv; faster, but iDigBio's indexed "
                             "ranks drop the epithet whenever it rewrites a name to "
                             "a senior synonym (54.6%% species coverage vs 91.3%%)")
    parser.add_argument("--tree-from", metavar="RANK",
                        help="start the taxonomy tree at this rank (e.g. family, "
                             "superfamily). By default the coarsest ranks are "
                             "dropped until each family appears once")
    parser.add_argument("--serve", action="store_true",
                        help="serve the dataset over localhost after building, "
                             "so images can be dragged into other sites as files")
    parser.add_argument("--port", type=int, default=8000,
                        help="port for --serve (default: 8000, 0 picks a free one)")
    parser.add_argument("--strict", action="store_true",
                        help="fail instead of warning when files in the media "
                             "folder cannot be matched to the archive")
    parser.add_argument("--open", action="store_true",
                        help="open the gallery in a browser when done")
    args = parser.parse_args()
    args.archive = args.archive or os.path.join(args.root, "dwca")
    args.media = args.media or os.path.join(args.root, "media")
    args.out = args.out or os.path.join(args.root, "gallery")

    media_dir = os.path.abspath(args.media)
    if not os.path.isdir(media_dir):
        sys.exit(f"no media folder at {media_dir} -- run download_media.py first")
    os.makedirs(args.out, exist_ok=True)

    if dl.interactive():
        print("Options (press Enter to accept each default):")
        if args.taxonomy is None:
            found = os.path.join(args.root, "taxonomy", "taxonworks.csv")
            if os.path.exists(found):
                shown = os.path.relpath(found, args.root)
                rows = sum(1 for _ in open(found, encoding="utf-8")) - 1
                if dl.ask_yes_no(f"add classifications from {shown} "
                                 f"({rows:,} specimens)", True):
                    args.taxonomy = found
                    print("    where both have a rank, which wins?")
                    print("      [1] the archive, TaxonWorks only fills gaps "
                          "[default]")
                    print("      [2] TaxonWorks, replacing the archive's "
                          "family/genus/etc")
                    args.taxonomy_authoritative = dl._ask(
                        "which", "1").strip() == "2"
        if not args.no_verbatim:
            args.no_verbatim = not dl.ask_yes_no(
                "prefer the publisher's own ranks in occurrence_raw.csv over "
                "iDigBio's indexed ones", True)
        if not args.strict:
            args.strict = dl.ask_yes_no(
                "stop with an error if media/ holds files this archive "
                "does not list", False)
        if not args.open:
            args.open = dl.ask_yes_no("open the gallery in a browser when built",
                                      True)
        if not args.serve:
            args.serve = dl.ask_yes_no(
                "serve it over localhost (needed to drag images into other "
                "sites)", False)
        print()


    taxonomy = load_taxonomy(args.taxonomy)
    if args.taxonomy:
        print(f"External taxonomy: {len(taxonomy['coreid']):,} by coreid, "
              f"{len(taxonomy['name']):,} by name", flush=True)

    print("Reading archive ...", flush=True)
    entries, total, vocabulary, trees, unnamed, unmatched = build_entries(
        args.archive, media_dir, taxonomy, args.taxonomy_authoritative,
        not args.no_verbatim, args.tree_from)

    if unmatched or unnamed:
        print()
        if unmatched:
            print(f"WARNING: {len(unmatched):,} files in {media_dir} match no "
                  f"record in {args.archive}", file=sys.stderr)
            print(f"         and are NOT in the gallery. They most likely belong "
                  f"to a different", file=sys.stderr)
            print(f"         download sharing this folder. For example:",
                  file=sys.stderr)
            for name in unmatched[:3]:
                print(f"           {name}", file=sys.stderr)
        if unnamed:
            print(f"WARNING: {len(unnamed):,} files have no media UUID in their "
                  f"name and were skipped", file=sys.stderr)
        if args.strict:
            sys.exit("refusing to build a gallery that ignores files (--strict)")
        print(file=sys.stderr)

    if not entries:
        sys.exit("no downloaded media found to index")

    page = (PAGE
            .replace("__FIELDS__", json.dumps(FIELDS))
            .replace("__DATA__", json.dumps(entries, ensure_ascii=False))
            .replace("__VOCAB__", json.dumps(vocabulary, ensure_ascii=False))
            .replace("__TREES__", json.dumps([{"label": t["label"], "labels": t["labels"]}
                                              for t in trees]))
            .replace("__TOTAL__", str(total))
            .replace("__RECORD__", IDIGBIO_RECORD)
            .replace("__MEDIA__", MEDIA_PREFIX))
    path = os.path.join(os.path.abspath(args.out), "gallery.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)

    sources = {}
    for entry in entries:
        key = entry[FIELDS.index("source")]
        sources[key] = sources.get(key, 0) + 1
    print(f"Indexed {len(entries):,} images of {total:,} in the archive")
    for tree in trees:
        print(f"  {tree['label'].lower()}: " + " > ".join(tree["labels"]))
    print("  classification: " + ", ".join(f"{n:,} {k}" for k, n in sources.items()))
    launcher = write_launcher(os.path.abspath(args.out), args.port or 8000)
    print(f"Wrote {path} ({os.path.getsize(path) / 1e6:.1f} MB)")
    print(f"      {launcher}  <- run this to serve the gallery and open it")
    print(f"      {os.path.join(os.path.dirname(launcher), 'serve.sh')}"
          f"  <- or double-click this")

    if args.serve:
        serve(os.path.abspath(args.root), path, args.port, args.open)
    elif args.open:
        webbrowser.open(f"file://{path}")


if __name__ == "__main__":
    main()
