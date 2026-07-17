from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import unittest
from unittest import mock
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from src.atlas import (
    BangladeshAdapter,
    BhutanAdapter,
    BuildSpec as AtlasBuildSpec,
    FdaAdapter,
    HsaAdapter,
    build_atlas,
    compare_atlas,
    render_legacy_compatibility,
    _eeml_medicine_components,
    _select_bhutan_generic_text,
    _validate_manifest_counts,
)
from src.fetch_sources import fetch_sources
from src.atlas_adjudications import (
    adjudicate_eml_concept_product,
    aggregate_current_marketing,
    canonical_reviewed_identity,
    canonicalize_atc,
    classify_eml_scope,
    external_source_for_observation,
    refine_identity,
    reviewed_equivalent,
)


EXTRACTION_DATE = date(2026, 7, 15)
REAL_SOURCE_REGRESSIONS = (
    Path(__file__).parent / "fixtures" / "real_source_regressions.json"
)


def BuildSpec(*args, **kwargs):
    """Create an explicitly unmanifested test-fixture build specification."""

    kwargs.setdefault("allow_unmanifested_test_fixture", True)
    return AtlasBuildSpec(*args, **kwargs)


class AtlasAdjudicationTests(unittest.TestCase):
    def test_identity_refinement_preserves_clinically_defining_active_portions(self):
        cases = {
            ("Hyoscine Butyl Bromide", "hyoscine"): "hyoscine butylbromide",
            ("Hyoscine N-Butylbromide", "hyoscine n"): "hyoscine butylbromide",
            ("Hyoscine Hydrobromide", "hyoscine"): "hyoscine hydrobromide",
            ("Scopolamine", "scopolamine"): "hyoscine hydrobromide",
            ("Disodium EDTA", "edetate"): "disodium edetate",
            ("Disodium Edetate", "edetate"): "disodium edetate",
            ("Sodium Calcium Edetate", "edetate"): "sodium calcium edetate",
            ("Edetate Calcium Disodium", "edetate"): "sodium calcium edetate",
            ("5-Aminosalicylic Acid", "aminosalicylic acid"): "mesalazine",
            ("p-Aminosalicylic Acid", "aminosalicylic acid"): "para aminosalicylic acid",
            ("Benzyl Benzoate", "benzyl"): "benzyl benzoate",
            ("Potassium Permanganate", "permanganate"): "potassium permanganate",
            ("Silver Diamine Fluoride", "silver diamine"): "silver diamine fluoride",
        }

        for (raw, normalized), expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(refine_identity(raw, normalized), expected)

    def test_reviewed_equivalence_resolves_spelling_holds_without_merging_prodrugs(self):
        equivalent_pairs = [
            ("porcatant alfa", "poractant alfa"),
            ("insulin glargin", "insulin glargine"),
            ("tioguanine", "thioguanine"),
            ("anastrozol", "anastrozole"),
            ("enoxaprin", "enoxaparin"),
            ("protamin", "protamine"),
            ("metformine", "metformin"),
            ("tenofovir disoproxil fumerate", "tenofovir disoproxil"),
        ]
        for left, right in equivalent_pairs:
            with self.subTest(left=left, right=right):
                self.assertTrue(reviewed_equivalent(left, right))

        self.assertFalse(
            reviewed_equivalent("tenofovir alafenamide", "tenofovir disoproxil")
        )
        self.assertFalse(reviewed_equivalent("esomeprazole", "omeprazole"))

    def test_reviewed_identity_uses_one_deterministic_preferred_key(self):
        cases = {
            "porcatant alfa": "poractant alfa",
            "insulin glargin": "insulin glargine",
            "thioguanine": "tioguanine",
            "anastrozol": "anastrozole",
            "enoxaprin": "enoxaparin",
            "protamin": "protamine",
            "metformine": "metformin",
            "esomeprazole strontium": "esomeprazole",
            "tenofovir disoproxil fumerate": "tenofovir disoproxil",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(canonical_reviewed_identity(source), expected)

        self.assertEqual(
            canonical_reviewed_identity("tenofovir alafenamide"),
            "tenofovir alafenamide",
        )

    def test_eeml_identity_refinement_splits_unsafe_pseudo_substances(self):
        cases = {
            "hyoscine butylbromide": ["hyoscine butylbromide"],
            "hyoscine hydrobromide": ["hyoscine hydrobromide"],
            "sodium calcium edetate": ["sodium calcium edetate"],
            "benzyl benzoate": ["benzyl benzoate"],
            "potassium permanganate": ["potassium permanganate"],
            "silver diamine fluoride": ["silver diamine fluoride"],
            "compound sodium lactate solution": ["compound sodium lactate"],
            "oral rehydration salts": ["oral rehydration salts"],
            "insulin (human, short-acting)": ["human insulin short acting"],
            "insulin (human, intermediate-acting)": ["human insulin intermediate acting"],
            "insulin (analogue, rapid-acting)": ["insulin analogue rapid acting"],
            "insulin (analogue, long-acting)": ["insulin analogue long acting"],
        }
        for medicine_name, expected in cases.items():
            with self.subTest(medicine_name=medicine_name):
                self.assertEqual(_eeml_medicine_components(medicine_name), expected)

    def test_scope_classification_covers_non_drug_register_object_classes(self):
        cases = {
            "whole blood": "blood_component",
            "red blood cells": "blood_component",
            "male condom": "barrier_device",
            "dental glass ionomer cement": "dental_material",
            "ready-to-use therapeutic food": "therapeutic_food",
            "sunscreen": "topical_protective_product",
            "resin-based composite (low-viscosity)": "dental_material",
            "copper-containing intrauterine device": "contraceptive_device",
        }
        for medicine_name, expected in cases.items():
            with self.subTest(medicine_name=medicine_name):
                self.assertEqual(classify_eml_scope(medicine_name, ""), expected)

        self.assertEqual(classify_eml_scope("amoxicillin", ""), "")

    def test_atc_hygiene_corrects_known_errors_and_rejects_placeholders(self):
        self.assertEqual(canonicalize_atc("A01BA02"), ("A10BA02", "corrected"))
        self.assertEqual(canonicalize_atc("A10AC01", "insulin (human, short-acting)"), ("A10AB01", "corrected"))
        self.assertEqual(canonicalize_atc("J01CR02"), ("J01CR02", "valid"))
        self.assertEqual(canonicalize_atc("J 01 CR 02"), ("J01CR02", "corrected"))
        self.assertEqual(canonicalize_atc("NOT AVAILABL"), ("", "invalid"))
        self.assertEqual(canonicalize_atc("PENDING"), ("", "invalid"))

    def test_current_marketing_requires_every_supporting_status_to_be_known(self):
        self.assertEqual(aggregate_current_marketing(["Discontinued"]), "NOT_MARKETED")
        self.assertEqual(
            aggregate_current_marketing(["Discontinued", ""]), "UNKNOWN"
        )
        self.assertEqual(
            aggregate_current_marketing(["Discontinued", "Prescription"]),
            "CONFIRMED",
        )

    def test_curated_concept_adjudication_is_product_and_formulation_specific(self):
        present = adjudicate_eml_concept_product(
            "oral rehydration salts",
            country_code="BD",
            product_name="ORS (Oral Rehydration Salts)",
            raw_ingredient_text=(
                "Anhydrous Glucose + Potassium Chloride + Sodium Chloride + "
                "Trisodium Citrate  6.75 gm + 750 mg + 1.3 gm + 1.45 gm/500 ml"
            ),
            form="Oral Saline",
            strength="",
            product_atc_codes="",
            ingredient_keys=frozenset(
                {"glucose", "potassium chloride", "sodium chloride", "trisodium citrate"}
            ),
        )
        self.assertEqual(present.state, "VERIFIED_PRESENT")

        bd_pancreatin = adjudicate_eml_concept_product(
            "pancreatic enzymes",
            country_code="BD",
            product_name="Pancreon 10000 Capsule",
            raw_ingredient_text="Pancreatin 150 mg",
            form="Capsule",
            strength="",
            product_atc_codes="",
            ingredient_keys=frozenset({"pancreatin"}),
        )
        self.assertEqual(bd_pancreatin.state, "INDETERMINATE")
        self.assertEqual(bd_pancreatin.needs_external_source, "ACTIVITY_UNIT_CONFIRMATION")

        oral_tretinoin = adjudicate_eml_concept_product(
            "all trans retinoic acid",
            country_code="SG",
            product_name="VESANOID CAPSULE 10 mg",
            raw_ingredient_text="TRETINOIN",
            form="CAPSULE",
            strength="10 mg",
            product_atc_codes="L01XX14",
            ingredient_keys=frozenset({"tretinoin"}),
        )
        topical_tretinoin = adjudicate_eml_concept_product(
            "all trans retinoic acid",
            country_code="BD",
            product_name="Trinon",
            raw_ingredient_text="Tretinoin 25 mg/100 gm",
            form="Cream",
            strength="",
            product_atc_codes="",
            ingredient_keys=frozenset({"tretinoin"}),
        )
        self.assertEqual(oral_tretinoin.state, "VERIFIED_PRESENT")
        self.assertIsNone(topical_tretinoin)

        long_acting = adjudicate_eml_concept_product(
            "insulin analogue long acting",
            country_code="BD",
            product_name="Insul Glargine",
            raw_ingredient_text="Insulin Glargin 100 IU/ml",
            form="Injection",
            strength="",
            product_atc_codes="",
            ingredient_keys=frozenset({"insulin glargine"}),
        )
        rapid_acting = adjudicate_eml_concept_product(
            "insulin analogue rapid acting",
            country_code="BD",
            product_name="Insul Glargine",
            raw_ingredient_text="Insulin Glargin 100 IU/ml",
            form="Injection",
            strength="",
            product_atc_codes="",
            ingredient_keys=frozenset({"insulin glargine"}),
        )
        self.assertEqual(long_acting.state, "VERIFIED_PRESENT")
        self.assertIsNone(rapid_acting)

        bt_bcg = adjudicate_eml_concept_product(
            "bcg vaccine",
            country_code="BT",
            product_name="",
            raw_ingredient_text="Live attenuated Bacille Calmette-Guérin Vaccine",
            form="",
            strength="",
            product_atc_codes="",
            ingredient_keys=frozenset({"bacille calmette guerin vaccine"}),
        )
        self.assertEqual(bt_bcg.state, "VERIFIED_PRESENT")

        us_short = adjudicate_eml_concept_product(
            "human insulin short acting",
            country_code="US",
            product_name="HUMULIN R",
            raw_ingredient_text="INSULIN HUMAN",
            form="SOLUTION;SUBCUTANEOUS",
            strength="100 UNITS/ML",
            product_atc_codes="",
            ingredient_keys=frozenset({"insulin"}),
        )
        self.assertEqual(us_short.state, "VERIFIED_PRESENT")

        non_dengue = adjudicate_eml_concept_product(
            "dengue vaccine",
            country_code="SG",
            product_name="ACAM2000",
            raw_ingredient_text="LIVE VACCINIA VIRUS",
            form="INJECTION",
            strength="",
            product_atc_codes="J07BX",
            ingredient_keys=frozenset({"vaccinia virus"}),
        )
        shingles = adjudicate_eml_concept_product(
            "varicella vaccine",
            country_code="SG",
            product_name="SHINGRIX",
            raw_ingredient_text="Recombinant Varicella Zoster Virus glycoprotein E",
            form="INJECTION",
            strength="",
            product_atc_codes="J07BK",
            ingredient_keys=frozenset({"recombinant varicella zoster virus glycoprotein e"}),
        )
        self.assertIsNone(non_dengue)
        self.assertIsNone(shingles)

        lispro_protamine = adjudicate_eml_concept_product(
            "human insulin intermediate acting",
            country_code="SG",
            product_name="INSULIN LISPRO PROTAMINE",
            raw_ingredient_text="INSULIN LISPRO PROTAMINE SUSPENSION",
            form="INJECTION",
            strength="100 IU/ml",
            product_atc_codes="A10AC04",
            ingredient_keys=frozenset({"insulin lispro protamine"}),
        )
        self.assertIsNone(lispro_protamine)

        human_premix = adjudicate_eml_concept_product(
            "human insulin intermediate acting",
            country_code="SG",
            product_name="MIXTARD 30",
            raw_ingredient_text=(
                "Insulin human (as isophane insulin) && "
                "Insulin human (as soluble insulin)"
            ),
            form="INJECTION",
            strength="70 IU/ml && 30 IU/ml",
            product_atc_codes="A10AD01",
            ingredient_keys=frozenset({"insulin"}),
        )
        self.assertEqual(human_premix.state, "VERIFIED_PRESENT")
        self.assertEqual(human_premix.mode_override, "COMBO_ONLY")

        hartmann_superset = adjudicate_eml_concept_product(
            "compound sodium lactate",
            country_code="US",
            product_name="DEXTROSE 5% IN LACTATED RINGER'S",
            raw_ingredient_text=(
                "CALCIUM CHLORIDE; DEXTROSE; POTASSIUM CHLORIDE; "
                "SODIUM CHLORIDE; SODIUM LACTATE"
            ),
            form="INJECTABLE;INJECTION",
            strength="",
            product_atc_codes="",
            ingredient_keys=frozenset(
                {
                    "calcium chloride",
                    "glucose",
                    "potassium chloride",
                    "sodium chloride",
                    "sodium lactate",
                }
            ),
        )
        self.assertIsNone(hartmann_superset)

    def test_insulin_subtypes_use_correct_atc_products_and_glargine_identity(self):
        self.assertEqual(
            canonicalize_atc(
                "A10AB01", "INSULATARD PENFILL INJECTION 100 iu/ml"
            ),
            ("A10AC01", "corrected"),
        )

        expected_present = {
            "HUMULIN R": "100 UNITS/ML",
            "HUMULIN R PEN": "100 UNITS/ML",
            "NOVOLIN R": "100 UNITS/ML",
        }
        for product_name, strength in expected_present.items():
            with self.subTest(product_name=product_name):
                adjudication = adjudicate_eml_concept_product(
                    "human insulin short acting",
                    country_code="US",
                    product_name=product_name,
                    raw_ingredient_text="INSULIN RECOMBINANT HUMAN",
                    form="INJECTABLE;INJECTION",
                    strength=strength,
                    product_atc_codes="",
                    ingredient_keys=frozenset({"insulin recombinant human"}),
                )
                self.assertIsNotNone(adjudication)
                self.assertEqual(adjudication.state, "VERIFIED_PRESENT")

        for product_name, form, strength in (
            ("HUMULIN R KWIKPEN", "SOLUTION;SUBCUTANEOUS", "500 UNITS/ML"),
            ("MYXREDLIN", "SOLUTION;INTRAVENOUS", "1 UNIT/ML"),
        ):
            with self.subTest(product_name=product_name):
                self.assertIsNone(
                    adjudicate_eml_concept_product(
                        "human insulin short acting",
                        country_code="US",
                        product_name=product_name,
                        raw_ingredient_text="INSULIN HUMAN",
                        form=form,
                        strength=strength,
                        product_atc_codes="",
                        ingredient_keys=frozenset({"insulin human"}),
                    )
                )

        self.assertEqual(
            refine_identity(
                "Insulin Glargine Impact 100 units/ml (Recombinant)",
                "insulin glargine impact",
            ),
            "insulin glargine",
        )

    def test_ors_products_are_graded_exact_related_or_requires_verification(self):
        cases = {
            (
                "Anhydrous Glucose + Potassium Chloride + Sodium Chloride + "
                "Trisodium Citrate  6.75 gm + 750 mg + 1.3 gm + 1.45 gm/500 ml"
            ): ("VERIFIED_PRESENT", "reviewed_ors_current_reduced_osmolarity"),
            (
                "Dextrose Anhydrous + Potassium Chloride + Sodium Chloride + "
                "Trisodium Citrate  20 gm + 1.5 gm + 3.5 gm + 2.9 gm/Litres"
            ): ("INDETERMINATE", "reviewed_ors_related_non_exact"),
            (
                "Dextrose Anhydrous + Potassium Chloride + Sodium Chloride + "
                "Trisodium Citrate  10 gm + 750 gm + 1.75 gm + 1.45 gm/500 ml"
            ): ("INDETERMINATE", "reviewed_ors_composition_requires_verification"),
        }
        keys = frozenset(
            {
                "dextrose",
                "potassium chloride",
                "sodium chloride",
                "trisodium citrate",
            }
        )
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                adjudication = adjudicate_eml_concept_product(
                    "oral rehydration salts",
                    country_code="BD",
                    product_name="Brac Saline",
                    raw_ingredient_text=raw,
                    form="Oral Saline",
                    strength="",
                    product_atc_codes="",
                    ingredient_keys=keys,
                )
                self.assertIsNotNone(adjudication)
                self.assertEqual(
                    (adjudication.state, adjudication.rule), expected
                )

    def test_us_cber_biologic_categories_require_the_missing_category_source(self):
        for concept in (
            "BCG vaccine",
            "anti-rabies immunoglobulin",
            "coagulation factor VIII",
            "coagulation factor IX",
            "tuberculin purified protein derivative",
        ):
            with self.subTest(concept=concept):
                self.assertEqual(
                    external_source_for_observation("US", concept),
                    "FDA_CBER_OR_PURPLE_BOOK",
                )
        for concept in (
            "anti rabies virus monoclonal antibodies",
            "category sibling fixture",
        ):
            with self.subTest(concept=concept):
                self.assertEqual(
                    external_source_for_observation(
                        "US",
                        concept,
                        "Sera, immunoglobulins and monoclonal antibodies",
                    ),
                    "FDA_CBER_OR_PURPLE_BOOK",
                )
        self.assertEqual(external_source_for_observation("US", "metformin"), "")


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, sep="\t", index=False)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def write_legacy_reference_inputs(root: Path) -> None:
    raw = root / "data" / "raw"
    write_csv(
        raw / "who" / "atc.csv",
        [
            {"atc_code": "J01CA04", "atc_name": "amoxicillin"},
            {"atc_code": "N02BE01", "atc_name": "paracetamol"},
            {"atc_code": "L04AB04", "atc_name": "adalimumab"},
        ],
    )
    (raw / "Rare Drugs.xls").write_text(
        "<table><tr><th>Generic Name</th></tr>"
        "<tr><td>unmatched fixture rare medicine</td></tr></table>",
        encoding="utf-8",
    )


