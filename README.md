# iDigBio type specimen media backup

Downloads the media files of an iDigBio Darwin Core Archive, restricted to
occurrences whose `dwc:typeStatus` is not empty, and builds a local HTML gallery
for browsing them.

## Layout

The three scripts are the whole of the source; every dataset is an untracked
working directory beside them. A dataset is any directory containing a `dwca/`,
so several downloads coexist without duplicating the code.

```
iDigBio_Backup/            git repo -- only the scripts and this file
├── download_media.py
├── make_gallery.py
├── fill_taxonomy.py
├── dwca/                  a dataset can sit at the root ...
├── media/                     downloaded files + manifest.csv + download_log.csv
├── gallery/                   gallery.html, references ../media/
├── taxonomy/                  taxonworks.csv + resumable API cache
└── chrysomelidae/         ... or in its own directory
    ├── dwca/
    └── media/  gallery/  taxonomy/
```

Inside `dwca/`, the files that matter:

| file | role |
|------|------|
| `occurrence.csv`     | indexed occurrence fields — `dwc:typeStatus` read here |
| `occurrence_raw.csv` | verbatim publisher values — better ranks, see below |
| `multimedia.csv`     | indexed media records — `ac:accessURI` read here |
| `multimedia_raw.csv` | verbatim media values |

## Usage

```bash
cd chrysomelidae                       # or stay at the root, if dwca/ is there
python3 ../download_media.py           # asks: type specimens only, or all?
python3 ../download_media.py --scope all
python3 ../fill_taxonomy.py            # optional, see TaxonWorks below
python3 ../make_gallery.py --open
```

Each script works on the dataset in the current directory — strictly, the
nearest directory holding a `dwca/`, falling back to where the scripts live.
`--root` picks a different one; `--archive`, `--media`, `--out` override
individually.

Run without `--scope` and the download asks at the prompt; when it is not
attached to a terminal (cron, a pipe) it takes `types` rather than blocking on a
question nobody can answer.

It is safe to run `--scope types` first and `--scope all` later: resume matches
on media UUID, so the type files are recognised and skipped rather than
re-fetched. Note that `manifest.csv` is a snapshot rewritten each run, so the
second run's manifest supersedes the first; `download_log.csv` is append-only
and keeps the full history.

Useful flags: `--limit N` (small trial), `--workers N` (default 8), `--dry-run`,
`--timeout`, `--retries`. `make_gallery.py --strict` refuses to build when files
in `media/` cannot be matched to the archive, instead of warning.

## How records are selected

`occurrence.csv` and `multimedia.csv` are both DwC-A *extensions* — there is no
core file — and are joined on `coreid`, the iDigBio occurrence UUID.

In the Chrysomelidae download used for the figures below, of 509,084 occurrences, 27,931 carry a non-empty `dwc:typeStatus`; 4,109 of those
have media attached, giving **14,716 media records** (a specimen averages ~3.6
views: habitus, labels, barcode).

`dwc:typeStatus` is free text with 4,208 distinct values, so the manifest
classifies each record in a `type_category` column:

| category   | meaning                                                        | media |
|------------|----------------------------------------------------------------|-------|
| `type`     | holotype, paratype, syntype, lectotype, tipo, typus, …          | 14,576 |
| `citation` | Arctos publication vouchers — `voucher of <taxon> in <pub>`      | 83    |
| `negative` | explicit denials: `no aplica`, `none`, `non-type`, `\|null\|`    | 45    |
| `other`    | unclear: `possibletype`, `original`, `figuré` — review these     | 12    |

All four are downloaded; the column exists so they can be separated afterwards.
Arctos citations are parsed into `citation_roles`, `cited_taxa`, `publications`
and `cited_pages`. Every segment of a citation chain is read, so a type buried at
the end of one is still classified as `type` (e.g. NMMNHS *Acanthodes kinneyi*,
`holotype of … in zidek (1992)` after four other citations).

## Resume and file naming

Files are named `<catalogNumber>__<media-uuid>.<ext>`, UUID last so the prefix can
change without breaking resume. The 1,559 records with no catalogue number fall
back to `occ-<first 8 of coreid>__<media-uuid>`, so a specimen's several views
still sort together — a bare UUID would scatter them. Zero collisions across all
14,716 names. Downloads write to `<name>.part` and are renamed only once the body
is complete.

