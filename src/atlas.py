"""Country-agnostic storage and comparison for national drug registers.

Adapters retain source observations.  Presence and cross-country comparisons
are projections over those observations, never fields written back to a
country record.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree

import pandas as pd

from .normalize import (
    normalize_fda_component,
    normalize_ingredient,
    split_fda_ingredients,
    split_hsa_ingredients,
)
from .pipeline import (
    LONG_COLUMNS,
    PSEUDO_SUBSTANCES,
    OUTPUT_COLUMNS,
    assign_availability,
    build_atc_class_lookup,
    build_atc_l5_lookup,
    detect_combo_atc_codes,
    load_atc,
    load_fda_product_substances,
    load_hsa_product_substances,
    load_rare_substance_keys,
    read_fda_table,
)


ATLAS_NAMESPACE = uuid.UUID("186c4a4a-a3aa-5be5-968d-ce32b54e4054")
SCHEMA_VERSION = "4"
SUBSTANCE_IDENTITY_VERSION = "normalized-ingredient-key-v1"
UNIVERSE_ID = "WHO_EML_2025"
SUPPORTED_COUNTRIES = ("US", "SG", "BD", "BT")
MINIMUM_DECLARED_ROWS = {
    "US": 50_000,
    "SG": 5_000,
    "BD": 30_000,
    "BT": 1_000,
}
EEML_COLUMNS = [
    "Medicine name",
    "EML section",
    "Formulations",
    "Indication",
    "ATC codes",
    "Combined with",
    "Status",
]
EEML_SOURCE_URL = "https://list.essentialmeds.org/print?format=xlsx"
EEML_LICENSE_URL = "https://list.essentialmeds.org/licencing"
FDA_SOURCE_URL = "https://www.fda.gov/media/89850/download"
HSA_SOURCE_URL = (
    "https://data.gov.sg/collections/161/view"
)
BD_SOURCE_URL = (
    "https://api.tr.ocl.dghs.gov.bd/orgs/MoHFW/collections/"
    "dgda-registered-drugs-valueset/concepts/"
)
BT_PRODUCTS_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1vlubMgQc67cSCQJFH1JuOwDtRbx6oohYNXGdqSNujyk/"
)
BT_ACTIONS_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1DWvVvz3PgzGWyMaou-Ckw1Y4RKXBQRAVgjA9lC0INxk/"
)
WHO_ADAPTATION_NOTICE = (
    "This is an adaptation of an original work by World Health Organization (WHO). "
    "Views and opinions expressed in the adaptation are the sole responsibility of the "
    "author or authors of the adaptation and are not endorsed by World Health Organization "
    "(WHO)."
)
LEGACY_DEPENDENCY_LICENSES = {
    "pipeline": {
        "license_name": "Repository code",
        "license_url": "",
        "license_status": "internal_build_logic",
    },
    "atc": {
        "license_name": "WHO ATC/DDD Index terms",
        "license_url": "https://atcddd.fhi.no/copyright_disclaimer/",
        "license_status": "human_review_required",
    },
    "rare_drugs": {
        "license_name": "U.S. FDA public source",
        "license_url": "https://www.accessdata.fda.gov/scripts/opdlisting/oopd/",
        "license_status": "reviewed_public_government_source",
    },
}
IDENTITY_UNCERTAINTY_STOP_TOKENS = frozenset(
    {
        "acid",
        "agent",
        "agents",
        "compound",
        "complex",
        "containing",
        "factor",
        "human",
        "injection",
        "normal",
        "oral",
        "oxide",
        "protein",
        "releasing",
        "ring",
        "solution",
        "tablet",
        "therapy",
        "type",
        "vaccine",
        "virus",
    }
)
REVIEWED_SPELLING_VARIANTS = frozenset(
    {
        frozenset(("anastrozole", "anastrozol")),
        frozenset(("carbimazole", "cabimazole")),
        frozenset(("chloramphenicol", "chloramphenical")),
        frozenset(("enoxaparin", "enoxaprin")),
        frozenset(("insulin glargine", "insulin glargin")),
        frozenset(("levodopa", "leodopa")),
        frozenset(("metformin", "metformine")),
        frozenset(("polymyxin b", "polimixin b")),
        frozenset(("procaine benzylpenicillin", "procain benzylpenicillin")),
        frozenset(("propranolol", "propanolol")),
        frozenset(("protamine", "protamin")),
        frozenset(("riboflavin", "riboflavine")),
        frozenset(("thiopental", "thipental")),
        frozenset(("tioguanine", "thioguanine")),
        frozenset(("trastuzumab", "trustuzumab")),
        frozenset(("trihexyphenidyl", "trihexiphenidyl")),
    }
)
VACCINE_PRODUCT_FAMILY_MARKERS = {
    "cholera vaccine": ("cholera",),
    "pneumococcal vaccine": ("pneumococcal", "pneumovax", "prevenar"),
    "papilloma virus vaccine": ("papillomavirus", "gardasil"),
    "meningococcal meningitis vaccine": ("meningococcal",),
}

PRODUCT_COLUMNS = [
    "country_code",
    "source_product_key",
    "registration_number",
    "product_name",
    "raw_ingredient_text",
    "ingredient_component_count",
    "unresolved_component_count",
    "application_type",
    "legacy_eligible",
    "observation_ordinal",
    "form",
    "strength",
    "sponsor",
    "approval_date",
    "validity_date",
    "included_in_presence",
    "current_qualified",
    "exclusion_reason",
    "marketing_status",
    "source_retired",
]

INGREDIENT_COLUMNS = [
    "country_code",
    "source_product_key",
    "position",
    "raw_component",
    "raw_strength",
    "normalized_ingredient_key",
    "is_combo",
    "atc_code",
    "match_method",
]

ISSUE_COLUMNS = [
    "country_code",
    "source_product_key",
    "issue_code",
    "severity",
    "detail",
]


@dataclass(frozen=True)
class SourcePolicy:
    country_code: str
    source_name: str
    source_url: str
    related_source_urls: str
    supports_current_qualification: bool
    coverage_scope: str
    observed_absence_wording: str
    status_semantics: str
    license_name: str
    license_url: str
    license_status: str
    attribution: str


@dataclass(frozen=True)
class AdapterBatch:
    policy: SourcePolicy
    products: pd.DataFrame
    ingredients: pd.DataFrame
    issues: pd.DataFrame
    metrics: dict[str, int | str] = field(default_factory=dict)


class CountryAdapter(Protocol):
    country_code: str

    def stage(self, raw_dir: Path, extraction_date: date) -> AdapterBatch:
        """Parse one immutable raw snapshot without writing output."""


@dataclass(frozen=True)
class BuildSpec:
    root: Path
    extraction_date: date
    output_dir: Path | None = None
    countries: tuple[str, ...] = SUPPORTED_COUNTRIES
    universe_id: str = UNIVERSE_ID
    raw_dir: Path | None = None
    # Unit fixtures only. Production callers must leave this false.
    allow_unmanifested_test_fixture: bool = False


@dataclass(frozen=True)
class BuildArtifact:
    build_id: str
    current_directory: Path
    database_path: Path
    manifest_path: Path
    table_paths: dict[str, Path]
    table_hashes: dict[str, str]
    view_paths: dict[str, Path]
    view_hashes: dict[str, str]
    report_path: Path


@dataclass(frozen=True)
class ComparisonResult:
    long: pd.DataFrame
    summary: pd.DataFrame
    wide: pd.DataFrame


class FdaAdapter:
    country_code = "US"
    policy = SourcePolicy(
        country_code="US",
        source_name="Drugs@FDA",
        source_url=FDA_SOURCE_URL,
        related_source_urls="",
        supports_current_qualification=False,
        coverage_scope=(
            "Drugs@FDA product observations for NDA, BLA, and ANDA applications; "
            "this does not assert that a product is currently marketed."
        ),
        observed_absence_wording="Not observed in the ingested Drugs@FDA snapshot.",
        status_semantics="Application and marketing fields are retained as source observations.",
        license_name="U.S. FDA public source",
        license_url="https://www.fda.gov/about-fda/about-website/website-policies",
        license_status="reviewed_public_government_source",
        attribution="U.S. Food and Drug Administration, Drugs@FDA.",
    )

    def stage(self, raw_dir: Path, extraction_date: date) -> AdapterBatch:
        source_metadata_path = raw_dir / "source_metadata.json"
        source_metadata = (
            json.loads(source_metadata_path.read_text(encoding="utf-8"))
            if source_metadata_path.exists()
            else {}
        )
        applications = read_fda_table(raw_dir / "Applications.txt").reset_index(drop=True)
        products = read_fda_table(raw_dir / "Products.txt").reset_index(drop=True)
        submissions = read_fda_table(raw_dir / "Submissions.txt").reset_index(drop=True)
        declared_row_count = len(products)
        application_numbers = set(applications["ApplNo"].astype(str))
        marketing_path = raw_dir / "MarketingStatus.txt"
        marketing_lookup_path = raw_dir / "MarketingStatus_Lookup.txt"
        if marketing_path.exists():
            marketing = read_fda_table(marketing_path)
            if marketing_lookup_path.exists():
                marketing_lookup = read_fda_table(marketing_lookup_path)
                marketing = marketing.merge(
                    marketing_lookup,
                    on="MarketingStatusID",
                    how="left",
                    sort=False,
                )
            else:
                marketing["MarketingStatusDescription"] = marketing["MarketingStatusID"]
            products = products.merge(
                marketing[
                    ["ApplNo", "ProductNo", "MarketingStatusDescription"]
                ].drop_duplicates(["ApplNo", "ProductNo"], keep="first"),
                on=["ApplNo", "ProductNo"],
                how="left",
                sort=False,
            ).fillna("")
        else:
            products["MarketingStatusDescription"] = ""

        applications["_application_ordinal"] = range(len(applications))
        products["_product_ordinal"] = range(len(products))
        applications = applications[applications["ApplType"].isin(["NDA", "BLA", "ANDA"])].copy()

        submissions["SubmissionStatusDate"] = pd.to_datetime(
            submissions["SubmissionStatusDate"], errors="coerce"
        )
        original_approvals = (
            submissions[
                submissions["SubmissionType"].eq("ORIG")
                & submissions["SubmissionStatus"].eq("AP")
            ]
            .groupby("ApplNo", as_index=False)["SubmissionStatusDate"]
            .min()
            .rename(columns={"SubmissionStatusDate": "approval_date"})
        )
        staged = (
            applications.merge(products, on="ApplNo", how="inner", sort=False)
            .merge(original_approvals, on="ApplNo", how="left", sort=False)
            .reset_index(drop=True)
        )

        product_rows: list[dict[str, object]] = []
        ingredient_rows: list[dict[str, object]] = []
        issue_rows: list[dict[str, object]] = [
            _issue(
                "US",
                f"{row.ApplNo}-{row.ProductNo}",
                "missing_application_record",
                "warning",
                f"ApplNo={row.ApplNo}; product={row.DrugName}",
            )
            for row in products[~products["ApplNo"].astype(str).isin(application_numbers)].itertuples(
                index=False
            )
        ]
        for ordinal, row in enumerate(staged.itertuples(index=False)):
            product_key = f"{row.ApplNo}-{row.ProductNo}"
            pairs: list[tuple[str, str]] = []
            discarded_components: list[tuple[str, str]] = []
            for raw_component in split_fda_ingredients(row.ActiveIngredient):
                normalized = normalize_fda_component(raw_component, row.DrugName)
                if len(normalized) >= 3:
                    pairs.append((raw_component, normalized))
                elif normalized:
                    discarded_components.append((raw_component, normalized))
            distinct = {normalized for _, normalized in pairs}
            is_combo = len(distinct) >= 2
            if not pairs:
                issue_rows.append(
                    _issue("US", product_key, "unresolved_ingredient", "warning", str(row.ActiveIngredient))
                )
            for raw_component, normalized in discarded_components:
                issue_rows.append(
                    _issue(
                        "US",
                        product_key,
                        "discarded_short_split_artifact",
                        "warning",
                        f"raw={raw_component}; normalized={normalized}",
                    )
                )

            product_rows.append(
                {
                    "country_code": "US",
                    "source_product_key": product_key,
                    "registration_number": str(row.ApplNo),
                    "product_name": str(row.DrugName),
                    "raw_ingredient_text": str(row.ActiveIngredient),
                    "ingredient_component_count": len(distinct),
                    "unresolved_component_count": 0,
                    "application_type": str(row.ApplType),
                    "legacy_eligible": str(row.ApplType) in {"NDA", "BLA"},
                    "observation_ordinal": ordinal,
                    "form": str(getattr(row, "Form", "")),
                    "strength": str(getattr(row, "Strength", "")),
                    "sponsor": str(getattr(row, "SponsorName", "")),
                    "approval_date": _iso_date(row.approval_date),
                    "validity_date": "",
                    "included_in_presence": bool(pairs),
                    "current_qualified": None,
                    "exclusion_reason": "" if pairs else "unresolved_ingredient",
                    "marketing_status": str(
                        getattr(row, "MarketingStatusDescription", "")
                    ),
                    "source_retired": None,
                }
            )
            for position, (raw_component, normalized) in enumerate(pairs):
                ingredient_rows.append(
                    _ingredient(
                        "US",
                        product_key,
                        position,
                        raw_component,
                        normalized,
                        is_combo,
                        raw_strength=str(getattr(row, "Strength", "")),
                    )
                )

        return AdapterBatch(
            policy=self.policy,
            products=_frame(product_rows, PRODUCT_COLUMNS),
            ingredients=_frame(ingredient_rows, INGREDIENT_COLUMNS),
            issues=_frame(issue_rows, ISSUE_COLUMNS),
            metrics={
                "declared_row_count": declared_row_count,
                "parsed_row_count": len(staged),
                "source_as_of_date": str(source_metadata.get("source_data_as_of", "")),
                "captured_on_date": str(source_metadata.get("captured_on", "")),
            },
        )


class HsaAdapter:
    country_code = "SG"
    policy = SourcePolicy(
        country_code="SG",
        source_name="HSA registered therapeutic products",
        source_url=HSA_SOURCE_URL,
        related_source_urls="",
        supports_current_qualification=False,
        coverage_scope="Therapeutic products appearing in the ingested HSA register snapshot.",
        observed_absence_wording="Not observed in the ingested HSA register snapshot.",
        status_semantics="Register presence is not a separate assertion of current marketing.",
        license_name="Singapore Open Data Licence version 1.0",
        license_url="https://data.gov.sg/open-data-licence",
        license_status="licensed_open",
        attribution=(
            "Contains information from Listing of Registered Therapeutic Products accessed on "
            "{access_date} from the Health Sciences Authority via data.gov.sg, made available "
            "under the Singapore Open Data Licence version 1.0."
        ),
    )

    def stage(self, raw_dir: Path, extraction_date: date) -> AdapterBatch:
        metadata_path = raw_dir / "hsa_registered_therapeutic_products_metadata.json"
        source_as_of_date = ""
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            last_updated = str((metadata.get("data") or {}).get("lastUpdatedAt") or "")
            source_as_of_date = last_updated[:10]
        source = pd.read_csv(
            raw_dir / "hsa_registered_therapeutic_products.csv", dtype=str
        ).fillna("")
        declared_row_count = len(source)
        fetch_metadata_path = raw_dir / "fetch_metadata.json"
        sample_path = raw_dir / "hsa_registered_therapeutic_products_sample.json"
        if fetch_metadata_path.exists():
            fetch_metadata = json.loads(fetch_metadata_path.read_text(encoding="utf-8"))
            declared_row_count = int(fetch_metadata["row_count"])
        elif sample_path.exists():
            sample = json.loads(sample_path.read_text(encoding="utf-8"))
            declared_row_count = int((sample.get("result") or {}).get("total", len(source)))
        if declared_row_count != len(source):
            raise ValueError(
                "HSA snapshot is incomplete: "
                f"declared {declared_row_count}, parsed {len(source)}"
            )
        product_rows: list[dict[str, object]] = []
        ingredient_rows: list[dict[str, object]] = []
        issue_rows: list[dict[str, object]] = []
        for ordinal, row in enumerate(source.itertuples(index=False)):
            product_key = str(row.licence_no)
            raw_components = split_hsa_ingredients(row.active_ingredients)
            normalized_components = [
                (index, raw, normalize_ingredient(raw))
                for index, raw in enumerate(raw_components)
            ]
            pairs = [
                (index, raw, normalized)
                for index, raw, normalized in normalized_components
                if len(normalized) >= 3
            ]
            distinct = {normalized for _, _, normalized in pairs}
            is_combo = len(raw_components) >= 2
            unresolved_component_count = max(len(raw_components) - len(pairs), 0)
            if not pairs:
                issue_rows.append(
                    _issue("SG", product_key, "unresolved_ingredient", "warning", row.active_ingredients)
                )
            elif unresolved_component_count:
                issue_rows.append(
                    _issue(
                        "SG",
                        product_key,
                        "partially_unresolved_ingredient",
                        "warning",
                        f"unresolved_components={unresolved_component_count}; raw={row.active_ingredients}",
                    )
                )
            product_rows.append(
                {
                    "country_code": "SG",
                    "source_product_key": product_key,
                    "registration_number": product_key,
                    "product_name": str(row.product_name),
                    "raw_ingredient_text": str(row.active_ingredients),
                    "ingredient_component_count": len(distinct)
                    + unresolved_component_count,
                    "unresolved_component_count": unresolved_component_count,
                    "application_type": "",
                    "legacy_eligible": True,
                    "observation_ordinal": ordinal,
                    "form": str(getattr(row, "dosage_form", "")),
                    "strength": str(getattr(row, "strength", "")),
                    "sponsor": str(getattr(row, "license_holder", "")),
                    "approval_date": _iso_date(getattr(row, "approval_d", "")),
                    "validity_date": "",
                    "included_in_presence": bool(pairs),
                    "current_qualified": None,
                    "exclusion_reason": "" if pairs else "unresolved_ingredient",
                    "marketing_status": "",
                    "source_retired": None,
                }
            )
            atc_code = str(getattr(row, "atc_code", "")).strip().upper()
            raw_strength = str(getattr(row, "strength", ""))
            strength_parts = [part.strip() for part in raw_strength.split("&&")]
            for position, (source_position, raw_component, normalized) in enumerate(pairs):
                component_strength = (
                    strength_parts[source_position]
                    if len(strength_parts) == len(raw_components)
                    else raw_strength
                )
                ingredient_rows.append(
                    _ingredient(
                        "SG",
                        product_key,
                        position,
                        raw_component,
                        normalized,
                        is_combo,
                        atc_code,
                        raw_strength=component_strength,
                    )
                )
        return AdapterBatch(
            policy=self.policy,
            products=_frame(product_rows, PRODUCT_COLUMNS),
            ingredients=_frame(ingredient_rows, INGREDIENT_COLUMNS),
            issues=_frame(issue_rows, ISSUE_COLUMNS),
            metrics={
                "declared_row_count": declared_row_count,
                "parsed_row_count": len(source),
                "source_as_of_date": source_as_of_date,
            },
        )


class BangladeshAdapter:
    country_code = "BD"
    policy = SourcePolicy(
        country_code="BD",
        source_name="DGDA registered drugs terminology mirror",
        source_url=BD_SOURCE_URL,
        related_source_urls="",
        supports_current_qualification=False,
        coverage_scope=(
            "Allopathic products represented in the ingested DGDA terminology mirror; "
            "other medicine systems are outside this source's coverage."
        ),
        observed_absence_wording=(
            "Not observed in the ingested Bangladesh allopathic register mirror; "
            "no inference is made about registration or legal status beyond that source."
        ),
        status_semantics=(
            "The source does not publish reliable approval, expiry, or legal-status fields."
        ),
        license_name="No redistribution licence recorded",
        license_url=BD_SOURCE_URL,
        license_status="human_review_required",
        attribution="Bangladesh DGDA registered-drugs terminology mirror via Open Concept Lab.",
    )

    def stage(self, raw_dir: Path, extraction_date: date) -> AdapterBatch:
        payload = json.loads((raw_dir / "dgda_concepts.json").read_text(encoding="utf-8"))
        concepts = payload.get("concepts", [])
        metadata = payload.get("metadata", {})
        declared = int(metadata.get("num_found", len(concepts)))
        if declared != len(concepts):
            raise ValueError(
                f"Bangladesh snapshot is incomplete: declared {declared}, parsed {len(concepts)}"
            )
        concept_ids = [str(concept.get("id") or "") for concept in concepts]
        if any(not concept_id for concept_id in concept_ids):
            raise ValueError("Bangladesh snapshot contains a concept without an OCL id")
        if len(set(concept_ids)) != len(concept_ids):
            raise ValueError("Bangladesh snapshot contains duplicate OCL concept ids")
        page_records = metadata.get("pages") or []
        if page_records:
            page_numbers = [int(page["page_number"]) for page in page_records]
            expected_pages = list(range(1, len(page_records) + 1))
            if page_numbers != expected_pages:
                raise ValueError(
                    "Bangladesh snapshot page sequence is incomplete or out of order: "
                    f"{page_numbers[:5]}..."
                )

        product_rows: list[dict[str, object]] = []
        ingredient_rows: list[dict[str, object]] = []
        issue_rows: list[dict[str, object]] = []
        for ordinal, concept in enumerate(concepts):
            extras = concept.get("extras") or {}
            product_key = str(concept.get("id") or ordinal)
            raw_text = str(extras.get("generic_content_raw") or "")
            raw_components = _south_asia_raw_components(raw_text)
            pairs = _split_south_asia_ingredients(raw_text)
            unresolved_component_count = max(len(raw_components) - len(pairs), 0)
            is_combo = len({normalized for _, normalized in pairs}) >= 2
            if not pairs:
                issue_rows.append(
                    _issue("BD", product_key, "unresolved_ingredient", "warning", raw_text)
                )
            elif unresolved_component_count:
                issue_rows.append(
                    _issue(
                        "BD",
                        product_key,
                        "partially_unresolved_ingredient",
                        "warning",
                        f"unresolved_components={unresolved_component_count}; raw={raw_text}",
                    )
                )
            product_rows.append(
                {
                    "country_code": "BD",
                    "source_product_key": product_key,
                    "registration_number": str(extras.get("dar_number") or ""),
                    "product_name": str(extras.get("trade_name") or concept.get("display_name") or ""),
                    "raw_ingredient_text": raw_text,
                    "ingredient_component_count": len(
                        {normalized for _, normalized in pairs}
                    )
                    + unresolved_component_count,
                    "unresolved_component_count": unresolved_component_count,
                    "application_type": "",
                    "legacy_eligible": False,
                    "observation_ordinal": ordinal,
                    "form": str(extras.get("dosage_form") or ""),
                    "strength": "",
                    "sponsor": str(extras.get("company") or ""),
                    "approval_date": "",
                    "validity_date": "",
                    "included_in_presence": bool(pairs),
                    "current_qualified": None,
                    "exclusion_reason": "" if pairs else "unresolved_ingredient",
                    "marketing_status": "",
                    "source_retired": bool(concept.get("retired", False)),
                }
            )
            if extras.get("dar_quality_flag"):
                issue_rows.append(
                    _issue(
                        "BD",
                        product_key,
                        "source_quality_flag",
                        "warning",
                        str(extras["dar_quality_flag"]),
                    )
                )
            for position, (raw_component, normalized) in enumerate(pairs):
                ingredient_rows.append(
                    _ingredient("BD", product_key, position, raw_component, normalized, is_combo)
                )

        return AdapterBatch(
            policy=self.policy,
            products=_frame(product_rows, PRODUCT_COLUMNS),
            ingredients=_frame(ingredient_rows, INGREDIENT_COLUMNS),
            issues=_frame(issue_rows, ISSUE_COLUMNS),
            metrics={"declared_row_count": declared, "parsed_row_count": len(concepts)},
        )


class BhutanAdapter:
    country_code = "BT"
    policy = SourcePolicy(
        country_code="BT",
        source_name="Bhutan DRA registered medicinal products",
        source_url=BT_PRODUCTS_URL,
        related_source_urls=BT_ACTIONS_URL,
        supports_current_qualification=True,
        coverage_scope=(
            "Human medicinal products listed in the ingested DRA register. Validity and matched "
            "regulatory actions support a separate current-qualified projection."
        ),
        observed_absence_wording=(
            "Not observed in the human-product scope of the ingested Bhutan DRA register; "
            "no general inference about legal status is made."
        ),
        status_semantics="Validity and published actions are evaluated as of the supplied extraction date.",
        license_name="No redistribution licence recorded",
        license_url=BT_PRODUCTS_URL,
        license_status="human_review_required",
        attribution="Bhutan Drug Regulatory Authority registered medicinal products register.",
    )

    def stage(self, raw_dir: Path, extraction_date: date) -> AdapterBatch:
        products = pd.read_csv(raw_dir / "registered_products.csv", dtype=str).fillna("")
        declared_row_count = len(products)
        actions_path = raw_dir / "regulatory_actions.csv"
        if not actions_path.exists():
            raise FileNotFoundError(
                "Bhutan current qualification requires regulatory_actions.csv"
            )
        actions = pd.read_csv(actions_path, dtype=str).fillna("")

        products["_source_ordinal"] = range(len(products))
        products["_registration_key"] = products["Reg_No"].map(_normalize_registration_number)
        duplicate_registration_counts = products["_registration_key"].value_counts()
        duplicate_registration_counts = duplicate_registration_counts[
            duplicate_registration_counts.gt(1)
        ].to_dict()
        products["_validity"] = pd.to_datetime(products["Product_validity"], errors="coerce")
        products = products.sort_values(
            ["_registration_key", "_validity", "_source_ordinal"],
            kind="stable",
            na_position="first",
        ).drop_duplicates("_registration_key", keep="last")
        products = products.sort_values("_source_ordinal", kind="stable").reset_index(drop=True)

        action_for: dict[str, tuple[pd.Timestamp, str, frozenset[str]]] = {}
        indeterminate_action_keys: set[str] = set()
        action_issue_rows: list[dict[str, object]] = []
        if not actions.empty:
            actions["_registration_key"] = actions["Registration number"].map(
                _normalize_registration_number
            )
            for _, row in actions.iterrows():
                registration_key = str(row["_registration_key"])
                raw_action_date = str(row["Date of action"]).strip()
                action_date, date_issue = _parse_bhutan_action_date(raw_action_date)
                if date_issue:
                    action_issue_rows.append(
                        _issue(
                            "BT",
                            str(row["Registration number"]),
                            date_issue,
                            "warning",
                            raw_action_date,
                        )
                    )
                    status = str(row["Status"]).strip().lower()
                    if any(token in status for token in ("cancel", "suspend", "withdraw")):
                        indeterminate_action_keys.add(registration_key)
                if action_date is None or action_date.date() > extraction_date:
                    continue
                status = str(row["Status"]).strip().lower()
                if not any(token in status for token in ("cancel", "suspend", "withdraw")):
                    continue
                action_ingredient_text = str(row.get("Generic ", "") or row.get("Generic", ""))
                action_ingredients = frozenset(
                    normalized
                    for _, normalized in _split_south_asia_ingredients(action_ingredient_text)
                )
                previous = action_for.get(registration_key)
                if previous is None or action_date > previous[0]:
                    action_for[registration_key] = (action_date, status, action_ingredients)

        product_rows: list[dict[str, object]] = []
        ingredient_rows: list[dict[str, object]] = []
        issue_rows: list[dict[str, object]] = list(action_issue_rows)
        for ordinal, (_, row) in enumerate(products.iterrows()):
            product_key = str(row["Reg_No"]).strip()
            registration_key = str(row["_registration_key"])
            declared_generic = str(row["Generic_Name"]).strip()
            declared_brand = str(row["BrandName"]).strip()
            raw_text = _select_bhutan_generic_text(declared_generic, declared_brand)
            raw_components = _south_asia_raw_components(raw_text)
            fields_swapped = (
                bool(declared_generic)
                and raw_text == declared_brand
                and raw_text != declared_generic
            )
            product_name = declared_generic if fields_swapped else declared_brand
            pairs = _split_south_asia_ingredients(raw_text)
            unresolved_component_count = max(len(raw_components) - len(pairs), 0)
            product_ingredient_set = frozenset(normalized for _, normalized in pairs)
            validity = row["_validity"]
            exclusion_reason = ""
            action_collision_detail = ""
            if pd.isna(validity):
                exclusion_reason = "missing_or_invalid_validity_date"
            elif validity.date() < extraction_date:
                exclusion_reason = "expired_before_extraction_date"
            if registration_key in action_for:
                _, action_status, action_ingredient_set = action_for[registration_key]
                if action_ingredient_set and action_ingredient_set != product_ingredient_set:
                    action_collision_detail = (
                        f"registration={product_key}; product_ingredients="
                        f"{sorted(product_ingredient_set)}; action_ingredients="
                        f"{sorted(action_ingredient_set)}"
                    )
                elif "cancel" in action_status:
                    exclusion_reason = "cancelled"
                elif "suspend" in action_status:
                    exclusion_reason = "suspended"
                else:
                    exclusion_reason = "withdrawn"
            if not pairs and not exclusion_reason:
                exclusion_reason = "unresolved_ingredient"
            if registration_key in indeterminate_action_keys and not exclusion_reason:
                exclusion_reason = "indeterminate_regulatory_action_date"
            if action_collision_detail and not exclusion_reason:
                exclusion_reason = "status_registration_collision"
            is_human = bool(re.search(r"/(?:H|H\d)|/H\d", product_key.upper()))
            if re.search(r"/V\d", product_key.upper()):
                is_human = False
            if not is_human:
                exclusion_reason = "outside_human_scope"
            included = bool(pairs) and is_human
            current_qualified: bool | None = included and not exclusion_reason
            if action_collision_detail:
                current_qualified = None

            product_rows.append(
                {
                    "country_code": "BT",
                    "source_product_key": product_key,
                    "registration_number": product_key,
                    "product_name": product_name,
                    "raw_ingredient_text": raw_text,
                    "ingredient_component_count": len(product_ingredient_set)
                    + unresolved_component_count,
                    "unresolved_component_count": unresolved_component_count,
                    "application_type": "",
                    "legacy_eligible": False,
                    "observation_ordinal": ordinal,
                    "form": "",
                    "strength": "",
                    "sponsor": str(row["MAH"]),
                    "approval_date": "",
                    "validity_date": _iso_date(validity),
                    "included_in_presence": included,
                    "current_qualified": current_qualified,
                    "exclusion_reason": exclusion_reason,
                    "marketing_status": "",
                    "source_retired": None,
                }
            )
            if exclusion_reason and exclusion_reason != "status_registration_collision":
                issue_rows.append(
                    _issue("BT", product_key, exclusion_reason, "info", raw_text)
                )
            if pairs and unresolved_component_count:
                issue_rows.append(
                    _issue(
                        "BT",
                        product_key,
                        "partially_unresolved_ingredient",
                        "warning",
                        f"unresolved_components={unresolved_component_count}; raw={raw_text}",
                    )
                )
            if action_collision_detail:
                issue_rows.append(
                    _issue(
                        "BT",
                        product_key,
                        "status_registration_collision",
                        "warning",
                        action_collision_detail,
                    )
                )
            if fields_swapped:
                issue_rows.append(
                    _issue(
                        "BT",
                        product_key,
                        "generic_brand_fields_swapped",
                        "warning",
                        f"declared_generic={row['Generic_Name']}; selected={raw_text}",
                    )
                )
            if re.match(r"^\s*HU-(?:DRA|MPD)", str(row["Reg_No"]), flags=re.IGNORECASE):
                issue_rows.append(
                    _issue(
                        "BT",
                        product_key,
                        "registration_number_normalized",
                        "warning",
                        f"raw={product_key}; canonical_key={registration_key}",
                    )
                )
            duplicate_count = int(duplicate_registration_counts.get(registration_key, 0))
            if duplicate_count:
                issue_rows.append(
                    _issue(
                        "BT",
                        product_key,
                        "duplicate_registration_collapsed",
                        "info",
                        f"canonical_key={registration_key}; source_rows={duplicate_count}",
                    )
                )
            for position, (raw_component, normalized) in enumerate(pairs):
                ingredient_rows.append(
                    _ingredient("BT", product_key, position, raw_component, normalized, is_combo=len({n for _, n in pairs}) >= 2)
                )

        return AdapterBatch(
            policy=self.policy,
            products=_frame(product_rows, PRODUCT_COLUMNS),
            ingredients=_frame(ingredient_rows, INGREDIENT_COLUMNS),
            issues=_frame(issue_rows, ISSUE_COLUMNS),
            metrics={
                "declared_row_count": declared_row_count,
                "parsed_row_count": len(products),
            },
        )


def _validate_fetch_manifest(
    raw_root: Path,
    extraction_date: date,
    country_codes: tuple[str, ...],
) -> dict[str, object] | None:
    """Bind a build to a complete fetch manifest when one is present."""

    path = raw_root / "manifest.json"
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if str(manifest.get("extraction_date")) != extraction_date.isoformat():
        raise ValueError(
            "Fetch manifest extraction date does not match the requested build date"
        )
    manifest_countries = {str(code).upper() for code in manifest.get("countries", [])}
    missing = sorted(set(country_codes) - manifest_countries)
    if missing:
        raise ValueError(
            "Fetch manifest does not cover selected countries: " + ", ".join(missing)
        )
    records = manifest.get("artifacts") or {}
    checks: dict[str, list[tuple[Path, str]]] = {
        "US": [(raw_root / "fda" / "drugsatfda.zip", "sha256")],
        "SG": [
            (
                raw_root / "hsa" / "hsa_registered_therapeutic_products.csv",
                "sha256",
            )
        ],
        "BD": [(raw_root / "bd" / "dgda_concepts.json", "sha256")],
        "BT": [
            (raw_root / "bt" / "registered_products.csv", "products_sha256"),
            (raw_root / "bt" / "regulatory_actions.csv", "actions_sha256"),
        ],
        UNIVERSE_ID: [(raw_root / "who" / "eeml_2025.xlsx", "sha256")],
        "WHO_ATC": [(raw_root / "who" / "atc.csv", "sha256")],
        "FDA_RARE": [(raw_root / "Rare Drugs.xls", "sha256")],
    }
    for record_key in (*country_codes, UNIVERSE_ID, "WHO_ATC", "FDA_RARE"):
        record = records.get(record_key)
        if not isinstance(record, dict):
            raise ValueError(f"Fetch manifest is missing artifact record {record_key}")
        for artifact_path, hash_field in checks[record_key]:
            if not artifact_path.exists():
                raise FileNotFoundError(
                    f"Fetch manifest artifact is missing: {artifact_path}"
                )
            expected = str(record.get(hash_field) or "")
            actual = _file_hash(artifact_path)
            if not expected or actual != expected:
                raise ValueError(
                    f"Fetch manifest hash mismatch for {artifact_path.name}"
                )
    if "US" in country_codes:
        archive_path = raw_root / "fda" / "drugsatfda.zip"
        with zipfile.ZipFile(archive_path) as archive:
            for member_name in (
                "Applications.txt",
                "Products.txt",
                "Submissions.txt",
                "MarketingStatus.txt",
                "MarketingStatus_Lookup.txt",
            ):
                extracted_path = raw_root / "fda" / member_name
                if member_name not in archive.namelist():
                    if member_name in {"MarketingStatus.txt", "MarketingStatus_Lookup.txt"}:
                        continue
                    raise FileNotFoundError(
                        f"FDA snapshot is missing archive member {member_name}"
                    )
                if not extracted_path.exists():
                    raise FileNotFoundError(
                        f"FDA snapshot is missing archive member {member_name}"
                    )
                archived_hash = hashlib.sha256(archive.read(member_name)).hexdigest()
                if _file_hash(extracted_path) != archived_hash:
                    raise ValueError(
                        f"FDA extracted file does not match archive: {member_name}"
                    )
    return manifest


def build_atlas(spec: BuildSpec) -> BuildArtifact:
    """Build the normalized SQLite atlas and deterministic table exports."""

    root = Path(spec.root).resolve()
    default_raw = root / "data" / "raw"
    if spec.raw_dir is None and (default_raw / "current").is_dir():
        default_raw = default_raw / "current"
    raw_root = Path(spec.raw_dir or default_raw).resolve()
    requested_output = Path(spec.output_dir or root / "data" / "atlas")
    output_dir = (
        requested_output
        if requested_output.is_absolute()
        else Path.cwd() / requested_output
    ).absolute()
    if spec.universe_id != UNIVERSE_ID:
        raise ValueError(
            f"This POC only supports universe_id={UNIVERSE_ID}; got {spec.universe_id}"
        )
    if output_dir.exists() and not (
        (output_dir / "manifest.json").is_file()
        and (output_dir / "atlas.sqlite").is_file()
    ):
        raise FileExistsError(
            f"Refusing to replace non-atlas output directory: {output_dir}"
        )
    country_codes = tuple(dict.fromkeys(code.upper() for code in spec.countries))
    unknown = sorted(set(country_codes) - set(SUPPORTED_COUNTRIES))
    if unknown:
        raise ValueError(f"Unsupported country codes: {', '.join(unknown)}")

    eeml_path = _find_eeml_snapshot(raw_root / "who")
    eeml = _read_eeml(eeml_path)
    if list(eeml.columns) != EEML_COLUMNS:
        raise ValueError(
            f"eEML schema changed: expected {EEML_COLUMNS}, got {list(eeml.columns)}"
        )
    eeml = eeml.fillna("").astype(str).apply(
        lambda column: column.map(_canonical_eeml_cell)
    )
    statuses = set(eeml["Status"])
    unknown_statuses = sorted(statuses - {"Added", "Removed"})
    if unknown_statuses:
        raise ValueError(f"eEML contains unknown status values: {unknown_statuses}")
    active_eeml = eeml[eeml["Status"].str.strip().str.casefold().eq("added")].copy()
    if active_eeml.empty:
        raise ValueError("eEML snapshot has no Status=Added recommendation rows")
    active_eeml["normalized_ingredient_key"] = active_eeml["Medicine name"].map(
        normalize_ingredient
    )
    active_eeml["member_keys"] = active_eeml["Medicine name"].map(
        _eeml_medicine_components
    )
    active_eeml = active_eeml[active_eeml["member_keys"].map(bool)].copy()
    if active_eeml.empty:
        raise ValueError("eEML snapshot produced no normalized active medicine names")
    if eeml_path.suffix.lower() == ".xlsx" and not any(
        "semaglutide" in member_keys for member_keys in active_eeml["member_keys"]
    ):
        raise ValueError("eEML 2025 sentinel missing: semaglutide must have an Added row")
    active_eeml["_member_key_sort"] = active_eeml["member_keys"].map(
        lambda values: "\x1f".join(values)
    )
    active_eeml = active_eeml.sort_values(
        [*EEML_COLUMNS, "_member_key_sort"], kind="stable"
    ).drop(columns="_member_key_sort").reset_index(drop=True)

    adapters: dict[str, CountryAdapter] = {
        "US": FdaAdapter(),
        "SG": HsaAdapter(),
        "BD": BangladeshAdapter(),
        "BT": BhutanAdapter(),
    }
    raw_dirs = {
        "US": raw_root / "fda",
        "SG": raw_root / "hsa",
        "BD": raw_root / "bd",
        "BT": raw_root / "bt",
    }
    fetch_manifest = _validate_fetch_manifest(
        raw_root, spec.extraction_date, country_codes
    )
    if fetch_manifest is None and not spec.allow_unmanifested_test_fixture:
        raise FileNotFoundError(
            "A verified consolidated fetch manifest is required for accepted source snapshots. "
            "Run python -m src.fetch_sources first."
        )
    batches = {
        code: adapters[code].stage(raw_dirs[code], spec.extraction_date)
        for code in country_codes
    }
    for code, batch in batches.items():
        _validate_adapter_batch(code, batch)
    if fetch_manifest is not None:
        _validate_manifest_counts(fetch_manifest, batches)
    legacy_rows = _build_legacy_observations(raw_root)

    normalizer_path = Path(__file__).with_name("normalize.py")
    source_hashes = {
        code: _directory_content_hash(raw_dirs[code]) for code in country_codes
    }
    eeml_logical_hash = _dataframe_hash(eeml[EEML_COLUMNS])
    legacy_dependency_paths = {
        "pipeline": Path(__file__).with_name("pipeline.py"),
        "atc": raw_root / "who" / "atc.csv",
        "rare_drugs": raw_root / "Rare Drugs.xls",
    }
    legacy_dependency_hashes = {
        name: _file_hash(path) if path.exists() else "not-present"
        for name, path in legacy_dependency_paths.items()
    }
    fetch_manifest_path = raw_root / "manifest.json"
    fetch_manifest_hash = (
        _file_hash(fetch_manifest_path) if fetch_manifest is not None else "not-present"
    )
    build_identity = {
        "schema_version": SCHEMA_VERSION,
        "extraction_date": spec.extraction_date.isoformat(),
        "countries": list(country_codes),
        "universe_id": spec.universe_id,
        "normalizer_sha256": _file_hash(normalizer_path),
        "builder_sha256": _file_hash(Path(__file__)),
        "legacy_dependency_hashes": legacy_dependency_hashes,
        "fetch_manifest_sha256": fetch_manifest_hash,
        "source_hashes": source_hashes,
        "eeml_logical_sha256": eeml_logical_hash,
    }
    build_id = hashlib.sha256(
        json.dumps(build_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]

    table_frames = _build_table_frames(
        build_id=build_id,
        spec=spec,
        country_codes=country_codes,
        batches=batches,
        source_hashes=source_hashes,
        acceptance_reason=(
            "fetch_manifest_hash_count_floor_and_adapter_contract_validated"
            if fetch_manifest is not None
            else "unmanifested_test_fixture_explicitly_allowed"
        ),
        fetch_manifest=fetch_manifest,
        active_eeml=active_eeml,
        eeml_path=eeml_path,
        eeml_logical_hash=eeml_logical_hash,
        legacy_rows=legacy_rows,
    )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-{build_id}-", dir=output_dir.parent)
    )
    try:
        tables_dir = staging / "tables"
        tables_dir.mkdir(parents=True, exist_ok=True)
        table_paths: dict[str, Path] = {}
        table_hashes: dict[str, str] = {}
        for table_name, frame in table_frames.items():
            path = tables_dir / f"{table_name}.csv"
            canonical = _canonical_frame(frame)
            path.write_bytes(_csv_bytes(canonical))
            table_paths[table_name] = path
            table_hashes[table_name] = _file_hash(path)

        database_path = staging / "atlas.sqlite"
        _write_database(database_path, table_frames)
        with sqlite3.connect(database_path) as connection:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Atlas foreign-key violations: {violations[:5]}")

        view_paths, view_hashes, report_path, report_hash = _write_views_and_report(
            staging=staging,
            database_path=database_path,
            build_id=build_id,
            extraction_date=spec.extraction_date,
            country_codes=country_codes,
            universe_id=spec.universe_id,
        )

        manifest = {
            **build_identity,
            "build_id": build_id,
            "source_artifacts": {
                **{
                    code: {
                        "path": _display_path(raw_dirs[code], root),
                        "sha256": source_hashes[code],
                        "source_url": batches[code].policy.source_url,
                        "related_source_urls": batches[code].policy.related_source_urls,
                    }
                    for code in country_codes
                },
                "WHO_EML_2025": {
                    "path": _display_path(eeml_path, root),
                    "raw_sha256": (
                        _file_hash(eeml_path)
                        if fetch_manifest is not None
                        else "not-recorded-unmanifested-test-fixture"
                    ),
                    "logical_sha256": eeml_logical_hash,
                    "license": "CC BY 3.0 IGO",
                    "license_url": EEML_LICENSE_URL,
                    "status_filter": "Added",
                    "identity_column": "Medicine name",
                    "combined_with_semantics": "co-prescribed metadata; not an ingredient",
                    "edition": "24th list (2025)",
                },
                "legacy_compatibility_dependencies": {
                    name: {
                        "path": _display_path(path, root),
                        "sha256": legacy_dependency_hashes[name],
                        **LEGACY_DEPENDENCY_LICENSES[name],
                    }
                    for name, path in legacy_dependency_paths.items()
                },
            },
            "fetch_manifest": (
                {
                    "path": _display_path(fetch_manifest_path, root),
                    "sha256": fetch_manifest_hash,
                    "verified": True,
                }
                if fetch_manifest is not None
                else {
                    "path": "",
                    "sha256": "not-present",
                    "verified": False,
                }
            ),
            "table_hashes": table_hashes,
            "view_hashes": view_hashes,
            "data_quality_report_sha256": report_hash,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        published_dir = _publish_directory(staging, output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return BuildArtifact(
        build_id=build_id,
        current_directory=output_dir,
        database_path=published_dir / "atlas.sqlite",
        manifest_path=published_dir / "manifest.json",
        table_paths={
            name: published_dir / "tables" / f"{name}.csv" for name in table_frames
        },
        table_hashes=table_hashes,
        view_paths={
            name: published_dir / "views" / path.name for name, path in view_paths.items()
        },
        view_hashes=view_hashes,
        report_path=published_dir / report_path.name,
    )


def compare_atlas(
    database_path: Path,
    countries: tuple[str, ...],
    universe_id: str = UNIVERSE_ID,
    current_qualified_countries: tuple[str, ...] = (),
) -> ComparisonResult:
    """Compare any selected countries over an explicit substance universe."""

    selected = tuple(dict.fromkeys(code.upper() for code in countries))
    current_qualified = tuple(
        dict.fromkeys(code.upper() for code in current_qualified_countries)
    )
    if not selected:
        raise ValueError("At least one country must be selected")
    invalid_qualification = sorted(set(current_qualified) - set(selected))
    if invalid_qualification:
        raise ValueError(
            "Current-qualified countries must also be selected: "
            + ", ".join(invalid_qualification)
        )
    placeholders = ",".join("?" for _ in selected)
    with sqlite3.connect(database_path) as connection:
        available = {
            row[0]
            for row in connection.execute("SELECT country_code FROM countries").fetchall()
        }
        unknown = sorted(set(selected) - available)
        if unknown:
            raise ValueError(f"Countries are not tracked in this build: {', '.join(unknown)}")
        universe = pd.read_sql_query(
            """
            SELECT DISTINCT s.substance_id, s.preferred_name
            FROM essential_medicine_members AS member
            JOIN essential_medicine_entries AS entry USING (entry_id)
            JOIN substances AS s USING (substance_id)
            WHERE entry.universe_id = ?
            ORDER BY s.preferred_name
            """,
            connection,
            params=(universe_id,),
        )
        if universe.empty:
            raise ValueError(f"Universe has no members: {universe_id}")
        snapshots = pd.read_sql_query(
            f"""
            SELECT country_code, snapshot_status, acceptance_reason, source_name,
                   source_url, related_source_urls,
                   supports_current_qualification,
                   observed_absence_wording, coverage_scope
            FROM source_snapshots
            WHERE country_code IN ({placeholders})
            """,
            connection,
            params=selected,
        )
        support_for = snapshots.set_index("country_code")[
            "supports_current_qualification"
        ].to_dict()
        unsupported_qualification = sorted(
            code for code in current_qualified if not bool(support_for.get(code, False))
        )
        if unsupported_qualification:
            raise ValueError(
                "Current-qualified comparison is not supported for: "
                + ", ".join(unsupported_qualification)
            )
        observations = pd.read_sql_query(
            f"""
            SELECT rp.country_code, pi.substance_id, rp.product_id,
                   CASE WHEN rp.ingredient_component_count > 1 THEN 1 ELSE 0 END
                       AS is_combo,
                   rp.product_name, rp.current_qualified
            FROM registered_products AS rp
            JOIN product_ingredients AS pi USING (product_id)
            JOIN source_snapshots AS ss USING (snapshot_id)
            WHERE rp.country_code IN ({placeholders})
              AND rp.included_in_presence = 1
              AND ss.snapshot_status = 'accepted'
            """,
            connection,
            params=selected,
        )
        identity_uncertainties = pd.read_sql_query(
            f"""
            SELECT u.country_code,
                   u.target_substance_id,
                   u.candidate_substance_id,
                   candidate.preferred_name AS candidate_preferred_name,
                   u.relation,
                   u.match_method,
                   u.evidence_note,
                   rp.product_id,
                   rp.current_qualified
            FROM substance_identity_uncertainties AS u
            JOIN substances AS candidate
              ON candidate.substance_id = u.candidate_substance_id
            JOIN product_ingredients AS pi
              ON pi.substance_id = u.candidate_substance_id
            JOIN registered_products AS rp
              ON rp.product_id = pi.product_id
             AND rp.country_code = u.country_code
            JOIN source_snapshots AS ss USING (snapshot_id)
            WHERE u.universe_id = ?
              AND u.country_code IN ({placeholders})
              AND rp.included_in_presence = 1
              AND ss.snapshot_status = 'accepted'
            """,
            connection,
            params=(universe_id, *selected),
        ).drop_duplicates(
            [
                "country_code",
                "target_substance_id",
                "candidate_substance_id",
                "product_id",
            ]
        )

    snapshot_for = snapshots.set_index("country_code").to_dict("index")
    observation_groups = {
        (country_code, substance_id): group
        for (country_code, substance_id), group in observations.groupby(
            ["country_code", "substance_id"], sort=False
        )
    }
    uncertainty_groups = {
        (country_code, target_substance_id): group
        for (country_code, target_substance_id), group in identity_uncertainties.groupby(
            ["country_code", "target_substance_id"], sort=False
        )
    }
    long_rows: list[dict[str, object]] = []
    for substance in universe.itertuples(index=False):
        for country_code in selected:
            snapshot = snapshot_for.get(country_code)
            if snapshot is None or snapshot["snapshot_status"] != "accepted":
                observation = "UNKNOWN"
                uncertainty_reason = "snapshot_missing_or_rejected"
                standalone_count = 0
                combo_count = 0
                if snapshot is None:
                    evidence_note = "No source snapshot is available for this country."
                    source_name = ""
                    source_url = ""
                    coverage_scope = ""
                    snapshot_status = "missing"
                    acceptance_reason = "no_snapshot"
                else:
                    source_name = str(snapshot["source_name"])
                    source_url = str(snapshot["source_url"])
                    coverage_scope = str(snapshot["coverage_scope"])
                    snapshot_status = str(snapshot["snapshot_status"])
                    acceptance_reason = str(snapshot["acceptance_reason"])
                    evidence_note = (
                        "Source snapshot is not accepted for absence inference: "
                        f"{acceptance_reason}."
                    )
            else:
                group = observation_groups.get((country_code, substance.substance_id))
                identity_group = uncertainty_groups.get(
                    (country_code, substance.substance_id)
                )
                source_name = str(snapshot["source_name"])
                source_url = str(snapshot["source_url"])
                coverage_scope = str(snapshot["coverage_scope"])
                snapshot_status = str(snapshot["snapshot_status"])
                acceptance_reason = str(snapshot["acceptance_reason"])
                indeterminate_current = False
                identity_review_group = identity_group
                if country_code in current_qualified and group is not None and not group.empty:
                    qualified_group = group[group["current_qualified"].eq(1)].copy()
                    indeterminate_current = (
                        qualified_group.empty and group["current_qualified"].isna().any()
                    )
                    group = qualified_group
                if (
                    country_code in current_qualified
                    and identity_group is not None
                    and not identity_group.empty
                ):
                    qualified_identity_group = identity_group[
                        identity_group["current_qualified"].eq(1)
                    ].copy()
                    if qualified_identity_group.empty:
                        identity_review_group = identity_group[
                            identity_group["current_qualified"].isna()
                        ].copy()
                    else:
                        identity_review_group = qualified_identity_group
                if indeterminate_current:
                    observation = "UNKNOWN"
                    uncertainty_reason = "current_qualification_indeterminate"
                    standalone_count = 0
                    combo_count = 0
                    evidence_note = (
                        "Listed product evidence exists, but current qualification is "
                        "indeterminate for this substance because source evidence conflicts "
                        "or is incomplete."
                    )
                elif group is not None and not group.empty:
                    uncertainty_reason = ""
                    standalone_ids = set(
                        group.loc[group["is_combo"].eq(0), "product_id"].astype(str)
                    )
                    combo_ids = set(
                        group.loc[group["is_combo"].eq(1), "product_id"].astype(str)
                    )
                    standalone_count = len(standalone_ids)
                    combo_count = len(combo_ids)
                    observation = "STANDALONE" if standalone_count else "COMBO_ONLY"
                    evidence_note = (
                        f"Observed in {standalone_count + combo_count} product records from "
                        f"{source_name}: {standalone_count} standalone and {combo_count} combination."
                    )
                elif identity_review_group is not None and not identity_review_group.empty:
                    observation = "UNKNOWN"
                    uncertainty_reason = "identity_match_requires_review"
                    standalone_count = 0
                    combo_count = 0
                    candidate_names = sorted(
                        set(identity_review_group["candidate_preferred_name"].astype(str))
                    )
                    evidence_note = (
                        "No exact normalized identity match was observed, but the register "
                        "contains broader or more-specific normalized identities requiring "
                        f"review: {', '.join(candidate_names)}. Absence is indeterminate; "
                        "no equivalence or presence assertion is made."
                    )
                else:
                    observation = "OBSERVED_ABSENCE"
                    uncertainty_reason = ""
                    standalone_count = 0
                    combo_count = 0
                    evidence_note = str(snapshot["observed_absence_wording"])
                    if country_code in current_qualified:
                        evidence_note = (
                            "Not observed among current-qualified product records. "
                            + evidence_note
                        )
            long_rows.append(
                {
                    "substance_id": substance.substance_id,
                    "preferred_name": substance.preferred_name,
                    "country_code": country_code,
                    "observation": observation,
                    "uncertainty_reason": uncertainty_reason,
                    "standalone_product_count": standalone_count,
                    "combo_product_count": combo_count,
                    "evidence_note": evidence_note,
                    "source_name": source_name,
                    "source_url": source_url,
                    "coverage_scope": coverage_scope,
                    "snapshot_status": snapshot_status,
                    "acceptance_reason": acceptance_reason,
                    "presence_basis": (
                        "current_qualified"
                        if country_code in current_qualified
                        else "listed_register_presence"
                    ),
                }
            )
    long = pd.DataFrame(long_rows).sort_values(
        ["preferred_name", "country_code"], kind="stable"
    ).reset_index(drop=True)

    summary_rows: list[dict[str, object]] = []
    present_states = {"STANDALONE", "COMBO_ONLY"}
    for (substance_id, preferred_name), group in long.groupby(
        ["substance_id", "preferred_name"], sort=True
    ):
        accepted_count = int(group["snapshot_status"].eq("accepted").sum())
        determinate_count = int(group["observation"].ne("UNKNOWN").sum())
        present_count = int(group["observation"].isin(present_states).sum())
        summary_rows.append(
            {
                "substance_id": substance_id,
                "preferred_name": preferred_name,
                "selected_country_count": len(selected),
                "accepted_snapshot_count": accepted_count,
                "determinate_country_count": determinate_count,
                "present_country_count": present_count,
                "present_countries": "|".join(
                    code
                    for code in selected
                    if group.loc[group["country_code"].eq(code), "observation"]
                    .isin(present_states)
                    .any()
                ),
                "global_penetration": (
                    present_count / determinate_count if determinate_count else None
                ),
                "all_selected_present": (
                    determinate_count == len(selected) and present_count == len(selected)
                ),
                "observed_gap_country_count": int(
                    group["observation"].eq("OBSERVED_ABSENCE").sum()
                ),
                "observed_gap_countries": "|".join(
                    code
                    for code in selected
                    if group.loc[group["country_code"].eq(code), "observation"]
                    .eq("OBSERVED_ABSENCE")
                    .any()
                ),
                "unknown_country_count": int(group["observation"].eq("UNKNOWN").sum()),
                "unknown_countries": "|".join(
                    code
                    for code in selected
                    if group.loc[group["country_code"].eq(code), "observation"]
                    .eq("UNKNOWN")
                    .any()
                ),
            }
        )
    summary = pd.DataFrame(summary_rows).sort_values(
        "preferred_name", kind="stable"
    ).reset_index(drop=True)

    if len(selected) <= 4:
        wide = summary.copy()
        for country_code in selected:
            country = long[long["country_code"].eq(country_code)][
                [
                    "preferred_name",
                    "substance_id",
                    "observation",
                    "standalone_product_count",
                    "combo_product_count",
                    "evidence_note",
                ]
            ].rename(
                columns={
                    "observation": f"{country_code} Observation",
                    "standalone_product_count": f"{country_code} Standalone Product Count",
                    "combo_product_count": f"{country_code} Combo Product Count",
                    "evidence_note": f"{country_code} Evidence Note",
                }
            )
            wide = wide.merge(
                country, on=["substance_id", "preferred_name"], how="left", sort=False
            )
        wide = wide.sort_values("preferred_name", kind="stable").reset_index(drop=True)
    else:
        wide = pd.DataFrame()
    return ComparisonResult(long=long, summary=summary, wide=wide)


def render_legacy_compatibility(database_path: Path) -> pd.DataFrame:
    """Render the historical US by SG columns from NDA/BLA plus HSA rows."""

    with sqlite3.connect(database_path) as connection:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='legacy_compatibility_observations'"
        ).fetchone()
        if table_exists:
            rows = pd.read_sql_query(
                "SELECT * FROM legacy_compatibility_observations ORDER BY row_ordinal",
                connection,
            ).drop(columns="row_ordinal")
        else:
            rows = pd.DataFrame(columns=LONG_COLUMNS)
        if rows.empty:
            rows = _legacy_rows_from_atlas(connection)
        eeml_keys = {
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT s.normalized_ingredient_key
                FROM essential_medicine_members em
                JOIN essential_medicine_entries ee USING (entry_id)
                JOIN substances s USING (substance_id)
                WHERE ee.universe_id = ?
                """,
                (UNIVERSE_ID,),
            ).fetchall()
        }
    if "substance_key" in rows:
        rows["is_on_who_eml"] = rows["substance_key"].isin(eeml_keys)
    for boolean_column in ("is_combo", "is_rare", "is_on_who_eml"):
        if boolean_column in rows:
            rows[boolean_column] = rows[boolean_column].fillna(0).astype(bool)
    return assign_availability(rows[LONG_COLUMNS]).reindex(columns=OUTPUT_COLUMNS)


