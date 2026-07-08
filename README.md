# US vs Singapore drug availability

This is the rebuilt FDA vs HSA availability dataset we talked about. For every active ingredient it
says whether it's registered in the US, Singapore, both, or only inside a combination in one of them.

The dataset is `data/output/fda_hsa_by_actives.csv`, one row per ingredient. The columns that matter
most are Availability (the label), Availability Reason (why it got that label, in plain english), and
FDA Status / HSA Status (standalone, combo-only, or absent in each country). The rest are the brand
names, ATC codes and classes, approval dates, and the WHO essential / orphan flags.

To rebuild it:

```
python -m src.pipeline --output data/output/fda_hsa_by_actives.csv
python -m unittest discover -s tests
```

The raw source files aren't committed since they're large, but they're all public (FDA Drugs@FDA, HSA
on data.gov.sg, WHO ATC, WHO EML, FDA orphan list). Drop them in `data/raw/` and it rebuilds, which is
also how a weekly refresh would run. The full list of naming edge cases we handle is in
`docs/active_ingredient_edge_cases.md`.

A few things worth knowing:

- Some ingredients don't have an ATC code attached. They're mostly real drugs whose US name differs
  from the WHO name, plus a few odd ones (botanicals, sunscreen filters, drugs too new for the 2023 ATC
  list). They still get a correct availability label since that's decided by ingredient name, they just
  have blank ATC class columns. We left those unmatched instead of guessing, because a wrong merge is
  worse than a blank cell.
- A few actives only ever exist inside combinations and have no standalone ATC code (carbidopa,
  clavulanic acid). Those are correctly left as combo components, not gaps.
- Normalization works off ingredient names, not chemical structure, and the WHO essential / orphan
  flags are name matches rather than official crosswalks, so both can be off at the margins.
