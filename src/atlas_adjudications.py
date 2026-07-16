"""Reviewed atlas-layer adjudications that must not change the core normalizer."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_IDENTITY_REFINEMENTS = (
    (re.compile(r"\bhyoscine\s+(?:n[- ]?)?butyl\s*bromide\b", re.I), "hyoscine butylbromide"),
    (re.compile(r"\bhyoscine\s+hydro\s*bromide\b", re.I), "hyoscine hydrobromide"),
    (re.compile(r"\bscopolamine\b", re.I), "hyoscine hydrobromide"),
    (re.compile(r"\bsodium\s+calcium\s+edetate\b", re.I), "sodium calcium edetate"),
    (re.compile(r"\bcalcium\s+disodium\s+edetate\b", re.I), "sodium calcium edetate"),
    (re.compile(r"\bedetate\s+calcium\s+disodium\b", re.I), "sodium calcium edetate"),
    (re.compile(r"\b(?:di[- ]?sodium\s+(?:edta|edetate)|edta\s+disodium)\b", re.I), "disodium edetate"),
    (re.compile(r"\b(?:5[- ]aminosalicylic\s+acid|mesalazine|mesalamine)\b", re.I), "mesalazine"),
    (re.compile(r"\b(?:p|para)[- ]aminosalicylic\s+acid\b", re.I), "para aminosalicylic acid"),
    (re.compile(r"\bbenzyl\s+benzoate\b", re.I), "benzyl benzoate"),
    (re.compile(r"\bpotassium\s+permanganate\b", re.I), "potassium permanganate"),
    (re.compile(r"\bsilver\s+diamine\s+fluoride\b", re.I), "silver diamine fluoride"),
    (re.compile(r"\bcompound\s+sodium\s+lactate(?:\s+solution)?\b", re.I), "compound sodium lactate"),
    (re.compile(r"\boral\s+rehydration\s+salts?\b", re.I), "oral rehydration salts"),
    (re.compile(r"\binsulin\s*\(\s*human\s*,\s*short[- ]acting\s*\)", re.I), "human insulin short acting"),
    (re.compile(r"\binsulin\s*\(\s*human\s*,\s*intermediate[- ]acting\s*\)", re.I), "human insulin intermediate acting"),
    (re.compile(r"\binsulin\s*\(\s*analogue\s*,\s*rapid[- ]acting\s*\)", re.I), "insulin analogue rapid acting"),
    (re.compile(r"\binsulin\s*\(\s*analogue\s*,\s*long[- ]acting\s*\)", re.I), "insulin analogue long acting"),
)


_REVIEWED_PREFERRED_KEYS = {
    "porcatant alfa": "poractant alfa",
    "poractant alfa": "poractant alfa",
    "insulin glargin": "insulin glargine",
    "insulin glargine": "insulin glargine",
    "thioguanine": "tioguanine",
    "tioguanine": "tioguanine",
    "anastrozol": "anastrozole",
    "anastrozole": "anastrozole",
    "enoxaprin": "enoxaparin",
    "enoxaparin": "enoxaparin",
    "protamin": "protamine",
    "protamine": "protamine",
    "metformine": "metformin",
    "metformin": "metformin",
    "p aminosalicylate": "para aminosalicylic acid",
    "aminosalicylic acid": "para aminosalicylic acid",
    "para aminosalicylic acid": "para aminosalicylic acid",
    "esomeprazole strontium": "esomeprazole",
    "esomeprazole": "esomeprazole",
    "tenofovir disoproxil fumerate": "tenofovir disoproxil",
    "tenofovir disoproxil": "tenofovir disoproxil",
    "l adrenaline": "adrenaline",
    "adrenaline": "adrenaline",
    "l noradrenaline": "noradrenaline",
    "noradrenaline": "noradrenaline",
}


_ATC_CORRECTIONS = {
    "A01BA02": "A10BA02",
}

CURATED_CONCEPT_KEYS = frozenset(
    {
        "bcg vaccine",
        "japanese encephalitis vaccine",
        "cholera vaccine",
        "dengue vaccine",
        "yellow fever vaccine",
        "typhoid vaccine",
        "varicella vaccine",
        "oral rehydration salts",
        "compound sodium lactate",
        "human insulin short acting",
        "human insulin intermediate acting",
        "insulin analogue rapid acting",
        "insulin analogue long acting",
        "pancreatic enzymes",
        "erythropoiesis stimulating agents",
        "ferrous salt",
        "all trans retinoic acid",
    }
)


@dataclass(frozen=True)
class ConceptAdjudication:
    state: str
    rule: str
    needs_external_source: str = ""
    mode_override: str = ""


def _plain(value: object) -> str:
    text = unicodedata.normalize("NFKD", "" if value is None else str(value))
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", text)
    text = text.lower().replace("-", " ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def refine_identity(raw_text: object, normalized_key: object) -> str:
    """Restore identity-bearing portions stripped by active-moiety normalization."""

    raw = unicodedata.normalize("NFKC", "" if raw_text is None else str(raw_text))
    for pattern, refined in _IDENTITY_REFINEMENTS:
        if pattern.search(raw):
            return refined
    return canonical_reviewed_identity(normalized_key)


def canonical_reviewed_identity(value: object) -> str:
    """Return the preferred key for reviewed naming variants only."""

    original = str(value).strip()
    return _REVIEWED_PREFERRED_KEYS.get(_plain(value), original)


def reviewed_equivalent(left: object, right: object) -> bool:
    """Return true only for an explicitly reviewed naming-equivalence family."""

    left_key = _plain(left)
    right_key = _plain(right)
    if left_key == right_key:
        return True
    return (
        left_key in _REVIEWED_PREFERRED_KEYS
        and right_key in _REVIEWED_PREFERRED_KEYS
        and _REVIEWED_PREFERRED_KEYS[left_key]
        == _REVIEWED_PREFERRED_KEYS[right_key]
    )


def has_veterinary_marker(*values: object) -> bool:
    """Detect audited veterinary naming and dosage-form evidence."""

    text = " ".join("" if value is None else str(value) for value in values)
    return bool(
        re.search(
            r"\bvet(?:erinary)?\b|\bbolus\b|\bvet[a-z0-9-]+\b|\b[a-z0-9-]+vet\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def aggregate_current_marketing(values: object) -> str:
    """Aggregate FDA marketing status without turning unknown support into certainty."""

    statuses = [str(value).strip().casefold() for value in values]
    if any(status in {"prescription", "over-the-counter"} for status in statuses):
        return "CONFIRMED"
    if statuses and all(status == "discontinued" for status in statuses):
        return "NOT_MARKETED"
    return "UNKNOWN"


def classify_eml_scope(medicine_name: object, formulations: object) -> str:
    """Classify EML objects that ordinary national drug registers cannot answer."""

    text = f"{_plain(medicine_name)} {_plain(formulations)}".strip()
    if re.search(
        r"\b(?:whole blood|red blood cells?|platelets?|fresh frozen plasma|"
        r"cryoprecipitate)\b",
        text,
    ):
        return "blood_component"
    if re.search(r"\b(?:male |female )?condoms?\b|\bdiaphragms?\b", text):
        return "barrier_device"
    if re.search(
        r"\bdental\b.*\b(?:cement|composite|restorative|sealant)\b|"
        r"\b(?:glass ionomer|resin(?: based)? composite)\b",
        text,
    ):
        return "dental_material"
    if re.search(r"\bintrauterine device\b|\bcopper iud\b", text):
        return "contraceptive_device"
    if re.search(r"\bready to use therapeutic food\b|\brutf\b", text):
        return "therapeutic_food"
    if re.search(r"\bsunscreens?\b", text):
        return "topical_protective_product"
    return ""


def external_source_for_observation(country_code: str, target_key: str) -> str:
    """Name a missing category source when the selected register cannot answer."""

    target = _plain(target_key)
    if country_code == "US" and (
        "vaccine" in target
        or "immune globulin" in target
        or "immunoglobulin" in target
        or re.search(r"\b(?:antiserum|antitoxin)\b", target)
    ):
        return "FDA_CBER_OR_PURPLE_BOOK"
    return ""


def adjudicate_eml_concept_product(
    target_key: str,
    *,
    country_code: str,
    product_name: object,
    raw_ingredient_text: object,
    form: object,
    strength: object,
    product_atc_codes: object,
    ingredient_keys: frozenset[str],
) -> ConceptAdjudication | None:
    """Resolve only the expert-reviewed EML recommendation concepts."""

    target = _plain(target_key)
    product = _plain(product_name)
    raw = _plain(raw_ingredient_text)
    dosage_form = _plain(form)
    strength_text = _plain(strength)
    atc_values = {
        re.sub(r"\s+", "", value.upper())
        for value in re.split(r"\|", str(product_atc_codes))
        if value.strip()
    }
    keys = frozenset(canonical_reviewed_identity(key) for key in ingredient_keys)
    evidence_text = " ".join((product, raw, dosage_form, strength_text))

    vaccine_rules = {
        "bcg vaccine": (("J07AN01",), ("bcg vaccine", "bacille calmette guerin")),
        "japanese encephalitis vaccine": (("J07BA02", "J07BA03"), ("ix iaro", "ixiaro", "japanese encephalitis")),
        "cholera vaccine": (("J07AE01",), ("dukoral", "cholera vaccine")),
        "dengue vaccine": (("J07BX04",), ("dengvaxia", "dengue vaccine")),
        "yellow fever vaccine": (("J07BL01",), ("stamaril", "yellow fever")),
        "typhoid vaccine": (("J07AP",), ("typhoid", "vaxphoid")),
        "varicella vaccine": (("J07BK01",), ("varicella vaccine", "varilrix", "varivax", "nexipox")),
    }
    if target in vaccine_rules:
        atc_prefixes, markers = vaccine_rules[target]
        if any(
            atc.startswith(prefix)
            for atc in atc_values
            for prefix in atc_prefixes
        ) or any(
            marker in evidence_text for marker in markers
        ):
            return ConceptAdjudication(
                "VERIFIED_PRESENT", "reviewed_vaccine_product", mode_override="STANDALONE"
            )
        return None

    if target == "oral rehydration salts":
        required = {"potassium chloride", "sodium chloride"}
        carbohydrate = {"glucose", "anhydrous glucose", "dextrose", "dextrose anhydrous"}
        citrate = {"trisodium citrate", "sodium citrate"}
        exact_name = "oral rehydration salts" in evidence_text
        exact_set = required.issubset(keys) and bool(keys & carbohydrate) and bool(keys & citrate)
        if (exact_name or exact_set) and re.search(r"\boral\b|\bors\b", evidence_text):
            return ConceptAdjudication(
                "VERIFIED_PRESENT", "reviewed_ors_composition", mode_override="STANDALONE"
            )
        return None

    if target == "compound sodium lactate":
        hartmann_set = {
            "calcium chloride",
            "potassium chloride",
            "sodium chloride",
            "sodium lactate",
        }
        named = bool(
            re.search(r"\bhartmann\b|\blactated ringer\b|\bcompound sodium lactate\b", evidence_text)
        )
        parenteral = bool(re.search(r"\binfus|\binject|\bintra venous\b|\biv\b", evidence_text))
        exact_composition = keys == hartmann_set
        audited_pseudo_product = named and keys == {"compound sodium lactate"}
        if parenteral and (exact_composition or audited_pseudo_product):
            return ConceptAdjudication(
                "VERIFIED_PRESENT", "reviewed_hartmann_composition", mode_override="STANDALONE"
            )
        return None

    if target == "insulin analogue rapid acting":
        if keys & {"insulin aspart", "insulin lispro", "insulin glulisine"} or re.search(
            r"\binsulin (?:aspart|lispro|glulisine)\b", raw
        ):
            return ConceptAdjudication("VERIFIED_PRESENT", "reviewed_insulin_rapid_analogue")
        return None
    if target == "insulin analogue long acting":
        if keys & {"insulin glargine", "insulin detemir", "insulin degludec"} or re.search(
            r"\binsulin (?:glargine|glargin|detemir|degludec)\b", raw
        ):
            return ConceptAdjudication("VERIFIED_PRESENT", "reviewed_insulin_long_analogue")
        return None
    if target == "human insulin short acting":
        short_marker = bool(
            re.search(r"\b(?:actrapid|humulin r|novolin r|myxredlin|regular insulin|soluble insulin)\b", evidence_text)
        )
        excluded = bool(re.search(r"\b(?:mix|protamine|isophane|nph)\b", evidence_text))
        human_insulin = "insulin human" in keys or bool(
            re.search(r"\b(?:insulin human|human insulin)\b", raw)
        )
        if any(atc.startswith("A10AB01") for atc in atc_values) or (
            human_insulin and short_marker and not excluded
        ):
            return ConceptAdjudication("VERIFIED_PRESENT", "reviewed_human_insulin_short")
        return None
    if target == "human insulin intermediate acting":
        human_intermediate_marker = bool(
            re.search(
                r"\b(?:isophane|nph|insulatard|humulin n|novolin n)\b",
                evidence_text,
            )
        )
        human_insulin = bool(
            re.search(r"\b(?:insulin human|human insulin)\b", raw)
            or re.search(r"\b(?:insulatard|humulin n|novolin n)\b", product)
        )
        if "A10AC01" in atc_values or (human_intermediate_marker and human_insulin):
            premix = "mix" in product or (
                "isophane" in raw and "soluble" in raw
            )
            return ConceptAdjudication(
                "VERIFIED_PRESENT",
                "reviewed_human_insulin_intermediate",
                mode_override="COMBO_ONLY" if premix else "",
            )
        return None

    if target == "pancreatic enzymes":
        pancreatic = bool(
            keys & {"pancreatin", "pancrelipase", "pancreas powder"}
            or re.search(r"\b(?:creon|pancreon|pancrelipase|pancreatin)\b", evidence_text)
        )
        if not pancreatic:
            return None
        if country_code == "BD" and not re.search(r"\b(?:units?|iu|usp units?)\b", f"{raw} {strength_text}"):
            return ConceptAdjudication(
                "INDETERMINATE",
                "pancreatin_activity_units_missing",
                "ACTIVITY_UNIT_CONFIRMATION",
            )
        return ConceptAdjudication(
            "VERIFIED_PRESENT",
            "reviewed_pancreatic_enzyme_product",
            mode_override="STANDALONE",
        )

    if target == "erythropoiesis stimulating agents":
        if any(
            key.startswith(("epoetin", "darbepoetin", "methoxy polyethylene glycol epoetin"))
            for key in keys
        ) and re.search(r"\binject|\bsyringe|\bparenteral\b", evidence_text):
            return ConceptAdjudication("VERIFIED_PRESENT", "reviewed_esa_class_member")
        return None

    if target == "ferrous salt":
        if any(key.startswith("ferrous ") for key in keys):
            return ConceptAdjudication("VERIFIED_PRESENT", "reviewed_ferrous_salt_class_member")
        return None

    if target == "all trans retinoic acid":
        oral_capsule = bool(re.search(r"\bcapsule\b|\boral\b", evidence_text))
        ten_mg = bool(re.search(r"\b10\s*mg\b", f"{product} {raw} {strength_text}"))
        if "tretinoin" in keys and oral_capsule and ten_mg:
            return ConceptAdjudication("VERIFIED_PRESENT", "reviewed_oral_tretinoin_10mg")
        return None

    return None


def canonicalize_atc(value: object, medicine_name: object = "") -> tuple[str, str]:
    """Return canonical ATC text and `valid`, `corrected`, `invalid`, or `blank`."""

    raw = unicodedata.normalize("NFKC", "" if value is None else str(value)).strip()
    if not raw:
        return "", "blank"
    cleaned = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069\s]", "", raw).upper()
    corrected = cleaned != raw.upper()
    if _plain(medicine_name) == "insulin human short acting" and cleaned == "A10AC01":
        cleaned = "A10AB01"
        corrected = True
    mapped = _ATC_CORRECTIONS.get(cleaned, cleaned)
    corrected = corrected or mapped != cleaned
    if not re.fullmatch(r"[A-Z][0-9]{2}(?:[A-Z](?:[A-Z](?:[0-9]{2})?)?)?", mapped):
        return "", "invalid"
    return mapped, "corrected" if corrected else "valid"
