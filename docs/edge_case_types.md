# Edge cases in confirming drug access and availability

A reference list of the ways a drug-availability call goes wrong, with real examples from the four
registers we ingest (US/FDA, Singapore/HSA, Bangladesh/DGDA, Bhutan/BFDA). Every case here is one we
actually hit, not a hypothetical.

Use it two ways: as the checklist a reviewer works through before accepting a status, and as the list
of rules the pipeline has to encode. `docs/active_ingredient_edge_cases.md` has the deeper chemistry
detail behind section 1; this doc is the wider map.

The single rule underneath all of it: **when the evidence isn't a clean match, the answer is
`UNKNOWN`, not a guess.** Under-claiming is recoverable. Over-claiming is how you get false gaps, and
false gaps are what make a dataset like this untrustworthy.

---

## 1. Same drug, different name (these must collapse)

Miss one of these and you invent a gap: the drug is registered in both countries, but under names that
don't match, so it looks like each country has something the other lacks.

- **Salts, esters, hydrates, counterions.** The moiety is the drug. Amlodipine besylate, besilate, and
  maleate are one molecule. So are metoprolol succinate, tartrate, and fumarate.
- **US vs international (INN) names.** acetaminophen = paracetamol, albuterol = salbutamol,
  epinephrine = adrenaline, glyburide = glibenclamide, meperidine = pethidine.
- **British and older spellings.** sulphate/sulfate, oestradiol/estradiol, lignocaine/lidocaine,
  phenobarbitone/phenobarbital.
- **Source typos.** These are common and they are load-bearing. WHO's own eEML lists "porcatant alfa"
  for poractant alfa. Bangladesh lists "Insulin Glargin" and "Poliomyelitis Vacine". Also anastrozol,
  enoxaprin, protamin.
- **Synonym families from different naming conventions.** benzathine benzylpenicillin = penicillin G
  benzathine = benzathine penicillin. This one bit us: the EML entry and the actual products sat under
  different identities, and an essential antibiotic read as absent in three countries that had it.
- **Vitamins by number vs INN.** vitamin B1 = thiamine, vitamin C = ascorbic acid, vitamin D3 =
  colecalciferol.
- **Biosimilar suffixes.** The four-letter suffix (trastuzumab-anns) is a manufacturing distinction,
  not a different molecule.

## 2. Different drug, similar name (these must never merge)

The opposite failure, and the more dangerous one: merging two drugs invents availability that isn't
there, and in a few cases the two products are not clinically interchangeable at all.

- **Stereoisomers.** esomeprazole ≠ omeprazole. levofloxacin ≠ ofloxacin. dexamfetamine ≠ amfetamine.
- **Prodrugs.** tenofovir alafenamide ≠ tenofovir disoproxil. Different dosing, different safety
  profile.
- **Positional isomers.** 5-aminosalicylic acid (mesalazine, for IBD) is not para-aminosalicylic acid
  (an anti-TB drug). Same atoms, unrelated medicines.
- **When the counterion defines the medicine.** hyoscine butylbromide (antispasmodic, ATC A03BB01) is
  not hyoscine hydrobromide / scopolamine (antiemetic, A04AD01). Salt-stripping is right until it isn't.
- **When the salt makes it a different product entirely.** disodium edetate is a dental root-canal
  chelator. Sodium calcium edetate is the lead-poisoning antidote. Treating the first as evidence for
  the second is a patient-safety error, not a data nitpick.
- **When the "salt" is actually the active.** benzyl benzoate is not "benzyl". Potassium permanganate
  is not "permanganate". Silver diamine fluoride is not "silver diamine". Strip the defining portion
  and you've destroyed the drug.
- **Depot vs plain.** benzathine benzylpenicillin (long-acting IM depot) and benzylpenicillin
  (short-acting IV) are separate EML medicines with separate uses.
- **Vaccine strains and serotypes.** Numeric serotypes have to stay distinct.

## 3. The substance matches but the medicine doesn't

The molecule is there. The medicine the EML is asking about is not. This is where most remaining
false positives come from.

- **Wrong route.** The EML wants oral tretinoin 10 mg for acute promyelocytic leukaemia. Bangladesh
  and Bhutan carry topical tretinoin for acne. Same molecule, different medicine. It doesn't count.
- **The substance is present as a trace, an excipient, or a diagnostic.** US "calcium" looked present
  because an injectable contrast agent (ISOPAQUE 280) contains 0.35 mg/mL of it. That is not oral
  elemental-calcium access.
- **Non-therapeutic dose.** The iron in a contraceptive pack's placebo tablets is not therapeutic
  iron. Neither is a sub-EML-dose tablet.
- **Radiopharmaceutical vs therapeutic.** Ferrous citrate Fe-59 is a radioactive tracer, not iron
  supplementation. It briefly made the US look like it had therapeutic iron.
- **Class entries vs named substances.** "Pancreatic enzymes", "erythropoiesis-stimulating agents",
  and "ferrous salt" are classes. The products are pancrelipase/pancreatin, epoetin/darbepoetin,
  ferrous sulfate/fumarate. Matching on the class name literally finds nothing.
