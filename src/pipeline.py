"""build the us x singapore drug availability dataset.

loads fda + hsa, matches actives to atc, then labels availability. the label
logic is isolated in assign_availability() (see README.md).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

try:
    from .normalize import (
        atc_match_names,
        fix_atc,
        normalize_fda_component,
        normalize_ingredient,
        remap_atc_code,
        split_fda_ingredients,
        split_hsa_ingredients,
    )
except ImportError:  # pragma: no cover - supports direct script execution.
    from normalize import (  # type: ignore
        atc_match_names,
        fix_atc,
        normalize_fda_component,
        normalize_ingredient,
        remap_atc_code,
        split_fda_ingredients,
        split_hsa_ingredients,
    )


ROOT = Path(__file__).resolve().parents[1]
ATC_L5_PATTERN = re.compile(r"^[A-Z][0-9]{2}[A-Z]{2}[0-9]{2}$")
PSEUDO_SUBSTANCES = {"", "combinations", "other combinations"}
WHO_EML_INDEX_ENTRY_PATTERN = re.compile(r"^(.*?)\s*\.{2,}\s*[\d, ]+$")
WHO_EML_BULLET_PREFIX_PATTERN = re.compile(r"^(?:[\u25af\u25a1]\s*|o\s+)")

OUTPUT_COLUMNS = [
    "Active Ingredient",
    "ATC Codes",
    "Therapeutic Class (L1)",
    "Drug Class (L2)",
    "Pharmacological Subgroup (L3)",
    "Chemical Subgroup (L4)",
    "Substance (L5)",
    "FDA Drug Name",
    "HSA Drug Name",
    "FDA Status",
    "HSA Status",
    "FDA Standalone Product Count",
    "FDA Combo Product Count",
    "HSA Standalone Product Count",
    "HSA Combo Product Count",
    "Last FDA Approval",
    "Last HSA Approval",
    "Rare/Orphan Drug",
    "WHO Essential Drug",
    "Availability",
    "Availability Reason",
]

COMBO_FORMULATION_COLUMNS = [
    "Combo Ingredients",
    "FDA Combo Products",
    "HSA Combo Products",
    "FDA Combo Count",
    "HSA Combo Count",
    "Combo/Formulation Availability",
    "Other Country Component Coverage",
    "Combo/Formulation Reason",
]

NORMALIZATION_SAMPLE_COLUMNS = [
    "source",
    "product_id",
    "product_name",
    "raw_component",
    "component_norm",
]


def report_short_normalizations(stage: str, discarded: list[tuple[object, object]]) -> None:
    if not discarded:
        return
    details = "; ".join(f"{raw!r} -> {normalized!r}" for raw, normalized in discarded)
    print(f"discarded_short_normalizations[{stage}]: {len(discarded)} ({details})", file=sys.stderr)


LONG_COLUMNS = [
    "substance_key",
    "source",
    "atc_level5",
    "Therapeutic Class (L1)",
    "Drug Class (L2)",
    "Pharmacological Subgroup (L3)",
    "Chemical Subgroup (L4)",
    "Substance (L5)",
    "product_id",
    "product_name",
    "approval_date",
    "is_combo",
    "is_rare",
    "is_on_who_eml",
]


@dataclass(frozen=True)
class PipelineResult:
    output: pd.DataFrame
    combo_formulation_gaps: pd.DataFrame
    fda_unmatched_components: pd.DataFrame
    hsa_product_atc_fallbacks: pd.DataFrame
    hsa_unmatched_components: pd.DataFrame
    normalization_sample: pd.DataFrame
    fda_unmatched_count: int
    fda_component_count: int
    fda_matched_component_count: int


# pipeline stages


def run_pipeline(root: Path = ROOT) -> PipelineResult:
    """run all stages, return the output plus audit tables."""

    raw_dir = root / "data" / "raw"

    # who atc lookups, shared by both countries + rare matching
    atc = load_atc(raw_dir / "who" / "atc.csv")
    atc_l5_lookup = build_atc_l5_lookup(atc)
    atc_classes = build_atc_class_lookup(atc)
    combo_atc_codes = detect_combo_atc_codes(atc)

    # orphan-drug flag, same two-pass match
    rare_substance_keys = load_rare_substance_keys(
        raw_dir / "Rare Drugs.xls", atc_l5_lookup, atc_classes
    )
    who_eml_terms = load_who_eml_terms(raw_dir / "who" / "who_eml_2023.pdf")

    # per-country product/substance rows
    fda_rows, fda_metrics, fda_unmatched_components = load_fda_product_substances(
        raw_dir / "fda", atc_l5_lookup, atc_classes, rare_substance_keys, who_eml_terms
    )
    hsa_rows, hsa_product_atc_fallbacks, hsa_unmatched_components = load_hsa_product_substances(
        raw_dir / "hsa" / "hsa_registered_therapeutic_products.csv",
        atc_l5_lookup,
        atc_classes,
        combo_atc_codes,
        rare_substance_keys,
        who_eml_terms,
    )

    # combine, drop the pseudo "combinations" rows: one row per substance,
    # not atc buckets
    long_rows = pd.concat([fda_rows, hsa_rows], ignore_index=True)
    long_rows = long_rows[~long_rows["substance_key"].fillna("").isin(PSEUDO_SUBSTANCES)].copy()
    long_rows = long_rows.drop_duplicates(
        ["source", "product_id", "substance_key", "atc_level5", "is_combo"]
    )

    # availability labels (see assign_availability)
    output = assign_availability(long_rows)

    return PipelineResult(
        output=output,
        combo_formulation_gaps=build_combo_formulation_gaps(long_rows),
        fda_unmatched_components=fda_unmatched_components,
        hsa_product_atc_fallbacks=hsa_product_atc_fallbacks,
        hsa_unmatched_components=hsa_unmatched_components,
        normalization_sample=build_normalization_sample(root),
        **fda_metrics,
    )


def build_dataset(root: Path = ROOT) -> pd.DataFrame:
    """just the final dataframe, for callers that don't need the audit tables."""

    return run_pipeline(root).output