def _frame(rows: list[dict[str, object]], columns: list[str]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns)


def _validate_adapter_batch(country_code: str, batch: AdapterBatch) -> None:
    """Fail closed before a source can support observed-absence claims."""

    if batch.policy.country_code != country_code:
        raise ValueError(
            f"{country_code} adapter policy returned {batch.policy.country_code}"
        )
    if list(batch.products.columns) != PRODUCT_COLUMNS:
        raise ValueError(f"{country_code} product contract changed")
    if list(batch.ingredients.columns) != INGREDIENT_COLUMNS:
        raise ValueError(f"{country_code} ingredient contract changed")
    if list(batch.issues.columns) != ISSUE_COLUMNS:
        raise ValueError(f"{country_code} issue contract changed")
    if batch.products["source_product_key"].astype(str).duplicated().any():
        raise ValueError(f"{country_code} adapter emitted duplicate product keys")
    component_counts = pd.to_numeric(
        batch.products["ingredient_component_count"], errors="coerce"
    )
    unresolved_counts = pd.to_numeric(
        batch.products["unresolved_component_count"], errors="coerce"
    )
    if component_counts.isna().any() or component_counts.lt(0).any():
        raise ValueError(f"{country_code} adapter emitted invalid component counts")
    if (
        unresolved_counts.isna().any()
        or unresolved_counts.lt(0).any()
        or unresolved_counts.gt(component_counts).any()
    ):
        raise ValueError(f"{country_code} adapter emitted invalid unresolved counts")
    product_keys = set(batch.products["source_product_key"].astype(str))
    ingredient_product_keys = set(batch.ingredients["source_product_key"].astype(str))
    unknown_product_keys = sorted(ingredient_product_keys - product_keys)
    if unknown_product_keys:
        raise ValueError(
            f"{country_code} ingredients reference unknown products: {unknown_product_keys[:3]}"
        )
    if not batch.ingredients.empty:
        positions = batch.ingredients[["source_product_key", "position"]]
        if positions.duplicated().any():
            raise ValueError(f"{country_code} adapter emitted duplicate ingredient positions")
        if batch.ingredients["normalized_ingredient_key"].astype(str).map(len).lt(3).any():
            raise ValueError(f"{country_code} adapter emitted a short ingredient identity")
    declared = int(batch.metrics.get("declared_row_count", len(batch.products)))
    parsed = int(batch.metrics.get("parsed_row_count", len(batch.products)))
    if declared < parsed:
        raise ValueError(
            f"{country_code} parsed count exceeds declared count: {parsed} > {declared}"
        )
    if country_code in {"SG", "BD"} and declared != parsed:
        raise ValueError(
            f"{country_code} snapshot is incomplete: declared {declared}, parsed {parsed}"
        )