def write_fixture_sources(root: Path) -> None:
    raw = root / "data" / "raw"

    write_tsv(
        raw / "fda" / "Applications.txt",
        [
            {"ApplNo": "1", "ApplType": "NDA", "SponsorName": "Originator"},
            {"ApplNo": "2", "ApplType": "ANDA", "SponsorName": "Generic Co"},
            {"ApplNo": "3", "ApplType": "BLA", "SponsorName": "Biologic Co"},
        ],
    )
    write_tsv(
        raw / "fda" / "Products.txt",
        [
            {
                "ApplNo": "1",
                "ProductNo": "001",
                "Form": "CAPSULE",
                "Strength": "500MG",
                "ReferenceDrug": "1",
                "DrugName": "AMOXICILLIN",
                "ActiveIngredient": "AMOXICILLIN TRIHYDRATE",
                "ReferenceStandard": "1",
            },
            {
                "ApplNo": "2",
                "ProductNo": "001",
                "Form": "TABLET",
                "Strength": "500MG",
                "ReferenceDrug": "0",
                "DrugName": "PARACETAMOL",
                "ActiveIngredient": "ACETAMINOPHEN",
                "ReferenceStandard": "0",
            },
            {
                "ApplNo": "3",
                "ProductNo": "001",
                "Form": "INJECTION",
                "Strength": "40MG",
                "ReferenceDrug": "1",
                "DrugName": "ADALIMUMAB",
                "ActiveIngredient": "ADALIMUMAB",
                "ReferenceStandard": "1",
            },
        ],
    )
    write_tsv(
        raw / "fda" / "Submissions.txt",
        [
            {
                "ApplNo": appl,
                "SubmissionClassCodeID": "",
                "SubmissionType": "ORIG",
                "SubmissionNo": "1",
                "SubmissionStatus": "AP",
                "SubmissionStatusDate": approved,
                "SubmissionsPublicNotes": "",
                "ReviewPriority": "",
                "InActivateDate": "",
            }
            for appl, approved in [
                ("1", "2000-01-01"),
                ("2", "2001-01-01"),
                ("3", "2002-01-01"),
            ]
        ],
    )
    write_tsv(
        raw / "fda" / "MarketingStatus.txt",
        [
            {"MarketingStatusID": "1", "ApplNo": "1", "ProductNo": "001"},
            {"MarketingStatusID": "3", "ApplNo": "2", "ProductNo": "001"},
            {"MarketingStatusID": "1", "ApplNo": "3", "ProductNo": "001"},
        ],
    )
    write_tsv(
        raw / "fda" / "MarketingStatus_Lookup.txt",
        [
            {"MarketingStatusID": "1", "MarketingStatusDescription": "Prescription"},
            {"MarketingStatusID": "3", "MarketingStatusDescription": "Discontinued"},
        ],
    )

    write_csv(
        raw / "hsa" / "hsa_registered_therapeutic_products.csv",
        [
            {
                "licence_no": "SG1",
                "product_name": "AMOXICILLIN 500",
                "approval_d": "2003-01-01",
                "atc_code": "",
                "active_ingredients": "AMOXICILLIN TRIHYDRATE",
                "license_holder": "SG Holder",
            },
            {
                "licence_no": "SG2",
                "product_name": "METFORMIN 500",
                "approval_d": "2004-01-01",
                "atc_code": "Pending",
                "active_ingredients": "METFORMIN HYDROCHLORIDE",
                "license_holder": "SG Holder",
            },
        ],
    )

    bd_payload = {
        "metadata": {
            "country_code": "BD",
            "num_found": 2,
            "num_returned": 2,
            "source_url": "https://api.tr.ocl.dghs.gov.bd/",
        },
        "concepts": [
            {
                "id": "bd-amox-clav",
                "display_name": "Co-amoxiclav",
                "retired": False,
                "extras": {
                    "dar_number": "100-0001-001",
                    "trade_name": "Co-amoxiclav",
                    "generic_content_raw": "Amoxicillin + Clavulanic Acid  500 mg + 125 mg",
                    "dosage_form": "Tablet",
                    "company": "BD Pharma",
                    "dar_quality_flag": "",
                },
            },
            {
                "id": "bd-para",
                "display_name": "Paracetamol",
                "retired": False,
                "extras": {
                    "dar_number": "100-0002-001",
                    "trade_name": "Paracetamol",
                    "generic_content_raw": "Paracetamol 500 mg",
                    "dosage_form": "Tablet",
                    "company": "BD Pharma",
                    "dar_quality_flag": "",
                },
            },
        ],
    }
    bd_path = raw / "bd" / "dgda_concepts.json"
    bd_path.parent.mkdir(parents=True, exist_ok=True)
    bd_path.write_text(json.dumps(bd_payload), encoding="utf-8")

    write_csv(
        raw / "bt" / "registered_products.csv",
        [
            {
                "Sr. No": "1",
                "Reg_No": "BHU-DRA/23/RN/H001",
                "Generic_Name": "Metformin Hydrochloride Tablets 500 mg",
                "BrandName": "METABIT",
                "Therapeutic Category": "Antidiabetic",
                "MAH": "BT Holder",
                "Packsize": "10 x 10",
                "Product_validity": "2026-07-15",
                "Manufacture": "BT Maker",
            },
            {
                "Sr. No": "2",
                "Reg_No": "BHU-DRA/23/RN/H002",
                "Generic_Name": "OLD PARA",
                "BrandName": "Paracetamol Tablets BP 500 mg",
                "Therapeutic Category": "Analgesic",
                "MAH": "BT Holder",
                "Packsize": "10 x 10",
                "Product_validity": "2024-01-01",
                "Manufacture": "BT Maker",
            },
            {
                "Sr. No": "3",
                "Reg_No": "BHU-DRA/23/RN/H003",
                "Generic_Name": "Amoxicillin 500 mg + Clavulanic Acid 125 mg",
                "BrandName": "BHUCLAV",
                "Therapeutic Category": "Antibiotic",
                "MAH": "BT Holder",
                "Packsize": "10 x 10",
                "Product_validity": "2027-01-01",
                "Manufacture": "BT Maker",
            },
            {
                "Sr. No": "4",
                "Reg_No": "BHU-MPD/24/RN/H165",
                "Generic_Name": "Magnesium Sulphate Injection BP",
                "BrandName": "NA",
                "Therapeutic Category": "Vitamins and minerals",
                "MAH": "Lamgong",
                "Packsize": "10 x 2 ml",
                "Product_validity": "2027-05-05",
                "Manufacture": "Montage",
            },
            {
                "Sr. No": "5",
                "Reg_No": "BHU-DRA/23/RN/H159",
                "Generic_Name": "OPTIVIEW Lubricant Eye Drop",
                "BrandName": "Carboxymethylcellulose Eye Drops IP 0.5% w/v",
                "Therapeutic Category": "Ophthalmic lubricant",
                "MAH": "Kuenphen",
                "Packsize": "10 ml",
                "Product_validity": "2026-12-04",
                "Manufacture": "Suyaash",
            },
        ],
    )
    write_csv(
        raw / "bt" / "regulatory_actions.csv",
        [
            {
                "Sl.no": "1",
                "Registration number": "BHU-DRA/23/RN/H003",
                "Generic ": "Amoxicillin + Clavulanic Acid",
                "Brand Name": "BHUCLAV",
                "Manufacturer": "BT Maker",
                "MAH": "BT Holder",
                "Status": "Cancelled",
                "Date of action": "2026-01-01",
                "Reason for cancellation/Withdrawal": "Fixture cancellation",
            },
            {
                "Sl.no": "2",
                "Registration number": "BHU-MPD/24/RN/H165",
                "Generic ": "Glipizide 5 mg BP Tablets",
                "Brand Name": "N/A",
                "Manufacturer": "Medico",
                "MAH": "KMT",
                "Status": "Cancelled",
                "Date of action": "2025-12-24",
                "Reason for cancellation/Withdrawal": "Fixture registration collision",
            },
        ],
    )

    write_csv(
        raw / "who" / "eeml_2025.csv",
        [
            {
                "Medicine name": "amoxicillin",
                "EML section": "Anti-infective medicines",
                "Formulations": "solid oral dosage form",
                "Indication": "",
                "ATC codes": "J01CA04",
                "Combined with": "",
                "Status": "Added",
            },
            {
                "Medicine name": "paracetamol",
                "EML section": "Pain and palliative care",
                "Formulations": "tablet",
                "Indication": "",
                "ATC codes": "N02BE01",
                "Combined with": "",
                "Status": "Added",
            },
            {
                "Medicine name": "metformin",
                "EML section": "Diabetes",
                "Formulations": "tablet",
                "Indication": "",
                "ATC codes": "A10BA02",
                "Combined with": "",
                "Status": "Added",
            },
            {
                "Medicine name": "amoxicillin + paracetamol",
                "EML section": "Combination fixture",
                "Formulations": "tablet",
                "Indication": "",
                "ATC codes": "",
                "Combined with": "gentamicin",
                "Status": "Added",
            },
            {
                "Medicine name": "obsolete medicine",
                "EML section": "Removed",
                "Formulations": "",
                "Indication": "",
                "ATC codes": "",
                "Combined with": "",
                "Status": "Removed",
            },
        ],
    )
    write_legacy_reference_inputs(root)