# stage 1: who atc + l1-l5 lookups


def load_atc(path: Path) -> pd.DataFrame:
    atc = pd.read_csv(path, dtype=str).fillna("")
    atc["atc_code"] = atc["atc_code"].str.strip().str.upper()
    atc["atc_name"] = atc["atc_name"].str.strip()
    return atc


def build_atc_l5_lookup(atc: pd.DataFrame) -> pd.DataFrame:
    lookup = (
        atc.loc[atc["atc_code"].str.len().eq(7), ["atc_code", "atc_name"]]
        .drop_duplicates()
        .copy()
    )
    lookup["name_norm"] = lookup["atc_name"].map(normalize_ingredient)
    lookup = lookup[lookup["name_norm"].str.len().ge(3)]
    lookup = lookup[~lookup["name_norm"].isin({"combinations", "other combinations"})]
    return lookup.drop_duplicates(["atc_code", "name_norm"]).reset_index(drop=True)


def build_atc_class_lookup(atc: pd.DataFrame) -> pd.DataFrame:
    atc_distinct = atc.drop_duplicates("atc_code")
    name_for = dict(zip(atc_distinct["atc_code"], atc_distinct["atc_name"]))

    rows: list[dict[str, str]] = []
    for code in sorted(atc_distinct.loc[atc_distinct["atc_code"].str.len().eq(7), "atc_code"]):
        substance_name = name_for.get(code, "").strip()
        rows.append(
            {
                "atc_level5": code,
                "Therapeutic Class (L1)": name_for.get(code[:1], ""),
                "Drug Class (L2)": name_for.get(code[:3], ""),
                "Pharmacological Subgroup (L3)": name_for.get(code[:4], ""),
                "Chemical Subgroup (L4)": name_for.get(code[:5], ""),
                "Substance (L5)": substance_name,
                # atc substance name is metadata. normalize it so rare/eml
                # matching share the vocab, but identity comes from the
                # source components.
                "substance_key": normalize_ingredient(substance_name),
            }
        )
    return pd.DataFrame(rows)


def detect_combo_atc_codes(atc: pd.DataFrame) -> set[str]:
    names = atc["atc_name"].str.lower()
    mask = atc["atc_code"].str.len().eq(7) & names.str.contains(
        r"\band\b|combinations?|,\s*comb|\swith\s", regex=True, na=False
    )
    return set(atc.loc[mask, "atc_code"])


# stage 2: two-pass ingredient -> atc l5 matching


def match_components_to_atc(components: pd.DataFrame, atc_l5_lookup: pd.DataFrame) -> pd.DataFrame:
    """two-pass: exact normalized name, then first-word fallback."""

    exact_candidates = components.copy()
    exact_candidates["atc_match_norm"] = exact_candidates["component_norm"].map(atc_match_names)
    exact_candidates = exact_candidates.explode("atc_match_norm")
    exact_candidates = exact_candidates[exact_candidates["atc_match_norm"].fillna("").str.len().gt(0)]

    exact = exact_candidates.merge(
        atc_l5_lookup[["atc_code", "name_norm"]],
        left_on="atc_match_norm",
        right_on="name_norm",
        how="inner",
    )
    exact["match_method"] = "exact"

    matched_ids = set(exact["component_id"])
    remaining = components[~components["component_id"].isin(matched_ids)].copy()
    if remaining.empty:
        matched = exact
    else:
        remaining = remaining[~remaining["component_norm"].map(has_isotope_marker)].copy()
        remaining["first_word"] = remaining["component_norm"].str.extract(r"^([a-z0-9-]+)", expand=False)

        fallback = remaining.merge(
            atc_l5_lookup[["atc_code", "name_norm"]],
            left_on="first_word",
            right_on="name_norm",
            how="inner",
        )
        fallback["match_method"] = "first_word"

        matched = pd.concat([exact, fallback], ignore_index=True)
    if matched.empty:
        return matched

    matched["atc_code"] = matched["atc_code"].map(remap_atc_code)
    return matched.drop_duplicates(["component_id", "atc_code"]).reset_index(drop=True)