def _validate_manifest_counts(
    manifest: dict[str, object], batches: dict[str, AdapterBatch]
) -> None:
    records = manifest.get("artifacts") or {}
    count_fields = {
        "US": "product_row_count",
        "SG": "row_count",
        "BD": "row_count",
        "BT": "product_row_count",
    }
    for code, batch in batches.items():
        record = records.get(code) or {}
        field = count_fields[code]
        if field not in record:
            raise ValueError(f"Fetch manifest {code} record is missing {field}")
        manifest_count = int(record[field])
        adapter_count = int(
            batch.metrics.get("declared_row_count", len(batch.products))
        )
        if manifest_count != adapter_count:
            raise ValueError(
                f"Fetch manifest row count mismatch for {code}: "
                f"manifest {manifest_count}, adapter {adapter_count}"
            )
        minimum = MINIMUM_DECLARED_ROWS[code]
        if adapter_count < minimum:
            raise ValueError(
                f"{code} source count {adapter_count} is below the POC acceptance floor "
                f"of {minimum}; review the upstream source before accepting gaps"
            )


def _ingredient(
    country_code: str,
    product_key: str,
    position: int,
    raw_component: str,
    normalized: str,
    is_combo: bool,
    atc_code: str = "",
    match_method: str = "source_ingredient_normalization",
    raw_strength: str = "",
) -> dict[str, object]:
    return {
        "country_code": country_code,
        "source_product_key": product_key,
        "position": position,
        "raw_component": str(raw_component),
        "raw_strength": str(raw_strength),
        "normalized_ingredient_key": str(normalized),
        "is_combo": bool(is_combo),
        "atc_code": str(atc_code),
        "match_method": match_method,
    }


