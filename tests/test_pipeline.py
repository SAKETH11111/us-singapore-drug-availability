import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from src.normalize import (
    ATC_REMAP,
    fix_atc,
    normalize_ingredient,
    split_fda_ingredients,
    split_hsa_ingredients,
)
from src.pipeline import (
    OUTPUT_COLUMNS,
    assign_availability,
    build_atc_l5_lookup,
    build_combo_formulation_gaps,
    explode_fda_components,
    flag_who_eml,
    load_atc,
    load_hsa_product_substances,
    match_components_to_atc,
    parse_who_eml_terms,
)


class NormalizeIngredientTests(unittest.TestCase):
    def test_synonyms_salts_and_protected_inns_match_reference_rules(self):
        self.assertEqual(normalize_ingredient("Acetaminophen"), "paracetamol")
        self.assertEqual(normalize_ingredient("ALBUTEROL SULFATE INHALER"), "salbutamol")
        self.assertEqual(normalize_ingredient("Leuprolide acetate injection"), "leuprorelin")
        self.assertEqual(
            normalize_ingredient("Dimethyl fumarate delayed release capsules"),
            "dimethyl fumarate",
        )

    def test_active_moiety_edge_cases_from_hsa_combos(self):
        self.assertEqual(normalize_ingredient("CLAVULANATE POTASSIUM"), "clavulanic acid")
        self.assertEqual(
            normalize_ingredient(
                "CLAVULANATE POTASSIUM with Microcrystalline Cellulose (1:1) EQV CLAVULANIC ACID"
            ),
            "clavulanic acid",
        )
        self.assertEqual(normalize_ingredient("VILANTEROL TRIFENATATE"), "vilanterol")
        self.assertEqual(
            normalize_ingredient("Tenofovir Alafenamide Fumarate"),
            "tenofovir alafenamide",
        )
        self.assertEqual(normalize_ingredient("Palonosetron HCl"), "palonosetron")
        self.assertEqual(
            normalize_ingredient("Sodium alendronate trihydrate 91.35 mg eqv to alendronic acid"),
            "alendronate",
        )
        self.assertEqual(normalize_ingredient("Alendronate Sodium 91.37mg eqv to Alendronate Acid"), "alendronate")
        self.assertEqual(normalize_ingredient("Alendronate Sodium Trihydate"), "alendronate")
        self.assertEqual(
            normalize_ingredient("Clopidogrel hydrogen sulphate equivalent to Clopidogrel"),
            "clopidogrel",
        )
        self.assertEqual(normalize_ingredient("CALCIUM FOLINATE"), "leucovorin")
        self.assertEqual(normalize_ingredient("Fusidic acid micronised"), "fusidic acid")
        self.assertEqual(normalize_ingredient("SODIUM FUSIDATE"), "fusidic acid")
        self.assertEqual(normalize_ingredient("CARBONYLDIAMIDE (UREA)"), "urea")
        self.assertEqual(normalize_ingredient("SIMETHICONE"), "simeticone")
        self.assertEqual(normalize_ingredient("ETHINYL ESTRADIOL"), "ethinylestradiol")
        self.assertEqual(normalize_ingredient("ETHINYLOESTRADIOL"), "ethinylestradiol")
        self.assertEqual(normalize_ingredient("DEXTROSE MONOHYDRATE"), "glucose")
        self.assertEqual(normalize_ingredient("CARBON ACTIVATED"), "activated charcoal")
        self.assertEqual(normalize_ingredient("CHARCOAL ACTIVATED"), "activated charcoal")
        self.assertEqual(normalize_ingredient("STERILE ARIPIPRAZOLE"), "aripiprazole")
        self.assertEqual(normalize_ingredient("MICRONISED PROGESTERONE"), "progesterone")

    def test_unmatched_us_names_normalize_to_local_who_inn_names(self):
        synonym_pairs = [
            ("CYCLOSPORINE", "ciclosporin"),
            ("NORETHINDRONE ACETATE", "norethisterone"),
            ("NITROGLYCERIN", "glyceryl trinitrate"),
            ("GLYCOPYRROLATE", "glycopyrronium"),
            ("ISOPROTERENOL SULFATE", "isoprenaline"),
            ("METAPROTERENOL SULFATE", "orciprenaline"),
            ("SUCCINYLCHOLINE CHLORIDE", "suxamethonium"),
            ("CROMOLYN SODIUM", "cromoglicic acid"),
            ("PHYTONADIONE", "phytomenadione"),
            ("CALCIPOTRIENE HYDRATE", "calcipotriol"),
            ("TORSEMIDE", "torasemide"),
            ("DICYCLOMINE HCl", "dicycloverine"),
            ("DIVALPROEX SODIUM", "valproic acid"),
            ("SODIUM VALPROATE", "valproic acid"),
            ("HYDROXYUREA", "hydroxycarbamide"),
            ("BENZTROPINE MESYLATE", "benzatropine"),
            ("DIETHYLPROPION HYDROCHLORIDE", "amfepramone"),
            ("AMPHETAMINE RESIN COMPLEX", "amfetamine"),
            ("DEXTROAMPHETAMINE RESIN COMPLEX", "dexamfetamine"),
            ("METHAMPHETAMINE HYDROCHLORIDE", "metamfetamine"),
            ("CHENODIOL", "chenodeoxycholic acid"),
            ("METHYLERGONOVINE MALEATE", "methylergometrine"),
            ("MECLIZINE HYDROCHLORIDE", "meclozine"),
            ("CHOLESTYRAMINE RESIN", "colestyramine"),
            ("CLOMIPHENE CITRATE", "clomifene"),
            ("METHIMAZOLE", "thiamazole"),
            ("ETIDRONATE DISODIUM", "etidronic acid"),
            ("URSODIOL", "ursodeoxycholic acid"),
            ("DICUMAROL", "dicoumarol"),
            ("FLURANDRENOLIDE", "fludroxycortide"),
            ("THIOTHIXENE HYDROCHLORIDE", "tiotixene"),
            ("EZOGABINE", "retigabine"),
            ("PROPOXYPHENE NAPSYLATE", "dextropropoxyphene"),
            ("LEVOLEUCOVORIN CALCIUM", "levofolinate"),
            ("GONADOTROPIN, CHORIONIC", "chorionic gonadotrophin"),
            ("MENOTROPINS (FSH", "human menopausal gonadotrophin"),
            ("TETRAHYDROZOLINE HCl", "tetryzoline"),
            ("PROPARACAINE HCl", "proxymetacaine"),
            ("BENOXINATE HYDROCHLORIDE", "oxybuprocaine"),
            ("CEPHAPIRIN SODIUM", "cefapirin"),
            ("CEPHALOTHIN SODIUM", "cefalotin"),
            ("AMDINOCILLIN", "mecillinam"),
            ("MOXALACTAM DISODIUM", "latamoxef"),
            ("METHSUXIMIDE", "mesuximide"),
            ("NIACINAMIDE", "nicotinamide"),
            ("NIACIN", "nicotinic acid"),
            ("SODIUM ASCORBATE", "ascorbic acid"),
            ("ALPHA-TOCOPHEROL ACETATE", "tocopherol"),
            ("POLYETHYLENE GLYCOL 3350", "macrogol"),
            ("AZILSARTAN KAMEDOXOMIL", "azilsartan medoxomil"),
            ("N-ACETYLCYSTEINE", "acetylcysteine"),
            ("MEGLUMINE ANTIMONATE", "meglumine antimonate"),
            ("ISAVUCONAZONIUM SULFATE", "isavuconazole"),
            ("CYSTEAMINE BITARTRATE", "mercaptamine"),
            ("ISOETHARINE MESYLATE", "isoetarine"),
            ("RAUWOLFIA SERPENTINA ROOT", "rauwolfia alkaloids, whole root"),
            ("CAMPHOR", "camphora"),
            ("PRECIPITATED SULPHUR", "sulfur"),
            ("DEHYDRATED ALCOHOL", "ethanol"),
            ("ALCOHOL ABSOLUTE", "ethanol"),
        ]

        for raw_name, who_name in synonym_pairs:
            with self.subTest(raw_name=raw_name, who_name=who_name):
                self.assertEqual(normalize_ingredient(raw_name), normalize_ingredient(who_name))

    def test_unmatched_typos_and_equivalence_phrases_keep_atc_matchable_target(self):
        self.assertEqual(
            normalize_ingredient("Pantoprazole sodium sesquihydrate 45.2mg eqvt to Pantoprazole"),
            "pantoprazole",
        )
        self.assertEqual(
            normalize_ingredient("Timolol Maleate 6.8 mg (eqvivalent to 5mg Timolol)"),
            "timolol",
        )
        self.assertEqual(normalize_ingredient("GLUCLOSE (as GLUCOSE MONOHYDRATE)"), "glucose")
        self.assertEqual(normalize_ingredient("Ruxolitinib Phosphate eqv Ruxolinitib"), "ruxolitinib")
        self.assertEqual(normalize_ingredient("Paroxetine HCl equv. to Paroxetin HCl"), "paroxetine")

    def test_meglumine_antimonate_remains_protected_who_l5_name(self):
        self.assertEqual(normalize_ingredient("MEGLUMINE ANTIMONATE"), "meglumine antimonate")

    def test_non_ethanol_alcohol_names_are_not_collapsed_to_ethanol(self):
        self.assertEqual(normalize_ingredient("ISOPROPYL ALCOHOL"), "isopropyl alcohol")
        self.assertEqual(normalize_ingredient("CETYL ALCOHOL"), "cetyl alcohol")

    def test_unmatched_contrast_hormone_and_sulfonamide_names_match_who_l5(self):
        synonym_pairs = [
            ("Glyceryl trinitrate(GTN in 2% Glucose", "glyceryl trinitrate"),
            ("ESTROGENS, CONJUGATED", "conjugated estrogens"),
            ("OESTROGENS (CONJUGATED)", "conjugated estrogens"),
            ("DIATRIZOATE MEGLUMINE", "diatrizoic acid"),
            ("SODIUM AMIDOTRIZOATE", "diatrizoic acid"),
            ("IOTHALAMATE SODIUM", "iotalamic acid"),
            ("METRIZOATE SODIUM", "metrizoic acid"),
            ("GADOBENATE DIMEGLUMINE", "gadobenic acid"),
            ("GADOTERATE MEGLUMINE", "gadoteric acid"),
            ("GADOPENTETATE DIMEGLUMINE", "gadopentetic acid"),
            ("GADOXETATE DISODIUM", "gadoxetic acid"),
            ("IOXAGLATE MEGLUMINE", "ioxaglic acid"),
            ("SULFAMETHAZINE", "sulfadimidine"),
            ("SULFISOXAZOLE", "sulfafurazole"),
        ]

        for raw_name, who_name in synonym_pairs:
            with self.subTest(raw_name=raw_name, who_name=who_name):
                self.assertEqual(normalize_ingredient(raw_name), normalize_ingredient(who_name))

    def test_audit_synonyms_do_not_collapse_distinct_drugs(self):
        self.assertEqual(normalize_ingredient("BUTABARBITAL SODIUM"), "butobarbital")
        self.assertNotEqual(normalize_ingredient("BUTALBITAL"), normalize_ingredient("BUTABARBITAL SODIUM"))
        self.assertNotEqual(normalize_ingredient("LEVALBUTEROL TARTRATE"), normalize_ingredient("ALBUTEROL"))
        self.assertNotEqual(normalize_ingredient("FOSAPREPITANT"), normalize_ingredient("APREPITANT"))
        self.assertNotEqual(normalize_ingredient("CEFTAROLINE"), normalize_ingredient("CEFTAROLINE FOSAMIL"))
        self.assertNotEqual(normalize_ingredient("NORGESTREL"), normalize_ingredient("LEVONORGESTREL"))
        self.assertNotEqual(normalize_ingredient("AMPHETAMINE"), normalize_ingredient("DEXTROAMPHETAMINE"))

    def test_stereochemical_prefixes_preserve_distinct_products(self):
        self.assertNotEqual(
            normalize_ingredient("cis-retinoic acid"),
            normalize_ingredient("trans-retinoic acid"),
        )
        self.assertNotEqual(normalize_ingredient("d-amphetamine"), normalize_ingredient("amphetamine"))
        self.assertNotEqual(normalize_ingredient("S-ketamine"), normalize_ingredient("ketamine"))
        self.assertNotEqual(normalize_ingredient("L-thyroxine"), normalize_ingredient("thyroxine"))

    def test_identity_bearing_prodrugs_remain_distinct_from_parent_drugs(self):
        self.assertEqual(
            normalize_ingredient("Tenofovir Alafenamide Fumarate"),
            "tenofovir alafenamide",
        )
        self.assertEqual(
            normalize_ingredient("Tenofovir Disoproxil Fumarate"),
            "tenofovir disoproxil",
        )
        self.assertNotEqual(
            normalize_ingredient("Tenofovir Alafenamide Fumarate"),
            normalize_ingredient("Tenofovir Disoproxil Fumarate"),
        )
        self.assertNotEqual(
            normalize_ingredient("Tenofovir Alafenamide Fumarate"),
            normalize_ingredient("Tenofovir"),
        )
        self.assertNotEqual(
            normalize_ingredient("Tenofovir Disoproxil Fumarate"),
            normalize_ingredient("Tenofovir"),
        )
        self.assertEqual(normalize_ingredient("Fosphenytoin Sodium"), "fosphenytoin")
        self.assertEqual(normalize_ingredient("Phenytoin Sodium"), "phenytoin")
        self.assertNotEqual(
            normalize_ingredient("Fosphenytoin Sodium"),
            normalize_ingredient("Phenytoin Sodium"),
        )

    def test_biosimilar_suffix_stripping_does_not_clip_small_molecule_names(self):
        self.assertEqual(normalize_ingredient("infliximab-dyyb"), "infliximab")
        self.assertEqual(normalize_ingredient("anacaulase-bcdb"), "anacaulase")
        self.assertEqual(normalize_ingredient("epoetin alfa-epbx"), "epoetin alfa")
        self.assertEqual(
            normalize_ingredient("collagenase clostridium histolyticum-aaes"),
            "collagenase clostridium histolyticum",
        )
        self.assertEqual(
            normalize_ingredient("asparaginase erwinia chrysanthemi-rywn"),
            "asparaginase erwinia chrysanthemi",
        )
        self.assertEqual(
            normalize_ingredient("asparaginase erwinia chrysanthemi (recombinant)-rywn"),
            "asparaginase erwinia chrysanthemi",
        )
        self.assertEqual(normalize_ingredient("levo-dopa"), "levo dopa")
        self.assertEqual(normalize_ingredient("co-careldopa"), "co careldopa")

    def test_inorganic_and_radiopharma_actives_are_not_erased(self):
        self.assertEqual(normalize_ingredient("SODIUM CHLORIDE"), "sodium chloride")
        self.assertEqual(normalize_ingredient("POTASSIUM CHLORIDE"), "potassium chloride")
        self.assertEqual(normalize_ingredient("CALCIUM CARBONATE"), "calcium carbonate")
        self.assertEqual(normalize_ingredient("BARIUM SULFATE"), "barium sulfate")
        self.assertEqual(
            normalize_ingredient("ALUMINIUM HYDROXIDE (DRIED GEL)"),
            "aluminum hydroxide",
        )
        self.assertEqual(normalize_ingredient("MAGNESIUM CARBONATE LIGHT"), "magnesium carbonate")
        self.assertEqual(normalize_ingredient("MAGNESIUM TRISILICATE"), "magnesium trisilicate")
        self.assertEqual(normalize_ingredient("MAGNESIUM OXIDE LIGHT"), "magnesium oxide")
        self.assertEqual(normalize_ingredient("COAL TAR PREPARED"), "coal tar")
        self.assertEqual(normalize_ingredient("SODIUM HYDROGEN CARBONATE"), "sodium bicarbonate")
        self.assertEqual(normalize_ingredient("SODIUM HYDROXIDE"), "sodium hydroxide")
        self.assertEqual(
            normalize_ingredient("SODIUM DIHYDROGEN PHOSPHATE DIHYDRATE"),
            "sodium phosphate",
        )
        self.assertEqual(
            normalize_ingredient("potassium phosphate, incl. combinations with other potassium salts"),
            "potassium phosphate",
        )
        self.assertEqual(normalize_ingredient("CLOPIDOGREL HYDROGEN SULFATE"), "clopidogrel")
        self.assertEqual(normalize_ingredient("RIVASTIGMINE HYDROGEN TARTRATE"), "rivastigmine")
        self.assertEqual(normalize_ingredient("SODIUM IODIDE I-131"), "sodium iodide i 131")
        self.assertEqual(normalize_ingredient("sodium iodide (131I)"), "sodium iodide i 131")
        self.assertEqual(normalize_ingredient("SODIUM FLUORIDE F-18"), "sodium fluoride f 18")
        self.assertEqual(normalize_ingredient("sodium fluoride (18F)"), "sodium fluoride f 18")
        self.assertEqual(
            normalize_ingredient("TECHNETIUM TC-99M PYROPHOSPHATE KIT"),
            "technetium tc 99m pyrophosphate",
        )
        self.assertEqual(
            normalize_ingredient("technetium (99mTc) pyrophosphate"),
            "technetium tc 99m pyrophosphate",
        )
        self.assertEqual(normalize_ingredient("13C UREA"), "urea c 13")
        self.assertEqual(normalize_ingredient("14-C UREA"), "urea c 14")
        self.assertEqual(
            normalize_ingredient("TECHNETIUM TC-99M SODIUM PERTECHNETATE"),
            "sodium pertechnetate tc 99m",
        )
        self.assertEqual(
            normalize_ingredient("Sodium pertechnetate (99mTc)"),
            "sodium pertechnetate tc 99m",
        )
        self.assertEqual(
            normalize_ingredient("technetium (99mTc) pertechnetate"),
            "sodium pertechnetate tc 99m",
        )
        self.assertEqual(normalize_ingredient("13C-urea"), "urea c 13")
        self.assertEqual(
            normalize_ingredient("TECHNETIUM TC-99M MEDRONATE"),
            "technetium tc 99m medronic acid",
        )
        self.assertEqual(
            normalize_ingredient("TECHNETIUM TC-99M PENTETATE KIT"),
            "technetium tc 99m pentetic acid",
        )
        self.assertEqual(
            normalize_ingredient("OXIDRONATE SODIUM"),
            "oxidronic acid",
        )
        self.assertEqual(
            normalize_ingredient("GALLIUM CITRATE GA-67"),
            "gallium ga 67 citrate",
        )
        self.assertEqual(
            normalize_ingredient("RUBIDIUM CHLORIDE RB-82"),
            "rubidium rb 82 chloride",
        )
        self.assertEqual(
            normalize_ingredient("ALBUMIN IODINATED I-125 SERUM"),
            "iodine i 125 human albumin",
        )
        self.assertEqual(normalize_ingredient("PHENOL"), "phenol")

    def test_required_long_tail_components_match_local_who_l5_entries(self):
        atc = load_atc(Path(__file__).resolve().parents[1] / "data" / "raw" / "who" / "atc.csv")
        lookup = build_atc_l5_lookup(atc)
        raw_components = {
            "epoetin alfa": "EPOETIN ALFA",
            "sodium pertechnetate": "TECHNETIUM TC-99M SODIUM PERTECHNETATE",
            "barium sulfate": "BARIUM SULFATE",
            "potassium phosphate": "POTASSIUM PHOSPHATE, MONOBASIC",
            "technetium medronate": "TECHNETIUM TC-99M MEDRONATE",
            "technetium pentetate": "TECHNETIUM TC-99M PENTETATE KIT",
            "technetium oxidronate": "TECHNETIUM TC-99M OXIDRONATE KIT",
            "gallium citrate": "GALLIUM CITRATE GA-67",
            "rubidium chloride": "RUBIDIUM CHLORIDE RB-82",
            "iodinated albumin i 125": "ALBUMIN IODINATED I-125 SERUM",
            "iodinated albumin i 131": "ALBUMIN IODINATED I-131 SERUM",
            "urea c 13": "UREA C-13",
        }
        components = pd.DataFrame(
            [
                {
                    "component_id": component_id,
                    "component_norm": normalize_ingredient(raw_component),
                    "product_id": component_id,
                    "product_name": raw_component,
                    "approval_date": pd.NaT,
                    "is_combo": False,
                }
                for component_id, raw_component in raw_components.items()
            ]
        )

        matched = match_components_to_atc(components, lookup)

        by_component = {
            component_id: set(group["atc_code"])
            for component_id, group in matched.groupby("component_id")
        }
        self.assertIn("B03XA01", by_component["epoetin alfa"])
        self.assertIn("V09FX01", by_component["sodium pertechnetate"])
        self.assertEqual(by_component["barium sulfate"], {"V08BA01", "V08BA02"})
        self.assertIn("B05XA06", by_component["potassium phosphate"])
        self.assertEqual(by_component["technetium medronate"], {"V09BA02"})
        self.assertEqual(by_component["technetium pentetate"], {"V09CA01", "V09EA01"})
        self.assertEqual(by_component["technetium oxidronate"], {"V09BA01"})
        self.assertEqual(by_component["gallium citrate"], {"V09HX01"})
        self.assertEqual(by_component["rubidium chloride"], {"V09GX04"})
        self.assertEqual(by_component["iodinated albumin i 125"], {"V09GB02"})
        self.assertEqual(by_component["iodinated albumin i 131"], {"V09XA03"})
        self.assertEqual(by_component["urea c 13"], {"V04CX05"})

    def test_atc_metadata_aliases_do_not_collapse_identity_keys(self):
        atc = load_atc(Path(__file__).resolve().parents[1] / "data" / "raw" / "who" / "atc.csv")
        lookup = build_atc_l5_lookup(atc)
        raw_components = {
            "activated charcoal": "CARBON ACTIVATED",
            "factor xiii": "Factor XIII",
            "fosaprepitant": "FOSAPREPITANT DIMEGLUMINE",
            "mycophenolate mofetil": "MYCOPHENOLATE MOFETIL",
            "methisoprinol": "METHISOPRINOL",
            "factor ix": "Factor IX",
            "factor viii": "Factor VIII",
            "antihemophilic factor": "Antihemophilic Factor",
            "factor vii": "Factor VII",
            "factor x": "Factor X",
            "l lysine": "L-LYSINE",
            "l methionine": "L-METHIONINE",
            "l tryptophan": "L-TRYPTOPHAN",
        }
        components = pd.DataFrame(
            [
                {
                    "component_id": component_id,
                    "component_norm": normalize_ingredient(raw_component),
                    "product_id": component_id,
                    "product_name": raw_component,
                    "approval_date": pd.NaT,
                    "is_combo": False,
                }
                for component_id, raw_component in raw_components.items()
            ]
        )

        matched = match_components_to_atc(components, lookup)

        by_component = {
            component_id: set(group["atc_code"])
            for component_id, group in matched.groupby("component_id")
        }
        self.assertEqual(by_component["activated charcoal"], {"A07BA01"})
        self.assertEqual(by_component["factor xiii"], {"B02BD07"})
        self.assertEqual(by_component["fosaprepitant"], {"A04AD12"})
        self.assertEqual(by_component["mycophenolate mofetil"], {"L04AA06"})
        self.assertEqual(by_component["methisoprinol"], {"J05AX05"})
        self.assertEqual(by_component["factor ix"], {"B02BD04"})
        self.assertEqual(by_component["factor viii"], {"B02BD02"})
        self.assertEqual(by_component["antihemophilic factor"], {"B02BD02"})
        self.assertEqual(by_component["factor vii"], {"B02BD05"})
        self.assertEqual(by_component["factor x"], {"B02BD13"})
        self.assertEqual(by_component["l lysine"], {"B05XB03"})
        self.assertIn("V03AB26", by_component["l methionine"])
        self.assertEqual(by_component["l tryptophan"], {"N06AX02"})
        self.assertEqual(normalize_ingredient("FOSAPREPITANT"), "fosaprepitant")
        self.assertNotEqual(normalize_ingredient("FOSAPREPITANT"), normalize_ingredient("APREPITANT"))
        self.assertEqual(normalize_ingredient("MYCOPHENOLATE MOFETIL"), "mycophenolate mofetil")
        self.assertNotEqual(
            normalize_ingredient("MYCOPHENOLATE MOFETIL"),
            normalize_ingredient("MYCOPHENOLIC ACID"),
        )

    def test_follitropin_alfa_beta_fda_artifact_uses_product_context(self):
        fda = pd.DataFrame(
            [
                {
                    "product_id": "gonal",
                    "DrugName": "GONAL-F",
                    "ActiveIngredient": "FOLLITROPIN ALFA/BETA",
                    "approval_date": pd.Timestamp("1997-01-01"),
                },
                {
                    "product_id": "follistim",
                    "DrugName": "FOLLISTIM AQ",
                    "ActiveIngredient": "FOLLITROPIN ALFA/BETA",
                    "approval_date": pd.Timestamp("1997-01-01"),
                },
            ]
        )

        components = explode_fda_components(fda)

        by_product = dict(zip(components["product_name"], components["component_norm"]))
        self.assertEqual(by_product["GONAL-F"], "follitropin alfa")
        self.assertEqual(by_product["FOLLISTIM AQ"], "follitropin beta")

    def test_combo_only_components_without_standalone_who_l5_stay_unmatched(self):
        atc = load_atc(Path(__file__).resolve().parents[1] / "data" / "raw" / "who" / "atc.csv")
        lookup = build_atc_l5_lookup(atc)
        combo_only = [
            "CARBIDOPA",
            "CLAVULANATE POTASSIUM",
            "CILASTATIN",
            "MESTRANOL",
            "NORGESTIMATE",
        ]
        components = pd.DataFrame(
            [
                {
                    "component_id": component,
                    "component_norm": normalize_ingredient(component),
                    "product_id": component,
                    "product_name": component,
                    "approval_date": pd.NaT,
                    "is_combo": True,
                }
                for component in combo_only
            ]
        )

        matched = match_components_to_atc(components, lookup)

        self.assertTrue(matched.empty)

        tazobactam = pd.DataFrame(
            [
                {
                    "component_id": "TAZOBACTAM",
                    "component_norm": normalize_ingredient("TAZOBACTAM"),
                    "product_id": "TAZOBACTAM",
                    "product_name": "TAZOBACTAM",
                    "approval_date": pd.NaT,
                    "is_combo": True,
                }
            ]
        )
        tazobactam_match = match_components_to_atc(tazobactam, lookup)
        self.assertEqual(set(tazobactam_match["atc_code"]), {"J01CG02"})

    def test_hsa_entresto_slash_complex_splits_to_components(self):
        raw = (
            "Sacubitril/Valsartan Sodium salt complex 56.551mg "
            "eqv sacubitril/valsartan free anhydrous acid"
        )
        self.assertEqual(split_hsa_ingredients(raw), ["sacubitril", "valsartan"])

    def test_liotrix_parenthetical_semicolon_is_not_an_fda_delimiter(self):
        self.assertEqual(split_fda_ingredients("LIOTRIX (T4;T3)"), ["LIOTRIX (T4;T3)"])
        self.assertEqual(normalize_ingredient("LIOTRIX (T4;T3)"), "liotrix")

    def test_fix_atc_ports_hsa_digit_position_typo_cleanup(self):
        self.assertEqual(fix_atc("A1OBAO2"), "A10BA02")
        self.assertEqual(fix_atc("JA5AR10"), "J05AR10")
        self.assertIsNone(fix_atc("Pending"))
        self.assertEqual(ATC_REMAP["L01XE01"], "L01EA01")