def append_fda_fixture_product(
    root: Path,
    *,
    appl_no: str,
    product_no: str,
    drug_name: str,
    active_ingredient: str,
    form: str,
    strength: str,
    marketing_status_id: str = "1",
) -> None:
    """Append one internally consistent Drugs@FDA fixture observation."""

    fda = root / "data" / "raw" / "fda"
    applications = pd.read_csv(fda / "Applications.txt", sep="\t", dtype=str)
    applications.loc[len(applications)] = {
        "ApplNo": appl_no,
        "ApplType": "NDA",
        "SponsorName": "Regression Fixture",
    }
    applications.to_csv(fda / "Applications.txt", sep="\t", index=False)

    products = pd.read_csv(fda / "Products.txt", sep="\t", dtype=str)
    products.loc[len(products)] = {
        "ApplNo": appl_no,
        "ProductNo": product_no,
        "Form": form,
        "Strength": strength,
        "ReferenceDrug": "1",
        "DrugName": drug_name,
        "ActiveIngredient": active_ingredient,
        "ReferenceStandard": "1",
    }
    products.to_csv(fda / "Products.txt", sep="\t", index=False)

    submissions = pd.read_csv(fda / "Submissions.txt", sep="\t", dtype=str)
    submissions.loc[len(submissions)] = {
        "ApplNo": appl_no,
        "SubmissionClassCodeID": "",
        "SubmissionType": "ORIG",
        "SubmissionNo": "1",
        "SubmissionStatus": "AP",
        "SubmissionStatusDate": "2003-01-01",
        "SubmissionsPublicNotes": "",
        "ReviewPriority": "",
        "InActivateDate": "",
    }
    submissions.to_csv(fda / "Submissions.txt", sep="\t", index=False)

    marketing = pd.read_csv(fda / "MarketingStatus.txt", sep="\t", dtype=str)
    marketing.loc[len(marketing)] = {
        "MarketingStatusID": marketing_status_id,
        "ApplNo": appl_no,
        "ProductNo": product_no,
    }
    marketing.to_csv(fda / "MarketingStatus.txt", sep="\t", index=False)