def _issue(
    country_code: str,
    product_key: str,
    issue_code: str,
    severity: str,
    detail: str,
) -> dict[str, object]:
    return {
        "country_code": country_code,
        "source_product_key": product_key,
        "issue_code": issue_code,
        "severity": severity,
        "detail": detail,
    }


def _split_south_asia_ingredients(value: object) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw in _south_asia_raw_components(value):
        normalized = _normalize_south_asia_component(raw)
        if len(normalized) >= 3 and _has_ingredient_signal(normalized):
            result.append((raw, normalized))
    return result


def _south_asia_raw_components(value: object) -> list[str]:
    text = "" if value is None else str(value).strip()
    if not text:
        return []
    text = re.sub(r"\bINN\b", " ", text, flags=re.IGNORECASE)
    name_segment = re.split(
        r"\s{2,}(?=[(<]*\.?\d)", text, maxsplit=1, flags=re.IGNORECASE
    )[0]
    raw_parts = re.split(
        r"\s*\+\s*|\s*;\s*|(?<!\d)\s*&\s*(?!\d)|"
        r"\s+(?i:and)\s+(?=[A-Za-z])|"
        r",\s*(?=(?!(?i:attenuated|live|inactivated|oral|freeze(?:-dried)?|"
        r"dried|equivalent|providing|corresponding|contains)\b)[A-Z])",
        name_segment,
    )
    result: list[str] = []
    for raw in raw_parts:
        if re.fullmatch(
            r"\s*[\d.,]+\s*(?:mcg|micrograms?|mg|milligrams?|g|grams?|ml|iu|units?)\s*",
            raw,
            flags=re.IGNORECASE,
        ):
            continue
        cleaned = raw.strip()
        if cleaned and _has_ingredient_signal(cleaned):
            result.append(cleaned)
    return result