- **Defined multi-component formulations.** Oral rehydration salts is a specific formula, not an
  ingredient, and the current reduced-osmolarity formula (13.5 / 1.5 / 2.6 / 2.9 g/L) is not the
  older 20 / 1.5 / 3.5 / 2.9 one. Same for compound sodium lactate = Hartmann's = lactated Ringer's,
  which is four components, not a substance called "compound".
- **Subtypes that aren't interchangeable.** Insulin is the sharp one: short-acting, intermediate/NPH,
  rapid analogue, long analogue, and premix are different medicines. A generic human insulin product
  does not establish glargine access.
- **Combination products.** Decompose them into actives, and note every register uses its own
  separator: FDA uses `;` or ` AND `, Singapore uses `&&`, Bangladesh and Bhutan use `+`, "and", and
  "with". Standalone-vs-combination is then just how many actives a product has; it isn't a label to
  store.
- **Ingredient overlap is not the combination.** A and B each being registered does not mean the A+B
  fixed-dose combination or co-pack exists. That's a separate, stricter question.

## 4. The record isn't a human drug at all

- **Veterinary products.** Bangladesh's register mixes them in with human products. We found poultry
  feed premixes (Rena Layer, Eskavit Grower) counted as human drug availability. The reliable marker
  is the DGDA **category** segment (`077` = Veterinary Drugs), *not* the product name. Watch the trap:
  `077` also appears as a **manufacturer** code, so matching it in the wrong position wrongly excludes
  53 ordinary human products from one company.
- **EML entries a drug register doesn't hold.** The WHO EML covers health-system needs, not just
  prescription drugs: whole blood, red cells, platelets, fresh frozen plasma, cryoprecipitate,
  condoms, diaphragms, IUDs, dental cements and composites, ready-to-use therapeutic food, sunscreen.
  These are out of scope for a drug register, not absent from the country.
- **Section headings that look like medicines.** "Medicines for COVID-19" is an EML heading pointing
  at WHO living guidelines. It is not a drug and should never get an availability call.
- **Register coverage limits.** Bangladesh's allopathic register is a separate database from its
  ayurvedic, unani, herbal, and homeopathic registers. Absence means absent from the register we
  ingested, nothing more.
- **Category-source gaps.** Drugs@FDA does not contain CBER products: vaccines, blood products,
  immunoglobulins, coagulation factors, tuberculin. Absence in Drugs@FDA proves nothing about a
  vaccine. That question needs CBER or the Purple Book.

## 5. Registered is not the same as available

- **Registration vs current marketing.** 28 US essential medicines are "present" only through
  *discontinued* products: cefotaxime, kanamycin, and chloramphenicol among them. Listed, not
  available.
- **Validity and expiry.** Bhutan publishes a validity-through date, and its cancelled, suspended, and
  withdrawn list is a **separate sheet** you have to join by registration number. Nothing links them
  automatically.
- **Duplicate registrations.** Bhutan's sheet had 1,281 rows for 1,219 unique registration numbers.
- **Source staleness.** Singapore's snapshot was 374 days older than the extraction date. That's a
  real limit on any "currently available" claim.
- **Sources that publish no status at all.** Bangladesh gives no approval date, no expiry, no legal
  status. "Is it currently marketed?" is unanswerable from that register. Not unknown because we
  haven't looked; unanswerable in principle. It needs a different source.
- **Garbled fields.** Bhutan had generic and brand names swapped in places, and malformed registration
  prefixes.

## 6. Metadata traps (mostly ATC)

- **ATC is not identity.** The same substance carries different ATC codes by route, indication, and
  country practice. Group on the substance; keep ATC as metadata. Grouping on ATC over-splits drugs.
- **Combination codes belong to the product, not each ingredient.** Clavulanic acid picking up
  J01CR02 is wrong. That code means "amoxicillin and beta-lactamase inhibitor", and it refers to
  amoxicillin.
- **Sources get ATC wrong.** Singapore's Insulatard Penfill carried A10AB01 (short-acting) when it's
  intermediate-acting (A10AC01). Metformin carried A01BA02 instead of A10BA02.
- **Malformed codes.** "PENDING", "NOT AVAILABL", embedded spaces, O-for-0 typos, and one invisible
  directionality character.
- **ATC gets reorganized.** WHO moved a batch of codes in 2021; old codes need remapping before
  anything is compared.

## 7. How to state the answer

The taxonomy that keeps all of the above honest. Every country/drug call lands in exactly one:

| State | Means | Does **not** mean |
|---|---|---|
| `STANDALONE` / `COMBO_ONLY` | We observed a matching product in the ingested source | That it's currently marketed |
| `OBSERVED_ABSENCE` | We looked in the ingested source and didn't find it | That it's unregistered or illegal there |
| `UNKNOWN` | The identity is ambiguous, or the source can't answer | That it's absent |
| `OUT_OF_SCOPE` | The object isn't a drug-register item | That the country lacks it |

Alongside these, `needs_external_source` names the specific source that *would* answer the question:
CBER or the Purple Book for vaccines and biologics, an OTC/supplement source for non-prescription
products, patent and exclusivity data for generic-entry questions, and something other than the DGDA
register for Bangladeshi marketing status.

The distinction that matters most is `OBSERVED_ABSENCE` vs `UNKNOWN` vs "not registered". Only the
first two are ever ours to claim. A dataset that says "not available" when it means "not in the file we
pulled" is worse than no dataset, because someone will make a procurement decision on it.