class AvailabilityLabelingTests(unittest.TestCase):
    def test_assign_availability_groups_on_substance_not_raw_ingredient(self):
        rows = pd.DataFrame(
            [
                {
                    "substance_key": "aspirin",
                    "source": "FDA",
                    "atc_level5": "B01AC06",
                    "Therapeutic Class (L1)": "BLOOD AND BLOOD FORMING ORGANS",
                    "Drug Class (L2)": "ANTITHROMBOTIC AGENTS",
                    "Pharmacological Subgroup (L3)": "ANTITHROMBOTIC AGENTS",
                    "Chemical Subgroup (L4)": "Platelet aggregation inhibitors excl. heparin",
                    "Substance (L5)": "aspirin",
                    "product_id": "us1",
                    "product_name": "ASPIRIN TABLET",
                    "approval_date": "1980-01-01",
                    "is_combo": False,
                    "is_rare": False,
                    "is_on_who_eml": False,
                },
                {
                    "substance_key": "aspirin",
                    "source": "HSA",
                    "atc_level5": "N02BA01",
                    "Therapeutic Class (L1)": "NERVOUS SYSTEM",
                    "Drug Class (L2)": "ANALGESICS",
                    "Pharmacological Subgroup (L3)": "OTHER ANALGESICS AND ANTIPYRETICS",
                    "Chemical Subgroup (L4)": "Salicylic acid and derivatives",
                    "Substance (L5)": "aspirin",
                    "product_id": "sg1",
                    "product_name": "ASPIRIN PLUS",
                    "approval_date": "1990-01-01",
                    "is_combo": True,
                    "is_rare": False,
                    "is_on_who_eml": False,
                },
            ]
        )

        output = assign_availability(rows)

        self.assertEqual(list(output.columns), OUTPUT_COLUMNS)
        self.assertEqual(len(output), 1)
        self.assertEqual(output.loc[0, "Active Ingredient"], "aspirin")
        self.assertEqual(output.loc[0, "ATC Codes"], "B01AC06 | N02BA01")
        self.assertEqual(output.loc[0, "FDA Status"], "STANDALONE")
        self.assertEqual(output.loc[0, "HSA Status"], "COMBO-ONLY")
        self.assertEqual(output.loc[0, "FDA Standalone Product Count"], 1)
        self.assertEqual(output.loc[0, "HSA Combo Product Count"], 1)
        self.assertEqual(output.loc[0, "Availability"], "PARTIAL GAP")
        self.assertIn("US: STANDALONE", output.loc[0, "Availability Reason"])
        self.assertIn("SG: COMBO-ONLY", output.loc[0, "Availability Reason"])

    def test_combo_only_on_both_sides_is_partial_gap_not_no_gap(self):
        rows = pd.DataFrame(
            [
                {
                    "substance_key": "clavulanic acid",
                    "source": "FDA",
                    "atc_level5": "J01CR02",
                    "Therapeutic Class (L1)": "ANTIINFECTIVES FOR SYSTEMIC USE",
                    "Drug Class (L2)": "ANTIBACTERIALS FOR SYSTEMIC USE",
                    "Pharmacological Subgroup (L3)": "ANTIBACTERIALS FOR SYSTEMIC USE",
                    "Chemical Subgroup (L4)": "Combinations of penicillins",
                    "Substance (L5)": "amoxicillin and beta-lactamase inhibitor",
                    "product_id": "us_combo",
                    "product_name": "AUGMENTIN",
                    "approval_date": "1984-08-13",
                    "is_combo": True,
                    "is_rare": False,
                    "is_on_who_eml": False,
                },
                {
                    "substance_key": "clavulanic acid",
                    "source": "HSA",
                    "atc_level5": "J01CR02",
                    "Therapeutic Class (L1)": "ANTIINFECTIVES FOR SYSTEMIC USE",
                    "Drug Class (L2)": "ANTIBACTERIALS FOR SYSTEMIC USE",
                    "Pharmacological Subgroup (L3)": "ANTIBACTERIALS FOR SYSTEMIC USE",
                    "Chemical Subgroup (L4)": "Combinations of penicillins",
                    "Substance (L5)": "amoxicillin and beta-lactamase inhibitor",
                    "product_id": "sg_combo",
                    "product_name": "AUGMENTIN",
                    "approval_date": "1999-03-01",
                    "is_combo": True,
                    "is_rare": False,
                    "is_on_who_eml": False,
                },
            ]
        )

        output = assign_availability(rows)

        self.assertEqual(output.loc[0, "Availability"], "PARTIAL GAP")
        self.assertIn("US: COMBO-ONLY", output.loc[0, "Availability Reason"])
        self.assertIn("SG: COMBO-ONLY", output.loc[0, "Availability Reason"])

    def test_combo_formulation_gap_is_separate_from_ingredient_gap(self):
        rows = pd.DataFrame(
            [
                {
                    "substance_key": "sacubitril",
                    "source": "FDA",
                    "atc_level5": "C09DX04",
                    "Therapeutic Class (L1)": "CARDIOVASCULAR SYSTEM",
                    "Drug Class (L2)": "AGENTS ACTING ON THE RENIN-ANGIOTENSIN SYSTEM",
                    "Pharmacological Subgroup (L3)": "ANGIOTENSIN II RECEPTOR BLOCKERS",
                    "Chemical Subgroup (L4)": "Angiotensin II receptor blockers, combinations",
                    "Substance (L5)": "valsartan and sacubitril",
                    "product_id": "us_combo",
                    "product_name": "ENTRESTO",
                    "approval_date": "2015-07-07",
                    "is_combo": True,
                    "is_rare": False,
                    "is_on_who_eml": False,
                },
                {
                    "substance_key": "valsartan",
                    "source": "FDA",
                    "atc_level5": "C09DX04",
                    "Therapeutic Class (L1)": "CARDIOVASCULAR SYSTEM",
                    "Drug Class (L2)": "AGENTS ACTING ON THE RENIN-ANGIOTENSIN SYSTEM",
                    "Pharmacological Subgroup (L3)": "ANGIOTENSIN II RECEPTOR BLOCKERS",
                    "Chemical Subgroup (L4)": "Angiotensin II receptor blockers, combinations",
                    "Substance (L5)": "valsartan and sacubitril",
                    "product_id": "us_combo",
                    "product_name": "ENTRESTO",
                    "approval_date": "2015-07-07",
                    "is_combo": True,
                    "is_rare": False,
                    "is_on_who_eml": False,
                },
                {
                    "substance_key": "sacubitril",
                    "source": "HSA",
                    "atc_level5": "C09DX04",
                    "Therapeutic Class (L1)": "CARDIOVASCULAR SYSTEM",
                    "Drug Class (L2)": "AGENTS ACTING ON THE RENIN-ANGIOTENSIN SYSTEM",
                    "Pharmacological Subgroup (L3)": "ANGIOTENSIN II RECEPTOR BLOCKERS",
                    "Chemical Subgroup (L4)": "Angiotensin II receptor blockers, combinations",
                    "Substance (L5)": "valsartan and sacubitril",
                    "product_id": "sg_sacubitril",
                    "product_name": "SACUBITRIL ONLY",
                    "approval_date": "2024-01-01",
                    "is_combo": False,
                    "is_rare": False,
                    "is_on_who_eml": False,
                },
                {
                    "substance_key": "valsartan",
                    "source": "HSA",
                    "atc_level5": "C09CA03",
                    "Therapeutic Class (L1)": "CARDIOVASCULAR SYSTEM",
                    "Drug Class (L2)": "AGENTS ACTING ON THE RENIN-ANGIOTENSIN SYSTEM",
                    "Pharmacological Subgroup (L3)": "ANGIOTENSIN II RECEPTOR BLOCKERS",
                    "Chemical Subgroup (L4)": "Angiotensin II receptor blockers, plain",
                    "Substance (L5)": "valsartan",
                    "product_id": "sg_valsartan",
                    "product_name": "VALSARTAN ONLY",
                    "approval_date": "2024-01-01",
                    "is_combo": False,
                    "is_rare": False,
                    "is_on_who_eml": False,
                },
            ]
        )

        main = assign_availability(rows)
        combo = build_combo_formulation_gaps(rows)

        self.assertTrue((main["Availability"] == "PARTIAL GAP").all())
        self.assertEqual(len(combo), 1)
        self.assertEqual(combo.loc[0, "Combo Ingredients"], "sacubitril + valsartan")
        self.assertEqual(combo.loc[0, "Combo/Formulation Availability"], "FDA_COMBO_ONLY")
        self.assertEqual(
            combo.loc[0, "Other Country Component Coverage"],
            "all components standalone in HSA",
        )

    def test_hsa_rows_with_invalid_atc_still_contribute_by_ingredient_name(self):
        with TemporaryDirectory() as temp_dir:
            hsa_path = Path(temp_dir) / "hsa.csv"
            hsa_path.write_text(
                "\n".join(
                    [
                        "licence_no,product_name,active_ingredients,atc_code,approval_d",
                        "SG1,APO-PRAVASTATIN TABLET 20 mg,PRAVASTATIN SODIUM,,2005-07-22",
                        (
                            "SG2,INON GRANULES,"
                            "ALUMINIUM HYDROXIDE (DRIED GEL)&&MAGNESIUM CARBONATE LIGHT,"
                            "A02AH,1989-06-08"
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            atc_l5_lookup = pd.DataFrame(
                [{"atc_code": "C10AA03", "name_norm": "pravastatin"}]
            )
            atc_classes = pd.DataFrame(
                [
                    {
                        "atc_level5": "C10AA03",
                        "Therapeutic Class (L1)": "CARDIOVASCULAR SYSTEM",
                        "Drug Class (L2)": "LIPID MODIFYING AGENTS",
                        "Pharmacological Subgroup (L3)": "LIPID MODIFYING AGENTS",
                        "Chemical Subgroup (L4)": "HMG CoA reductase inhibitors",
                        "Substance (L5)": "pravastatin",
                        "substance_key": "pravastatin",
                    }
                ]
            )

            rows, fallback_audit, unmatched_audit = load_hsa_product_substances(
                hsa_path,
                atc_l5_lookup,
                atc_classes,
                combo_atc_codes=set(),
                rare_substance_keys=set(),
            )

        self.assertEqual(set(rows["substance_key"]), {"pravastatin", "aluminum hydroxide", "magnesium carbonate"})
        self.assertEqual(rows.loc[rows["substance_key"].eq("pravastatin"), "atc_level5"].iloc[0], "C10AA03")
        self.assertTrue(fallback_audit.empty)
        self.assertEqual(set(unmatched_audit["component_norm"]), {"aluminum hydroxide", "magnesium carbonate"})


class WhoEmlTests(unittest.TestCase):
    def test_parse_who_eml_terms_normalizes_index_entries_and_splits_combinations(self):
        text = """
        WHO Model List of Essential Medicines - 23rd List (2023)
        Index
        amoxicillin + clavulanic acid ..................................... 9, 17
        omeprazole ........................................................ 44
        oxygen ............................................................. 1, 2
        paracetamol (acetaminophen) ........................................ 2, 26
        WHO Model List of Essential Medicines - 23rd List (2023) page 67
        """

        terms = parse_who_eml_terms(text)

        self.assertIn("amoxicillin", terms)
        self.assertIn("clavulanic acid", terms)
        self.assertIn("omeprazole", terms)
        self.assertIn("oxygen", terms)
        self.assertIn("paracetamol", terms)

    def test_flag_who_eml_uses_normalized_identity_without_substring_matches(self):
        terms = {"amlodipine", "clavulanic acid", "paracetamol"}
        rows = pd.DataFrame(
            [
                {"substance_key": "Acetaminophen"},
                {"substance_key": "CLAVULANATE POTASSIUM"},
                {"substance_key": "amlodipine"},
                {"substance_key": "levamlodipine"},
                {"substance_key": "rare cosmetic active"},
            ]
        )

        flagged = flag_who_eml(rows, terms)
        by_key = dict(zip(flagged["substance_key"], flagged["is_on_who_eml"]))

        self.assertTrue(by_key["Acetaminophen"])
        self.assertTrue(by_key["CLAVULANATE POTASSIUM"])
        self.assertTrue(by_key["amlodipine"])
        self.assertFalse(by_key["levamlodipine"])
        self.assertFalse(by_key["rare cosmetic active"])


if __name__ == "__main__":
    unittest.main()