def _normalize_south_asia_component(value: object) -> str:
    text = str(value)
    text = re.sub(r"\bmagnessium\b", "magnesium", text, flags=re.IGNORECASE)
    text = re.sub(r"\bvacine\b", "vaccine", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:I\s*\.\s*P|B\s*\.\s*P|U\s*\.\s*S\s*\.\s*P)\.?",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[- ]+\d+\s*doses?\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:eye|ophthalmic)\s+(drops?|ointments?|solutions?)\b",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b\d+(?:\.\d+)?\s*%\s*(?:w\s*/\s*v|v\s*/\s*v|w\s*/\s*w)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b(?:w\s*/\s*v|v\s*/\s*v|w\s*/\s*w)\b", " ", text, flags=re.IGNORECASE)
    return normalize_ingredient(text)


def _has_ingredient_signal(value: object) -> bool:
    tokens = re.findall(r"[A-Za-z]+", str(value).casefold())
    non_identity_tokens = {
        "g",
        "gm",
        "iu",
        "mcg",
        "mg",
        "microgram",
        "micrograms",
        "milligram",
        "milligrams",
        "ml",
        "unit",
        "units",
        "v",
        "w",
    }
    return any(token not in non_identity_tokens for token in tokens)


def _normalize_registration_number(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).strip().upper()
    normalized = re.sub(r"\s+", "", normalized)
    compact = re.sub(r"[^A-Z0-9]", "", normalized)
    if compact.startswith(("HUDRA", "HUMPD")):
        compact = f"B{compact}"
    return compact


def _parse_bhutan_action_date(value: str) -> tuple[pd.Timestamp | None, str]:
    text = value.strip()
    if not text:
        return None, "missing_action_date"
    iso_match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso_match:
        parsed = pd.to_datetime(text, format="%Y-%m-%d", errors="coerce")
        return (None, "invalid_action_date") if pd.isna(parsed) else (parsed, "")
    slash_match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not slash_match:
        return None, "invalid_action_date"
    first, second, year = (int(part) for part in slash_match.groups())
    if first <= 12 and second <= 12:
        return None, "ambiguous_action_date"
    if first > 12 and second <= 12:
        day, month = first, second
    elif second > 12 and first <= 12:
        month, day = first, second
    else:
        return None, "invalid_action_date"
    parsed = pd.to_datetime(f"{year:04d}-{month:02d}-{day:02d}", errors="coerce")
    return (None, "invalid_action_date") if pd.isna(parsed) else (parsed, "")


def _select_bhutan_generic_text(generic_name: object, brand_name: object) -> str:
    generic = str(generic_name).strip()
    brand = str(brand_name).strip()
    if (
        _looks_like_brand_in_generic_field(generic)
        and _ingredient_text_score(brand) > _ingredient_text_score(generic) + 2
    ):
        return brand
    return generic or brand


def _looks_like_brand_in_generic_field(value: str) -> bool:
    text = value.strip()
    if not text:
        return True
    if "®" in text or "™" in text:
        return True
    first_token = re.sub(r"[^A-Za-z]", "", text.split()[0]) if text.split() else ""
    if len(first_token) >= 5 and first_token.isupper():
        return True
    if (
        re.search(r"\d", text)
        and len(text.split()) <= 3
        and not re.match(r"^(?:vitamins?|calcium|iron|zinc|sodium|potassium)\b", text, re.I)
    ):
        return True
    return False


def _ingredient_text_score(value: str) -> int:
    lowered = value.lower()
    score = len(
        re.findall(
            r"\b(?:tablet|capsule|injection|infusion|suspension|solution|cream|"
            r"ointment|gel|drops?|paste|syrup)\b",
            lowered,
        )
    )
    score += 2 * len(re.findall(r"\b(?:mcg|microgram|mg|g|ml|iu)\b|%", lowered))
    score += 2 * len(re.findall(r"\b(?:bp|ip|usp|inn|ph\.?\s*eur)\b", lowered))
    score += 2 * len(re.findall(r"\b\w+(?:mab|vir|caine|gliptin|formin|flozin|"
                                r"prazole|sartan|statin|cillin|cycline|mycin|flurane|"
                                r"cellulose|oxazole|azole|dipine|setron|lukast)\b", lowered))
    score += 2 if re.search(r"\+|\s&\s|\sand\s", lowered) else 0
    score += 1 if len(value.split()) >= 3 else 0
    if lowered in {"na", "n/a", "nil", "none", ""}:
        score -= 10
    return score


def _iso_date(value: object) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.date().isoformat()


def _find_eeml_snapshot(who_dir: Path) -> Path:
    candidates = [
        who_dir / "eeml_2025.xlsx",
        who_dir / "eeml_2025.csv",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "A saved WHO eEML 2025 export is required at "
        "data/raw/who/eeml_2025.xlsx (or .csv for a test fixture)."
    )


def _eeml_medicine_components(value: object) -> list[str]:
    """Return active-moiety identities from an eEML Medicine name cell.

    Plus signs and spaced dashes in that column denote fixed-dose combination
    members.  The separate Combined with column is intentionally never passed
    here because it describes co-prescription, not medicine identity.
    """

    raw_parts = re.split(r"\s*\+\s*|\s+-\s+", str(value).strip())
    components: list[str] = []
    for raw_part in raw_parts:
        normalized = normalize_ingredient(raw_part)
        if len(normalized) >= 3 and normalized not in components:
            components.append(normalized)
    return components


def _build_legacy_observations(regulator_raw_root: Path) -> pd.DataFrame:
    """Reuse the delivered two-country logic as an explicit compatibility seam."""

    raw_dir = regulator_raw_root
    required = [
        raw_dir / "who" / "atc.csv",
        raw_dir / "Rare Drugs.xls",
        regulator_raw_root / "fda" / "Applications.txt",
        regulator_raw_root / "fda" / "Products.txt",
        regulator_raw_root / "fda" / "Submissions.txt",
        regulator_raw_root / "hsa" / "hsa_registered_therapeutic_products.csv",
    ]
    missing = [path for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Required legacy compatibility inputs are missing: "
            + ", ".join(str(path) for path in missing)
            + ". Supply WHO ATC and FDA Rare Drugs inputs to src.fetch_sources; "
            "the build will not publish an empty compatibility renderer."
        )

    atc = load_atc(raw_dir / "who" / "atc.csv")
    atc_l5_lookup = build_atc_l5_lookup(atc)
    atc_classes = build_atc_class_lookup(atc)
    combo_atc_codes = detect_combo_atc_codes(atc)
    rare_keys = load_rare_substance_keys(
        raw_dir / "Rare Drugs.xls", atc_l5_lookup, atc_classes
    )
    # The non-commercial WHO PDF is validation-only. The compatibility rows are
    # built with no PDF-derived flags, then the renderer applies the open 2025
    # eEML membership stored in this atlas.
    legacy_eml_terms: set[str] = set()
    fda_rows, _, _ = load_fda_product_substances(
        regulator_raw_root / "fda",
        atc_l5_lookup,
        atc_classes,
        rare_keys,
        legacy_eml_terms,
    )
    hsa_rows, _, _ = load_hsa_product_substances(
        regulator_raw_root / "hsa" / "hsa_registered_therapeutic_products.csv",
        atc_l5_lookup,
        atc_classes,
        combo_atc_codes,
        rare_keys,
        legacy_eml_terms,
    )
    rows = pd.concat([fda_rows, hsa_rows], ignore_index=True)
    rows = rows[~rows["substance_key"].fillna("").isin(PSEUDO_SUBSTANCES)].copy()
    rows = rows.drop_duplicates(
        ["source", "product_id", "substance_key", "atc_level5", "is_combo"]
    )[LONG_COLUMNS].reset_index(drop=True)
    rows["approval_date"] = rows["approval_date"].map(_iso_date)
    for column in LONG_COLUMNS:
        if column == "approval_date":
            continue
        if column in {"is_combo", "is_rare", "is_on_who_eml"}:
            rows[column] = rows[column].fillna(False).astype(bool)
        else:
            rows[column] = rows[column].fillna("").astype(str)
    return rows


def _legacy_rows_from_atlas(connection: sqlite3.Connection) -> pd.DataFrame:
    rows = pd.read_sql_query(
        """
        SELECT
            s.normalized_ingredient_key AS substance_key,
            CASE rp.country_code WHEN 'US' THEN 'FDA' ELSE 'HSA' END AS source,
            pi.atc_code AS atc_level5,
            '' AS "Therapeutic Class (L1)",
            '' AS "Drug Class (L2)",
            '' AS "Pharmacological Subgroup (L3)",
            '' AS "Chemical Subgroup (L4)",
            s.preferred_name AS "Substance (L5)",
            rp.product_id,
            rp.product_name,
            rp.approval_date,
            CASE WHEN rp.ingredient_component_count > 1 THEN 1 ELSE 0 END AS is_combo,
            0 AS is_rare,
            CASE WHEN EXISTS (
                SELECT 1
                FROM essential_medicine_members em
                JOIN essential_medicine_entries ee USING (entry_id)
                WHERE em.substance_id = pi.substance_id
                  AND ee.universe_id = 'WHO_EML_2025'
            ) THEN 1 ELSE 0 END AS is_on_who_eml,
            rp.observation_ordinal,
            pi.position
        FROM registered_products rp
        JOIN product_ingredients pi USING (product_id)
        JOIN substances s USING (substance_id)
        WHERE rp.included_in_presence = 1
          AND (
              (rp.country_code = 'US' AND rp.legacy_eligible = 1)
              OR rp.country_code = 'SG'
          )
        ORDER BY rp.country_code, rp.observation_ordinal, pi.position
        """,
        connection,
    )
    if rows.empty:
        return pd.DataFrame(columns=LONG_COLUMNS)
    rows["_name_key"] = rows["product_name"].str.lower().str.strip()
    rows = rows.drop_duplicates(
        ["_name_key", "substance_key", "atc_level5", "source"], keep="first"
    ).drop(columns=["_name_key", "observation_ordinal", "position"])
    rows = rows.drop_duplicates(
        ["source", "product_id", "substance_key", "atc_level5", "is_combo"]
    )
    return rows[LONG_COLUMNS].reset_index(drop=True)


