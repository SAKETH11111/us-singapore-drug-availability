# Drug availability atlas POC

This repository builds a country-agnostic dataset of registered drug products and their normalized
active ingredients. The POC covers the United States, Singapore, Bangladesh, and Bhutan over the WHO
2025 Essential Medicines List.

The storage model is long and relational: countries add product and ingredient rows, not columns.
Country overlap, gaps, product mode, penetration, and the readable four-country table are computed
views. The delivered US by Singapore file remains available through an explicit compatibility
renderer.

## Reproduce the POC

Use Python 3.11 or newer. Fetching and building are separate by design, and both require an explicit
extraction date.

```bash
python -m pip install -r requirements.txt

# Refresh all four regulators plus the eEML and bind the required local
# ATC/Rare reference inputs into one immutable snapshot.
python -m src.fetch_sources \
  --extraction-date 2026-07-15 \
  --atc-path data/raw/who/atc.csv \
  --rare-drugs-path 'data/raw/Rare Drugs.xls'

# Build all four countries from data/raw/current without network access.
python -m src.atlas build --extraction-date 2026-07-15

# Query any tracked selection. The long and summary views support N countries;
# the readable wide view is generated for selections of up to four.
python -m src.atlas compare \
  --database data/atlas/atlas.sqlite \
  --countries US SG BD BT \
  --output-dir data/atlas/ad-hoc-comparison

# Optional sensitivity view: require Bhutan validity and action checks.
python -m src.atlas compare \
  --database data/atlas/atlas.sqlite \
  --countries US SG BD BT \
  --current-qualified-countries BT \
  --output-dir data/atlas/ad-hoc-bt-current

python -m pytest -q
```

`src.fetch_sources` preflights the ATC CSV and FDA Rare Drugs export before making any network
request, copies them into the dated snapshot, and records their hashes. The build fails if either is
absent; it never substitutes an empty legacy renderer. Refresh/source instructions for these two
inputs remain in `../satvik-project/docs/DATA_SOURCES.md`. The ATC bulk-file redistribution decision
is intentionally not made by this repository.

The fetch first publishes `data/raw/snapshots/<extraction-date>/`, then atomically switches the
`data/raw/current` pointer only after all four country sources and the eEML validate. The build
requires and verifies that snapshot manifest, then atomically switches `data/atlas/` to an immutable
build. Raw files without a consolidated manifest cannot produce accepted absence claims; manifest
counts must also clear conservative source-specific plausibility floors:

- `atlas.sqlite`: normalized relational database with foreign keys.
- `tables/`: deterministic CSV exports of every normalized table.
- `views/eml_presence_long.csv`: one EML ingredient by country observation.
- `views/eml_comparison_summary.csv`: overlap, gaps, and penetration.
- `views/eml_comparison_wide.csv`: readable US, SG, BD, and BT display.
- `views/*_bt_current_qualified.csv`: the explicit Bhutan-current sensitivity views.
- `views/bd_bt_*`: listed and Bhutan-current Bangladesh–Bhutan joint-procurement slices.
- `views/us_sg_legacy_compatibility.csv`: historical 21-column renderer.
- `tables/substance_identity_uncertainties.csv`: review-required EML/source identity candidates
  that prevent related names from becoming false gap claims without asserting presence.
- `data_quality_report.md`: counts, overlap result, source caveats, and ingest issues.
- `manifest.json`: source, code, table, and view hashes for the build.

## Semantics that matter

- Ingredient identity always comes from `src/normalize.py`, the canonical fixed normalizer.
- The atlas ingests FDA NDA, BLA, and ANDA records. The legacy US by Singapore renderer filters FDA
  to NDA/BLA before applying the historical brand deduplication, preserving its 2,923 rows and all
  20 non-EML columns exactly. Its WHO flag is intentionally refreshed from the open 2025 eEML.
- `OBSERVED_ABSENCE` means an ingredient was not observed in an accepted ingested register snapshot.
  It does not mean a drug is illegal or definitively unregistered in the country.
- Broader/specific identities, a reviewed spelling-variant set, full disease-signature vaccine
  variants, reviewed vaccine product families, and supported acronym expansions produce `UNKNOWN`,
  not absence. These review holds are stored with both substance IDs and source-country provenance;
  they never count as presence without an approved equivalence.
- The primary four-country comparison uses listed presence. Bhutan validity and matching regulatory
  actions are also emitted as a separate current-qualified comparison because Bangladesh cannot
  support an equivalent legal-current determination. Colliding or ambiguous action evidence is
  unknown, not current.
- Combination mode comes from a validated product-level component count that includes unresolved
  declared components. A two-active source product cannot be mislabeled standalone just because one
  component failed identity resolution. Audited parser fragments that are not independent declared
  actives are excluded from that count.
- Bangladesh coverage is the allopathic register mirror only. Missing rows cannot be generalized to
  other medicine systems.
- The POC universe comes from the WHO electronic EML export under CC BY 3.0 IGO. Only `Status=Added`
  recommendation rows enter the universe. Combination members are split from `Medicine name`;
  `Combined with` is retained as co-prescription metadata and never treated as an ingredient. The WHO
  PDF is validation-only because its license is non-commercial.
- `source_as_of_date=unknown` is preserved when a regulator does not publish a reliable update date;
  it is never replaced with the extraction date. Source URLs, acceptance reason, coverage, and
  bounded absence wording are stored with each snapshot.
- Every national snapshot stores licence name, URL, status, and attribution. HSA uses the Singapore
  Open Data Licence 1.0 attribution. Bangladesh, Bhutan, and the local WHO ATC bulk input remain
  explicitly `human_review_required`; that flag is not a legal conclusion.

WHO attribution: WHO electronic Essential Medicines List (eEML), World Health Organization, 2020.
https://list.essentialmeds.org/ (beta version 1.0). Licence: CC BY 3.0 IGO.

WHO adaptation notice: This is an adaptation of an original work by World Health Organization
(WHO). Views and opinions expressed in the adaptation are the sole responsibility of the author or
authors of the adaptation and are not endorsed by World Health Organization (WHO).

The original two-country build remains available as `python -m src.pipeline`. The atlas compatibility
renderer intentionally consumes a preserved old-pipeline observation sidecar for historical
ATC/class/rare columns. It is not a pure projection of the normalized atlas tables; only its
availability semantics can be reproduced from those tables. A committed frozen sidecar fixture keeps
all 2,923 rows and 20 historical non-EML columns under clean-clone regression.