class CountryAdapterContractTests(unittest.TestCase):
    def test_real_south_asia_records_emit_only_declared_active_components(self):
        cases = json.loads(REAL_SOURCE_REGRESSIONS.read_text(encoding="utf-8"))[
            "parser_cases"
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw"

            bd_cases = [item for item in cases if item["country_code"] == "BD"]
            bd_payload = {
                "metadata": {
                    "country_code": "BD",
                    "num_found": len(bd_cases),
                    "num_returned": len(bd_cases),
                    "source_url": "frozen real-source regression slice",
                },
                "concepts": [
                    {
                        "id": case["source_product_key"],
                        "display_name": case["product_name"],
                        "retired": False,
                        "extras": {
                            "dar_number": case["source_product_key"].split("--", 1)[0],
                            "trade_name": case["product_name"],
                            "generic_content_raw": case["raw_ingredient_text"],
                            "dosage_form": "source slice",
                            "company": "source slice",
                            "dar_quality_flag": "",
                        },
                    }
                    for case in bd_cases
                ],
            }
            bd_path = raw / "bd" / "dgda_concepts.json"
            bd_path.write_text(json.dumps(bd_payload), encoding="utf-8")

            bt_cases = [item for item in cases if item["country_code"] == "BT"]
            bt_path = raw / "bt" / "registered_products.csv"
            write_csv(
                bt_path,
                [
                    {
                        "Sr. No": str(index),
                        "Reg_No": case["source_product_key"],
                        "Generic_Name": case["raw_ingredient_text"],
                        "BrandName": case["product_name"],
                        "Therapeutic Category": "source slice",
                        "MAH": "source slice",
                        "Packsize": "source slice",
                        "Product_validity": "2028-12-31",
                        "Manufacture": "source slice",
                    }
                    for index, case in enumerate(bt_cases, start=1)
                ],
            )

            batches = {
                "BD": BangladeshAdapter().stage(raw / "bd", EXTRACTION_DATE),
                "BT": BhutanAdapter().stage(raw / "bt", EXTRACTION_DATE),
            }

        for case in cases:
            batch = batches[case["country_code"]]
            product = batch.products.set_index("source_product_key").loc[
                case["source_product_key"]
            ]
            actual = batch.ingredients.loc[
                batch.ingredients["source_product_key"].eq(case["source_product_key"]),
                "normalized_ingredient_key",
            ].tolist()
            self.assertEqual(actual, case["expected_components"])
            self.assertEqual(
                int(product["ingredient_component_count"]),
                len(case["expected_components"]),
            )
            self.assertEqual(int(product["unresolved_component_count"]), 0)

    def test_bhutan_swap_detector_does_not_prefer_strength_bearing_brand_text(self):
        self.assertEqual(
            _select_bhutan_generic_text(
                "Vitamin D3 Oral Solution",
                "Arachitol® Kids 400 IU/0.5 ml",
            ),
            "Vitamin D3 Oral Solution",
        )
        self.assertEqual(
            _select_bhutan_generic_text(
                "Calcium Dobesilate",
                "Cadosil LD ointment 30 g",
            ),
            "Calcium Dobesilate",
        )

    def test_fda_adapter_ingests_anda_but_marks_only_nda_bla_legacy_eligible(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)

            batch = FdaAdapter().stage(root / "data" / "raw" / "fda", EXTRACTION_DATE)

        self.assertEqual(set(batch.products["application_type"]), {"NDA", "ANDA", "BLA"})
        legacy = batch.products.loc[batch.products["legacy_eligible"], "application_type"]
        self.assertEqual(set(legacy), {"NDA", "BLA"})
        self.assertEqual(set(batch.ingredients["normalized_ingredient_key"]), {
            "amoxicillin",
            "paracetamol",
            "adalimumab",
        })
        self.assertIn("NDA, BLA, and ANDA", batch.policy.coverage_scope)
        status_by_type = batch.products.set_index("application_type")["marketing_status"]
        self.assertEqual(status_by_type.loc["ANDA"], "Discontinued")

    def test_fda_adapter_filters_short_split_artifacts_and_audits_orphan_products(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw" / "fda"
            applications = pd.read_csv(raw / "Applications.txt", sep="\t", dtype=str)
            applications.loc[len(applications)] = {
                "ApplNo": "4",
                "ApplType": "NDA",
                "SponsorName": "Fixture",
            }
            applications.to_csv(raw / "Applications.txt", sep="\t", index=False)
            products = pd.read_csv(raw / "Products.txt", sep="\t", dtype=str)
            products.loc[len(products)] = {
                "ApplNo": "4",
                "ProductNo": "001",
                "Form": "INJECTION",
                "Strength": "75 IU",
                "ReferenceDrug": "1",
                "DrugName": "MENOTROPINS",
                "ActiveIngredient": "MENOTROPINS (FSH;LH)",
                "ReferenceStandard": "1",
            }
            products.loc[len(products)] = {
                "ApplNo": "999",
                "ProductNo": "001",
                "Form": "TABLET",
                "Strength": "1MG",
                "ReferenceDrug": "0",
                "DrugName": "ORPHAN FIXTURE",
                "ActiveIngredient": "ORPHAN FIXTURE",
                "ReferenceStandard": "0",
            }
            products.to_csv(raw / "Products.txt", sep="\t", index=False)

            batch = FdaAdapter().stage(raw, EXTRACTION_DATE)

        keys = set(batch.ingredients["normalized_ingredient_key"])
        self.assertIn("menopausal gonadotrophin", keys)
        self.assertNotIn("lh", keys)
        self.assertEqual(batch.metrics["declared_row_count"], 5)
        self.assertEqual(batch.metrics["parsed_row_count"], 4)
        self.assertIn("missing_application_record", set(batch.issues["issue_code"]))
        menotropins = batch.products.set_index("source_product_key").loc["4-001"]
        self.assertEqual(int(menotropins["ingredient_component_count"]), 1)
        self.assertEqual(int(menotropins["unresolved_component_count"]), 0)
        self.assertIn(
            "discarded_short_split_artifact", set(batch.issues["issue_code"])
        )

    def test_hsa_adapter_keeps_products_with_missing_or_invalid_atc(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)

            batch = HsaAdapter().stage(root / "data" / "raw" / "hsa", EXTRACTION_DATE)

        self.assertEqual(len(batch.products), 2)
        self.assertEqual(
            set(batch.ingredients["normalized_ingredient_key"]),
            {"amoxicillin", "metformin"},
        )
        products = batch.products.set_index("source_product_key")
        self.assertEqual(products.loc["SG2", "raw_product_atc_codes"], "Pending")
        self.assertEqual(products.loc["SG2", "product_atc_codes"], "")
        self.assertIn("invalid_product_atc", set(batch.issues["issue_code"]))

    def test_hsa_product_atc_is_not_assigned_to_each_combo_ingredient(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw" / "hsa"
            source = pd.read_csv(
                raw / "hsa_registered_therapeutic_products.csv", dtype=str
            ).fillna("")
            source.loc[0, "active_ingredients"] = (
                "AMOXICILLIN TRIHYDRATE && CLAVULANATE POTASSIUM"
            )
            source.loc[0, "atc_code"] = "J01CR02"
            source.to_csv(raw / "hsa_registered_therapeutic_products.csv", index=False)
            batch = HsaAdapter().stage(raw, EXTRACTION_DATE)

        product = batch.products.set_index("source_product_key").loc["SG1"]
        self.assertEqual(product["product_atc_codes"], "J01CR02")
        ingredient_codes = batch.ingredients.loc[
            batch.ingredients["source_product_key"].eq("SG1"),
            "ingredient_atc_codes",
        ]
        self.assertEqual(set(ingredient_codes), {""})

    def test_hsa_adapter_preserves_valid_components_of_multi_code_atc(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw" / "hsa"
            source = pd.read_csv(
                raw / "hsa_registered_therapeutic_products.csv", dtype=str
            ).fillna("")
            source.loc[0, "atc_code"] = "C01BB01&&N01BB02"
            source.loc[1, "atc_code"] = "N04BB01&&Pending"
            source.to_csv(raw / "hsa_registered_therapeutic_products.csv", index=False)
            batch = HsaAdapter().stage(raw, EXTRACTION_DATE)

        products = batch.products.set_index("source_product_key")
        self.assertEqual(products.loc["SG1", "product_atc_codes"], "C01BB01|N01BB02")
        self.assertEqual(products.loc["SG2", "product_atc_codes"], "N04BB01")
        self.assertEqual(products.loc["SG2", "raw_product_atc_codes"], "N04BB01&&Pending")
        self.assertIn("invalid_product_atc", set(batch.issues["issue_code"]))

    def test_hsa_adapter_fails_closed_on_truncation_and_filters_short_identity(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw" / "hsa"
            write_csv(
                raw / "hsa_registered_therapeutic_products.csv",
                [
                    {
                        "licence_no": "SG-SHORT",
                        "product_name": "SODIUM S-LACTATE",
                        "approval_d": "",
                        "atc_code": "",
                        "active_ingredients": "SODIUM S-LACTATE",
                        "license_holder": "HSA",
                        "strength": "",
                    }
                ],
            )
            (raw / "fetch_metadata.json").write_text(
                json.dumps({"row_count": 2}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "incomplete"):
                HsaAdapter().stage(raw, EXTRACTION_DATE)
            (raw / "fetch_metadata.json").write_text(
                json.dumps({"row_count": 1}), encoding="utf-8"
            )
            batch = HsaAdapter().stage(raw, EXTRACTION_DATE)

        self.assertTrue(batch.ingredients.empty)
        self.assertFalse(bool(batch.products.iloc[0]["included_in_presence"]))

    def test_hsa_preserves_unresolved_declared_component_in_product_mode(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw" / "hsa"
            write_csv(
                raw / "hsa_registered_therapeutic_products.csv",
                [
                    {
                        "licence_no": "SG-COMBO",
                        "product_name": "DECLARED COMBINATION",
                        "approval_d": "",
                        "atc_code": "",
                        "active_ingredients": (
                            "AMOXICILLIN TRIHYDRATE && SODIUM S-LACTATE"
                        ),
                        "license_holder": "HSA",
                        "strength": "500 mg && 50%",
                    }
                ],
            )
            (raw / "fetch_metadata.json").write_text(
                json.dumps({"row_count": 1}), encoding="utf-8"
            )
            batch = HsaAdapter().stage(raw, EXTRACTION_DATE)

        self.assertEqual(
            list(batch.ingredients["normalized_ingredient_key"]), ["amoxicillin"]
        )
        product = batch.products.iloc[0]
        self.assertEqual(int(product["ingredient_component_count"]), 2)
        self.assertEqual(int(product["unresolved_component_count"]), 1)

    def test_manifest_acceptance_rejects_implausibly_small_self_consistent_source(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            batch = HsaAdapter().stage(root / "data" / "raw" / "hsa", EXTRACTION_DATE)
            with self.assertRaisesRegex(ValueError, "acceptance floor"):
                _validate_manifest_counts(
                    {"artifacts": {"SG": {"row_count": len(batch.products)}}},
                    {"SG": batch},
                )

    def test_bangladesh_adapter_splits_combinations_and_never_claims_legal_absence(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)

            batch = BangladeshAdapter().stage(root / "data" / "raw" / "bd", EXTRACTION_DATE)

        combo = batch.ingredients[batch.ingredients["source_product_key"].eq("bd-amox-clav")]
        self.assertEqual(
            list(combo["normalized_ingredient_key"]),
            ["amoxicillin", "clavulanic acid"],
        )
        self.assertEqual(batch.metrics["declared_row_count"], 2)
        self.assertEqual(batch.metrics["parsed_row_count"], 2)
        self.assertIn("allopathic", batch.policy.coverage_scope.lower())
        self.assertFalse(bool(batch.products["source_retired"].any()))
        self.assertIn("not observed", batch.policy.observed_absence_wording.lower())
        self.assertNotIn("not registered", batch.policy.observed_absence_wording.lower())

    def test_bangladesh_veterinary_marker_excludes_products_from_human_presence(self):
        concepts = [
            {
                "id": "vet-name",
                "display_name": "Longtin Vet Inj",
                "retired": False,
                "extras": {
                    "trade_name": "Longtin Vet Inj",
                    "generic_content_raw": "Moxidectin 1 gm/100 ml",
                    "dosage_form": "Injection",
                },
            },
            {
                "id": "vet-metadata",
                "display_name": "Prazitel",
                "retired": False,
                "extras": {
                    "trade_name": "Prazitel",
                    "generic_content_raw": "Praziquantel + Pyrantel 18.2 mg + 72.6 mg",
                    "dosage_form": "Veterinary bolus",
                },
            },
            {
                "id": "human-substring",
                "display_name": "Carvetab",
                "retired": False,
                "extras": {
                    "trade_name": "Carvetab",
                    "generic_content_raw": "Paracetamol 500 mg",
                    "dosage_form": "Tablet",
                },
            },
            {
                "id": "vet-brand",
                "display_name": "Vetodex",
                "retired": False,
                "extras": {
                    "trade_name": "Vetodex",
                    "generic_content_raw": "Dexamethasone Sodium Phosphate 2 mg/ml",
                    "dosage_form": "Injection",
                },
            },
            {
                "id": "vet-bolus",
                "display_name": "Dirozyl",
                "retired": False,
                "extras": {
                    "trade_name": "Dirozyl",
                    "generic_content_raw": "Metronidazole 2 gm",
                    "dosage_form": "Bolus",
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            raw = Path(tmp) / "bd"
            raw.mkdir()
            (raw / "dgda_concepts.json").write_text(
                json.dumps(
                    {
                        "metadata": {"num_found": len(concepts)},
                        "concepts": concepts,
                    }
                ),
                encoding="utf-8",
            )
            batch = BangladeshAdapter().stage(raw, EXTRACTION_DATE)

        products = batch.products.set_index("source_product_key")
        for product_key in ("vet-name", "vet-metadata", "vet-brand", "vet-bolus"):
            with self.subTest(product_key=product_key):
                self.assertFalse(bool(products.loc[product_key, "included_in_presence"]))
                self.assertEqual(
                    products.loc[product_key, "exclusion_reason"],
                    "outside_human_scope_veterinary",
                )
        self.assertTrue(bool(products.loc["human-substring", "included_in_presence"]))
        excluded_issues = batch.issues.loc[
            batch.issues["issue_code"].eq("outside_human_scope_veterinary")
        ]
        self.assertEqual(
            set(excluded_issues["source_product_key"]),
            {"vet-name", "vet-metadata", "vet-brand", "vet-bolus"},
        )

    def test_bangladesh_077_veterinary_sector_excludes_unmarked_products(self):
        concepts = [
            {
                "id": "328-0022-077--chlortrimed",
                "display_name": "Chlortrimed",
                "retired": False,
                "extras": {
                    "dar_number": "328-0022-077",
                    "trade_name": "Chlortrimed",
                    "generic_content_raw": "Chlortetracycline 20 gm/100 gm",
                    "dosage_form": "Powder",
                    "company": "Sector fixture",
                    "dar_quality_flag": "",
                },
            },
            {
                "id": "327-0025-077--niclemet",
                "display_name": "Niclemet",
                "retired": False,
                "extras": {
                    "dar_number": "327-0025-077",
                    "trade_name": "Niclemet",
                    "generic_content_raw": (
                        "Levamisole + Metoclopramide + Niclosamide "
                        "50 mg + 2.5 mg + 450 mg"
                    ),
                    "dosage_form": "Tablet",
                    "company": "Sector fixture",
                    "dar_quality_flag": "",
                },
            },
            {
                "id": "077-0022-006--paracetamol",
                "display_name": "Paracetamol",
                "retired": False,
                "extras": {
                    "dar_number": "077-0022-006",
                    "trade_name": "Paracetamol",
                    "generic_content_raw": "Paracetamol 500 mg",
                    "dosage_form": "Tablet",
                    "company": "Decent Pharma",
                    "dar_quality_flag": "",
                },
            },
            {
                "id": "100-0002-001--paracetamol",
                "display_name": "Paracetamol",
                "retired": False,
                "extras": {
                    "dar_number": "100-0002-001",
                    "trade_name": "Paracetamol",
                    "generic_content_raw": "Paracetamol 500 mg",
                    "dosage_form": "Tablet",
                    "company": "Human fixture",
                    "dar_quality_flag": "",
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            raw = Path(tmp) / "bd"
            raw.mkdir()
            (raw / "dgda_concepts.json").write_text(
                json.dumps(
                    {
                        "metadata": {"num_found": len(concepts)},
                        "concepts": concepts,
                    }
                ),
                encoding="utf-8",
            )
            batch = BangladeshAdapter().stage(raw, EXTRACTION_DATE)

        products = batch.products.set_index("source_product_key")
        for product_key in (
            "328-0022-077--chlortrimed",
            "327-0025-077--niclemet",
        ):
            with self.subTest(product_key=product_key):
                self.assertFalse(bool(products.loc[product_key, "included_in_presence"]))
                self.assertEqual(
                    products.loc[product_key, "exclusion_reason"],
                    "outside_human_scope_veterinary",
                )
        for product_key in (
            "077-0022-006--paracetamol",
            "100-0002-001--paracetamol",
        ):
            with self.subTest(product_key=product_key):
                self.assertTrue(
                    bool(products.loc[product_key, "included_in_presence"])
                )

    def test_bangladesh_missing_generic_is_unresolved_not_brand_identity(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw" / "bd" / "dgda_concepts.json"
            payload = json.loads(raw.read_text(encoding="utf-8"))
            payload["metadata"]["num_found"] = 1
            payload["concepts"] = [
                {
                    "id": "missing-generic",
                    "display_name": "BRAND ONLY",
                    "retired": False,
                    "extras": {"trade_name": "BRAND ONLY"},
                }
            ]
            raw.write_text(json.dumps(payload), encoding="utf-8")
            batch = BangladeshAdapter().stage(raw.parent, EXTRACTION_DATE)

        self.assertTrue(batch.ingredients.empty)
        self.assertEqual(batch.products.iloc[0]["raw_ingredient_text"], "")
        self.assertFalse(bool(batch.products.iloc[0]["included_in_presence"]))
        self.assertIn("unresolved_ingredient", set(batch.issues["issue_code"]))

    def test_bhutan_adapter_separates_listed_presence_from_current_qualification(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)

            batch = BhutanAdapter().stage(root / "data" / "raw" / "bt", EXTRACTION_DATE)

        products = batch.products.set_index("source_product_key")
        self.assertTrue(bool(products.loc["BHU-DRA/23/RN/H001", "included_in_presence"]))
        self.assertTrue(bool(products.loc["BHU-DRA/23/RN/H001", "current_qualified"]))
        self.assertTrue(bool(products.loc["BHU-DRA/23/RN/H002", "included_in_presence"]))
        self.assertFalse(bool(products.loc["BHU-DRA/23/RN/H002", "current_qualified"]))
        self.assertEqual(
            products.loc["BHU-DRA/23/RN/H002", "exclusion_reason"],
            "expired_before_extraction_date",
        )
        self.assertTrue(bool(products.loc["BHU-DRA/23/RN/H003", "included_in_presence"]))
        self.assertFalse(bool(products.loc["BHU-DRA/23/RN/H003", "current_qualified"]))
        self.assertEqual(
            products.loc["BHU-DRA/23/RN/H003", "exclusion_reason"],
            "cancelled",
        )
        included_ingredients = batch.ingredients[
            batch.ingredients["source_product_key"].eq("BHU-DRA/23/RN/H001")
        ]
        self.assertEqual(list(included_ingredients["normalized_ingredient_key"]), ["metformin"])
        collision = products.loc["BHU-MPD/24/RN/H165"]
        self.assertTrue(bool(collision["included_in_presence"]))
        self.assertTrue(pd.isna(collision["current_qualified"]))
        self.assertIn(
            "status_registration_collision",
            set(batch.issues["issue_code"]),
        )
        swapped = batch.ingredients[
            batch.ingredients["source_product_key"].eq("BHU-DRA/23/RN/H159")
        ]
        self.assertEqual(
            list(swapped["normalized_ingredient_key"]),
            ["carboxymethylcellulose"],
        )
        self.assertEqual(products.loc["BHU-DRA/23/RN/H159", "product_name"], "OPTIVIEW Lubricant Eye Drop")
        self.assertIn("generic_brand_fields_swapped", set(batch.issues["issue_code"]))

    def test_bhutan_adapter_applies_reviewed_row_level_cleanups(self):
        additions = [
            (
                "BHU-MPD/25/RN/H235",
                "Diclofenac Sodium with Paracetamol Tablets",
                "Vivian PLUS",
                {"diclofenac", "paracetamol"},
            ),
            (
                "BHU-DRA/23/H155",
                "Linagliptin INN 2.5 mg and Metformin HCl BP 850 mg",
                "Adlinameg 2.5/850",
                {"linagliptin", "metformin"},
            ),
            (
                "BHU-MPD/24/CR/H027",
                "Iron with Folic Acid",
                "Feofol",
                {"iron", "folic acid"},
            ),
            (
                "BHU-MPD/25/ER/H312",
                "Intravenous Fat Emulsion with Medium and Long Chain "
                "Triglycerides (20% w/v)",
                "CELEPIDMCT-LCT 20%",
                {"medium and long chain triglycerides"},
            ),
            (
                "BHU-DRA/23/ER/H201",
                "Thiamine Mononitrate. Pyridoxine Hydrochloride,Niacinamide, "
                "Cyanocobalamin and Calcium Pantothenate Tablets",
                "Neuromax-Forte",
                {
                    "thiamine",
                    "pyridoxine",
                    "nicotinamide",
                    "cyanocobalamin",
                    "pantothenic acid",
                },
            ),
            (
                "BHU-MPD/25/H199",
                "Levonorgestrel and Ethinylestradiol Tablets BP with Ferrous "
                "Fumarate Tablets BP",
                "Evron-28 Plus",
                {"levonorgestrel", "ethinylestradiol", "ferrous fumarate"},
            ),
            (
                "BHU-MPD-25/H307",
                "Levonorgestrel and Ethinylestradiol Tablets BP with Ferrous "
                "Fumarate Tablets BP",
                "Contrament",
                {"levonorgestrel", "ethinylestradiol", "ferrous fumarate"},
            ),
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            path = root / "data" / "raw" / "bt" / "registered_products.csv"
            products = pd.read_csv(path, dtype=str).fillna("")
            for ordinal, (registration, generic, brand, _) in enumerate(
                additions, start=6
            ):
                products.loc[len(products)] = {
                    "Sr. No": str(ordinal),
                    "Reg_No": registration,
                    "Generic_Name": generic,
                    "BrandName": brand,
                    "Therapeutic Category": "Regression fixture",
                    "MAH": "BT Holder",
                    "Packsize": "fixture",
                    "Product_validity": "2028-12-31",
                    "Manufacture": "BT Maker",
                }
            products.to_csv(path, index=False)
            batch = BhutanAdapter().stage(path.parent, EXTRACTION_DATE)

        staged_products = batch.products.set_index("source_product_key")
        for registration, _, _, expected in additions:
            with self.subTest(registration=registration):
                actual = set(
                    batch.ingredients.loc[
                        batch.ingredients["source_product_key"].eq(registration),
                        "normalized_ingredient_key",
                    ]
                )
                self.assertEqual(actual, expected)
                self.assertEqual(
                    int(staged_products.loc[registration, "ingredient_component_count"]),
                    len(expected),
                )

        for registration in ("BHU-MPD/25/H199", "BHU-MPD-25/H307"):
            product = staged_products.loc[registration]
            ingredient_keys = frozenset(
                batch.ingredients.loc[
                    batch.ingredients["source_product_key"].eq(registration),
                    "normalized_ingredient_key",
                ]
            )
            adjudication = adjudicate_eml_concept_product(
                "ferrous salt",
                country_code="BT",
                product_name=product["product_name"],
                raw_ingredient_text=product["raw_ingredient_text"],
                form=product["form"],
                strength=product["strength"],
                product_atc_codes=product["product_atc_codes"],
                ingredient_keys=ingredient_keys,
            )
            self.assertIsNotNone(adjudication)
            self.assertEqual(adjudication.state, "INDETERMINATE")
            self.assertEqual(
                adjudication.rule,
                "bt_contraceptive_ferrous_salt_strength_unverified",
            )

    def test_bhutan_current_qualification_requires_actions_snapshot(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw" / "bt"
            (raw / "regulatory_actions.csv").unlink()
            with self.assertRaises(FileNotFoundError):
                BhutanAdapter().stage(raw, EXTRACTION_DATE)

    def test_bhutan_veterinary_rows_are_excluded_with_an_audit_reason(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw" / "bt"
            products_path = raw / "registered_products.csv"
            products = pd.read_csv(products_path, dtype=str).fillna("")
            products.loc[len(products)] = {
                "Sr. No": "6",
                "Reg_No": "BHU-DRA/23/RN/V001",
                "Generic_Name": "Ivermectin 10 mg",
                "BrandName": "VETMECTIN",
                "Therapeutic Category": "Veterinary antiparasitic",
                "MAH": "BT Vet Holder",
                "Packsize": "10 x 10",
                "Product_validity": "2027-01-01",
                "Manufacture": "BT Vet Maker",
            }
            products.to_csv(products_path, index=False)

            batch = BhutanAdapter().stage(raw, EXTRACTION_DATE)

        veterinary = batch.products.set_index("source_product_key").loc[
            "BHU-DRA/23/RN/V001"
        ]
        self.assertFalse(bool(veterinary["included_in_presence"]))
        self.assertEqual(veterinary["exclusion_reason"], "outside_human_scope")
        self.assertIn("outside_human_scope", set(batch.issues["issue_code"]))


class AtlasBuildTests(unittest.TestCase):
    def test_build_emits_cited_verification_report_and_quality_buckets(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))

            verification_report = artifact.verification_report_path.read_text(
                encoding="utf-8"
            )
            evidence = pd.read_csv(
                artifact.verification_evidence_path, dtype=str
            ).fillna("")
            quality_report = artifact.report_path.read_text(encoding="utf-8")

        self.assertIn("# Atlas verification report", verification_report)
        self.assertIn("Four-country source-snapshot overlap", verification_report)
        self.assertIn("Bangladesh–Bhutan source-snapshot overlap", verification_report)
        self.assertIn("Standalone in both", verification_report)
        self.assertIn("needs_external_source", verification_report)
        self.assertIn("OUT_OF_SCOPE", verification_report)
        self.assertEqual(
            set(
                [
                    "finding_group",
                    "decision_state",
                    "country_code",
                    "target_identity",
                    "source_product_key",
                    "product_name",
                    "raw_ingredient_text",
                    "evidence_citation",
                    "rule",
                    "needs_external_source",
                ]
            ).difference(evidence.columns),
            set(),
        )
        self.assertTrue(
            evidence["evidence_citation"].str.match(r"tables/.+\.csv:\d+").any()
        )
        self.assertIn("Source-snapshot observation", quality_report)
        self.assertIn("Current marketing", quality_report)
        self.assertIn("OUT_OF_SCOPE", quality_report)

    def test_build_materializes_product_backed_eml_concept_adjudications(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)

            bd_path = root / "data" / "raw" / "bd" / "dgda_concepts.json"
            payload = json.loads(bd_path.read_text(encoding="utf-8"))
            for product_key, product_name, raw, form in [
                (
                    "bd-ors",
                    "ORS (Oral Rehydration Salts)",
                    "Anhydrous Glucose + Potassium Chloride + Sodium Chloride + "
                    "Trisodium Citrate  6.75 gm + 750 mg + 1.3 gm + 1.45 gm/500 ml",
                    "Oral Saline",
                ),
                ("bd-pancreatin", "Pancreon 10000", "Pancreatin 150 mg", "Capsule"),
                ("bd-tretinoin", "Trinon", "Tretinoin 25 mg/100 gm", "Cream"),
            ]:
                payload["concepts"].append(
                    {
                        "id": product_key,
                        "display_name": product_name,
                        "retired": False,
                        "extras": {
                            "trade_name": product_name,
                            "generic_content_raw": raw,
                            "dosage_form": form,
                        },
                    }
                )
            payload["metadata"]["num_found"] = len(payload["concepts"])
            payload["metadata"]["num_returned"] = len(payload["concepts"])
            bd_path.write_text(json.dumps(payload), encoding="utf-8")

            hsa_path = (
                root
                / "data"
                / "raw"
                / "hsa"
                / "hsa_registered_therapeutic_products.csv"
            )
            hsa = pd.read_csv(hsa_path, dtype=str).fillna("")
            hsa.loc[len(hsa)] = {
                "licence_no": "SG-VESANOID",
                "product_name": "VESANOID CAPSULE 10 mg",
                "approval_d": "2004-01-01",
                "atc_code": "L01XX14",
                "active_ingredients": "TRETINOIN",
                "license_holder": "SG Holder",
            }
            hsa.to_csv(hsa_path, index=False)

            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            for medicine_name in (
                "oral rehydration salts",
                "pancreatic enzymes",
                "all-trans retinoic acid",
            ):
                eeml.loc[len(eeml)] = {
                    "Medicine name": medicine_name,
                    "EML section": "Concept fixture",
                    "Formulations": "fixture",
                    "Indication": "",
                    "ATC codes": "",
                    "Combined with": "",
                    "Status": "Added",
                }
            eeml.to_csv(eeml_path, index=False)

            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))
            adjudications = pd.read_csv(
                artifact.table_paths["eml_product_adjudications"], dtype=str
            ).fillna("")

        by_target_country = adjudications.set_index(["target_key", "country_code"])
        self.assertEqual(
            by_target_country.loc[("oral rehydration salts", "BD"), "decision_state"],
            "VERIFIED_PRESENT",
        )
        self.assertEqual(
            by_target_country.loc[("pancreatic enzymes", "BD"), "decision_state"],
            "INDETERMINATE",
        )
        self.assertEqual(
            by_target_country.loc[("pancreatic enzymes", "BD"), "needs_external_source"],
            "ACTIVITY_UNIT_CONFIRMATION",
        )
        self.assertEqual(
            by_target_country.loc[("all trans retinoic acid", "SG"), "decision_state"],
            "VERIFIED_PRESENT",
        )
        self.assertNotIn(("all trans retinoic acid", "BD"), by_target_country.index)

    def test_eml_section_heading_pointer_is_excluded_from_medicine_universe(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            eeml.loc[len(eeml)] = {
                "Medicine name": "Medicines for COVID-19",
                "EML section": "Medicines for COVID-19",
                "Formulations": "Refer to WHO living guidelines",
                "Indication": "COVID-19",
                "ATC codes": "",
                "Combined with": "",
                "Status": "Added",
            }
            eeml.to_csv(eeml_path, index=False)

            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))
            entries = pd.read_csv(
                artifact.table_paths["essential_medicine_entries"], dtype=str
            ).fillna("")
            substances = pd.read_csv(
                artifact.table_paths["substances"], dtype=str
            ).fillna("")

        self.assertNotIn("Medicines for COVID-19", set(entries["medicine_name"]))
        self.assertNotIn("medicines covid", set(substances["preferred_name"]))

    def test_build_preserves_raw_eml_atc_and_emits_canonical_codes_with_issues(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            eeml.loc[eeml["Medicine name"].eq("metformin"), "ATC codes"] = "A01BA02"
            eeml.loc[len(eeml)] = {
                "Medicine name": "insulin (human, short-acting)",
                "EML section": "Diabetes",
                "Formulations": "injection",
                "Indication": "",
                "ATC codes": "A10AC01",
                "Combined with": "",
                "Status": "Added",
            }
            eeml.loc[len(eeml)] = {
                "Medicine name": "invalid ATC fixture",
                "EML section": "Fixture",
                "Formulations": "tablet",
                "Indication": "",
                "ATC codes": "PENDING",
                "Combined with": "",
                "Status": "Added",
            }
            eeml.to_csv(eeml_path, index=False)

            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))
            entries = pd.read_csv(
                artifact.table_paths["essential_medicine_entries"], dtype=str
            ).fillna("")
            issues = pd.read_csv(
                artifact.table_paths["eml_atc_issues"], dtype=str
            ).fillna("")

        by_name = entries.set_index("medicine_name")
        self.assertEqual(by_name.loc["metformin", "raw_atc_codes"], "A01BA02")
        self.assertEqual(by_name.loc["metformin", "atc_codes"], "A10BA02")
        self.assertEqual(
            by_name.loc["insulin (human, short-acting)", "atc_codes"], "A10AB01"
        )
        self.assertEqual(by_name.loc["invalid ATC fixture", "raw_atc_codes"], "PENDING")
        self.assertEqual(by_name.loc["invalid ATC fixture", "atc_codes"], "")
        self.assertEqual(
            set(issues["issue_state"]), {"corrected", "invalid"}
        )

    def test_substance_atc_codes_come_from_who_index_not_source_assertions(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)

            atc_path = root / "data" / "raw" / "who" / "atc.csv"
            atc = pd.read_csv(atc_path, dtype=str).fillna("")
            atc = pd.concat(
                [
                    atc,
                    pd.DataFrame(
                        [
                            {"atc_code": "R05CB01", "atc_name": "acetylcysteine"},
                            {"atc_code": "S01XA08", "atc_name": "acetylcysteine"},
                            {"atc_code": "V03AB23", "atc_name": "acetylcysteine"},
                        ]
                    ),
                ],
                ignore_index=True,
            )
            atc.to_csv(atc_path, index=False)

            hsa_path = (
                root
                / "data"
                / "raw"
                / "hsa"
                / "hsa_registered_therapeutic_products.csv"
            )
            hsa = pd.read_csv(hsa_path, dtype=str).fillna("")
            hsa.loc[len(hsa)] = {
                "licence_no": "SG-ACETYLCYSTEINE",
                "product_name": "ACETYLCYSTEINE 200",
                "approval_d": "2005-01-01",
                "atc_code": "A01BA02",
                "active_ingredients": "ACETYLCYSTEINE",
                "license_holder": "SG Holder",
            }
            hsa.to_csv(hsa_path, index=False)

            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            eeml.loc[len(eeml)] = {
                "Medicine name": "acetylcysteine",
                "EML section": "Antidotes",
                "Formulations": "oral liquid",
                "Indication": "",
                "ATC codes": "A01BA02",
                "Combined with": "",
                "Status": "Added",
            }
            eeml.to_csv(eeml_path, index=False)

            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))
            substances = pd.read_csv(
                artifact.table_paths["substances"], dtype=str
            ).fillna("")
            products = pd.read_csv(
                artifact.table_paths["registered_products"], dtype=str
            ).fillna("")
            with sqlite3.connect(artifact.database_path) as connection:
                canonical_codes = pd.read_sql_query(
                    """
                    SELECT code.atc_code
                    FROM substance_atc_codes AS code
                    JOIN substances AS substance USING (substance_id)
                    WHERE substance.normalized_ingredient_key = 'acetylcysteine'
                    ORDER BY code.atc_code
                    """,
                    connection,
                )["atc_code"].tolist()

            comparison_view_names = [
                "eml_presence_long",
                "eml_comparison_wide",
                "eml_presence_long_bt_current_qualified",
                "eml_comparison_wide_bt_current_qualified",
                "bd_bt_eml_presence_long",
                "bd_bt_eml_comparison_wide",
                "bd_bt_eml_presence_long_bt_current_qualified",
                "bd_bt_eml_comparison_wide_bt_current_qualified",
            ]
            comparison_codes = {}
            for view_name in comparison_view_names:
                view = pd.read_csv(artifact.view_paths[view_name], dtype=str).fillna("")
                comparison_codes[view_name] = set(
                    view.loc[view["preferred_name"].eq("acetylcysteine"), "atc_codes"]
                )

        expected = "R05CB01|S01XA08|V03AB23"
        substance = substances.set_index("normalized_ingredient_key").loc[
            "acetylcysteine"
        ]
        self.assertEqual(substance["atc_codes"], expected)
        self.assertEqual(canonical_codes, expected.split("|"))
        self.assertEqual(
            products.set_index("source_product_key").loc[
                "SG-ACETYLCYSTEINE", "raw_product_atc_codes"
            ],
            "A01BA02",
        )
        self.assertNotIn("A01BA02", canonical_codes)
        self.assertNotIn("A10BA02", canonical_codes)
        self.assertTrue(
            all(codes == {expected} for codes in comparison_codes.values())
        )

    def test_substance_without_exact_who_atc_match_stays_empty(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            atc_path = root / "data" / "raw" / "who" / "atc.csv"
            atc = pd.read_csv(atc_path, dtype=str).fillna("")
            atc.loc[len(atc)] = {
                "atc_code": "A10BD07",
                "atc_name": "metformin and sitagliptin",
            }
            atc.to_csv(atc_path, index=False)

            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))
            substances = pd.read_csv(
                artifact.table_paths["substances"], dtype=str
            ).fillna("")
            with sqlite3.connect(artifact.database_path) as connection:
                relation_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM substance_atc_codes AS code
                    JOIN substances AS substance USING (substance_id)
                    WHERE substance.normalized_ingredient_key = 'metformin'
                    """
                ).fetchone()[0]

        metformin = substances.set_index("normalized_ingredient_key").loc["metformin"]
        self.assertEqual(metformin["atc_codes"], "")
        self.assertEqual(relation_count, 0)

    def test_build_applies_atlas_identity_refinement_to_source_and_eml_rows(self):
        source_rows = [
            ("bd-hyoscine-butyl", "Hyoscine Butyl Bromide 10 mg"),
            ("bd-hyoscine-hydro", "Hyoscine Hydrobromide 400 mcg"),
            ("bd-edta", "Disodium EDTA 17%"),
            ("bd-mesalazine", "5-Aminosalicylic Acid 400 mg"),
            ("bd-benzoate", "Benzyl Benzoate 25%"),
            ("bd-permanganate", "Potassium Permanganate 1:10000"),
            ("bd-sdf", "Silver Diamine Fluoride 38%"),
        ]
        eeml_names = [
            "hyoscine butylbromide",
            "hyoscine hydrobromide",
            "sodium calcium edetate",
            "p-aminosalicylate sodium",
            "benzyl benzoate",
            "potassium permanganate",
            "silver diamine fluoride",
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            bd_path = root / "data" / "raw" / "bd" / "dgda_concepts.json"
            payload = json.loads(bd_path.read_text(encoding="utf-8"))
            for product_key, raw_ingredient in source_rows:
                payload["concepts"].append(
                    {
                        "id": product_key,
                        "display_name": product_key,
                        "retired": False,
                        "extras": {
                            "trade_name": product_key,
                            "generic_content_raw": raw_ingredient,
                            "dosage_form": "fixture",
                        },
                    }
                )
            payload["metadata"]["num_found"] = len(payload["concepts"])
            payload["metadata"]["num_returned"] = len(payload["concepts"])
            bd_path.write_text(json.dumps(payload), encoding="utf-8")

            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            for medicine_name in eeml_names:
                eeml.loc[len(eeml)] = {
                    "Medicine name": medicine_name,
                    "EML section": "Identity fixture",
                    "Formulations": "fixture",
                    "Indication": "",
                    "ATC codes": "",
                    "Combined with": "",
                    "Status": "Added",
                }
            eeml.to_csv(eeml_path, index=False)

            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))
            substances = pd.read_csv(artifact.table_paths["substances"], dtype=str)
            product_ingredients = pd.read_csv(
                artifact.table_paths["product_ingredients"], dtype=str
            )
            products = pd.read_csv(artifact.table_paths["registered_products"], dtype=str)

        keys = set(substances["normalized_ingredient_key"])
        expected = {
            "hyoscine butylbromide",
            "hyoscine hydrobromide",
            "sodium calcium edetate",
            "disodium edetate",
            "para aminosalicylic acid",
            "mesalazine",
            "benzyl benzoate",
            "potassium permanganate",
            "silver diamine fluoride",
        }
        self.assertTrue(expected.issubset(keys))
        joined = product_ingredients.merge(products[["product_id", "source_product_key"]])
        substance_for = substances.set_index("substance_id")["normalized_ingredient_key"]
        source_keys = joined.set_index("source_product_key")["substance_id"].map(substance_for)
        self.assertEqual(source_keys.loc["bd-edta"], "disodium edetate")
        self.assertEqual(source_keys.loc["bd-mesalazine"], "mesalazine")

    def test_build_fails_loudly_when_legacy_inputs_are_missing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            (root / "data" / "raw" / "who" / "atc.csv").unlink()
            (root / "data" / "raw" / "Rare Drugs.xls").unlink()
            output_dir = root / "atlas"

            with self.assertRaisesRegex(
                FileNotFoundError, "legacy compatibility inputs are missing"
            ):
                build_atlas(
                    BuildSpec(
                        root=root,
                        extraction_date=EXTRACTION_DATE,
                        output_dir=output_dir,
                    )
                )

        self.assertFalse(output_dir.exists())

    def test_source_snapshots_expose_license_status_and_hsa_attribution(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            artifact = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            with sqlite3.connect(artifact.database_path) as connection:
                rows = {
                    row[0]: row[1:]
                    for row in connection.execute(
                        "SELECT country_code, license_name, license_url, "
                        "license_status, attribution FROM source_snapshots"
                    )
                }

        self.assertEqual(rows["SG"][0], "Singapore Open Data Licence version 1.0")
        self.assertEqual(rows["SG"][1], "https://data.gov.sg/open-data-licence")
        self.assertEqual(rows["SG"][2], "licensed_open")
        self.assertIn("Health Sciences Authority", rows["SG"][3])
        self.assertIn(EXTRACTION_DATE.isoformat(), rows["SG"][3])
        self.assertEqual(rows["BD"][2], "human_review_required")
        self.assertEqual(rows["BT"][2], "human_review_required")

    def test_build_fails_closed_without_consolidated_fetch_manifest(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            with self.assertRaisesRegex(FileNotFoundError, "fetch manifest is required"):
                build_atlas(
                    AtlasBuildSpec(
                        root=root,
                        extraction_date=EXTRACTION_DATE,
                        output_dir=root / "atlas",
                    )
                )
            self.assertFalse((root / "atlas").exists())

    def test_build_is_deterministic_and_enforces_relational_integrity(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            first = build_atlas(
                BuildSpec(
                    root=root,
                    extraction_date=EXTRACTION_DATE,
                    output_dir=root / "build-a",
                )
            )
            second = build_atlas(
                BuildSpec(
                    root=root,
                    extraction_date=EXTRACTION_DATE,
                    output_dir=root / "build-b",
                )
            )

            self.assertEqual(first.build_id, second.build_id)
            self.assertEqual(first.table_hashes, second.table_hashes)
            self.assertEqual(first.view_hashes, second.view_hashes)
            self.assertEqual(
                first.table_paths["substances"].read_bytes(),
                second.table_paths["substances"].read_bytes(),
            )
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "6")
            self.assertRegex(manifest["adjudication_sha256"], r"^[0-9a-f]{64}$")
            with sqlite3.connect(first.database_path) as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                application_types = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT application_type FROM registered_products "
                        "WHERE country_code = 'US'"
                    )
                }

        self.assertEqual(application_types, {"NDA", "ANDA", "BLA"})

    def test_report_retains_mandatory_eeml_limitations(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            artifact = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            report = artifact.report_path.read_text(encoding="utf-8")

        self.assertIn("may not contain every historical removed record", report)
        self.assertIn("no stable record ID or release timestamp", report)
        self.assertIn("Blank formulation and ATC fields are valid", report)

    def test_eeml_row_order_does_not_change_build_or_table_hashes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            first = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            original_manifest_inode = first.manifest_path.stat().st_ino
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            eeml.iloc[::-1].to_csv(eeml_path, index=False)
            second = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            second_manifest_inode = second.manifest_path.stat().st_ino

        self.assertEqual(first.build_id, second.build_id)
        self.assertEqual(first.table_hashes, second.table_hashes)
        self.assertEqual(first.view_hashes, second.view_hashes)
        self.assertEqual(second_manifest_inode, original_manifest_inode)

    def test_removed_eeml_rows_are_in_full_workbook_logical_hash(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            first = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "a")
            )
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            eeml.loc[eeml["Status"].eq("Removed"), "Indication"] = "changed removed row"
            eeml.to_csv(eeml_path, index=False)
            second = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "b")
            )

        self.assertNotEqual(first.build_id, second.build_id)

    def test_pdf_bytes_never_affect_atlas_output(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            first = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "a")
            )
            pdf = root / "data" / "raw" / "who" / "who_eml_2023.pdf"
            pdf.write_bytes(b"not a real PDF and deliberately not an input")
            second = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "b")
            )

        self.assertEqual(first.build_id, second.build_id)
        self.assertEqual(first.table_hashes, second.table_hashes)
        self.assertEqual(first.view_hashes, second.view_hashes)

    def test_build_rejects_noncanonical_universe_and_publishes_via_pointer(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            with self.assertRaisesRegex(ValueError, "only supports"):
                build_atlas(
                    BuildSpec(
                        root=root,
                        extraction_date=EXTRACTION_DATE,
                        output_dir=root / "bad",
                        universe_id="made-up",
                    )
                )
            artifact = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            self.assertTrue((root / "atlas").is_symlink())
            self.assertTrue(artifact.database_path.is_file())
            original_target = (root / "atlas").resolve()
            repeated = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            self.assertEqual(repeated.build_id, artifact.build_id)
            self.assertEqual((root / "atlas").resolve(), original_target)
            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            original_removed_indication = eeml.loc[
                eeml["Status"].eq("Removed"), "Indication"
            ].iloc[0]
            eeml.loc[eeml["Status"].eq("Removed"), "Indication"] = "new build"
            eeml.to_csv(eeml_path, index=False)
            changed = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            self.assertNotEqual(changed.build_id, artifact.build_id)
            self.assertNotEqual(changed.database_path, artifact.database_path)
            self.assertTrue(artifact.database_path.is_file())
            self.assertEqual(
                (root / "atlas").resolve(), changed.database_path.parent.resolve()
            )
            original_inode = artifact.database_path.stat().st_ino
            eeml.loc[
                eeml["Status"].eq("Removed"), "Indication"
            ] = original_removed_indication
            eeml.to_csv(eeml_path, index=False)
            revisited = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            self.assertEqual(revisited.build_id, artifact.build_id)
            self.assertEqual(revisited.database_path, artifact.database_path)
            self.assertEqual(artifact.database_path.stat().st_ino, original_inode)
            self.assertEqual(
                (root / "atlas").resolve(), artifact.database_path.parent.resolve()
            )

    def test_build_requires_electronic_eml_before_publishing_any_output(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            (root / "data" / "raw" / "who" / "eeml_2025.csv").unlink()
            output_dir = root / "atlas"

            with self.assertRaises(FileNotFoundError):
                build_atlas(
                    BuildSpec(
                        root=root,
                        extraction_date=EXTRACTION_DATE,
                        output_dir=output_dir,
                    )
                )
            self.assertFalse(output_dir.exists())


class ComparisonQueryTests(unittest.TestCase):
    def test_us_ferrous_salt_nontherapeutic_evidence_remains_unknown(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            for product in (
                {
                    "appl_no": "4",
                    "product_no": "001",
                    "drug_name": "FERROUS CITRATE FE-59",
                    "active_ingredient": "FERROUS CITRATE FE-59",
                    "form": "INJECTABLE;INJECTION",
                    "strength": "25 MICROCURIES/ML",
                    "marketing_status_id": "3",
                },
                {
                    "appl_no": "5",
                    "product_no": "001",
                    "drug_name": "FERROUS FUMARATE",
                    "active_ingredient": "FERROUS FUMARATE",
                    "form": "TABLET;ORAL",
                    "strength": "75MG",
                },
                {
                    "appl_no": "6",
                    "product_no": "001",
                    "drug_name": "NORMINEST FE",
                    "active_ingredient": (
                        "ETHINYL ESTRADIOL;NORETHINDRONE;FERROUS FUMARATE"
                    ),
                    "form": "TABLET;ORAL-28",
                    "strength": "0.035MG;0.5MG;75MG",
                },
            ):
                append_fda_fixture_product(root, **product)
            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            eeml.loc[len(eeml)] = {
                "Medicine name": "ferrous salt",
                "EML section": "Medicines affecting the blood",
                "Formulations": "Oral solid dosage form: equivalent to 60 mg iron",
                "Indication": "Iron deficiency anaemia",
                "ATC codes": "B03AA",
                "Combined with": "",
                "Status": "Added",
            }
            eeml.to_csv(eeml_path, index=False)

            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))
            result = compare_atlas(artifact.database_path, ("US",))

        row = result.long.set_index("preferred_name").loc["ferrous salt"]
        self.assertEqual(row["observation"], "UNKNOWN")
        self.assertEqual(row["current_marketing"], "UNKNOWN")
        self.assertEqual(
            row["needs_external_source"], "FDA_OTC_OR_SUPPLEMENT_SOURCE"
        )
        self.assertEqual(int(row["standalone_product_count"]), 0)
        self.assertEqual(int(row["combo_product_count"]), 0)

    def test_benzathine_synonyms_share_eml_identity_without_merging_benzylpenicillin(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            append_fda_fixture_product(
                root,
                appl_no="4",
                product_no="001",
                drug_name="BICILLIN L-A",
                active_ingredient="PENICILLIN G BENZATHINE",
                form="SUSPENSION;INTRAMUSCULAR",
                strength="2400000 UNITS/4ML",
            )

            bd_path = root / "data" / "raw" / "bd" / "dgda_concepts.json"
            payload = json.loads(bd_path.read_text(encoding="utf-8"))
            payload["concepts"].extend(
                [
                    {
                        "id": "bd-benzathine",
                        "display_name": "Benzapen",
                        "retired": False,
                        "extras": {
                            "dar_number": "100-0003-001",
                            "trade_name": "Benzapen",
                            "generic_content_raw": "Benzathine Penicillin 12 lac units",
                            "dosage_form": "Injection",
                        },
                    },
                    {
                        "id": "bd-benzylpenicillin",
                        "display_name": "Benzylpenicillin",
                        "retired": False,
                        "extras": {
                            "dar_number": "100-0004-001",
                            "trade_name": "Benzylpenicillin",
                            "generic_content_raw": "Benzylpenicillin 1 gm",
                            "dosage_form": "Injection",
                        },
                    },
                ]
            )
            payload["metadata"]["num_found"] = len(payload["concepts"])
            payload["metadata"]["num_returned"] = len(payload["concepts"])
            bd_path.write_text(json.dumps(payload), encoding="utf-8")

            bt_path = root / "data" / "raw" / "bt" / "registered_products.csv"
            bt = pd.read_csv(bt_path, dtype=str).fillna("")
            bt.loc[len(bt)] = {
                "Sr. No": "6",
                "Reg_No": "BHU-MPD/24/ER/H157",
                "Generic_Name": "Penicillin G Benzathine 2.4 million units",
                "BrandName": "Benzathine Penicillin",
                "Therapeutic Category": "Antibiotic",
                "MAH": "BT Holder",
                "Packsize": "1 vial",
                "Product_validity": "2028-12-31",
                "Manufacture": "BT Maker",
            }
            bt.to_csv(bt_path, index=False)

            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            for medicine_name in (
                "benzathine benzylpenicillin",
                "benzylpenicillin",
            ):
                eeml.loc[len(eeml)] = {
                    "Medicine name": medicine_name,
                    "EML section": "Anti-infective medicines",
                    "Formulations": "injection",
                    "Indication": "",
                    "ATC codes": "J01CE08" if "benzathine" in medicine_name else "J01CE01",
                    "Combined with": "",
                    "Status": "Added",
                }
            eeml.to_csv(eeml_path, index=False)

            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))
            result = compare_atlas(artifact.database_path, ("US", "SG", "BD", "BT"))

        rows = result.long.set_index(["preferred_name", "country_code"])
        benzathine = rows.loc["benzathine benzylpenicillin"]
        self.assertEqual(
            benzathine["observation"].to_dict(),
            {
                "BD": "STANDALONE",
                "BT": "STANDALONE",
                "SG": "OBSERVED_ABSENCE",
                "US": "STANDALONE",
            },
        )
        self.assertEqual(
            int(rows.loc[("benzathine benzylpenicillin", "BD"), "standalone_product_count"]),
            1,
        )
        self.assertFalse(
            reviewed_equivalent("benzathine benzylpenicillin", "benzylpenicillin")
        )
        self.assertNotEqual(
            rows.loc[("benzathine benzylpenicillin", "BD"), "substance_id"],
            rows.loc[("benzylpenicillin", "BD"), "substance_id"],
        )

    def test_us_calcium_contrast_agent_evidence_remains_unknown(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            append_fda_fixture_product(
                root,
                appl_no="4",
                product_no="001",
                drug_name="ISOPAQUE 280",
                active_ingredient="CALCIUM; MEGLUMINE; METRIZOIC ACID",
                form="INJECTABLE;INJECTION",
                strength="0.35MG/ML;140.1MG/ML;461.8MG/ML",
                marketing_status_id="3",
            )
            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            eeml.loc[len(eeml)] = {
                "Medicine name": "calcium",
                "EML section": "Vitamins and minerals",
                "Formulations": "Oral solid dosage form: 500 mg elemental calcium",
                "Indication": "",
                "ATC codes": "A12AA",
                "Combined with": "",
                "Status": "Added",
            }
            eeml.to_csv(eeml_path, index=False)

            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))
            result = compare_atlas(artifact.database_path, ("US",))

        row = result.long.set_index("preferred_name").loc["calcium"]
        self.assertEqual(row["observation"], "UNKNOWN")
        self.assertEqual(row["uncertainty_reason"], "concept_evidence_indeterminate")
        self.assertEqual(row["current_marketing"], "UNKNOWN")
        self.assertEqual(int(row["standalone_product_count"]), 0)
        self.assertEqual(int(row["combo_product_count"]), 0)

    def test_comparison_separates_snapshot_presence_scope_and_current_status(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)

            bd_path = root / "data" / "raw" / "bd" / "dgda_concepts.json"
            payload = json.loads(bd_path.read_text(encoding="utf-8"))
            payload["concepts"].extend(
                [
                    {
                    "id": "bd-moxidectin-vet",
                    "display_name": "Longtin Vet Inj",
                    "retired": False,
                    "extras": {
                        "trade_name": "Longtin Vet Inj",
                        "generic_content_raw": "Moxidectin  1 gm/100 ml",
                        "dosage_form": "Injection",
                    },
                    },
                    {
                        "id": "bd-human-benzylpenicillin",
                        "display_name": "Benzylpenicillin",
                        "retired": False,
                        "extras": {
                            "dar_number": "100-0003-001",
                            "trade_name": "Benzylpenicillin",
                            "generic_content_raw": "Benzylpenicillin 1 gm",
                            "dosage_form": "Injection",
                        },
                    },
                    {
                        "id": "002-0170-077--streptopen",
                        "display_name": "Streptopen",
                        "retired": False,
                        "extras": {
                            "dar_number": "002-0170-077",
                            "trade_name": "Streptopen",
                            "generic_content_raw": (
                                "Procaine Benzylpenicillin + Streptomycin"
                            ),
                            "dosage_form": "Injection",
                        },
                    },
                ]
            )
            payload["metadata"]["num_found"] = len(payload["concepts"])
            payload["metadata"]["num_returned"] = len(payload["concepts"])
            bd_path.write_text(json.dumps(payload), encoding="utf-8")

            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            for medicine_name in (
                "moxidectin",
                "procaine benzylpenicillin",
                "BCG vaccine",
                "whole blood",
            ):
                eeml.loc[len(eeml)] = {
                    "Medicine name": medicine_name,
                    "EML section": "Comparison fixture",
                    "Formulations": "fixture",
                    "Indication": "",
                    "ATC codes": "",
                    "Combined with": "",
                    "Status": "Added",
                }
            eeml.to_csv(eeml_path, index=False)

            artifact = build_atlas(BuildSpec(root=root, extraction_date=EXTRACTION_DATE))
            result = compare_atlas(artifact.database_path, ("US", "SG", "BD", "BT"))

        rows = result.long.set_index(["preferred_name", "country_code"])
        vet_only = rows.loc[("moxidectin", "BD")]
        self.assertEqual(vet_only["observation"], "OBSERVED_ABSENCE")
        self.assertFalse(bool(vet_only["observed_in_source_snapshot"]))
        self.assertEqual(vet_only["current_authorization"], "UNKNOWN")
        self.assertEqual(vet_only["current_marketing"], "UNKNOWN")
        self.assertIn("only a veterinary product was observed", vet_only["evidence_note"])
        self.assertIn("no human-scope product was found", vet_only["evidence_note"])
        self.assertIn("Longtin Vet Inj", vet_only["evidence_note"])

        vet_only_pen = rows.loc[("procaine benzylpenicillin", "BD")]
        self.assertEqual(vet_only_pen["observation"], "OBSERVED_ABSENCE")
        self.assertIn("only a veterinary product was observed", vet_only_pen["evidence_note"])
        self.assertIn("Streptopen", vet_only_pen["evidence_note"])

        us_vaccine = rows.loc[("bcg vaccine", "US")]
        self.assertEqual(us_vaccine["observation"], "UNKNOWN")
        self.assertEqual(
            us_vaccine["needs_external_source"], "FDA_CBER_OR_PURPLE_BOOK"
        )

        whole_blood = rows.loc[("whole blood", "US")]
        self.assertEqual(whole_blood["observation"], "OUT_OF_SCOPE")
        self.assertEqual(whole_blood["scope_status"], "blood_component")
        self.assertTrue(pd.isna(whole_blood["observed_in_source_snapshot"]))

        discontinued = rows.loc[("paracetamol", "US")]
        self.assertEqual(discontinued["observation"], "STANDALONE")
        self.assertEqual(discontinued["current_marketing"], "NOT_MARKETED")
        self.assertEqual(discontinued["current_authorization"], "UNKNOWN")

        whole_blood_summary = result.summary.set_index("preferred_name").loc["whole blood"]
        self.assertEqual(int(whole_blood_summary["determinate_country_count"]), 0)
        self.assertFalse(bool(whole_blood_summary["all_selected_present"]))

    def test_real_registry_vaccine_variants_never_publish_false_absence(self):
        fixture = json.loads(REAL_SOURCE_REGRESSIONS.read_text(encoding="utf-8"))
        cases = fixture["identity_cases"]
        nonmatches = fixture["identity_nonmatches"]
        source_cases = [*cases, *nonmatches]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw"

            hsa_path = raw / "hsa" / "hsa_registered_therapeutic_products.csv"
            hsa = pd.read_csv(hsa_path, dtype=str).fillna("")
            for case in [item for item in source_cases if item["country_code"] == "SG"]:
                hsa.loc[len(hsa)] = {
                    "licence_no": case["source_product_key"],
                    "product_name": case["product_name"],
                    "approval_d": "2000-01-01",
                    "atc_code": "J07BD52",
                    "active_ingredients": case["raw_ingredient_text"],
                    "license_holder": "Frozen HSA regression fixture",
                }
            hsa.to_csv(hsa_path, index=False)

            bt_path = raw / "bt" / "registered_products.csv"
            bt = pd.read_csv(bt_path, dtype=str).fillna("")
            for ordinal, case in enumerate(
                [item for item in source_cases if item["country_code"] == "BT"], start=6
            ):
                bt.loc[len(bt)] = {
                    "Sr. No": str(ordinal),
                    "Reg_No": case["source_product_key"],
                    "Generic_Name": case["raw_ingredient_text"],
                    "BrandName": case["product_name"],
                    "Therapeutic Category": "Vaccines",
                    "MAH": "Frozen Bhutan regression fixture",
                    "Packsize": "source slice",
                    "Product_validity": "2028-12-31",
                    "Manufacture": "source slice",
                }
            bt.to_csv(bt_path, index=False)

            bd_path = raw / "bd" / "dgda_concepts.json"
            bd = json.loads(bd_path.read_text(encoding="utf-8"))
            for case in [item for item in source_cases if item["country_code"] == "BD"]:
                bd["concepts"].append(
                    {
                        "id": case["source_product_key"],
                        "display_name": case["product_name"],
                        "retired": False,
                        "extras": {
                            "dar_number": "363-0008-069",
                            "trade_name": case["product_name"],
                            "generic_content_raw": case["raw_ingredient_text"],
                            "dosage_form": "Vaccine",
                            "company": "Frozen Bangladesh regression fixture",
                            "dar_quality_flag": "",
                        },
                    }
                )
            bd["metadata"]["num_found"] = len(bd["concepts"])
            bd["metadata"]["num_returned"] = len(bd["concepts"])
            bd_path.write_text(json.dumps(bd), encoding="utf-8")

            eeml_path = raw / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            for target in sorted(
                {target for case in source_cases for target in case["eml_targets"]}
            ):
                eeml.loc[len(eeml)] = {
                    "Medicine name": target,
                    "EML section": "Frozen vaccine regression fixture",
                    "Formulations": "source slice",
                    "Indication": "",
                    "ATC codes": "",
                    "Combined with": "",
                    "Status": "Added",
                }
            eeml.to_csv(eeml_path, index=False)

            artifact = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            comparison = compare_atlas(
                artifact.database_path, countries=("SG", "BD", "BT")
            )

        by_pair = comparison.long.set_index(["preferred_name", "country_code"])
        for case in cases:
            for target in case["eml_targets"]:
                row = by_pair.loc[(target, case["country_code"])]
                if (case["source_product_key"], target) in {
                    ("SIN12099P", "cholera vaccine"),
                    ("BHU-MPD/24/AR/H041", "bcg vaccine"),
                }:
                    self.assertEqual(row["observation"], "STANDALONE")
                    self.assertEqual(row["uncertainty_reason"], "")
                    continue
                self.assertEqual(
                    row["observation"],
                    "UNKNOWN",
                    f"{case['country_code']} {target} must not publish a false absence",
                )
                self.assertEqual(
                    row["uncertainty_reason"], "identity_match_requires_review"
                )
        for case in nonmatches:
            for target in case["eml_targets"]:
                row = by_pair.loc[(target, case["country_code"])]
                self.assertEqual(row["observation"], "OBSERVED_ABSENCE")
                self.assertEqual(row["uncertainty_reason"], "")

    def test_lexically_related_source_identity_makes_gap_unknown_not_absent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            bt_path = root / "data" / "raw" / "bt" / "registered_products.csv"
            bt = pd.read_csv(bt_path, dtype=str).fillna("")
            bt.loc[len(bt)] = {
                "Sr. No": "6",
                "Reg_No": "BHU-DRA/23/RN/H006",
                "Generic_Name": "Inactivated Poliomyelitis Vaccine",
                "BrandName": "IPV",
                "Therapeutic Category": "Vaccines",
                "MAH": "BT Holder",
                "Packsize": "1 vial",
                "Product_validity": "2027-01-01",
                "Manufacture": "BT Maker",
            }
            bt.to_csv(bt_path, index=False)
            eeml_path = root / "data" / "raw" / "who" / "eeml_2025.csv"
            eeml = pd.read_csv(eeml_path, dtype=str).fillna("")
            eeml.loc[len(eeml)] = {
                "Medicine name": "poliomyelitis vaccine",
                "EML section": "Immunologicals",
                "Formulations": "injection",
                "Indication": "",
                "ATC codes": "J07BF",
                "Combined with": "",
                "Status": "Added",
            }
            eeml.to_csv(eeml_path, index=False)
            artifact = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            result = compare_atlas(artifact.database_path, countries=("BT",))
            with sqlite3.connect(artifact.database_path) as connection:
                uncertainty_count = connection.execute(
                    "SELECT COUNT(*) FROM substance_identity_uncertainties "
                    "WHERE country_code = 'BT'"
                ).fetchone()[0]

        polio = result.long.set_index("preferred_name").loc["poliomyelitis vaccine"]
        self.assertEqual(polio["observation"], "UNKNOWN")
        self.assertEqual(polio["uncertainty_reason"], "identity_match_requires_review")
        self.assertIn("inactivated poliomyelitis vaccine", polio["evidence_note"])
        self.assertGreater(uncertainty_count, 0)

    def test_comparison_keeps_resolved_member_of_partly_unresolved_combo_as_combo_only(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            hsa_path = (
                root
                / "data"
                / "raw"
                / "hsa"
                / "hsa_registered_therapeutic_products.csv"
            )
            hsa = pd.read_csv(hsa_path, dtype=str).fillna("")
            hsa.loc[hsa["licence_no"].eq("SG1"), "active_ingredients"] = (
                "AMOXICILLIN TRIHYDRATE && SODIUM S-LACTATE"
            )
            hsa.loc[hsa["licence_no"].eq("SG1"), "strength"] = "500 mg && 50%"
            hsa.to_csv(hsa_path, index=False)
            artifact = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "atlas")
            )
            result = compare_atlas(artifact.database_path, countries=("SG",))

        observation = result.long.set_index("preferred_name").loc[
            "amoxicillin", "observation"
        ]
        self.assertEqual(observation, "COMBO_ONLY")

    def test_comparison_computes_presence_observed_absence_and_penetration_for_any_selection(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            artifact = build_atlas(
                BuildSpec(
                    root=root,
                    extraction_date=EXTRACTION_DATE,
                    output_dir=root / "build",
                )
            )

            result = compare_atlas(
                artifact.database_path,
                countries=("US", "SG", "BD", "BT"),
                universe_id="WHO_EML_2025",
            )

        by_pair = result.long.set_index(["preferred_name", "country_code"])
        self.assertEqual(by_pair.loc[("amoxicillin", "US"), "observation"], "STANDALONE")
        self.assertEqual(by_pair.loc[("amoxicillin", "BD"), "observation"], "COMBO_ONLY")
        self.assertEqual(
            by_pair.loc[("metformin", "US"), "observation"],
            "OBSERVED_ABSENCE",
        )
        self.assertIn(
            "not observed",
            by_pair.loc[("metformin", "US"), "evidence_note"].lower(),
        )

        summary = result.summary.set_index("preferred_name")
        self.assertEqual(int(summary.loc["amoxicillin", "present_country_count"]), 4)
        self.assertAlmostEqual(float(summary.loc["amoxicillin", "global_penetration"]), 1.0)
        self.assertTrue(bool(summary.loc["amoxicillin", "all_selected_present"]))
        self.assertEqual(len(result.wide), 3)
        self.assertIn("US Observation", result.wide.columns)
        self.assertIn("BT Evidence Note", result.wide.columns)
        self.assertIn("substance_id", result.wide.columns)

    def test_current_qualified_projection_is_explicit_and_country_scoped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            artifact = build_atlas(
                BuildSpec(root=root, extraction_date=EXTRACTION_DATE, output_dir=root / "build")
            )
            listed = compare_atlas(
                artifact.database_path, countries=("US", "BT")
            )
            with sqlite3.connect(artifact.database_path) as connection:
                connection.execute(
                    "UPDATE registered_products SET current_qualified = NULL "
                    "WHERE country_code = 'BT' "
                    "AND source_product_key = 'BHU-DRA/23/RN/H001'"
                )
                connection.commit()
            current = compare_atlas(
                artifact.database_path,
                countries=("US", "BT"),
                current_qualified_countries=("BT",),
            )
            with self.assertRaisesRegex(ValueError, "not supported"):
                compare_atlas(
                    artifact.database_path,
                    countries=("US",),
                    current_qualified_countries=("US",),
                )

        listed_pair = listed.long.set_index(["preferred_name", "country_code"])
        current_pair = current.long.set_index(["preferred_name", "country_code"])
        self.assertEqual(listed_pair.loc[("amoxicillin", "BT"), "observation"], "COMBO_ONLY")
        self.assertEqual(
            current_pair.loc[("amoxicillin", "BT"), "observation"],
            "OBSERVED_ABSENCE",
        )
        self.assertEqual(
            current_pair.loc[("amoxicillin", "BT"), "presence_basis"],
            "current_qualified",
        )
        self.assertEqual(
            current_pair.loc[("metformin", "BT"), "observation"], "UNKNOWN"
        )

    def test_removed_eeml_rows_do_not_enter_the_universe(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            artifact = build_atlas(
                BuildSpec(
                    root=root,
                    extraction_date=EXTRACTION_DATE,
                    output_dir=root / "build",
                )
            )
            with sqlite3.connect(artifact.database_path) as connection:
                names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT s.preferred_name "
                        "FROM essential_medicine_members em "
                        "JOIN substances s USING (substance_id)"
                    )
                }

        self.assertNotIn("obsolete medicine", names)
        self.assertNotIn("amoxicillin paracetamol", names)
        self.assertNotIn("gentamicin", names)
        self.assertIn("amoxicillin", names)
        self.assertIn("paracetamol", names)

    def test_rejected_snapshot_produces_unknown_not_observed_absence(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            artifact = build_atlas(
                BuildSpec(
                    root=root,
                    extraction_date=EXTRACTION_DATE,
                    output_dir=root / "build",
                )
            )
            with sqlite3.connect(artifact.database_path) as connection:
                connection.execute(
                    "UPDATE source_snapshots SET snapshot_status = 'rejected' "
                    "WHERE country_code = 'BD'"
                )
                connection.commit()

            result = compare_atlas(
                artifact.database_path,
                countries=("US", "BD"),
                universe_id="WHO_EML_2025",
            )

        by_pair = result.long.set_index(["preferred_name", "country_code"])
        self.assertEqual(by_pair.loc[("amoxicillin", "BD"), "observation"], "UNKNOWN")
        summary = result.summary.set_index("preferred_name")
        self.assertEqual(int(summary.loc["amoxicillin", "accepted_snapshot_count"]), 1)
        self.assertEqual(float(summary.loc["amoxicillin", "global_penetration"]), 1.0)
        self.assertFalse(bool(summary.loc["amoxicillin", "all_selected_present"]))


class LegacyCompatibilityTests(unittest.TestCase):
    def test_fixture_legacy_renderer_excludes_anda_while_atlas_keeps_it(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            artifact = build_atlas(
                BuildSpec(
                    root=root,
                    extraction_date=EXTRACTION_DATE,
                    output_dir=root / "build",
                )
            )

            legacy = render_legacy_compatibility(artifact.database_path)

        by_name = legacy.set_index("Active Ingredient")
        self.assertEqual(by_name.loc["amoxicillin", "FDA Status"], "STANDALONE")
        self.assertNotIn("paracetamol", by_name.index)
        self.assertEqual(by_name.loc["metformin", "FDA Status"], "ABSENT")
        self.assertEqual(by_name.loc["metformin", "Availability"], "HSA_ONLY")

    def test_frozen_legacy_renderer_preserves_exact_delivered_projection(self):
        root = Path(__file__).resolve().parents[1]
        fixture = Path(__file__).parent / "fixtures" / "legacy_compatibility_observations.csv.gz"
        self.assertEqual(
            hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "462a22272329c45190c073afee432e0a99b80e75fefbd46ae8b12cc1a885b566",
        )
        reference = pd.read_csv(
            root / "data" / "output" / "fda_hsa_by_actives.csv",
            keep_default_na=False,
        )
        observations = pd.read_csv(fixture, keep_default_na=False)
        for column in ("is_combo", "is_rare", "is_on_who_eml"):
            observations[column] = (
                observations[column].astype(str).str.casefold().eq("true").astype(int)
            )
        eeml_fixture = Path(__file__).parent / "fixtures" / "legacy_eeml_keys.txt"
        self.assertEqual(
            hashlib.sha256(eeml_fixture.read_bytes()).hexdigest(),
            "e1afaf8fab0440e5478575f3d90874144d4c518970f895a1bcb539459e9b5aa4",
        )
        eeml_names = eeml_fixture.read_text(encoding="utf-8").splitlines()
        with TemporaryDirectory() as tmp:
            database = Path(tmp) / "legacy.sqlite"
            with sqlite3.connect(database) as connection:
                observations.to_sql(
                    "legacy_compatibility_observations",
                    connection,
                    if_exists="fail",
                    index=False,
                )
                connection.executescript(
                    """
                    CREATE TABLE substances (
                        substance_id TEXT PRIMARY KEY,
                        normalized_ingredient_key TEXT NOT NULL
                    );
                    CREATE TABLE essential_medicine_entries (
                        entry_id TEXT PRIMARY KEY,
                        universe_id TEXT NOT NULL
                    );
                    CREATE TABLE essential_medicine_members (
                        entry_id TEXT NOT NULL,
                        substance_id TEXT NOT NULL
                    );
                    """
                )
                for ordinal, name in enumerate(eeml_names):
                    substance_id = f"fixture-substance-{ordinal}"
                    entry_id = f"fixture-entry-{ordinal}"
                    connection.execute(
                        "INSERT INTO substances VALUES (?, ?)", (substance_id, name)
                    )
                    connection.execute(
                        "INSERT INTO essential_medicine_entries VALUES (?, ?)",
                        (entry_id, "WHO_EML_2025"),
                    )
                    connection.execute(
                        "INSERT INTO essential_medicine_members VALUES (?, ?)",
                        (entry_id, substance_id),
                    )
                connection.commit()
            actual = render_legacy_compatibility(database)

        self.assertEqual(list(actual.columns), list(reference.columns))
        self.assertEqual(len(actual), 2923)
        non_eml_columns = [column for column in reference if column != "WHO Essential Drug"]
        pd.testing.assert_frame_equal(
            actual[non_eml_columns].reset_index(drop=True),
            reference[non_eml_columns].reset_index(drop=True),
            check_dtype=False,
        )
        self.assertEqual(int(actual["WHO Essential Drug"].sum()), 501)
        self.assertEqual(
            actual["Availability"].value_counts().to_dict(),
            {"FDA_ONLY": 1419, "NO GAP": 856, "HSA_ONLY": 523, "PARTIAL GAP": 125},
        )


class SnapshotReferenceInputTests(unittest.TestCase):
    def test_build_uses_legacy_references_from_selected_raw_snapshot(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture_sources(root)
            raw = root / "data" / "raw"
            snapshot = root / "frozen-raw-snapshot"
            shutil.copytree(raw, snapshot)
            (raw / "who" / "atc.csv").unlink()
            (raw / "Rare Drugs.xls").unlink()

            artifact = build_atlas(
                BuildSpec(
                    root=root,
                    raw_dir=snapshot,
                    extraction_date=EXTRACTION_DATE,
                    output_dir=root / "atlas",
                )
            )
            manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["legacy_dependency_hashes"]["atc"],
                hashlib.sha256((snapshot / "who" / "atc.csv").read_bytes()).hexdigest(),
            )
            self.assertEqual(
                manifest["legacy_dependency_hashes"]["rare_drugs"],
                hashlib.sha256((snapshot / "Rare Drugs.xls").read_bytes()).hexdigest(),
            )


class FetchPublicationTests(unittest.TestCase):
    def test_fetch_preflights_required_legacy_reference_inputs_before_network(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "src.fetch_sources._fetch_fda",
                side_effect=AssertionError("network fetch must not start"),
            ):
                with self.assertRaisesRegex(
                    FileNotFoundError, "ATC.*Rare Drugs"
                ):
                    fetch_sources(root, EXTRACTION_DATE)

    def test_fetch_rejects_malformed_legacy_inputs_before_network(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_legacy_reference_inputs(root)
            cases = {
                "ATC": (root / "data" / "raw" / "who" / "atc.csv", b""),
                "Rare Drugs": (
                    root / "data" / "raw" / "Rare Drugs.xls",
                    b"not an html or spreadsheet table",
                ),
            }
            for expected, (path, invalid_bytes) in cases.items():
                with self.subTest(reference=expected):
                    write_legacy_reference_inputs(root)
                    path.write_bytes(invalid_bytes)
                    with mock.patch(
                        "src.fetch_sources._fetch_fda",
                        side_effect=AssertionError("network fetch must not start"),
                    ):
                        with self.assertRaisesRegex(ValueError, expected):
                            fetch_sources(root, EXTRACTION_DATE)

    def test_fetch_publishes_one_snapshot_and_switches_current_only_after_success(self):
        def fake_fetch(name: str):
            def implementation(destination, *args):
                records = args[-1]
                destination.mkdir(parents=True, exist_ok=True)
                path = destination / f"{name}.dat"
                path.write_bytes(name.encode("ascii"))
                records[name] = {"sha256": name}
                return {name: path}

            return implementation

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_legacy_reference_inputs(root)
            patches = (
                mock.patch("src.fetch_sources._fetch_fda", side_effect=fake_fetch("US")),
                mock.patch("src.fetch_sources._fetch_hsa", side_effect=fake_fetch("SG")),
                mock.patch("src.fetch_sources._fetch_bangladesh", side_effect=fake_fetch("BD")),
                mock.patch("src.fetch_sources._fetch_bhutan", side_effect=fake_fetch("BT")),
                mock.patch(
                    "src.fetch_sources._fetch_eeml",
                    side_effect=fake_fetch("WHO_EML_2025"),
                ),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                artifact = fetch_sources(root, EXTRACTION_DATE)

            current = root / "data" / "raw" / "current"
            self.assertTrue(current.is_symlink())
            self.assertEqual(artifact.manifest_path, current.resolve() / "manifest.json")
            manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["countries"]), {"US", "SG", "BD", "BT"})
            self.assertEqual(
                manifest["artifacts"]["WHO_ATC"]["license_status"],
                "human_review_required",
            )
            self.assertTrue((current.resolve() / "who" / "atc.csv").is_file())
            self.assertTrue((current.resolve() / "Rare Drugs.xls").is_file())

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_legacy_reference_inputs(root)
            with mock.patch(
                "src.fetch_sources._fetch_fda", side_effect=RuntimeError("network failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "network failed"):
                    fetch_sources(root, EXTRACTION_DATE, countries=("US",))
            self.assertFalse((root / "data" / "raw" / "current").exists())
            snapshots = root / "data" / "raw" / "snapshots"
            self.assertEqual(list(snapshots.glob("2026-07-15*")), [])


if __name__ == "__main__":
    unittest.main()