def _read_eeml(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, dtype=str).fillna("")
    if path.suffix.lower() != ".xlsx":
        raise ValueError(f"Unsupported eEML snapshot format: {path.suffix}")
    return _read_basic_xlsx(path)


def _read_basic_xlsx(path: Path) -> pd.DataFrame:
    """Read the eEML workbook with the standard library.

    The official export is a simple one-sheet workbook.  Keeping this reader
    narrow avoids introducing an Excel engine merely to ingest seven text
    columns.
    """

    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns = {"x": main_ns}
    with zipfile.ZipFile(path) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        sheet_names = [
            sheet.attrib.get("name", "")
            for sheet in workbook.findall(".//x:sheets/x:sheet", ns)
        ]
        if sheet_names != ["Worksheet"]:
            raise ValueError(f"eEML workbook sheet changed: expected ['Worksheet'], got {sheet_names}")
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("x:si", ns):
                shared_strings.append(
                    "".join(node.text or "" for node in item.findall(".//x:t", ns))
                )
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in archive.namelist():
            raise ValueError("eEML workbook does not contain xl/worksheets/sheet1.xml")
        sheet = ElementTree.fromstring(archive.read(sheet_name))

    rows: list[list[str]] = []
    for row_node in sheet.findall(".//x:sheetData/x:row", ns):
        cells: dict[int, str] = {}
        for cell in row_node.findall("x:c", ns):
            reference = cell.attrib.get("r", "A1")
            column_letters = re.match(r"[A-Z]+", reference)
            if not column_letters:
                continue
            column_index = _excel_column_index(column_letters.group(0))
            cell_type = cell.attrib.get("t", "")
            if cell_type == "inlineStr":
                value = "".join(
                    node.text or "" for node in cell.findall(".//x:is/x:t", ns)
                )
            else:
                value_node = cell.find("x:v", ns)
                value = value_node.text if value_node is not None and value_node.text else ""
                if cell_type == "s" and value:
                    value = shared_strings[int(value)]
            cells[column_index] = value
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(index, "") for index in range(width)])
    if not rows:
        raise ValueError("eEML workbook contains no rows")
    headers = rows[0]
    return pd.DataFrame(
        [row + [""] * (len(headers) - len(row)) for row in rows[1:]],
        columns=headers,
    ).fillna("")


def _canonical_eeml_cell(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value))
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _excel_column_index(letters: str) -> int:
    result = 0
    for letter in letters:
        result = result * 26 + (ord(letter) - ord("A") + 1)
    return result - 1


