# Active ingredient normalization

How I decide two products are the same drug when I line up the FDA and Singapore registries,
and the cases that need care.

I match on the active ingredient name, not the ATC code. The same substance can carry several
ATC codes depending on route, indication, or how a regulator classifies it, so grouping on ATC
would split one drug into pieces. Every row keeps the raw ingredient name next to the normalized
one, so any call I made can be checked.

It comes down to two buckets: strip the stuff that is just noise (same drug), and never merge
the stuff where a prefix actually changes the product.

## Stripped as noise (same drug)

- Salts and counterions: hydrochloride, sodium, sulfate, mesylate, besylate, and so on.
  Metoprolol succinate, fumarate, and tartrate all become metoprolol.
- Hydrates and crystal forms: monohydrate, trihydrate, anhydrous, micronized.
- Strength and dosage-form text: "5 mg", "tablet", "film coated", "solution".
- Equivalence phrasing on the Singapore labels: "X eqv to Y" keeps Y.
- US vs international spellings: acetaminophen/paracetamol, besylate/besilate, sulfate/sulphate,
  cyclosporine/ciclosporin. Full list at the bottom.
- Routine depot esters (enanthate, cypionate, palmitate) collapse to the active moiety. That
  trades formulation detail for cleaner availability matching, and the raw ester name is still
  on the row if you want it.

## Never merged (different product)

This is the part where the prefix actually changes the drug.

- Identity-bearing prodrugs: tenofovir alafenamide vs disoproxil (TAF vs TDF), fosphenytoin vs
  phenytoin, mycophenolate mofetil vs mycophenolic acid. Same base, genuinely different products.
- Single enantiomer vs the racemate: esomeprazole vs omeprazole, escitalopram vs citalopram,
  levamlodipine vs amlodipine, dexlansoprazole vs lansoprazole, arformoterol vs formoterol,
  esketamine vs ketamine. Different products, and a market can carry one and not the other. The
  stereo prefixes (d-, l-, r-, s-, cis-, trans-) are kept.
- Pegylated biologics: pegfilgrastim is not filgrastim.
- Biologic qualifiers like alfa/beta stay (agalsidase beta, follitropin alfa). The FDA
  four-letter biosimilar suffix (infliximab-dyyb becomes infliximab) comes off only when the base
  looks like a biologic, so small-molecule names like levodopa are not clipped.

## Other cases worth calling out

- Combination products: FDA splits actives on ";" or " AND ", Singapore on "&&". Two or more
  actives makes it a combo. If an ingredient only ever shows up inside a combo in a country, it is
  flagged combo-only, not standalone.
- Combo vs ingredient gap: if FDA has A+B as one product and Singapore has A and B separately,
  that is not an ingredient gap (both are available), it is a combo/formulation gap, tracked in
  its own file.
- Inorganic and electrolyte actives (sodium chloride, magnesium trisilicate, aluminum hydroxide)
  are kept whole, not stripped down to "hydroxide" or "chloride".
- Radiopharmaceuticals: the isotope stays and order variants line up, so FDA "technetium Tc-99m
  sodium pertechnetate" and Singapore "sodium pertechnetate (99mTc)" land on the same row.
- Vaccines, antivenoms, and immune globulins are kept as their established names.
- Some actives only exist inside combinations and have no standalone ATC code (carbidopa,
  clavulanic acid, cilastatin). Those are left without an ATC on purpose, since WHO only codes them
  as part of a combination.

## US / INN name mappings

Names where the US or local spelling differs from the WHO INN. Each was only added when the target
name exists as a WHO ATC L5 substance in the ATC file, so nothing is a guess. A substance can show
more than one ATC code where WHO assigns it by route or use.

