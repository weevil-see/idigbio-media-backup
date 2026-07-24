# idigbio-media-backup

Download the media files referenced by an [iDigBio](https://www.idigbio.org)
Darwin Core Archive, browse them in a self-contained HTML gallery, and
optionally resolve their classification against the
[TaxonWorks](https://taxonworks.org) API.

Three scripts, no dependencies beyond `requests`. They work on any iDigBio
DwC-A — nothing is specific to a taxonomic group.

```bash
cd my-dataset                    # a directory containing dwca/
python3 ../download_media.py     # asks: type specimens only, or all media?
python3 ../fill_taxonomy.py      # optional, see TaxonWorks below
python3 ../make_gallery.py
python3 gallery/serve.py         # open it, served so drag-and-drop works
```

Each script asks for its options at startup, so none of them need flags. The
build also writes `gallery/serve.sh`, a shell wrapper you can double-click —
file managers open `.py` files in an editor, and may need telling to run
executable text files at all.

## Requirements

Python 3.9+ and `requests` (`pip install requests`). A browser to view the
gallery; no server needed.

## Getting an archive

Search [iDigBio's portal](https://www.idigbio.org/portal/search), request a
download, and unpack the resulting zip into a directory named `dwca/`. The files
that matter:

| file | role |
|------|------|
| `occurrence.csv`     | indexed occurrence fields — `dwc:typeStatus` is read here |
| `occurrence_raw.csv` | verbatim publisher values — often better ranks, see below |
| `multimedia.csv`     | indexed media records — `ac:accessURI` is read here |
| `multimedia_raw.csv` | verbatim media values |

Note there is **no core file**: `occurrence.csv` and `multimedia.csv` are both
DwC-A *extensions*, joined on `coreid`, the iDigBio occurrence UUID.

## Layout

The scripts are the whole of the source; each dataset is an untracked working
directory. A dataset is any directory containing a `dwca/`, so several downloads
coexist without duplicating code.

```
.
├── download_media.py
├── make_gallery.py
├── fill_taxonomy.py
├── dwca/                  a dataset can sit at the repo root ...
├── media/                     downloaded files + manifest.csv + download_log.csv
├── gallery/                   gallery.html + serve.py/serve.sh, written by the build
├── taxonomy/                  taxonworks.csv + resumable API cache
└── some-other-dataset/    ... or in its own directory
    ├── dwca/
    └── media/  gallery/  taxonomy/
```

Each script operates on the dataset in the current directory — strictly, the
nearest directory holding a `dwca/`, falling back to where the scripts live.
`--root` selects a different one; `--archive`, `--media`, `--out` override
individually.

## Downloading

```bash
python3 ../download_media.py                # prompts for scope
python3 ../download_media.py --scope types  # only occurrences with a typeStatus
python3 ../download_media.py --scope all    # every media record
```

All three scripts ask for their options at startup, so they are useful run bare
with no flags. Press Enter to accept each default; Ctrl-C cancels rather than
accepting them. A flag given on the command line is never asked about, and
nothing is asked unless **both** stdin and stdout are a terminal — so cron,
pipes and redirects get the defaults instead of blocking on a question whose
text the pipe swallowed. For the download that means `--scope types`.

Running `--scope types` first and `--scope all` later is safe: resume matches on
media UUID, so the type files are recognised and skipped. `manifest.csv` is a
snapshot rewritten each run, so the second supersedes the first;
`download_log.csv` is append-only and keeps the full history.

Other flags: `--limit N` (small trial), `--workers N` (default 8, asked if
omitted), `--dry-run`, `--timeout`, `--retries`.

### File naming and resume

Files are named `<catalogNumber>__<media-uuid>.<ext>`, with the UUID **last** so
the prefix can change without breaking anything. Records with no catalogue
number fall back to `occ-<first 8 of coreid>__<media-uuid>`, so a specimen's
several views still sort together.

A re-run lists the media folder, extracts the UUID from each filename, and skips
those records. **The filesystem is the index** — the manifest and log can be
deleted without affecting resume. Downloads write to `<name>.part` and are
renamed only once the body is complete, so a partial file is never mistaken for
a finished one. Ctrl-C is safe: in-flight downloads finish and the manifest is
still written.

`coreid` is what links an image to a specimen. `multimedia.csv` carries *only*
`coreid` — the catalogue number lives on the occurrence side and reaches media
through the join. Catalogue numbers are only unique within an institution, so
they order the gallery for readability but never decide what belongs together.

### Failures

Not everything is fetchable, and the reasons are worth knowing:

- **Provider bot protection.** Some institutions serve media from behind
  Cloudflare, which answers `403` with a challenge page. This is an access
  control, not a rate limit — no header changes get around it. Ask the
  institution for API/bulk access, or fetch those specimens via GBIF.
- **Dead hosts.** Some URLs point at servers that no longer answer.
- **Malformed `ac:accessURI`.** A few records publish a local staging path
  (`/mnt/…`) instead of a URL; these are skipped up front as `invalid-url`.

HTTP 4xx is not retried (except 408/429) — a refusal will not become a success
on the second attempt. 5xx and network errors retry with backoff.

After `DEAD_HOST_STRIKES` (5) consecutive *connection* failures a host is set
aside, which avoids spending 3 × the connect timeout on each of its remaining
files. It is set aside for `DEAD_HOST_COOLDOWN` (300 s) only, never for the rest
of the run: after that one file is let through as a probe, and a success clears
the record entirely. Files skipped meanwhile are re-queued once before being
failed.

A local network drop is told apart from dead hosts. Remote servers do not fail
in unison, so when `OUTAGE_HOSTS` (3) distinct hosts fail to connect inside
`OUTAGE_WINDOW` (60 s) the run treats the problem as local: no host is written
off, and it pauses for `OUTAGE_PAUSE` (30 s) before carrying on. Without this a
brief outage at your end would blame every host at once and waste the rest of
the run.

Progress is reported from the rate of *successful* transfers, since a burst of
instant refusals otherwise makes the estimate meaningless.

## Gallery

`make_gallery.py` reads the archive plus whatever is in `media/`, so it can be
run mid-download. It writes a single self-contained `gallery.html`: metadata is
embedded inline as JSON, images are referenced as `../media/<file>`, and nothing
is fetched at runtime — a `file://` page cannot read a local CSV, and this way
there is no server to run. It is a snapshot; re-run it after downloading more.

Thumbnails lazy-load in pages of 120. Search covers catalogue number, taxon,
typeStatus, institution, publication, licence and any externally matched name.
Dropdowns filter by type category, institution and licence. Clicking an image
opens it full size with its metadata and lineage, plus links to the iDigBio
record and the original media URL; arrow keys step through results.

### Dragging images into other sites

Opened from disk, the page is a `file://` document and **may not read its own
images** — `fetch` is blocked and a canvas readback taints. A dragged `<img>`
can then offer only its URL, so a site you drop it on follows it as a link and
navigates instead of uploading.

Every build writes **`gallery/serve.py`** next to the page. Run it and the
gallery opens over http, where it can read its own images and attach a real file
to the drag:

```bash
python3 gallery/serve.py            # opens the browser; Ctrl-C to stop
python3 gallery/serve.py 8123       # another port; 0 picks a free one
```

The launcher is standalone and regenerated on every build, so the gallery folder
keeps working if it is copied elsewhere. `make_gallery.py --serve` does the same
thing immediately after building.

| origin | drag carries | drop target sees |
|--------|--------------|------------------|
| `file://`   | `text/uri-list`, `DownloadURL`            | a link |
| `http://127.0.0.1` | `text/uri-list`, `DownloadURL`, **`Files`** | an `image/jpeg` file |

The file is fetched when you hover or press on an image, because `dragstart`
cannot wait for it. `DownloadURL` is set either way, which lets Chromium save
the image when dragged to a file manager. When the page cannot do this it says
so under the heading rather than failing silently.

`--serve` binds to `127.0.0.1` only, and `--port 0` picks a free port.

The bytes are fetched when an image is hovered or opened, so a drag begun the
instant a thumbnail appears may still fall back to a link. Cards, the modal
image and the filmstrip are all draggable.

### Opening the files in a file manager

Some upload widgets refuse a browser-to-browser drag whatever it carries. When
served, the detail panel offers **show these files in the file manager**, which
opens `media/` with that specimen's files already selected, the way a browser
reveals something it has just downloaded — from there they drag as ordinary
files.

**This needs `serve.py`.** It works by the launcher running a command on your
machine (`dolphin --select …`, `nautilus --select …`, and so on), which no web
page can do by itself. A gallery opened from disk, or copied to a static web
host, cannot offer it: browsers may not start local programs, and the button
says so instead of failing quietly. Only names that resolve inside `media/` are
accepted, and `xdg-open` on the folder is the fallback where no known file
manager is installed.

Starting the launcher while an older copy is serving the same port stops that
one first — but only if it is this script; anything else keeps the port and the
new instance moves to a free one.

### When an image may show a different specimen

The archive links media to occurrences by `coreid`, and that link is sometimes
wrong: a photograph of one specimen attached to another. Institutions name image
files after the specimen photographed, so the archive carries the evidence —
NHMUK `013885056_additional_3` sitting on a record catalogued `nhmuk013885065`,
digits transposed.

Titles hold several numbers (`010131654_127044_890916` is a barcode and two
unrelated ids), so only numbers of the same length as the catalogue number are
compared. If one matches, the title agrees; if numbers of that length exist and
none matches, the image is flagged. A title numbered on another scheme entirely
says nothing and is left alone. In one weevil archive that flagged **58 media
across 12 specimens (0.09%)**, each a plausible transposition.

Flagged records carry a `⚠ check` badge in the grid and an explanation in the
detail panel, and a `title_check` column in `manifest.csv`. It is a prompt to
look, not a verdict: the archive states the link and nothing here changes it.

### Licence filter

Licence terms come from `dcterms:rights` with the URL from
`xmpRights:WebStatement`, and the detail panel links one to the other. Records
where the publisher stated nothing are shown as **`(none stated)`** rather than
left blank: an empty cell reads as "unknown" when what it means is "not
stated", and that is exactly the group to isolate before reusing anything. Both
fields are also columns in `manifest.csv`.

Licence sits on the *media* record, not the specimen, so different images of one
specimen can carry different terms — filtering by licence can therefore split a
specimen's views. That is faithful to the data, not a bug.

### Attribution

A licence says what may be done; it does not say whom to credit. Reusing an
image — adding it to TaxonWorks, say — needs the rights holder and usually the
photographer as well, and those live in the verbatim media file under several
different terms depending on the publisher. The first one present is taken, most
specific first:

| role | fields consulted, in order | coverage |
|---|---|---:|
| creator — the photographer | `dc:creator`, `dcterms:creator`, `photoshop:Credit` | 91% |
| copyright holder — the licensor | `dcterms:rightsHolder`, `xmpRights:Owner` | 34% |
| provider — served the record | `ac:providerLiteral`, `ac:provider` | 89% |
| terms | `dcterms:license`, `xmpRights:UsageTerms` | 95% |

**The provider is not an attribution.** That an institution published the record
does not establish that it holds the rights, so it is reported separately and
kept out of the copied line. Counting it as a rights holder would have claimed
94% coverage where the truth is 34%.

They are joined to the individual image on `ac:accessURI` where the publisher
gives one (92.6% of rows), falling back to the specimen's `coreid`.

Only **30%** of type media in one weevil archive carry both a creator and a
copyright holder; **4%** carry neither and name only a provider. Where a licence
requires attribution and the archive does not say whom to credit, the honest
course is to ask the provider rather than guess — the panel says so instead of
showing a blank field.

The panel shows two rows, the two roles TaxonWorks would have you fill —
**Creator** and **Copyright holder** — each saying *not stated* when the archive
is silent. The provider appears only inside the holder row, and only when there
is no holder, as somewhere to ask: naming it as the holder would assert a right
the archive does not record. **Copy attribution** puts one line on the
clipboard:

```
Jens Prena / Smithsonian Institution, NMNH, Entomology
(https://www.si.edu/termsofuse). https://collections.nmnh.si.edu/media/…
```

`manifest.csv` gains `rights_holder`, `creator`, `provider` and `usage_terms`
columns, so the three can be reconciled outside the gallery too.

### Capitalisation

iDigBio lower-cases what it indexes, so the archive yields `animalia`, `usnm`,
`united states`. The gallery restores conventional capitalisation for display:
taxon names above species are capitalised, epithets stay lower case, place names
are title-cased (`Democratic Republic of the Congo`), and institution codes are
upper-cased.

Each rule applies **only to an all-lower-case value**, so anything already
properly cased is left untouched — externally resolved names arrive as
`Entiminae` and `Ptilopus` and must not be mangled. This is presentation only:
the stored data, the manifest and the search index are unchanged, so searching
still works in lower case.

If files in the media folder match no record in the archive — usually another
dataset's download sharing the folder — you get a warning naming examples.
`--strict` fails instead of building a gallery that silently omits them.

### Trees

The sidebar holds a taxonomy tree (kingdom → … → species) and a geography tree
(continent → country → stateProvince), each with per-node counts, a type-ahead
filter and its own selection. Counts respond to the search box and the other
tree, but not to a tree's own selection, so sibling counts stay meaningful.
Selections appear as removable chips.

Records missing a level sit under an explicit *unplaced (rank)* node rather than
being dropped, so counts stay honest. Levels that do not branch are opened
automatically, and a rank nothing populates is left out entirely — `dwc:tribe`
reappears by itself once something fills it.

## Classification

### Verbatim vs indexed ranks

iDigBio's `occurrence.csv` carries its *matched* classification. Where it
rewrites a name to a senior synonym but cannot place the species under it, it
keeps the accepted genus and drops the epithet. A real example, *Hadropoda
xanthoura* (*Hadropoda* being a synonym of *Aedmon*):

| field           | `occurrence_raw.csv` (publisher) | `occurrence.csv` (iDigBio) |
|-----------------|----------------------------------|----------------------------|
| genus           | `Hadropoda`                      | `aedmon`                    |
| scientificName  | `Hadropoda xanthoura`            | `hadropoda`                 |
| specificEpithet | `xanthoura`                      | *(empty)*                   |

Such records appear in the tree under the accepted genus → *unplaced*. Reading
the verbatim file back in recovers the epithet — in one Chrysomelidae download
that raised species coverage from 54.6% to 95.3%, and recovered `subgenus`. The
gallery does this by default; `--no-verbatim` skips it and avoids a large read.

This affects metadata only. Which media get downloaded depends solely on
`dwc:typeStatus`.

`dwc:tribe` is absent from `occurrence.csv` altogether. Where
`dwc:higherClassification` carries one it is recovered by name ending (`-ini`
zoological, `-eae` botanical, excluding `-aceae`/`-oideae`), but that field is
sparse; tribes generally need an external source.

### Filling ranks from an external source

`make_gallery.py --taxonomy names.csv` merges externally resolved ranks without
touching the archive:

```csv
coreid,family,genus,tribe
6f1e…,Familyidae,Genus,Tribeini
```

Matched on `coreid` when that column is present, otherwise on `scientificName`
(case-insensitive); any subset of rank columns is accepted, with or without the
`dwc:` prefix. By default it fills only what the archive left empty —
`--taxonomy-authoritative` lets it overwrite. Each record keeps its provenance
(`archive` / `mixed` / `external`), shown in the detail panel, so an inferred
classification is never mistaken for a published one.

### fill_taxonomy.py — TaxonWorks

Resolves names against the [TaxonWorks API](https://api.taxonworks.org) and
writes `taxonomy/taxonworks.csv` in exactly the format above. The archive is
opened read-only.

TaxonWorks is a general nomenclatural workbench covering all taxonomic groups
and all the codes (ICZN, ICN, ICNP), so this is not limited to any one group.
What it can answer is bounded by the *project* a token belongs to, not by taxon.

```bash
python3 ../fill_taxonomy.py --missing-only    # prompts for the token
python3 ../fill_taxonomy.py --report-only     # report from the cache, no API calls
python3 ../fill_taxonomy.py --retry-misses    # re-query names cached as misses
python3 ../make_gallery.py --taxonomy taxonomy/taxonworks.csv
```

#### Credentials

Never hard-coded, never committed. They live in a git-ignored `api.yml`, looked
for in the dataset directory first and then next to the scripts, so one file can
serve every dataset or a dataset can carry its own:

```yaml
---
url: https://sfg.taxonworks.org/api/v1
project_token: <token>
```

If no such file exists you are prompted (`getpass`, so it does not echo or reach
shell history) and the file is written for you with mode `600`.
`--project-token` and `--base-url` override it for automation. A project token
is not secret in TaxonWorks' model — it marks a project's data as public — but
credentials still do not belong in a repository. Create one in your project's
preferences.

#### What it may and may not change

It fills only ranks **above** the species epithet. The ladder runs the full
range — kingdom, subkingdom, infrakingdom, superphylum, phylum, subphylum,
superclass, class, subclass, infraclass, superorder, order, suborder,
infraorder, superfamily, epifamily, family, subfamily, supertribe, tribe,
subtribe, genus, subgenus, section, series — because listing a rank costs
nothing: the gallery drops any level nothing fills. Darwin Core has terms for
some of these; the rest are prefixed `tw:` and can only come from an external
source. `dwc:specificEpithet`, `dwc:infraspecificEpithet` and `dwc:scientificName`
are left exactly as published, and `scientificName` is not even an output
column: an external database supplies the scaffold above a name, it does not
rename a specimen.

The name TaxonWorks matched is *appended*, never substituted — recorded as
`taxonworks_name`, shown in the gallery's detail panel as a voucher for how the
placement was reached, and included in the search index.

A rank is only overwritten when the archive left it empty, unless you pass
`--taxonomy-authoritative`.

#### Names that have changed since publication

A published name is often not the current one: `Pandeleteius subtropicus Fall,
1907` is now `Scalaventer subtropicus (Fall, 1907)`. Where a match is flagged
invalid, it is followed to the current name through `cached_valid_taxon_name_id`
and the classification is taken from there. Both are recorded —
`taxonworks_name` for the name in use, `matched_name` for what the search hit,
`is_synonym` marking the difference — and `dwc:scientificName` is untouched, so
the published name stays and the interpretation sits beside it.

#### How names are matched

Records are keyed on **`coreid`**, never on the name: the indexed
`scientificName` is shortened to the bare genus whenever iDigBio cannot match a
species, so one string can stand for dozens of distinct taxa and a name-keyed
lookup would stamp a single lineage onto all of them.

The name queried is the publisher's verbatim one where available, else a
genus/epithet pair, else the indexed name. It is then normalised, because
publishers write authorities into the name and TaxonWorks matches on the name
alone:

```
Acalles basalis LeConte 1876       ->  Acalles basalis
Alluria spinosa (Fabricius, 1801)  ->  Alluria spinosa
```

Under both codes the genus is capitalised and every epithet is lower case, while
an authority is capitalised — so the leading capitalised word plus the lower-case
words after it are kept, and everything from the next capital is dropped.

If the species still does not match, the **genus** is tried on its own. This is
what places open nomenclature — `Curculio sp.`, `Larinus cf. obtusus` can never
match as written — and species that are simply absent from the project. Such
records still gain family, tribe and genus, which is all that gets filled
anyway. The gallery marks them `(placed by genus)` so a genus-level placement is
never mistaken for a species-level determination.

#### Gender variants

`--gender-variants` is tried before falling back to the genus, and is **off
unless asked for**. An epithet agrees in gender with its genus, so one name has
up to three written forms — `albidus`, `albida`, `albidum` — and a species moved
to a genus of another gender keeps its stem and changes only its ending. Where
the archive spells a name for one gender and the nomenclator for another, they
are the same name; matching them is a rule, not a resemblance. The genus must
still be the one asked about, since a different genus would replace every rank
this fills.

An earlier attempt scored candidates by string similarity. It was withdrawn as
unsafe: across 6,753 binomials from one archive, **609 pairs of distinct species
in the same genus scored above 0.90** and 63 still did at 0.95 —
`Conotrachelus carinatus` against `C. ecarinatus` at 0.979, `aratus` against
`armatus` at 0.976. No threshold separates a typo from a sibling species.
Gender agreement has no such failure mode: the stems differ, so those two
generate disjoint sets of forms and can never meet. On the same archive the rule
unifies 16 within-genus pairs, each one species written for two genders —
`sitona hispidula` and `sitona hispidulus`, `micracis nanula` and `nanulus`.

Matches made this way are marked `matched-gender-variant` in the report.

#### The match report

Every run writes `taxonomy/match_report.csv`, one row per queried name, and
prints a summary. `--report-only` regenerates it from the cache without touching
the network — safe to run while a resolve is still going.

| status | meaning |
|--------|---------|
| `matched`             | the name itself resolved |
| `matched-via-genus`   | species unmatched; placed by its genus |
| `absent-from-project` | not in the project the token belongs to |
| `open-nomenclature`   | `sp.`, `cf.`, `aff.`, `indet.` — cannot match by design |
| `unparsable`          | nothing usable left after normalisation |
| `not-queried`         | not in the cache yet — not reached, or excluded by `--missing-only` |

Columns: the raw name, what was actually sent to the API, how it matched, the
returned name/rank/id, how many candidates the search returned, which ranks were
filled, the resulting lineage, and how many specimens and media files the name
covers — so a name accounting for hundreds of images is visible immediately.

A large `not-queried` share simply means the run has not finished. Resolving is
not fast: each new name costs one search call, a second if the genus fallback
runs, plus one call per ancestor while walking the lineage, with a pause between
each (`--pause`). Both caches persist, so stopping and restarting is cheap.

`--missing-only` skips specimens that already have every rank in
`--missing-ranks` (default `family,genus,tribe,subfamily`). Since most archives
carry no tribe or subfamily at all, the default usually selects everything —
narrow it (`--missing-ranks genus`) for a short run. `--list-only` prints the
names that would be queried and exits without touching the network.
`--user-token` with `--project-id` authenticates as a user instead of using a
project token.

**Expect a substantial `absent-from-project` share.** A TaxonWorks project covers
the groups its curators maintain, not all of nomenclature; absence is an
ordinary outcome, not a failure, which is why it is reported as its own status.

#### Files, and what deleting them does

Nothing is fetched at view time — the gallery embeds its data at build time, so
these only matter when you next run a script.

| file | role | delete it and … |
|------|------|-----------------|
| `taxonomy/taxonworks_cache.json` | per-name results, so an interrupted resolve does not re-ask | nothing downstream changes; the next resolve re-queries those names |
| `taxonomy/taxonworks_ancestors.json` | the lineage records behind them, shared between names | the next resolve re-walks every lineage — slow, but harmless |
| `taxonomy/taxonworks.csv`        | the actual input to the gallery | the next `make_gallery.py` run falls back to archive-only classification |
| `taxonomy/match_report.csv`      | the report, regenerable any time | nothing; recreate with `--report-only` |
| `gallery/gallery.html`           | a snapshot with values already embedded | it keeps working until you rebuild it |

The enrichment is opt-in either way: without `--taxonomy` the gallery is
archive-only regardless of what sits in `taxonomy/`. Pointing `--taxonomy` at a
missing file is an error, not a silent fallback.

## How type records are selected

`--scope types` keeps occurrences whose `dwc:typeStatus` is not empty. That
field is free text and, in practice, messy — one download contained 4,208
distinct values. The manifest therefore classifies each record in a
`type_category` column:

| category   | meaning |
|------------|---------|
| `type`     | holotype, paratype, syntype, lectotype, tipo, typus, … |
| `citation` | Arctos publication vouchers — `voucher of <taxon> in <publication>` |
| `negative` | explicit denials: `no aplica`, `none`, `non-type`, `\|null\|` |
| `other`    | unclear: `possibletype`, `original`, `figuré` — worth reviewing |

All are downloaded; the column exists so they can be separated afterwards.

Some publishers (Arctos) use this field for specimen citations rather than
nomenclatural status. Those are parsed into `citation_roles`, `cited_taxa`,
`publications` and `cited_pages`. Every segment of a citation chain is
inspected, because a genuine type designation can hide at the end of one — for
example a record listing four ordinary citations followed by
`holotype of … in zidek (1992)`.

## Output files

| file | |
|------|--|
| `media/<name>.<ext>`      | the media files |
| `media/manifest.csv`      | every record considered, rewritten each run: status, filename, URL, coreid, taxonomy, geography, licence, type category, citation fields |
| `media/download_log.csv`  | append-only, one row per attempt, flushed immediately; survives interruption and accumulates across runs |
| `gallery/gallery.html`    | the browsable gallery |
| `taxonomy/taxonworks.csv` | external classification, if resolved |
| `taxonomy/match_report.csv` | per-name account of what matched and what did not |
| `taxonomy/taxonworks_cache.json` | per-name API cache, so an interrupted resolve resumes |
| `taxonomy/taxonworks_ancestors.json` | cached lineage records, shared across names |

## Licence

None specified yet — add one before reusing.