def _build_table_frames(
    *,
    build_id: str,
    spec: BuildSpec,
    country_codes: tuple[str, ...],
    batches: dict[str, AdapterBatch],
    source_hashes: dict[str, str],
    acceptance_reason: str,
    fetch_manifest: dict[str, object] | None,
    active_eeml: pd.DataFrame,
    eeml_path: Path,
    eeml_logical_hash: str,
    legacy_rows: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    country_names = {
        "US": "United States",
        "SG": "Singapore",
        "BD": "Bangladesh",
        "BT": "Bhutan",
    }
    countries = pd.DataFrame(
        [
            {"country_code": code, "country_name": country_names[code]}
            for code in country_codes
        ]
    )
    build_runs = pd.DataFrame(
        [
            {
                "build_id": build_id,
                "extraction_date": spec.extraction_date.isoformat(),
                "schema_version": SCHEMA_VERSION,
                "universe_id": spec.universe_id,
            }
        ]
    )

    snapshot_rows: list[dict[str, object]] = []
    product_frames: list[pd.DataFrame] = []
    ingredient_frames: list[pd.DataFrame] = []
    issue_frames: list[pd.DataFrame] = []
    for code in country_codes:
        batch = batches[code]
        manifest_record = (
            ((fetch_manifest.get("artifacts") or {}).get(code) or {})
            if fetch_manifest is not None
            else {}
        )
        snapshot_id = _stable_id("snapshot", build_id, code)
        captured_on_date = str(
            manifest_record.get("captured_on")
            or batch.metrics.get("captured_on_date")
            or "unknown"
        )
        snapshot_rows.append(
            {
                "snapshot_id": snapshot_id,
                "build_id": build_id,
                "country_code": code,
                "source_name": batch.policy.source_name,
                "source_url": batch.policy.source_url,
                "related_source_urls": batch.policy.related_source_urls,
                "supports_current_qualification": batch.policy.supports_current_qualification,
                "snapshot_status": "accepted",
                "acceptance_reason": acceptance_reason,
                "extraction_date": spec.extraction_date.isoformat(),
                "captured_on_date": captured_on_date,
                "source_as_of_date": str(batch.metrics.get("source_as_of_date") or "unknown"),
                "coverage_scope": batch.policy.coverage_scope,
                "observed_absence_wording": batch.policy.observed_absence_wording,
                "status_semantics": batch.policy.status_semantics,
                "license_name": batch.policy.license_name,
                "license_url": batch.policy.license_url,
                "license_status": batch.policy.license_status,
                "attribution": batch.policy.attribution.format(
                    access_date=(
                        captured_on_date
                        if captured_on_date != "unknown"
                        else spec.extraction_date.isoformat()
                    )
                ),
                "declared_row_count": int(batch.metrics.get("declared_row_count", len(batch.products))),
                "parsed_row_count": int(batch.metrics.get("parsed_row_count", len(batch.products))),
                "artifact_sha256": source_hashes[code],
            }
        )

        products = batch.products.copy().reset_index(drop=True)
        products.insert(0, "snapshot_id", snapshot_id)
        products.insert(
            0,
            "product_id",
            [
                _stable_id(
                    "product",
                    code,
                    row.source_product_key,
                )
                for row in products.itertuples(index=False)
            ],
        )
        product_frames.append(products)

        product_id_for = {
            (row.country_code, row.source_product_key): row.product_id
            for row in products.itertuples(index=False)
        }
        ingredients = batch.ingredients.copy().reset_index(drop=True)
        if not ingredients.empty:
            ingredients.insert(
                0,
                "product_id",
                [
                    product_id_for[(row.country_code, row.source_product_key)]
                    for row in ingredients.itertuples(index=False)
                ],
            )
        ingredient_frames.append(ingredients)

        issues = batch.issues.copy().reset_index(drop=True)
        if not issues.empty:
            issues.insert(0, "snapshot_id", snapshot_id)
            issues.insert(
                0,
                "issue_id",
                [
                    _stable_id(
                        "issue",
                        code,
                        row.source_product_key,
                        row.issue_code,
                        index,
                    )
                    for index, row in enumerate(issues.itertuples(index=False))
                ],
            )
        issue_frames.append(issues)

    source_snapshots = pd.DataFrame(snapshot_rows)
    registered_products = pd.concat(product_frames, ignore_index=True)
    staged_ingredients = pd.concat(ingredient_frames, ignore_index=True)
    staged_issues = (
        pd.concat(issue_frames, ignore_index=True)
        if any(not frame.empty for frame in issue_frames)
        else pd.DataFrame(columns=["issue_id", "snapshot_id", *ISSUE_COLUMNS])
    )

    product_substance_keys = set(
        staged_ingredients["normalized_ingredient_key"].dropna().astype(str)
    )
    eeml_substance_keys = {
        key for keys in active_eeml["member_keys"] for key in keys
    }
    all_substance_keys = sorted(
        key for key in product_substance_keys | eeml_substance_keys if key
    )
    substance_id_for = {
        key: _stable_id("substance", SUBSTANCE_IDENTITY_VERSION, key)
        for key in all_substance_keys
    }
    substances = pd.DataFrame(
        [
            {
                "substance_id": substance_id_for[key],
                "normalized_ingredient_key": key,
                "preferred_name": key,
                "unii": "",
                "identity_basis": "normalized_ingredient_key_poc",
            }
            for key in all_substance_keys
        ]
    )

    product_ingredients = staged_ingredients.copy()
    if product_ingredients.empty:
        product_ingredients = pd.DataFrame(
            columns=[
                "product_ingredient_id",
                "product_id",
                "substance_id",
                "position",
                "raw_component",
                "raw_strength",
                "atc_code",
                "match_method",
            ]
        )
    else:
        product_ingredients.insert(
            0,
            "substance_id",
            product_ingredients["normalized_ingredient_key"].map(substance_id_for),
        )
        product_ingredients.insert(
            0,
            "product_ingredient_id",
            [
                _stable_id("product-ingredient", row.product_id, row.position, row.substance_id)
                for row in product_ingredients.itertuples(index=False)
            ],
        )
        product_ingredients = product_ingredients.drop(
            columns=[
                "country_code",
                "source_product_key",
                "normalized_ingredient_key",
                "is_combo",
            ]
        )

    essential_medicine_sets = pd.DataFrame(
        [
            {
                "universe_id": spec.universe_id,
                "title": "WHO Model List of Essential Medicines, 24th list (2025)",
                "edition": "24",
                "publication_year": 2025,
                "source_name": "WHO electronic Essential Medicines List",
                "source_artifact": eeml_path.name,
                "source_url": EEML_SOURCE_URL,
                "source_logical_sha256": eeml_logical_hash,
                "license": "CC BY 3.0 IGO",
                "license_url": EEML_LICENSE_URL,
                "attribution": (
                    "WHO electronic Essential Medicines List (eEML), World Health "
                    "Organization, 2020. https://list.essentialmeds.org/ (beta version 1.0). "
                    "Licence: CC BY 3.0 IGO."
                ),
                "adaptation_notice": WHO_ADAPTATION_NOTICE,
            }
        ]
    )
    entry_rows: list[dict[str, object]] = []
    member_rows: list[dict[str, object]] = []
    for source_ordinal, (_, row) in enumerate(active_eeml.reset_index(drop=True).iterrows()):
        entry_key = str(row["normalized_ingredient_key"])
        entry_id = _stable_id("eml-entry", spec.universe_id, source_ordinal, entry_key)
        entry_rows.append(
            {
                "entry_id": entry_id,
                "universe_id": spec.universe_id,
                "source_ordinal": source_ordinal,
                "medicine_name": str(row.get("Medicine name", "")),
                "eml_section": str(row.get("EML section", "")),
                "formulations": str(row.get("Formulations", "")),
                "indication": str(row.get("Indication", "")),
                "atc_codes": str(row.get("ATC codes", "")),
                "combined_with": str(row.get("Combined with", "")),
                "source_status": str(row.get("Status", "")),
            }
        )
        for key in row["member_keys"]:
            member_rows.append(
                {
                    "entry_id": entry_id,
                    "substance_id": substance_id_for[key],
                    "member_role": "medicine_name",
                }
            )
    essential_medicine_entries = pd.DataFrame(entry_rows)
    essential_medicine_members = pd.DataFrame(member_rows).drop_duplicates(
        ["entry_id", "substance_id", "member_role"]
    )

    listed_source_identities = staged_ingredients.merge(
        registered_products[
            [
                "country_code",
                "source_product_key",
                "product_name",
                "raw_ingredient_text",
                "included_in_presence",
            ]
        ],
        on=["country_code", "source_product_key"],
        how="inner",
        sort=False,
    )
    listed_source_identities = listed_source_identities[
        listed_source_identities["included_in_presence"].eq(True)  # noqa: E712
    ]
    identity_uncertainty_rows: list[dict[str, object]] = []
    seen_identity_uncertainties: set[tuple[str, str, str, str]] = set()
    for country_code in country_codes:
        country_source_identities = listed_source_identities.loc[
            listed_source_identities["country_code"].eq(country_code)
        ]
        candidate_keys = sorted(
            set(country_source_identities["normalized_ingredient_key"].astype(str))
        )
        for target_key in sorted(eeml_substance_keys):
            for candidate_key in candidate_keys:
                relation = _identity_uncertainty_relation(target_key, candidate_key)
                if not relation:
                    continue
                uncertainty_key = (
                    country_code,
                    target_key,
                    candidate_key,
                    relation,
                )
                if uncertainty_key in seen_identity_uncertainties:
                    continue
                seen_identity_uncertainties.add(uncertainty_key)
                identity_uncertainty_rows.append(
                    {
                        "uncertainty_id": _stable_id(
                            "identity-uncertainty",
                            SUBSTANCE_IDENTITY_VERSION,
                            spec.universe_id,
                            country_code,
                            target_key,
                            candidate_key,
                            relation,
                        ),
                        "universe_id": spec.universe_id,
                        "country_code": country_code,
                        "target_substance_id": substance_id_for[target_key],
                        "candidate_substance_id": substance_id_for[candidate_key],
                        "relation": relation,
                        "match_method": f"{relation}_review_required",
                        "evidence_note": (
                            f"EML identity '{target_key}' and listed source identity "
                            f"'{candidate_key}' have a review-required identity relationship. "
                            "This is a review hold, not an equivalence or presence assertion."
                        ),
                    }
                )
            if target_key not in VACCINE_PRODUCT_FAMILY_MARKERS:
                continue
            for product in country_source_identities.itertuples(index=False):
                if not _vaccine_product_family_match(
                    target_key, product.product_name, product.raw_ingredient_text
                ):
                    continue
                candidate_key = str(product.normalized_ingredient_key)
                family_markers = VACCINE_PRODUCT_FAMILY_MARKERS[target_key]
                if candidate_key == target_key or not any(
                    marker in candidate_key for marker in family_markers
                ):
                    continue
                relation = "source_identity_product_vaccine_family"
                uncertainty_key = (
                    country_code,
                    target_key,
                    candidate_key,
                    relation,
                )
                if uncertainty_key in seen_identity_uncertainties:
                    continue
                seen_identity_uncertainties.add(uncertainty_key)
                identity_uncertainty_rows.append(
                    {
                        "uncertainty_id": _stable_id(
                            "identity-uncertainty",
                            SUBSTANCE_IDENTITY_VERSION,
                            spec.universe_id,
                            country_code,
                            target_key,
                            candidate_key,
                            relation,
                        ),
                        "universe_id": spec.universe_id,
                        "country_code": country_code,
                        "target_substance_id": substance_id_for[target_key],
                        "candidate_substance_id": substance_id_for[candidate_key],
                        "relation": relation,
                        "match_method": "reviewed_vaccine_product_family_hold",
                        "evidence_note": (
                            f"EML identity '{target_key}' has review-required vaccine-family "
                            f"evidence in listed product '{product.product_name}' through source "
                            f"identity '{candidate_key}'. This is a review hold, not an "
                            "equivalence or presence assertion."
                        ),
                    }
                )
    substance_identity_uncertainties = pd.DataFrame(
        identity_uncertainty_rows,
        columns=[
            "uncertainty_id",
            "universe_id",
            "country_code",
            "target_substance_id",
            "candidate_substance_id",
            "relation",
            "match_method",
            "evidence_note",
        ],
    )
    legacy_compatibility_observations = legacy_rows.copy().reset_index(drop=True)
    legacy_compatibility_observations.insert(
        0, "row_ordinal", range(len(legacy_compatibility_observations))
    )

    return {
        "build_runs": build_runs,
        "countries": countries,
        "source_snapshots": source_snapshots,
        "substances": substances,
        "registered_products": registered_products,
        "product_ingredients": product_ingredients,
        "essential_medicine_sets": essential_medicine_sets,
        "essential_medicine_entries": essential_medicine_entries,
        "essential_medicine_members": essential_medicine_members,
        "substance_identity_uncertainties": substance_identity_uncertainties,
        "ingest_issues": staged_issues,
        "legacy_compatibility_observations": legacy_compatibility_observations,
    }


def _write_database(path: Path, frames: dict[str, pd.DataFrame]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE build_runs (
                build_id TEXT PRIMARY KEY,
                extraction_date TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                universe_id TEXT NOT NULL
            );
            CREATE TABLE countries (
                country_code TEXT PRIMARY KEY,
                country_name TEXT NOT NULL
            );
            CREATE TABLE source_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                build_id TEXT NOT NULL REFERENCES build_runs(build_id),
                country_code TEXT NOT NULL REFERENCES countries(country_code),
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                related_source_urls TEXT NOT NULL,
                supports_current_qualification INTEGER NOT NULL
                    CHECK (supports_current_qualification IN (0, 1)),
                snapshot_status TEXT NOT NULL CHECK (snapshot_status IN ('accepted', 'rejected')),
                acceptance_reason TEXT NOT NULL,
                extraction_date TEXT NOT NULL,
                captured_on_date TEXT NOT NULL,
                source_as_of_date TEXT NOT NULL,
                coverage_scope TEXT NOT NULL,
                observed_absence_wording TEXT NOT NULL,
                status_semantics TEXT NOT NULL,
                license_name TEXT NOT NULL,
                license_url TEXT NOT NULL,
                license_status TEXT NOT NULL,
                attribution TEXT NOT NULL,
                declared_row_count INTEGER NOT NULL,
                parsed_row_count INTEGER NOT NULL,
                artifact_sha256 TEXT NOT NULL
                ,UNIQUE (build_id, country_code)
                ,UNIQUE (snapshot_id, country_code)
            );
            CREATE TABLE substances (
                substance_id TEXT PRIMARY KEY,
                normalized_ingredient_key TEXT NOT NULL UNIQUE,
                preferred_name TEXT NOT NULL,
                unii TEXT NOT NULL,
                identity_basis TEXT NOT NULL
            );
            CREATE TABLE registered_products (
                product_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
                country_code TEXT NOT NULL REFERENCES countries(country_code),
                source_product_key TEXT NOT NULL,
                registration_number TEXT NOT NULL,
                product_name TEXT NOT NULL,
                raw_ingredient_text TEXT NOT NULL,
                ingredient_component_count INTEGER NOT NULL
                    CHECK (ingredient_component_count >= 0),
                unresolved_component_count INTEGER NOT NULL
                    CHECK (unresolved_component_count >= 0
                           AND unresolved_component_count <= ingredient_component_count),
                application_type TEXT NOT NULL,
                legacy_eligible INTEGER NOT NULL CHECK (legacy_eligible IN (0, 1)),
                observation_ordinal INTEGER NOT NULL,
                form TEXT NOT NULL,
                strength TEXT NOT NULL,
                sponsor TEXT NOT NULL,
                approval_date TEXT NOT NULL,
                validity_date TEXT NOT NULL,
                included_in_presence INTEGER NOT NULL CHECK (included_in_presence IN (0, 1)),
                current_qualified INTEGER CHECK (current_qualified IN (0, 1)),
                exclusion_reason TEXT NOT NULL,
                marketing_status TEXT NOT NULL,
                source_retired INTEGER CHECK (source_retired IN (0, 1)),
                UNIQUE (snapshot_id, source_product_key),
                FOREIGN KEY (snapshot_id, country_code)
                    REFERENCES source_snapshots(snapshot_id, country_code)
            );
            CREATE INDEX registered_products_country_idx
                ON registered_products(country_code, included_in_presence);
            CREATE TABLE product_ingredients (
                product_ingredient_id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES registered_products(product_id),
                substance_id TEXT NOT NULL REFERENCES substances(substance_id),
                position INTEGER NOT NULL,
                raw_component TEXT NOT NULL,
                raw_strength TEXT NOT NULL,
                atc_code TEXT NOT NULL,
                match_method TEXT NOT NULL,
                UNIQUE (product_id, position)
            );
            CREATE INDEX product_ingredients_substance_idx
                ON product_ingredients(substance_id, product_id);
            CREATE TABLE essential_medicine_sets (
                universe_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                edition TEXT NOT NULL,
                publication_year INTEGER NOT NULL,
                source_name TEXT NOT NULL,
                source_artifact TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_logical_sha256 TEXT NOT NULL,
                license TEXT NOT NULL,
                license_url TEXT NOT NULL,
                attribution TEXT NOT NULL,
                adaptation_notice TEXT NOT NULL
            );
            CREATE TABLE essential_medicine_entries (
                entry_id TEXT PRIMARY KEY,
                universe_id TEXT NOT NULL REFERENCES essential_medicine_sets(universe_id),
                source_ordinal INTEGER NOT NULL,
                medicine_name TEXT NOT NULL,
                eml_section TEXT NOT NULL,
                formulations TEXT NOT NULL,
                indication TEXT NOT NULL,
                atc_codes TEXT NOT NULL,
                combined_with TEXT NOT NULL,
                source_status TEXT NOT NULL,
                UNIQUE (universe_id, source_ordinal)
            );
            CREATE TABLE essential_medicine_members (
                entry_id TEXT NOT NULL REFERENCES essential_medicine_entries(entry_id),
                substance_id TEXT NOT NULL REFERENCES substances(substance_id),
                member_role TEXT NOT NULL,
                PRIMARY KEY (entry_id, substance_id, member_role)
            );
            CREATE TABLE substance_identity_uncertainties (
                uncertainty_id TEXT PRIMARY KEY,
                universe_id TEXT NOT NULL REFERENCES essential_medicine_sets(universe_id),
                country_code TEXT NOT NULL REFERENCES countries(country_code),
                target_substance_id TEXT NOT NULL REFERENCES substances(substance_id),
                candidate_substance_id TEXT NOT NULL REFERENCES substances(substance_id),
                relation TEXT NOT NULL CHECK (
                    relation IN (
                        'source_identity_more_specific',
                        'source_identity_broader',
                        'source_identity_vaccine_variant',
                        'source_identity_product_vaccine_family',
                        'source_identity_acronym_variant',
                        'source_identity_spelling_variant'
                    )
                ),
                match_method TEXT NOT NULL,
                evidence_note TEXT NOT NULL,
                UNIQUE (
                    universe_id,
                    country_code,
                    target_substance_id,
                    candidate_substance_id,
                    match_method
                )
            );
            CREATE INDEX substance_identity_uncertainties_lookup_idx
                ON substance_identity_uncertainties(
                    universe_id, country_code, target_substance_id
                );
            CREATE TABLE ingest_issues (
                issue_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
                country_code TEXT NOT NULL REFERENCES countries(country_code),
                source_product_key TEXT NOT NULL,
                issue_code TEXT NOT NULL,
                severity TEXT NOT NULL,
                detail TEXT NOT NULL,
                FOREIGN KEY (snapshot_id, country_code)
                    REFERENCES source_snapshots(snapshot_id, country_code)
            );
            CREATE TABLE legacy_compatibility_observations (
                row_ordinal INTEGER PRIMARY KEY,
                substance_key TEXT NOT NULL,
                source TEXT NOT NULL,
                atc_level5 TEXT NOT NULL,
                "Therapeutic Class (L1)" TEXT NOT NULL,
                "Drug Class (L2)" TEXT NOT NULL,
                "Pharmacological Subgroup (L3)" TEXT NOT NULL,
                "Chemical Subgroup (L4)" TEXT NOT NULL,
                "Substance (L5)" TEXT NOT NULL,
                product_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                approval_date TEXT,
                is_combo INTEGER NOT NULL CHECK (is_combo IN (0, 1)),
                is_rare INTEGER NOT NULL CHECK (is_rare IN (0, 1)),
                is_on_who_eml INTEGER NOT NULL CHECK (is_on_who_eml IN (0, 1))
            );
            """
        )
        for table_name, frame in frames.items():
            frame.to_sql(table_name, connection, if_exists="append", index=False)
        connection.commit()


def _write_views_and_report(
    *,
    staging: Path,
    database_path: Path,
    build_id: str,
    extraction_date: date,
    country_codes: tuple[str, ...],
    universe_id: str,
) -> tuple[dict[str, Path], dict[str, str], Path, str]:
    comparison = compare_atlas(database_path, country_codes, universe_id)
    current_comparison = (
        compare_atlas(
            database_path,
            country_codes,
            universe_id,
            current_qualified_countries=("BT",),
        )
        if "BT" in country_codes
        else None
    )
    bd_bt_comparison = (
        compare_atlas(database_path, ("BD", "BT"), universe_id)
        if {"BD", "BT"}.issubset(country_codes)
        else None
    )
    bd_bt_current_comparison = (
        compare_atlas(
            database_path,
            ("BD", "BT"),
            universe_id,
            current_qualified_countries=("BT",),
        )
        if bd_bt_comparison is not None
        else None
    )
    legacy = render_legacy_compatibility(database_path)
    views = {
        "eml_presence_long": comparison.long,
        "eml_comparison_summary": comparison.summary,
        "eml_comparison_wide": comparison.wide,
        "us_sg_legacy_compatibility": legacy,
    }
    if current_comparison is not None:
        views.update(
            {
                "eml_presence_long_bt_current_qualified": current_comparison.long,
                "eml_comparison_summary_bt_current_qualified": current_comparison.summary,
                "eml_comparison_wide_bt_current_qualified": current_comparison.wide,
            }
        )
    if bd_bt_comparison is not None and bd_bt_current_comparison is not None:
        views.update(
            {
                "bd_bt_eml_presence_long": bd_bt_comparison.long,
                "bd_bt_eml_comparison_summary": bd_bt_comparison.summary,
                "bd_bt_eml_comparison_wide": bd_bt_comparison.wide,
                "bd_bt_eml_presence_long_bt_current_qualified": bd_bt_current_comparison.long,
                "bd_bt_eml_comparison_summary_bt_current_qualified": bd_bt_current_comparison.summary,
                "bd_bt_eml_comparison_wide_bt_current_qualified": bd_bt_current_comparison.wide,
            }
        )
    views_dir = staging / "views"
    views_dir.mkdir(parents=True, exist_ok=True)
    view_paths: dict[str, Path] = {}
    view_hashes: dict[str, str] = {}
    for name, frame in views.items():
        path = views_dir / f"{name}.csv"
        path.write_bytes(_csv_bytes(frame.reset_index(drop=True)))
        view_paths[name] = path
        view_hashes[name] = _file_hash(path)

    with sqlite3.connect(database_path) as connection:
        product_counts = pd.read_sql_query(
            """
            SELECT
                rp.country_code,
                COUNT(DISTINCT rp.product_id) AS stored_product_records,
                COUNT(DISTINCT CASE WHEN rp.included_in_presence = 1
                    THEN rp.product_id END) AS listed_product_records,
                COUNT(DISTINCT CASE WHEN rp.included_in_presence = 1
                    THEN pi.substance_id END) AS distinct_listed_substances,
                COUNT(DISTINCT CASE WHEN rp.current_qualified = 1
                    THEN rp.product_id END)
                    AS current_qualified_records
            FROM registered_products rp
            LEFT JOIN product_ingredients pi USING (product_id)
            GROUP BY rp.country_code
            """,
            connection,
        ).set_index("country_code")
        snapshot_rows = pd.read_sql_query(
            """
            SELECT country_code, source_name, source_url, declared_row_count, parsed_row_count,
                   captured_on_date, source_as_of_date, snapshot_status, acceptance_reason,
                   coverage_scope, observed_absence_wording, status_semantics,
                   license_name, license_url, license_status, attribution
            FROM source_snapshots
            """,
            connection,
        ).set_index("country_code")
        issue_counts = pd.read_sql_query(
            """
            SELECT country_code, issue_code, COUNT(*) AS issue_count
            FROM ingest_issues
            GROUP BY country_code, issue_code
            ORDER BY country_code, issue_count DESC, issue_code
            """,
            connection,
        )
        identity_candidate_counts = pd.read_sql_query(
            """
            SELECT country_code,
                   COUNT(*) AS candidate_relationships
            FROM substance_identity_uncertainties
            GROUP BY country_code
            ORDER BY country_code
            """,
            connection,
        )

    overlap_count = int(comparison.summary["all_selected_present"].sum())
    current_overlap_count = (
        int(current_comparison.summary["all_selected_present"].sum())
        if current_comparison is not None
        else None
    )
    bd_bt_overlap_count = (
        int(bd_bt_comparison.summary["all_selected_present"].sum())
        if bd_bt_comparison is not None
        else None
    )
    bd_bt_current_overlap_count = (
        int(bd_bt_current_comparison.summary["all_selected_present"].sum())
        if bd_bt_current_comparison is not None
        else None
    )
    bt_screened_out: list[str] = []
    bt_current_unknown: list[str] = []
    if current_comparison is not None:
        listed_all = set(
            comparison.summary.loc[
                comparison.summary["all_selected_present"], "preferred_name"
            ].astype(str)
        )
        current_all = set(
            current_comparison.summary.loc[
                current_comparison.summary["all_selected_present"], "preferred_name"
            ].astype(str)
        )
        bt_screened_out = sorted(listed_all - current_all)
        bt_current_unknown = sorted(
            current_comparison.long.loc[
                current_comparison.long["country_code"].eq("BT")
                & current_comparison.long["uncertainty_reason"].eq(
                    "current_qualification_indeterminate"
                ),
                "preferred_name",
            ].astype(str)
        )
    universe_count = len(comparison.summary)
    report_lines = [
        "# N-country EML POC build report",
        "",
        f"- Build ID: `{build_id}`",
        f"- Extraction date: `{extraction_date.isoformat()}`",
        f"- Universe: `{universe_id}` ({universe_count} normalized medicine identities)",
        f"- Listed in all {len(country_codes)} selected country snapshots: **{overlap_count}**",
        *(
            [
                "- Present in all selected countries when Bhutan is restricted to "
                f"current-qualified records: **{current_overlap_count}**"
            ]
            if current_overlap_count is not None
            else []
        ),
        *(
            [
                f"- Bangladesh–Bhutan listed EML overlap: **{bd_bt_overlap_count}**",
                "- Bangladesh–Bhutan overlap with Bhutan current-qualified: "
                f"**{bd_bt_current_overlap_count}**",
            ]
            if bd_bt_overlap_count is not None
            else []
        ),
        "- Primary comparison basis: listed presence in each accepted source snapshot.",
        "- The Bhutan current-qualified comparison is a separate sensitivity view. It is "
        "not symmetric with Bangladesh, whose source has no reliable current-status fields.",
        *(
            [
                "- Four-country listed identities screened out by Bhutan current qualification: "
                + ", ".join(bt_screened_out)
            ]
            if bt_screened_out
            else []
        ),
        *(
            [
                "- Bhutan current status remains indeterminate for: "
                + ", ".join(bt_current_unknown)
            ]
            if bt_current_unknown
            else []
        ),
        "",
        "## Per-country counts",
        "",
        "| Country | Captured on | Source as of | Source rows declared | Normalized product records | Listed product records | Distinct listed substances | EML standalone | EML combo-only | EML observed absence | Current-qualified records |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for code in country_codes:
        products = product_counts.loc[code]
        snapshot = snapshot_rows.loc[code]
        statuses = comparison.long[
            comparison.long["country_code"].eq(code)
        ]["observation"].value_counts()
        current_qualified = int(products["current_qualified_records"])
        current_display = str(current_qualified) if code == "BT" else "n/a"
        report_lines.append(
            f"| {code} | {snapshot['captured_on_date']} | {snapshot['source_as_of_date']} | "
            f"{int(snapshot['declared_row_count'])} | "
            f"{int(products['stored_product_records'])} | "
            f"{int(products['listed_product_records'])} | "
            f"{int(products['distinct_listed_substances'])} | "
            f"{int(statuses.get('STANDALONE', 0))} | "
            f"{int(statuses.get('COMBO_ONLY', 0))} | "
            f"{int(statuses.get('OBSERVED_ABSENCE', 0))} | {current_display} |"
        )

    report_lines.extend(["", "## Presence and source caveats", ""])
    for code in country_codes:
        snapshot = snapshot_rows.loc[code]
        report_lines.extend(
            [
                f"### {code}: {snapshot['source_name']}",
                "",
                f"- Coverage: {snapshot['coverage_scope']}",
                f"- Gap wording: {snapshot['observed_absence_wording']}",
                f"- Status semantics: {snapshot['status_semantics']}",
                f"- Licence: {snapshot['license_name']} ({snapshot['license_status']})",
                f"- Licence URL: {snapshot['license_url']}",
                f"- Attribution: {snapshot['attribution']}",
                f"- Acceptance: {snapshot['acceptance_reason']}",
                f"- Source URL: {snapshot['source_url']}",
                "",
            ]
        )
        source_as_of = str(snapshot["source_as_of_date"])
        if source_as_of == "unknown":
            report_lines.insert(
                len(report_lines) - 1,
                "- Freshness warning: the register publishes no reliable source-as-of date; "
                "the extraction date is capture provenance only.",
            )
        else:
            parsed_as_of = pd.to_datetime(source_as_of, errors="coerce")
            if not pd.isna(parsed_as_of):
                age_days = (pd.Timestamp(extraction_date) - parsed_as_of).days
                if age_days > 180:
                    report_lines.insert(
                        len(report_lines) - 1,
                        f"- Freshness warning: source metadata is {age_days} days older than "
                        "the extraction date.",
                    )

    report_lines.extend(
        [
            "## Identity review holds",
            "",
            "A review hold is emitted when no exact normalized identity is present but a listed "
            "source identity is a broader/specific token form, one-edit spelling, supported "
            "vaccine naming variant, or BCG acronym expansion of an EML identity. These candidates "
            "produce `UNKNOWN`, never presence or observed absence, until a provenance-backed "
            "equivalence is approved.",
            "",
            "| Country | EML identities held from gap claims | Stored candidate relationships |",
            "|---|---:|---:|",
        ]
    )
    held_identity_counts = (
        comparison.long.loc[
            comparison.long["uncertainty_reason"].eq("identity_match_requires_review")
        ]
        .groupby("country_code")
        .size()
        .to_dict()
    )
    candidate_count_for = identity_candidate_counts.set_index("country_code")[
        "candidate_relationships"
    ].to_dict()
    for country_code in country_codes:
        report_lines.append(
            f"| {country_code} | {int(held_identity_counts.get(country_code, 0))} | "
            f"{int(candidate_count_for.get(country_code, 0))} |"
        )

    report_lines.extend(
        [
            "",
            "## Ingest issues",
            "",
            "These are retained audit signals, not silently discarded records.",
            "",
            "| Country | Issue | Count |",
            "|---|---|---:|",
        ]
    )
    for row in issue_counts.itertuples(index=False):
        report_lines.append(f"| {row.country_code} | `{row.issue_code}` | {row.issue_count} |")
    report_lines.extend(
        [
            "",
            "## Compatibility result",
            "",
            f"The NDA/BLA-only US by Singapore renderer contains {len(legacy)} rows. "
            "ANDA observations remain queryable in the atlas but do not enter that renderer.",
            "Its 20 historical non-EML columns retain the delivered semantics; the WHO "
            "Essential Drug column is recomputed from the open electronic 2025 eEML, not the "
            "non-commercial 2023 PDF.",
            "",
            "The electronic 2025 EML is the POC universe and is licensed CC BY 3.0 IGO. "
            "Only rows with `Status=Added` are used, and `Combined with` is retained as "
            "co-prescription metadata rather than treated as an ingredient. The PDF is "
            "validation-only because its license is non-commercial.",
            "",
            "Attribution: WHO electronic Essential Medicines List (eEML), World Health "
            "Organization, 2020. https://list.essentialmeds.org/ (beta version 1.0). "
            "Licence: CC BY 3.0 IGO.",
            "",
            f"Adaptation notice: {WHO_ADAPTATION_NOTICE}",
            f"Official licence terms: {EEML_LICENSE_URL}.",
            "",
            "The substance table includes an optional UNII anchor, but this POC does not emit "
            "unverified mappings. The canonical normalized ingredient key remains the working "
            "identity until an open, provenance-backed UNII resolution step is added.",
            "",
            "The eEML endpoint is unversioned, the workbook has no edition marker, electronic "
            "content can differ from the binding PDF, and recommendation rows are not unique "
            "medicines. Counts are therefore extraction-date observations, not permanent 2025 "
            "constants.",
            "",
            "The eEML may not contain every historical removed record and its export has no "
            "stable record ID or release timestamp. Blank formulation and ATC fields are valid; "
            "they are retained as source limitations rather than treated as failed medicine "
            "records.",
            "",
            "The legacy compatibility columns retain ATC-derived metadata from the existing local "
            "working input. That input is not used as substance identity in the atlas, and its "
            "bulk redistribution rights are marked `human_review_required` and must be decided by "
            "the project owner before a commercial release. Bangladesh and Bhutan redistribution "
            "rights carry the same review status in their source snapshots.",
            "",
        ]
    )
    report_path = staging / "data_quality_report.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    return view_paths, view_hashes, report_path, _file_hash(report_path)


def _canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    canonical = frame.copy()
    if canonical.empty:
        return canonical.reset_index(drop=True)
    sort_columns = list(canonical.columns)
    comparable = canonical.fillna("").astype(str)
    order = comparable.sort_values(sort_columns, kind="stable").index
    return canonical.loc[order].reset_index(drop=True)


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, lineterminator="\n", na_rep="").encode("utf-8")


def _dataframe_hash(frame: pd.DataFrame) -> str:
    return hashlib.sha256(_csv_bytes(_canonical_frame(frame))).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _directory_content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(str(child.relative_to(path)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_hash(child).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _identity_uncertainty_relation(target_key: str, candidate_key: str) -> str:
    """Identify strict normalized-token containment without asserting equivalence.

    This is a conservative gap-safety rule, not a synonym dictionary. A related
    broader or more-specific source identity causes review-required UNKNOWN when
    no exact normalized identity is present; it never establishes presence.
    """

    if target_key == candidate_key:
        return ""
    target_tokens = _identity_tokens(target_key)
    candidate_tokens = _identity_tokens(candidate_key)
    if not target_tokens or not candidate_tokens:
        return ""
    discriminative_overlap = (
        (target_tokens & candidate_tokens) - IDENTITY_UNCERTAINTY_STOP_TOKENS
    )
    if any(len(token) >= 5 for token in discriminative_overlap):
        if target_tokens < candidate_tokens:
            return "source_identity_more_specific"
        if candidate_tokens < target_tokens:
            return "source_identity_broader"
    target_discriminators = target_tokens - IDENTITY_UNCERTAINTY_STOP_TOKENS
    vaccine_source_markers = {
        "antigen",
        "attenuated",
        "live",
        "toxoid",
        "vaccine",
        "virus",
    }
    vaccine_disease_signature = target_tokens - {"human", "vaccine", "virus"}
    vaccine_disease_match = bool(vaccine_disease_signature) and all(
        any(
            candidate == target
            or (
                len(target) >= 5
                and _edit_distance_at_most_one(target, candidate)
            )
            for candidate in candidate_tokens
        )
        for target in vaccine_disease_signature
    )
    if (
        "vaccine" in target_tokens
        and vaccine_disease_match
        and (
            bool(candidate_tokens & vaccine_source_markers)
            or len(candidate_tokens) == 1
        )
    ):
        return "source_identity_vaccine_variant"
    if "bcg" in target_tokens and {"bacille", "calmette", "guerin"}.issubset(
        candidate_tokens
    ):
        return "source_identity_acronym_variant"
    if frozenset((target_key, candidate_key)) in REVIEWED_SPELLING_VARIANTS:
        return "source_identity_spelling_variant"
    return ""


def _vaccine_product_family_match(
    target_key: str, product_name: object, raw_ingredient_text: object
) -> bool:
    """Hold a small reviewed set of vaccine-family gaps for product-level review."""

    markers = VACCINE_PRODUCT_FAMILY_MARKERS.get(target_key)
    if not markers:
        return False
    evidence = unicodedata.normalize(
        "NFKD", f"{product_name} {raw_ingredient_text}"
    ).encode("ascii", "ignore").decode().casefold()
    if not any(marker in evidence for marker in markers):
        return False
    vaccine_evidence_markers = {
        "antigen",
        "conjugate",
        "inactivated",
        "polysaccharide",
        "protein",
        "toxoid",
        "vaccine",
    }
    return any(marker in evidence for marker in vaccine_evidence_markers)


def _identity_tokens(value: str) -> frozenset[str]:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return frozenset(re.findall(r"[a-z0-9]+", ascii_value.casefold()))


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    index_left = index_right = differences = 0
    while index_left < len(left) and index_right < len(right):
        if left[index_left] == right[index_right]:
            index_left += 1
            index_right += 1
            continue
        differences += 1
        if differences > 1:
            return False
        if len(left) == len(right):
            index_left += 1
        index_right += 1
    return True


def _stable_id(*parts: object) -> str:
    return str(uuid.uuid5(ATLAS_NAMESPACE, "\x1f".join(str(part) for part in parts)))


def _publish_directory(staging: Path, destination: Path) -> Path:
    """Publish an immutable build and atomically switch the public pointer."""

    build_id = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))["build_id"]
    build_store = destination.parent / f".{destination.name}-builds"
    build_store.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / f".{destination.name}.publish.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        published = build_store / build_id
        if published.exists():
            published_hash = _directory_content_hash(published)
            staging_hash = _directory_content_hash(staging)
            if published_hash != staging_hash:
                raise RuntimeError(
                    "immutable build collision for "
                    f"{build_id}: stored={published_hash} rebuilt={staging_hash}"
                )
            shutil.rmtree(staging)
        else:
            os.replace(staging, published)

        if destination.is_symlink() and destination.resolve() == published.resolve():
            return published

        pointer = destination.parent / f".{destination.name}.next-{os.getpid()}"
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(os.path.relpath(published, destination.parent), target_is_directory=True)

        if destination.exists() and not destination.is_symlink():
            # One-time migration from the repo's historical real directory. All
            # subsequent switches replace one symlink with another atomically.
            migrated = build_store / "pre-versioned-output"
            if migrated.exists():
                shutil.rmtree(migrated)
            os.replace(destination, migrated)
            os.replace(pointer, destination)
            shutil.rmtree(migrated)
        else:
            os.replace(pointer, destination)
        return published


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="build from saved raw snapshots")
    build_parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    build_parser.add_argument("--extraction-date", type=date.fromisoformat, required=True)
    build_parser.add_argument("--output-dir", type=Path)
    build_parser.add_argument(
        "--raw-dir",
        type=Path,
        help="saved raw snapshot root; defaults to data/raw/current when present",
    )
    build_parser.add_argument(
        "--countries",
        nargs="+",
        default=list(SUPPORTED_COUNTRIES),
        choices=list(SUPPORTED_COUNTRIES),
    )

    compare_parser = subparsers.add_parser("compare", help="query an existing atlas")
    compare_parser.add_argument("--database", type=Path, required=True)
    compare_parser.add_argument("--countries", nargs="+", required=True)
    compare_parser.add_argument("--universe", default=UNIVERSE_ID)
    compare_parser.add_argument(
        "--current-qualified-countries",
        nargs="*",
        default=[],
        help="selected countries whose presence must pass current_qualified",
    )
    compare_parser.add_argument("--output-dir", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "build":
        artifact = build_atlas(
            BuildSpec(
                root=args.root,
                extraction_date=args.extraction_date,
                output_dir=args.output_dir,
                countries=tuple(args.countries),
                raw_dir=args.raw_dir,
            )
        )
        print(f"build_id: {artifact.build_id}")
        print(f"database: {artifact.database_path}")
        print(f"report: {artifact.report_path}")
        return

    result = compare_atlas(
        args.database,
        countries=tuple(args.countries),
        universe_id=args.universe,
        current_qualified_countries=tuple(args.current_qualified_countries),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in (
        ("presence_long", result.long),
        ("comparison_summary", result.summary),
        ("comparison_wide", result.wide),
    ):
        path = args.output_dir / f"{name}.csv"
        path.write_bytes(_csv_bytes(frame))
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