| Source name | WHO name | ATC L5 |
|---|---|---|
| cyclosporine | ciclosporin | L04AD01; S01XA18 |
| norethindrone | norethisterone | G03AC01; G03DC02 |
| nitroglycerin; GTN in glucose vehicle text | glyceryl trinitrate | C01DA02; C05AE01 |
| glycopyrrolate | glycopyrronium | A03AB02; D11AA01; R03BB06 |
| isoproterenol | isoprenaline | C01CA02; R03AB02; R03CB01 |
| metaproterenol | orciprenaline | R03AB03; R03CB03 |
| succinylcholine | suxamethonium | M03AB01 |
| cromolyn; cromoglycate | cromoglicic acid | A07EB01; D11AH03; R01AC01; R03BC01; S01GX01 |
| phytonadione | phytomenadione | B02BA01 |
| calcipotriene | calcipotriol | D05AX02 |
| torsemide | torasemide | C03CA04 |
| dicyclomine | dicycloverine | A03AA07 |
| divalproex; valproate | valproic acid | N03AG01 |
| hydroxyurea | hydroxycarbamide | L01XX05 |
| amphetamine; dextroamphetamine; methamphetamine | amfetamine; dexamfetamine; metamfetamine | N06BA01; N06BA02; N06BA03 |
| benztropine | benzatropine | N04AC01 |
| diethylpropion | amfepramone | A08AA03 |
| chenodiol | chenodeoxycholic acid | A05AA01 |
| methylergonovine | methylergometrine | G02AB01 |
| meclizine | meclozine | R06AE05 |
| cholestyramine / cholestyramine resin | colestyramine | C10AC01 |
| clomiphene | clomifene | G03GB02 |
| methimazole | thiamazole | H03BB02 |
| etidronate | etidronic acid | M05BA01 |
| ursodiol | ursodeoxycholic acid | A05AA02 |
| dicumarol | dicoumarol | B01AA01 |
| flurandrenolide | fludroxycortide | D07AC07 |
| thiothixene | tiotixene | N05AF04 |
| ezogabine | retigabine | N03AX21 |
| propoxyphene | dextropropoxyphene | N02AC04 |
| levoleucovorin | levofolinate | V03AF04; V03AF10 |
| chorionic gonadotropin / gonadotropin, chorionic | chorionic gonadotrophin | G03GA01 |
| menotropins / menotrophin highly purified | menopausal gonadotrophin | G03GA02 |
| tetrahydrozoline | tetryzoline | R01AA06; R01AB03; S01GA02 |
| proparacaine | proxymetacaine | S01HA04 |
| benoxinate | oxybuprocaine | D04AB03; S01HA02 |
| cephapirin; cephalothin; amdinocillin; moxalactam | cefapirin; cefalotin; mecillinam; latamoxef | J01DB08; J01DB03; J01CA11; J01DD06 |
| methsuximide | mesuximide | N03AD03 |
| niacinamide; niacin; sodium ascorbate; alpha-tocopherol | nicotinamide; nicotinic acid; ascorbic acid; tocopherol | A11HA01; C04AC01/C10AD02; A11GA01/G01AD03/S01XA15; A11HA03 |
| polyethylene glycol 3350 | macrogol | A06AD15 |
| azilsartan kamedoxomil | azilsartan medoxomil | C09CA09 |
| N-acetylcysteine | acetylcysteine | R05CB01; S01XA08; V03AB23 |
| conjugated/oestrogens order variants | conjugated estrogens | G03CA57 |
| diatrizoate/amidotrizoate and contrast salt variants | diatrizoic acid | V08AA01 |
| iothalamate; metrizoate; ioxaglate | iotalamic acid; metrizoic acid; ioxaglic acid | V08AA04; V08AA02; V08AB03 |
| gadobenate; gadoterate; gadopentetate; gadoxetate | gadobenic acid; gadoteric acid; gadopentetic acid; gadoxetic acid | V08CA08; V08CA02; V08CA01; V08CA10 |
| butabarbital | butobarbital | N05CA03 |
| sulfamethazine; sulfisoxazole | sulfadimidine; sulfafurazole | J01EB03; J01EB05; S01AB02 |
| ruxolinitib / paroxetin / gluclose typos | ruxolitinib; paroxetine; glucose | D11AH09/L01EJ01; N06AB05; B05CX01/V04CA02/V06DC01 |

A few more where I attach the ATC as metadata but keep the original name as the identity, so a
biologic qualifier or a prodrug is not quietly collapsed:

| Source name | WHO name | ATC L5 |
|---|---|---|
| potassium phosphate | potassium phosphate, incl. combinations with other potassium salts | B05XA06 |
| FDA `FOLLITROPIN ALFA/BETA` artifact | follitropin alfa or follitropin beta by product | G03GA05; G03GA06 |
| sodium pertechnetate Tc-99m | technetium (99mTc) pertechnetate | V09FX01 |
| barium (over-stripped from barium sulfate) | barium sulfate | V08BA01; V08BA02 |
| epoetin alfa / beta | erythropoietin (name kept as epoetin alfa/beta) | B03XA01 |
| rauwolfia serpentina root | rauwolfia alkaloids, whole root | C02AA04 |
| cysteamine | mercaptamine | A16AA04; S01XA21 |
| isavuconazonium sulfate | isavuconazole | J02AC05 |
| fosaprepitant | aprepitant (name kept as fosaprepitant) | A04AD12 |
| mycophenolate mofetil | mycophenolic acid (name kept as mycophenolate mofetil) | L04AA06 |
| alcohol / dehydrated alcohol | ethanol | D08AX08; V03AB16; V03AZ01 |
| factor VIII/IX/VII/X/XIII wording | matching coagulation-factor names | B02BD02; B02BD04; B02BD05; B02BD07; B02BD13 |
| isoetharine, camphor, methisoprinol, precipitated sulfur, activated charcoal | isoetarine, camphora, inosine pranobex, sulfur, medicinal charcoal | R03AC07; C01EB02; J05AX05; D10AB02; A07BA01 |

Things I deliberately kept separate, and check for in tests so they don't get merged later:

- tenofovir alafenamide, tenofovir disoproxil, and tenofovir
- fosaprepitant and aprepitant
- ceftaroline and ceftaroline fosamil
- mycophenolate mofetil and mycophenolic acid
- epoetin alfa and epoetin beta
- levalbuterol and salbutamol
- norgestrel and levonorgestrel
- butalbital and butobarbital
- amphetamine and dexamfetamine
- azilsartan medoxomil and azilsartan
