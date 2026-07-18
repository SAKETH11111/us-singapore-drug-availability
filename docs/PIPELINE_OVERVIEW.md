# How the pipeline works

This is a walkthrough of the whole pipeline. `ATLAS_SCHEMA.md` is the precise schema reference; this is
the plain-language version.

The pipeline pulls each country's drug register, normalizes every product to a consistent
active-ingredient identity, and stores what each country has rather than any comparison between them.
Overlap, gaps, and availability are computed on demand from the stored tables for whatever set of
countries is selected. Adding a country is adding rows, not changing the structure.

## The flow

1. **Fetch.** Each register is downloaded to a raw snapshot: FDA Drugs@FDA, Singapore HSA, Bangladesh
   DGDA API, Bhutan DRA sheet.
2. **Adapt.** One adapter per country maps its raw fields onto a common shape and absorbs per-source
   quirks (Singapore packs strength into the brand name, Bangladesh publishes no dates).
3. **Normalize.** Every active-ingredient string runs through `src/normalize.py`, which collapses the
   same drug written differently (paracetamol = acetaminophen, salt and spelling variants) while keeping
   different drugs apart (esomeprazole is not omeprazole). This is what gives a drug a single identity
   across all four registers.
4. **Store** the result in the tables below.
5. **Compare.** The WHO Essential Medicines list scopes the starting universe; overlap, gap, and
   presence views are queries over the stored tables.

## The three core tables

The comparison is split into three normalized tables so that country lives in the data, not in the
columns.

`registered_products` is every registered product, one row per product per country, roughly 96,000
rows. This is the brand level: name, form, strength, status, dates.

`substances` is every distinct active ingredient, one row each, no country, roughly 3,552 rows.
Amoxicillin is a single row here no matter how many products or countries carry it. This is the identity
layer, and everything else keys back to it.

`product_ingredients` links the two: one row for each active ingredient in a product, carrying the raw
source text and strength. It holds active ingredients only. Excipients are dropped and a salt folds into
its active moiety, with the original text preserved in `raw_component`. Augmentin is one product with
two rows here, pointing at amoxicillin and clavulanic acid; the source wrote the second as "CLAVULANATE
POTASSIUM" but the substance it links to is `clavulanic acid`. Because of this table, standalone versus
combination is never a stored label, it is simply how many active ingredients a product has.

## Identity and classification

Substances are keyed on a stable internal id, not on the name. Names get corrected and carry variants,
so keying on the id means renaming a drug never breaks the links that point at it.

ATC codes live in their own table, `substance_atc_codes`, one row per substance-code pair, rather than
as a field on the substance. A substance legitimately carries several ATC codes by route and indication
(acetylcysteine has three), which does not fit one column, and keeping them keyed on the substance id
holds the same stability guarantee. The CSV export pipe-joins them onto the substance row for
convenience, but the table is the source of truth. A substance with no clean WHO match has no row here
rather than a guessed code.

`substance_identity_uncertainties` records the cases the pipeline was not confident enough to match.
These are held as UNKNOWN rather than forced into a presence or an absence.

## The essential-medicines universe

The WHO EML scopes the priority set the atlas compares over.

| Table | What it holds |
|---|---|
| `essential_medicine_sets` / `_entries` / `_members` | The list, its entries, and which substances belong to each entry (some entries are combinations or classes). |
| `eml_product_adjudications` | Why a product was accepted as matching an entry, with evidence. |
| `eml_scope_classifications` | Entries that are not drug-register items (blood, devices, food), marked out of scope. |
| `eml_atc_issues` | Source ATC codes corrected during the build. |

## Provenance and determinism

Every build is reproducible. `build_runs` records each build's fingerprint (`build_id`), extraction
date, and universe; the same inputs always produce the same build_id, which is how a dataset is shown to
come from a clean build rather than a hand-edit. `source_snapshots` records each source pull with its
dates and licence. `countries` is the country dimension, the canonical list of tracked countries;
because countries are rows and not columns, adding one is a new row here plus an adapter, with no schema
change.

## Backward compatibility

`legacy_compatibility_observations` preserves the original US/Singapore deliverable. Before the
four-country atlas, that deliverable was a 2,923-row availability table in a fixed column format; this
table holds the product-level rows that roll up into that exact output, kept frozen so the atlas work
never disturbs it. It is why the legacy output is still 2,923 rows to the number.

## Audit

`ingest_issues` is the log of everything the pipeline flagged or handled: veterinary exclusions,
duplicate registration numbers, corrected ATC codes. It is a record of what was caught and what was done
about it, not a queue of things still to review.
