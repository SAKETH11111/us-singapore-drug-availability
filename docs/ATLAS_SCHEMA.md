# Atlas schema

The schema stores source observations and normalized ingredient membership. It does not store a
country comparison as truth. Comparison columns are generated from the selected countries, universe,
and accepted source snapshots.

## Core tables

| Table | Grain | Purpose |
|---|---|---|
| `build_runs` | one deterministic build | Extraction date, schema version, and universe |
| `countries` | one country | Stable ISO-like country code and display name |
| `source_snapshots` | one country source in a build | URLs, capture/source-as-of dates, acceptance reason, row counts, source hash, coverage, and absence wording |
| `substances` | one normalized ingredient identity | Deterministic ID, canonical normalized key, display name, identity basis, and optional UNII anchor |
| `registered_products` | one normalized source product | Country, source key, declared/resolved component counts, application type, dates/status evidence, and scope flags |
| `product_ingredients` | one ingredient in one product | Ingredient position, raw text/strength, normalized substance, and optional ATC metadata |
| `essential_medicine_sets` | one named universe | Edition, source, logical hash, and license |
| `essential_medicine_entries` | one active eEML recommendation row | Original recommendation fields, including co-prescription metadata |
| `essential_medicine_members` | one ingredient member of an EML entry | Links single and combination recommendations to normalized substances |
| `substance_identity_uncertainties` | one EML/source identity review candidate per country | Prevents related but non-identical normalized names from becoming false gap claims without asserting equivalence |
| `ingest_issues` | one audit signal | Unresolved identity, source flags, collisions, swaps, and exclusions |

`legacy_compatibility_observations` is an isolated regression seam. It retains the delivered
NDA/BLA-before-dedup US and HSA observation shape so the historical 21-column renderer cannot leak
ANDA records or new country rules into the old output.

## Presence model

The primary comparison uses `registered_products.included_in_presence` plus accepted snapshots.
Standalone versus combination mode is derived from one validated product-level component count. The
count includes declared source components that could not resolve to an identity, so a known
multi-active product cannot become `STANDALONE` merely because one component is unresolved. The mode
is not repeated on every ingredient link. Audited parser artifacts that are not independent source
actives, such as the short `LH` fragment inside FDA's MENOTROPINS composition annotation, are not
counted as unresolved co-actives.

| Computed observation | Meaning |
|---|---|
| `STANDALONE` | At least one listed product contains only that normalized ingredient |
| `COMBO_ONLY` | The ingredient is listed, but every observed product containing it is a combination |
| `OBSERVED_ABSENCE` | No matching listed product was observed in an accepted snapshot |
| `UNKNOWN` | Snapshot acceptance, current qualification, or exact identity evidence is insufficient for an absence inference |

`OBSERVED_ABSENCE` is source-bounded. The source URL, snapshot status, acceptance reason,
`evidence_note`, `coverage_scope`, and presence basis travel with every long comparison result.
Rejected snapshots retain their provenance but produce `UNKNOWN`, never observed absence.

An accepted snapshot can still produce identity-level `UNKNOWN`. The build records strict
whole-token containment between an EML identity and a broader or more-specific listed source
identity in `substance_identity_uncertainties`. If no exact identity is present, that candidate
holds the gap claim for review. It never establishes equivalence or presence. The long view exposes
the reason as `identity_match_requires_review`, including the candidate names in its evidence note.

Bhutan additionally has nullable `current_qualified`. It evaluates validity only when the separate
actions snapshot is present and only when registration number and normalized ingredient identity
agree. Collisions and ambiguous action dates remain unknown at country-substance grain: any true
product establishes presence, no true product plus any null evidence yields `UNKNOWN`, and only
all-false/no-product evidence yields current-qualified absence. The build emits both listed-evidence
and Bhutan-current sensitivity views because Bangladesh cannot support the same legal-current test.

## Identity and deterministic IDs

- `src/normalize.py` is the sole ingredient normalization vocabulary.
- `unii` is present as the intended open long-term anchor but is blank when no verified free mapping
  exists; the POC identity basis remains the canonical normalized ingredient key.
- Substance IDs are UUIDv5 values derived from schema version plus normalized ingredient key.
- Product IDs are UUIDv5 values derived from country and source product key.
- Snapshot IDs are UUIDv5 values derived from build ID and country.
- The build ID hashes the explicit extraction date, selected countries, schema/universe versions,
  builder, normalizer, compatibility dependencies, fetch manifest, source directory hashes, and the
  canonical logical eEML workbook rows.
- Accepted national snapshots require a verified consolidated fetch manifest, exact hash/count
  reconciliation, and conservative source-specific row-count floors. A failed gate stops the build
  before absence claims are published.

The eEML XLSX binary is archived and hashed, but its package timestamp is not used as the logical
content identity. The logical hash covers the full workbook, including Added, Removed, and duplicate
rows. Canonical row order makes entry IDs and table exports independent of workbook row order. Only
`Status=Added` entries are members. Fixed-dose components in `Medicine name` are split; `Combined
with` is stored on the entry and never becomes an ingredient member.

Builds publish to immutable `data/.atlas-builds/<build_id>/` directories. `data/atlas` is only the
atomically replaced current pointer; returned `BuildArtifact` paths point at the immutable version,
and older versions are retained until an explicit retention job removes them.

## Comparison outputs

`compare_atlas()` supports any number of selected tracked countries in its long and summary outputs.
`current_qualified_countries` can explicitly apply a supported currentness filter to selected
countries; the emitted `presence_basis` makes that choice visible.
Global penetration is:

```text
countries with STANDALONE or COMBO_ONLY
---------------------------------------
selected countries with determinate evidence
```

Accepted-snapshot count, determinate-country count, numerator, and denominator are emitted
separately. `all_selected_present` can only be true when every selected country has determinate
observed presence. The readable wide renderer is limited to four countries because columns are a
display concern, not the storage model.