def load_rare_substance_keys(
    rare_path: Path, atc_l5_lookup: pd.DataFrame, atc_classes: pd.DataFrame
) -> set[str]:
    if not rare_path.exists():
        return set()

    rare = pd.read_html(rare_path, encoding="cp1252")[0]
    if "Generic Name" not in rare.columns:
        return set()

    components = pd.DataFrame(
        {
            "component_id": [f"rare:{i}" for i in range(len(rare))],
            "component_norm": rare["Generic Name"].map(normalize_ingredient),
            "product_id": "",
            "product_name": "",
            "approval_date": pd.NaT,
            "is_combo": False,
        }
    )
    keep = components["component_norm"].str.len().ge(3)
    report_short_normalizations(
        "rare",
        list(zip(rare.loc[~keep, "Generic Name"], components.loc[~keep, "component_norm"])),
    )
    components = components[keep].copy()
    matched = match_components_to_atc(components, atc_l5_lookup)
    if matched.empty:
        return set()

    keyed = matched.rename(columns={"atc_code": "atc_level5"}).merge(
        atc_classes[["atc_level5", "substance_key"]], on="atc_level5", how="left"
    )
    return set(keyed["substance_key"].dropna())


# stage 3a: fda load + original approvals + atc match


def load_fda_product_substances(
    fda_dir: Path,
    atc_l5_lookup: pd.DataFrame,
    atc_classes: pd.DataFrame,
    rare_substance_keys: set[str],
    who_eml_terms: set[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, int], pd.DataFrame]:
    products = read_fda_table(fda_dir / "Products.txt")
    applications = read_fda_table(fda_dir / "Applications.txt")
    submissions = read_fda_table(fda_dir / "Submissions.txt")

    submissions["SubmissionStatusDate"] = pd.to_datetime(
        submissions["SubmissionStatusDate"], errors="coerce"
    )
    original_approvals = (
        submissions[
            submissions["SubmissionType"].eq("ORIG") & submissions["SubmissionStatus"].eq("AP")
        ]
        .groupby("ApplNo", as_index=False)["SubmissionStatusDate"]
        .min()
        .rename(columns={"SubmissionStatusDate": "approval_date"})
    )

    fda = (
        applications[applications["ApplType"].isin(["NDA", "BLA"])]
        .merge(products, on="ApplNo", how="inner")
        .merge(original_approvals, on="ApplNo", how="left")
    )
    fda["product_id"] = fda["ApplNo"].astype(str) + "-" + fda["ProductNo"].astype(str)

    components = explode_fda_components(fda)
    if components.empty:
        metrics = {
            "fda_unmatched_count": 0,
            "fda_component_count": 0,
            "fda_matched_component_count": 0,
        }
        return pd.DataFrame(columns=LONG_COLUMNS), metrics, pd.DataFrame()

    matched = match_components_to_atc(components, atc_l5_lookup)
    matched_component_ids = set(matched["component_id"])
    unmatched = components[~components["component_id"].isin(matched_component_ids)].copy()
    unmatched_audit = unmatched[
        ["component_id", "product_id", "product_name", "raw_component", "component_norm", "is_combo"]
    ].copy()
    metrics = {
        "fda_component_count": int(components.drop_duplicates(["product_id", "component_norm"]).shape[0]),
        "fda_matched_component_count": int(
            components[components["component_id"].isin(matched_component_ids)]
            .drop_duplicates(["product_id", "component_norm"])
            .shape[0]
        ),
        "fda_unmatched_count": int(
            components[~components["component_id"].isin(matched_component_ids)]
            .drop_duplicates(["product_id", "component_norm"])
            .shape[0]
        ),
    }

    long_rows = pd.concat(
        [
            finalize_long_rows(matched, "FDA", atc_classes, rare_substance_keys, who_eml_terms),
            finalize_unmatched_component_rows(unmatched, "FDA", rare_substance_keys, who_eml_terms),
        ],
        ignore_index=True,
    )
    # dedup same brand/substance repeated across strengths
    long_rows["_name_key"] = long_rows["product_name"].str.lower().str.strip()
    long_rows = long_rows.drop_duplicates(["_name_key", "substance_key", "atc_level5", "source"]).drop(
        columns="_name_key"
    )
    return long_rows[LONG_COLUMNS].copy(), metrics, unmatched_audit