A re-run lists `media/`, extracts the UUID from each filename and skips those
records — the filesystem is the index, so the manifest and log can be deleted
without affecting resume. `.part` files are never counted as complete. Ctrl-C is
safe: in-flight downloads finish and the manifest is still written.

## Expected failures (~1,390, roughly 9.5%)

| host / cause                  | files | detail                                  |
|-------------------------------|-------|-----------------------------------------|
| `mediaphoto.mnhn.fr`          | 1,236 | HTTP 403, Cloudflare bot challenge       |
| `arctos.database.museum`      | 84    | connect timeout                          |
| `digitalgallery.nhm.org:8085` | 58    | connect timeout                          |
| malformed `ac:accessURI`      | 15    | `/mnt/target-images/…` staging paths     |

The MNHN block is an access control, not a rate limit — a header change does not
get around it, and neither does iDigBio's own cache (`api.idigbio.org/v2/media/
<etag>` returns 404 for all of these, i.e. iDigBio could not fetch them either).
The legitimate routes are an MNHN data request or the same specimens via GBIF.

HTTP 4xx responses are not retried (except 408/429), since a refusal will not
become a success on the second attempt; 5xx and network errors retry with
backoff.

An unreachable host costs 3 attempts x the 60 s connect timeout *per file*, and
142 files sit behind two such hosts — about 55 minutes of waiting. After
`DEAD_HOST_STRIKES` (5) consecutive connection failures a host is written off for
the rest of the run and its remaining files fail instantly; they are logged
normally and retried next run. A 403 or 404 does not count, since those prove the
host is up and answering.

### Linking images to specimens

`multimedia.csv` carries **only `coreid`** — no catalogue number. (`multimedia_raw.csv`
has a `dwc:catalogNumber` column, but it is empty for all 14,716 rows.) So the
occurrence UUID is the sole link between an image and its specimen, and the
gallery groups on it. Catalogue numbers merely order the grid for readability:
they are unique per specimen in *this* archive (0 collisions across 3,758) but
are only guaranteed unique within an institution.

## Gallery

`make_gallery.py` reads the archive plus whatever is in `media/` — it can be run
mid-download. `gallery.html` embeds its metadata inline (no server, no network),
lazy-loads thumbnails in pages of 120, and supports search over catalogue number,
taxon, typeStatus, institution and publication, plus category/institution
filters. Clicking an image opens it full size with its metadata and lineage, a
link to the iDigBio record and to the original media URL; arrow keys step through
results.

### Taxonomic tree

The sidebar is a rank-nested tree with per-node counts; selecting a node filters
the grid, and it re-counts as the search and dropdown filters change. Records
missing a rank sit under an explicit *unplaced (rank)* node rather than being
dropped, so the counts stay honest. Levels that do not branch are opened
automatically (this archive is entirely Chrysomelidae, so it lands on the genus
list), and a rank nothing populates is left out of the tree entirely.

Rank coverage in this archive, across the 14,716 type media records:

| rank            | coverage | source                                          |
|-----------------|---------:|-------------------------------------------------|
| kingdom         |   99.5%  | `occurrence.csv`                                 |
| phylum … family |    100%  | `occurrence.csv`                                 |
| tribe           |     0.6% | **not a column** — recovered from `dwc:higherClassification` |
| genus           |    99.8% | `occurrence.csv`, gaps filled from `occurrence_raw.csv` |
| subgenus        |     5.0% | `occurrence_raw.csv` only                        |
| specificEpithet |    95.3% | 54.6% indexed, raised by `occurrence_raw.csv` (see below) |

`dwc:tribe` exists in `occurrence_raw.csv` but is empty for every record here, so
tribes are parsed out of `dwc:higherClassification` instead — an unranked
lineage, where the rank has to be inferred from the name ending (`-ini`
zoological, `-eae` botanical, excluding `-aceae`/`-oideae`). That yields 84
records across 6 tribes; the rest stay unplaced.

### Verbatim vs indexed ranks

iDigBio's `occurrence.csv` carries its *matched* classification. Where it
rewrites a name to a senior synonym but cannot place the species under it, it
keeps the accepted genus and drops the epithet:

| field           | `occurrence_raw.csv` (publisher) | `occurrence.csv` (iDigBio) |
|-----------------|----------------------------------|----------------------------|
| genus           | `Hadropoda`                      | `aedmon` (senior synonym)   |
| scientificName  | `Hadropoda xanthoura`            | `hadropoda`                 |
| specificEpithet | `xanthoura`                      | *(empty)*                   |

That is why the tree shows *Hadropoda* specimens under **aedmon → unplaced**:
the archive says so. Reading the verbatim file back in raises species coverage
from **54.6% to 95.3%** and recovers `subgenus` (5%), so `make_gallery.py` does
it by default; `--no-verbatim` skips it and saves a ~574 MB read.

Note this affects metadata only — the set of media records to download is
identical either way, since selection depends solely on `dwc:typeStatus`.
`manifest.csv` is still written from the indexed values.

### Filling ranks from an external source

Ranks the publisher left empty — `specificEpithet` for 45% of records, `tribe`
for nearly all — are meant to be fillable from a name-parsing service or the GBIF
backbone. `make_gallery.py --taxonomy names.csv` merges such a file in without
touching the archive:

```csv
scientificName,tribe,specificEpithet
chrysomela falsa,Chrysomelini,falsa
```

Matched on `coreid` when that column is present, otherwise on `scientificName`
(case-insensitive); any subset of rank columns is accepted, with or without the
`dwc:` prefix. By default it only fills what the archive left empty —
`--taxonomy-authoritative` lets it overwrite. Each record keeps its provenance
(`archive` / `mixed` / `external`), shown in the image detail panel, so an
inferred classification is never mistaken for a published one. A rank supplied
this way appears in the tree automatically.

### fill_taxonomy.py — TaxonWorks

Resolves names against the [TaxonWorks API](https://api.taxonworks.org) and
writes `taxonomy/taxonworks.csv` in exactly the `--taxonomy` format above. The
archive is opened read-only; nothing in `dwca/` is ever modified.

TaxonWorks is not limited to any one group — it is a general nomenclatural
workbench covering all taxonomic groups (animals, plants, fungi, bacteria; ICZN,
ICN, ICNP), so this works for any DwC-A, not just insects. What it *can* answer
is bounded by the project the token belongs to, not by taxon.

```bash
python3 ../fill_taxonomy.py --missing-only     # prompts for the token
python3 ../make_gallery.py --taxonomy taxonomy/taxonworks.csv
```

**The token is never stored.** It is not written into the scripts, not read from
an environment variable by default, and not saved to disk — you are prompted for
it at the start of each run (`getpass`, so it does not echo or land in shell
history). `--project-token` exists for automation if you want it.

What it may and may not change: it fills only ranks **above** the species
epithet. `dwc:scientificName` and `dwc:specificEpithet` are always left exactly
as published — an external database supplies the scaffold above a name, it does
not rename a specimen. The name TaxonWorks matched is recorded separately in a
`taxonworks_name` column, shown in the gallery's detail panel as a voucher for
how the placement was reached and included in the search index.

Output is keyed on **`coreid`**, never on the name. Keying on
`dwc:scientificName` would be wrong: iDigBio shortens it to the bare genus
whenever it cannot match the species, so in the Chrysomelidae download a single
`diabrotica` string covered 98 distinct taxa across 5,634 media records — one
lookup would have stamped the same lineage onto all of them. The name actually
queried is the publisher's verbatim one where available (far less ambiguous:
2,782 distinct, 10 ambiguous), else a genus/epithet pair, else the indexed name.

* base `https://sfg.taxonworks.org/api/v1` (`--base-url` for the sandbox)
* every call needs `project_token`, or `token` + `project_id`, as query
  parameters. A project token is **not secret** — it marks a project's data as
  public — but it is per project, so results only cover that project's names.
  Create one in your TaxonWorks project preferences.
* `GET /taxon_names?name=&name_exact=` to search, then `parent_id` is walked
  upward through `GET /taxon_names/{id}`; `rank_string`
  (`NomenclaturalRank::Iczn::GenusGroup::Genus`) gives each ancestor's rank.
* responses are cached in `taxonomy/taxonworks_cache.json`, so an interrupted
  run resumes instead of re-querying. `--list-only` shows the work without
  touching the network; `--missing-only` restricts to the 2,124 names that still
  lack a tribe or epithet after the archive is exhausted.

**Verified so far:** name selection, caching, CSV output, the missing-token
guard and the live 401 response. The resolution path itself has *not* been run
against real data — that needs a project token, which is yours to supply.