def explode_fda_components(fda: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    discarded: list[tuple[object, object]] = []
    for row in fda.itertuples(index=False):
        component_pairs = []
        for component in split_fda_ingredients(row.ActiveIngredient):
            component_norm = normalize_fda_component(component, row.DrugName)
            if len(component_norm) >= 3:
                component_pairs.append((component, component_norm))
            else:
                discarded.append((component, component_norm))
        normalized_components = [component_norm for _, component_norm in component_pairs]
        is_combo = len(set(normalized_components)) >= 2

        for pos, (raw_component, component_norm) in enumerate(component_pairs):
            rows.append(
                {
                    "component_id": f"{row.product_id}:{pos}",
                    "product_id": row.product_id,
                    "product_name": row.DrugName,
                    "approval_date": row.approval_date,
                    "is_combo": is_combo,
                    "raw_component": raw_component,
                    "component_norm": component_norm,
                }
            )
    report_short_normalizations("fda", discarded)
    return pd.DataFrame(rows)


def read_fda_table(path: Path) -> pd.DataFrame:
    # submissions.txt has a cp1252 smart-quote byte despite looking ascii
    return pd.read_csv(path, sep="\t", dtype=str, encoding="cp1252").fillna("")


# stage 3b: hsa load + atc typo fixes


def load_hsa_product_substances(
    hsa_path: Path,
    atc_l5_lookup: pd.DataFrame,
    atc_classes: pd.DataFrame,
    combo_atc_codes: set[str],
    rare_substance_keys: set[str],
    who_eml_terms: set[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    hsa = pd.read_csv(hsa_path, dtype=str).fillna("")
    hsa["hsa_ATC"] = [remap_atc_code(code) for code in fix_atc(hsa["atc_code"].tolist())]
    hsa["hsa_ATC"] = hsa["hsa_ATC"].where(hsa["hsa_ATC"].map(is_valid_l5_atc), "")
    hsa["approval_date"] = pd.to_datetime(hsa["approval_d"], errors="coerce")

    components = explode_hsa_components(hsa)
    if components.empty:
        return pd.DataFrame(columns=LONG_COLUMNS), pd.DataFrame(), pd.DataFrame()

    matched = match_components_to_atc(components, atc_l5_lookup)
    matched_ids = set(matched["component_id"])
    unmatched = components[~components["component_id"].isin(matched_ids)].copy()

    # hsa only gives a product-level atc, but we need component-level. match
    # components first, fall back to the product atc for the leftovers.
    fallback = unmatched[unmatched["product_atc"].map(is_valid_l5_atc)].copy()
    fallback_audit = fallback[
        ["component_id", "product_id", "product_name", "raw_component", "component_norm", "product_atc", "is_combo"]
    ].copy()
    fallback_ids = set(fallback["component_id"])
    unmatched_without_fallback = unmatched[~unmatched["component_id"].isin(fallback_ids)].copy()
    keep = unmatched_without_fallback["component_norm"].astype(str).str.len().ge(3)
    report_short_normalizations(
        "hsa_unmatched",
        list(
            zip(
                unmatched_without_fallback.loc[~keep, "raw_component"],
                unmatched_without_fallback.loc[~keep, "component_norm"],
            )
        ),
    )
    unmatched_no_atc = unmatched_without_fallback[keep].copy()
    unmatched_audit = unmatched_no_atc[
        ["component_id", "product_id", "product_name", "raw_component", "component_norm", "product_atc", "is_combo"]
    ].copy()
    fallback["atc_code"] = fallback["product_atc"]
    fallback["match_method"] = "hsa_product_atc_fallback"
    fallback["component_match_fallback"] = True
    matched["component_match_fallback"] = False
    matched = pd.concat([matched, fallback], ignore_index=True)

    # if a component matched a real single-substance code, don't also inherit
    # the combo code across every split ingredient
    matched = matched[
        ~(matched["is_combo"] & matched["atc_code"].isin(combo_atc_codes) & ~matched["component_match_fallback"])
    ].copy()

    long_rows = pd.concat(
        [
            finalize_long_rows(matched, "HSA", atc_classes, rare_substance_keys, who_eml_terms),
            finalize_unmatched_component_rows(unmatched_no_atc, "HSA", rare_substance_keys, who_eml_terms),
        ],
        ignore_index=True,
    )
    if long_rows.empty:
        return pd.DataFrame(columns=LONG_COLUMNS), fallback_audit, unmatched_audit

    # dedup same brand/substance across strengths/variants
    long_rows["_name_key"] = long_rows["product_name"].str.lower().str.strip()
    long_rows = long_rows.drop_duplicates(["_name_key", "substance_key", "atc_level5", "source"]).drop(
        columns="_name_key"
    )
    return long_rows[LONG_COLUMNS].copy(), fallback_audit, unmatched_audit


def explode_hsa_components(hsa: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    discarded: list[tuple[object, object]] = []
    for row in hsa.itertuples(index=False):
        raw_components = split_hsa_ingredients(row.active_ingredients)
        if not raw_components:
            # rare, but keep the product-level atc row instead of dropping it
            raw_components = [""]

        component_pairs = []
        for component in raw_components:
            component_norm = normalize_ingredient(component)
            if component == "" or len(component_norm) >= 3:
                component_pairs.append((component, component_norm))
            else:
                discarded.append((component, component_norm))
        normalized_components = [component_norm for _, component_norm in component_pairs]
        non_empty_norms = [value for value in normalized_components if value]
        is_combo = len(set(non_empty_norms)) >= 2

        for pos, (raw_component, component_norm) in enumerate(component_pairs):
            rows.append(
                {
                    "component_id": f"{row.licence_no}:{pos}",
                    "product_id": row.licence_no,
                    "product_name": row.product_name,
                    "approval_date": row.approval_date,
                    "is_combo": is_combo,
                    "raw_component": raw_component,
                    "component_norm": component_norm,
                    "product_atc": row.hsa_ATC,
                }
            )
    report_short_normalizations("hsa", discarded)
    return pd.DataFrame(rows)


def is_valid_l5_atc(code: object) -> bool:
    if code is None or pd.isna(code):
        return False
    return bool(ATC_L5_PATTERN.match(str(code).strip().upper()))


def has_isotope_marker(component_norm: object) -> bool:
    return bool(re.search(r"\b(tc|i|f|ga|cu|lu|y|in|sm|tl|c|n|o|rb|zr|zn|ra|sr|p|cr|fe)\s+\d", str(component_norm)))


# stage 4: attach class / rare / eml fields


def finalize_long_rows(
    matched: pd.DataFrame,
    source: str,
    atc_classes: pd.DataFrame,
    rare_substance_keys: set[str],
    who_eml_terms: set[str] | None = None,
) -> pd.DataFrame:
    if matched.empty:
        return pd.DataFrame(columns=LONG_COLUMNS)

    rows = matched.rename(columns={"atc_code": "atc_level5"}).merge(
        atc_classes, on="atc_level5", how="left", suffixes=("", "_atc")
    )

    # identity comes from the source component, not atc. atc varies by
    # route/strength/use, so only fall back to it when there's no component text.
    atc_substance_key = rows["substance_key"].fillna("")
    component_key = rows["component_norm"].fillna("")
    rows["substance_key"] = component_key.where(component_key.str.len().gt(0), atc_substance_key)
    rows["substance_key"] = rows["substance_key"].where(
        rows["substance_key"].str.len().gt(0), rows["atc_level5"]
    )
    rows["Substance (L5)"] = rows["Substance (L5)"].fillna("")
    rows["Substance (L5)"] = rows["Substance (L5)"].where(
        rows["Substance (L5)"].str.len().gt(0), rows["substance_key"]
    )

    rows["source"] = source
    rows["is_rare"] = rows["substance_key"].isin(rare_substance_keys)
    rows = flag_who_eml(rows, who_eml_terms or set())
    return rows[LONG_COLUMNS].copy()


def finalize_unmatched_component_rows(
    components: pd.DataFrame,
    source: str,
    rare_substance_keys: set[str],
    who_eml_terms: set[str] | None = None,
) -> pd.DataFrame:
    if components.empty:
        return pd.DataFrame(columns=LONG_COLUMNS)

    rows = components.copy()
    rows["substance_key"] = rows["component_norm"]
    rows["source"] = source
    rows["atc_level5"] = ""
    rows["Therapeutic Class (L1)"] = ""
    rows["Drug Class (L2)"] = ""
    rows["Pharmacological Subgroup (L3)"] = ""
    rows["Chemical Subgroup (L4)"] = ""
    rows["Substance (L5)"] = rows["component_norm"]
    rows["is_rare"] = rows["substance_key"].isin(rare_substance_keys)
    rows = flag_who_eml(rows, who_eml_terms or set())
    return rows[LONG_COLUMNS].copy()


def load_who_eml_terms(path: Path) -> set[str]:
    """pull normalized eml medicine names from the who 2023 pdf index."""

    if not path.exists():
        return set()

    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency is present in project runtime.
        return set()

    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return parse_who_eml_terms(text)


def parse_who_eml_terms(text: str) -> set[str]:
    """parse + normalize medicine names from the eml index.

    the index is cleaner than the dosage tables. split combos on "+" and
    normalize each part like any other ingredient.
    """

    index_match = re.search(r"(?m)^Index\s*$", text)
    index_text = text[index_match.end() :] if index_match else text

    raw_terms: list[str] = []
    pending = ""
    for raw_line in index_text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        if "WHO Model List of Essential Medicines" in line:
            continue
        if re.fullmatch(r"page \d+", line):
            continue

        entry_match = WHO_EML_INDEX_ENTRY_PATTERN.match(line)
        if entry_match:
            name = entry_match.group(1).strip()
            if pending:
                name = f"{pending} {name}".strip()
                pending = ""
            raw_terms.append(name)
        elif pending or line.endswith("+"):
            pending = f"{pending} {line}".strip()

    terms: set[str] = set()
    for raw_term in raw_terms:
        for part in re.split(r"\s*\+\s*", raw_term):
            for candidate in [part, *re.findall(r"\(([^)]+)\)", part)]:
                candidate = WHO_EML_BULLET_PREFIX_PATTERN.sub("", candidate)
                candidate = re.sub(r"\[[^\]]+\]|\*+", " ", candidate)
                normalized = normalize_ingredient(candidate)
                if len(normalized) >= 3:
                    terms.add(normalized)
    return terms


def flag_who_eml(rows: pd.DataFrame, who_eml_terms: set[str]) -> pd.DataFrame:
    """flag rows whose substance is on the who eml."""

    flagged = rows.copy()
    normalized_terms = {
        normalized
        for term in who_eml_terms
        if len(normalized := normalize_ingredient(term)) >= 3
    }
    if flagged.empty:
        flagged["is_on_who_eml"] = False
        return flagged

    def is_eml_substance(value: object) -> bool:
        normalized = normalize_ingredient(value)
        return len(normalized) >= 3 and normalized in normalized_terms

    flagged["is_on_who_eml"] = flagged["substance_key"].map(is_eml_substance)
    return flagged


# stage 5: availability labeling


def assign_availability(rows: pd.DataFrame) -> pd.DataFrame:
    """collapse to one row per substance and label availability."""

    if rows.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    output_rows: list[dict[str, object]] = []
    for substance_key, group in rows.groupby("substance_key", sort=True, dropna=False):
        fda = group[group["source"].str.upper().eq("FDA")]
        hsa = group[group["source"].str.upper().isin(["HSA", "SG"])]

        us_status = country_status(fda)
        sg_status = country_status(hsa)
        fda_counts = product_counts_by_mode(fda)
        hsa_counts = product_counts_by_mode(hsa)
        label = availability_label(us_status, sg_status)
        reason = (
            f"US: {status_reason(us_status, fda)}; "
            f"SG: {status_reason(sg_status, hsa)} -> {label}"
        )

        output_rows.append(
            {
                "Active Ingredient": str(substance_key),
                "ATC Codes": join_unique(group["atc_level5"]),
                "Therapeutic Class (L1)": join_unique(group["Therapeutic Class (L1)"]),
                "Drug Class (L2)": join_unique(group["Drug Class (L2)"]),
                "Pharmacological Subgroup (L3)": join_unique(group["Pharmacological Subgroup (L3)"]),
                "Chemical Subgroup (L4)": join_unique(group["Chemical Subgroup (L4)"]),
                "Substance (L5)": join_unique(group["Substance (L5)"]),
                "FDA Drug Name": join_unique(fda["product_name"]),
                "HSA Drug Name": join_unique(hsa["product_name"]),
                "FDA Status": us_status,
                "HSA Status": sg_status,
                "FDA Standalone Product Count": fda_counts["standalone"],
                "FDA Combo Product Count": fda_counts["combo"],
                "HSA Standalone Product Count": hsa_counts["standalone"],
                "HSA Combo Product Count": hsa_counts["combo"],
                "Last FDA Approval": latest_date(fda["approval_date"]),
                "Last HSA Approval": latest_date(hsa["approval_date"]),
                "Rare/Orphan Drug": bool(group["is_rare"].fillna(False).any()),
                "WHO Essential Drug": bool(group["is_on_who_eml"].fillna(False).any()),
                "Availability": label,
                "Availability Reason": reason,
            }
        )

    return pd.DataFrame(output_rows, columns=OUTPUT_COLUMNS)


def build_normalization_sample(root: Path = ROOT) -> pd.DataFrame:
    raw_dir = root / "data" / "raw"
    rows: list[dict[str, object]] = []

    products_path = raw_dir / "fda" / "Products.txt"
    if products_path.exists():
        products = read_fda_table(products_path)
        for row in products.itertuples(index=False):
            product_id = f"{row.ApplNo}-{row.ProductNo}"
            for raw_component in split_fda_ingredients(row.ActiveIngredient):
                component_norm = normalize_ingredient(raw_component)
                if len(component_norm) >= 3:
                    rows.append(
                        {
                            "source": "FDA",
                            "product_id": product_id,
                            "product_name": row.DrugName,
                            "raw_component": raw_component,
                            "component_norm": component_norm,
                        }
                    )

    hsa_path = raw_dir / "hsa" / "hsa_registered_therapeutic_products.csv"
    if hsa_path.exists():
        hsa = pd.read_csv(hsa_path, dtype=str).fillna("")
        for row in hsa.itertuples(index=False):
            for raw_component in split_hsa_ingredients(row.active_ingredients):
                component_norm = normalize_ingredient(raw_component)
                if len(component_norm) >= 3:
                    rows.append(
                        {
                            "source": "HSA",
                            "product_id": row.licence_no,
                            "product_name": row.product_name,
                            "raw_component": raw_component,
                            "component_norm": component_norm,
                        }
                    )

    if not rows:
        return pd.DataFrame(columns=NORMALIZATION_SAMPLE_COLUMNS)
    return (
        pd.DataFrame(rows, columns=NORMALIZATION_SAMPLE_COLUMNS)
        .drop_duplicates(["source", "raw_component", "component_norm"])
        .sort_values(["source", "component_norm", "raw_component"])
        .reset_index(drop=True)
    )


def build_combo_formulation_gaps(long_rows: pd.DataFrame) -> pd.DataFrame:
    """flag exact fixed-dose combos present on only one side."""

    if long_rows.empty:
        return pd.DataFrame(columns=COMBO_FORMULATION_COLUMNS)

    product_components: list[dict[str, object]] = []
    for (source, product_id), product in long_rows.groupby(["source", "product_id"], dropna=False):
        ingredients = tuple(sorted(set(product["substance_key"].dropna().astype(str))))
        if len(ingredients) < 2:
            continue
        product_components.append(
            {
                "source": str(source).upper(),
                "product_id": product_id,
                "product_name": join_unique(product["product_name"]),
                "combo_key": " + ".join(ingredients),
                "ingredients": ingredients,
            }
        )

    combos = pd.DataFrame(product_components)
    if combos.empty:
        return pd.DataFrame(columns=COMBO_FORMULATION_COLUMNS)

    combo_rollup = (
        combos.groupby(["source", "combo_key"], as_index=False)
        .agg(
            ingredients=("ingredients", "first"),
            product_names=("product_name", join_unique),
            product_count=("product_id", "nunique"),
        )
        .copy()
    )

    fda = combo_rollup[combo_rollup["source"].eq("FDA")].rename(
        columns={"product_names": "FDA Combo Products", "product_count": "FDA Combo Count"}
    )
    hsa = combo_rollup[combo_rollup["source"].isin(["HSA", "SG"])].rename(
        columns={"product_names": "HSA Combo Products", "product_count": "HSA Combo Count"}
    )
    merged = fda[["combo_key", "ingredients", "FDA Combo Products", "FDA Combo Count"]].merge(
        hsa[["combo_key", "HSA Combo Products", "HSA Combo Count"]],
        on="combo_key",
        how="outer",
        indicator=True,
    )

    status_lookup = {
        (str(source).upper(), ingredient): country_status(group)
        for (source, ingredient), group in long_rows.groupby(["source", "substance_key"], dropna=False)
    }

    output_rows: list[dict[str, object]] = []
    for _, row in merged.iterrows():
        ingredients = combo_ingredients_from_row(row)
        merge_status = row["_merge"]
        if merge_status == "both":
            continue

        if merge_status == "left_only":
            label = "FDA_COMBO_ONLY"
            coverage = combo_component_coverage(ingredients, "HSA", status_lookup)
            reason = f"FDA has exact combo; HSA exact combo absent; {coverage}"
        else:
            label = "HSA_COMBO_ONLY"
            coverage = combo_component_coverage(ingredients, "FDA", status_lookup)
            reason = f"HSA has exact combo; FDA exact combo absent; {coverage}"

        output_rows.append(
            {
                "Combo Ingredients": row["combo_key"],
                "FDA Combo Products": clean_cell(row.get("FDA Combo Products", "")),
                "HSA Combo Products": clean_cell(row.get("HSA Combo Products", "")),
                "FDA Combo Count": int(clean_count(row.get("FDA Combo Count", 0))),
                "HSA Combo Count": int(clean_count(row.get("HSA Combo Count", 0))),
                "Combo/Formulation Availability": label,
                "Other Country Component Coverage": coverage,
                "Combo/Formulation Reason": reason,
            }
        )

    return pd.DataFrame(output_rows, columns=COMBO_FORMULATION_COLUMNS).sort_values(
        ["Combo/Formulation Availability", "Combo Ingredients"]
    )


def combo_component_coverage(
    ingredients: tuple[str, ...], other_source: str, status_lookup: dict[tuple[str, str], str]
) -> str:
    statuses = {ingredient: status_lookup.get((other_source, ingredient), "ABSENT") for ingredient in ingredients}
    if all(status == "STANDALONE" for status in statuses.values()):
        return f"all components standalone in {other_source}"
    if all(status != "ABSENT" for status in statuses.values()):
        return f"all components present in {other_source}, but exact combo absent"
    missing = ", ".join(sorted(ingredient for ingredient, status in statuses.items() if status == "ABSENT"))
    return f"missing components in {other_source}: {missing}"


def combo_ingredients_from_row(row: pd.Series) -> tuple[str, ...]:
    value = row.get("ingredients")
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return tuple(part.strip() for part in str(row["combo_key"]).split(" + ") if part.strip())


def clean_cell(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)


def clean_count(value: object) -> int:
    if value is None or pd.isna(value) or value == "":
        return 0
    return int(value)


def country_status(country_rows: pd.DataFrame) -> str:
    if country_rows.empty:
        return "ABSENT"
    if (~country_rows["is_combo"].fillna(False).astype(bool)).any():
        return "STANDALONE"
    return "COMBO-ONLY"


def product_counts_by_mode(country_rows: pd.DataFrame) -> dict[str, int]:
    if country_rows.empty:
        return {"standalone": 0, "combo": 0}
    is_combo = country_rows["is_combo"].fillna(False).astype(bool)
    return {
        "standalone": int(country_rows.loc[~is_combo, "product_id"].dropna().astype(str).nunique()),
        "combo": int(country_rows.loc[is_combo, "product_id"].dropna().astype(str).nunique()),
    }


def availability_label(us_status: str, sg_status: str) -> str:
    if us_status != "ABSENT" and sg_status == "ABSENT":
        return "FDA_ONLY"
    if sg_status != "ABSENT" and us_status == "ABSENT":
        return "HSA_ONLY"
    if us_status == "STANDALONE" and sg_status == "STANDALONE":
        return "NO GAP"
    return "PARTIAL GAP"


def status_reason(status: str, rows: pd.DataFrame) -> str:
    if rows.empty:
        return "ABSENT (0 products)"
    counts = product_counts_by_mode(rows)
    total = counts["standalone"] + counts["combo"]
    if status == "STANDALONE":
        return f"STANDALONE ({total} products; {counts['standalone']} standalone)"
    return f"COMBO-ONLY ({counts['combo']} combo products)"


# output helpers


def join_unique(values: pd.Series) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values.dropna():
        text = str(value).strip()
        if not text or text in {"NaT", "nan", "NA"}:
            continue
        if text not in seen:
            seen.add(text)
            cleaned.append(text)
    return " | ".join(sorted(cleaned))


def latest_date(values: pd.Series) -> str:
    parsed = pd.to_datetime(values, errors="coerce").dropna()
    if parsed.empty:
        return ""
    return parsed.max().date().isoformat()


def write_output(root: Path = ROOT, output_path: Path | None = None) -> PipelineResult:
    result = run_pipeline(root)
    destination = output_path or (root / "data" / "output" / "fda_hsa_by_actives.csv")
    destination.parent.mkdir(parents=True, exist_ok=True)
    result.output.to_csv(destination, index=False)

    combo_destination = destination.with_name("fda_hsa_combo_formulation_gaps.csv")
    result.combo_formulation_gaps.to_csv(combo_destination, index=False)
    unmatched_destination = destination.with_name("fda_unmatched_components.csv")
    result.fda_unmatched_components.to_csv(unmatched_destination, index=False)
    hsa_fallback_destination = destination.with_name("hsa_product_atc_fallback_components.csv")
    result.hsa_product_atc_fallbacks.to_csv(hsa_fallback_destination, index=False)
    hsa_unmatched_destination = destination.with_name("hsa_unmatched_components.csv")
    result.hsa_unmatched_components.to_csv(hsa_unmatched_destination, index=False)
    sample_destination = destination.with_name("ingredient_normalization_sample.csv")
    result.normalization_sample.to_csv(sample_destination, index=False)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the FDA-HSA availability dataset.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    result = write_output(args.root, args.output)
    output_path = args.output or (args.root / "data" / "output" / "fda_hsa_by_actives.csv")

    print(f"output: {output_path}")
    print(f"combo_formulation_gaps: {output_path.with_name('fda_hsa_combo_formulation_gaps.csv')}")
    print(f"fda_unmatched_components: {output_path.with_name('fda_unmatched_components.csv')}")
    print(f"hsa_product_atc_fallbacks: {output_path.with_name('hsa_product_atc_fallback_components.csv')}")
    print(f"hsa_unmatched_components: {output_path.with_name('hsa_unmatched_components.csv')}")
    print(f"normalization_sample: {output_path.with_name('ingredient_normalization_sample.csv')}")
    print(f"rows: {len(result.output)}")
    print(f"combo_formulation_gap_rows: {len(result.combo_formulation_gaps)}")
    print(f"hsa_product_atc_fallback_rows: {len(result.hsa_product_atc_fallbacks)}")
    print(f"hsa_unmatched_component_rows: {len(result.hsa_unmatched_components)}")
    print(f"normalization_sample_rows: {len(result.normalization_sample)}")
    print(f"fda_component_count: {result.fda_component_count}")
    print(f"fda_matched_component_count: {result.fda_matched_component_count}")
    print(f"fda_unmatched_count: {result.fda_unmatched_count}")
    print("availability_counts:")
    for label, count in result.output["Availability"].value_counts().sort_index().items():
        print(f"  {label}: {count}")


if __name__ == "__main__":
    main()
