"""Fetch immutable regulator snapshots; never build or compare data here."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import email.utils
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd


FDA_URL = "https://www.fda.gov/media/89850/download"
HSA_RESOURCE_ID = "d_767279312753558cbf19d48344577084"
HSA_DATA_URL = "https://data.gov.sg/api/action/datastore_search"
HSA_METADATA_URL = (
    "https://api-production.data.gov.sg/v2/public/api/datasets/"
    f"{HSA_RESOURCE_ID}/metadata"
)
BD_FIRST_PAGE_URL = (
    "https://api.tr.ocl.dghs.gov.bd/orgs/MoHFW/collections/"
    "dgda-registered-drugs-valueset/concepts/?limit=1000&includeExtras=true"
)
BT_PRODUCTS_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1vlubMgQc67cSCQJFH1JuOwDtRbx6oohYNXGdqSNujyk/export?format=csv"
)
BT_ACTIONS_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1DWvVvz3PgzGWyMaou-Ckw1Y4RKXBQRAVgjA9lC0INxk/export?format=csv&gid=0"
)
EEML_URL = "https://list.essentialmeds.org/print?format=xlsx"
ATC_INDEX_URL = "https://atcddd.fhi.no/atc_ddd_index/"
FDA_RARE_URL = "https://www.accessdata.fda.gov/scripts/opdlisting/oopd/"


@dataclass(frozen=True)
class FetchArtifact:
    extraction_date: date
    manifest_path: Path
    artifacts: dict[str, Path]


def fetch_sources(
    root: Path,
    extraction_date: date,
    countries: tuple[str, ...] = ("US", "SG", "BD", "BT"),
    atc_path: Path | None = None,
    rare_drugs_path: Path | None = None,
) -> FetchArtifact:
    """Fetch selected country registers plus the required electronic EML.

    Requests are staged into one immutable dated snapshot. Only after every
    selected source validates is the directory published and, for a complete
    four-country fetch, the data/raw/current pointer switched atomically.
    """

    root = Path(root).resolve()
    raw = root / "data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    atc_input = Path(atc_path or raw / "who" / "atc.csv").resolve()
    rare_drugs_input = Path(rare_drugs_path or raw / "Rare Drugs.xls").resolve()
    missing_reference_inputs = []
    if not atc_input.is_file():
        missing_reference_inputs.append(f"ATC ({atc_input})")
    if not rare_drugs_input.is_file():
        missing_reference_inputs.append(f"Rare Drugs ({rare_drugs_input})")
    if missing_reference_inputs:
        raise FileNotFoundError(
            "Required legacy reference inputs are missing before fetch: "
            + ", ".join(missing_reference_inputs)
            + ". Supply --atc-path and --rare-drugs-path; redistribution rights for the "
            "ATC bulk file require project-owner review."
        )
    _validate_legacy_reference_inputs(atc_input, rare_drugs_input)
    selected = tuple(dict.fromkeys(code.upper() for code in countries))
    unknown = sorted(set(selected) - {"US", "SG", "BD", "BT"})
    if unknown:
        raise ValueError(f"Unsupported country codes: {', '.join(unknown)}")

    snapshot_name = extraction_date.isoformat()
    if set(selected) != {"US", "SG", "BD", "BT"}:
        snapshot_name += "-" + "-".join(selected)
    snapshots = raw / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    published = snapshots / snapshot_name
    if published.exists():
        raise FileExistsError(f"Raw snapshot already exists: {published}")
    staging = Path(tempfile.mkdtemp(prefix=f".{snapshot_name}-", dir=snapshots))
    try:
        records: dict[str, dict[str, object]] = {}
        staged_artifacts: dict[str, Path] = {}
        staged_atc = staging / "who" / "atc.csv"
        staged_rare = staging / "Rare Drugs.xls"
        _write_bytes_atomic(staged_atc, atc_input.read_bytes())
        _write_bytes_atomic(staged_rare, rare_drugs_input.read_bytes())
        records["WHO_ATC"] = {
            "source_url": ATC_INDEX_URL,
            "sha256": _file_sha256(staged_atc),
            "license_name": "WHO ATC/DDD Index terms",
            "license_url": "https://atcddd.fhi.no/copyright_disclaimer/",
            "license_status": "human_review_required",
        }
        records["FDA_RARE"] = {
            "source_url": FDA_RARE_URL,
            "sha256": _file_sha256(staged_rare),
            "license_name": "U.S. FDA public source",
            "license_url": FDA_RARE_URL,
            "license_status": "reviewed_public_government_source",
        }
        staged_artifacts.update({"WHO_ATC": staged_atc, "FDA_RARE": staged_rare})
        if "US" in selected:
            staged_artifacts.update(_fetch_fda(staging / "fda", extraction_date, records))
        if "SG" in selected:
            staged_artifacts.update(_fetch_hsa(staging / "hsa", records))
        if "BD" in selected:
            staged_artifacts.update(_fetch_bangladesh(staging / "bd", records))
        if "BT" in selected:
            staged_artifacts.update(_fetch_bhutan(staging / "bt", records))
        staged_artifacts.update(_fetch_eeml(staging / "who", records))
        for record in records.values():
            record.setdefault("captured_on", extraction_date.isoformat())

        manifest = {
            "manifest_version": 1,
            "extraction_date": extraction_date.isoformat(),
            "countries": list(selected),
            "artifacts": records,
        }
        staged_manifest = staging / "manifest.json"
        _write_bytes_atomic(
            staged_manifest,
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.replace(staging, published)
        if set(selected) == {"US", "SG", "BD", "BT"}:
            _replace_directory_pointer(raw / "current", published)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    artifacts = {
        name: published / path.relative_to(staging)
        for name, path in staged_artifacts.items()
    }
    return FetchArtifact(extraction_date, published / "manifest.json", artifacts)


def _validate_legacy_reference_inputs(atc_path: Path, rare_drugs_path: Path) -> None:
    """Reject unreadable compatibility inputs before any regulator request."""

    try:
        atc = pd.read_csv(atc_path, dtype=str)
    except Exception as exc:
        raise ValueError(f"ATC reference input is not a readable CSV: {atc_path}") from exc
    required_atc_columns = {"atc_code", "atc_name"}
    if not required_atc_columns.issubset(atc.columns) or atc.empty:
        raise ValueError(
            "ATC reference input must contain non-empty atc_code and atc_name columns: "
            f"{atc_path}"
        )
    usable_atc_rows = atc["atc_code"].fillna("").astype(str).str.strip().ne("") & atc[
        "atc_name"
    ].fillna("").astype(str).str.strip().ne("")
    if not usable_atc_rows.any():
        raise ValueError(f"ATC reference input contains no usable rows: {atc_path}")

    try:
        tables = pd.read_html(rare_drugs_path, encoding="cp1252")
    except Exception as exc:
        raise ValueError(
            f"Rare Drugs reference input is not a readable table: {rare_drugs_path}"
        ) from exc
    if not tables or "Generic Name" not in tables[0].columns or tables[0].empty:
        raise ValueError(
            "Rare Drugs reference input must contain a non-empty Generic Name column: "
            f"{rare_drugs_path}"
        )


def _fetch_fda(
    destination: Path,
    extraction_date: date,
    records: dict[str, dict[str, object]],
) -> dict[str, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    body, headers, final_url = _request(FDA_URL)
    zip_path = destination / "drugsatfda.zip"
    _write_bytes_atomic(zip_path, body)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = set(archive.namelist())
        required = {"Applications.txt", "Products.txt", "Submissions.txt"}
        missing = sorted(required - names)
        if missing:
            raise ValueError(f"FDA archive is missing files: {', '.join(missing)}")
        for member in archive.infolist():
            member_path = Path(member.filename)
            if member.is_dir() or member_path.name != member.filename:
                continue
            _write_bytes_atomic(destination / member.filename, archive.read(member))
    last_modified = headers.get("last-modified", "")
    if last_modified:
        try:
            source_as_of = email.utils.parsedate_to_datetime(last_modified).date().isoformat()
        except (TypeError, ValueError):
            source_as_of = "unknown"
    else:
        source_as_of = "unknown"
    source_metadata = {
        "captured_on": extraction_date.isoformat(),
        "source_data_as_of": source_as_of,
        "source_url": FDA_URL,
    }
    _write_bytes_atomic(
        destination / "source_metadata.json",
        (json.dumps(source_metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    records["US"] = {
        "source_url": FDA_URL,
        "final_url": final_url,
        "sha256": _sha256(body),
        "content_type": headers.get("content-type", ""),
        "archive_members": sorted(names),
        "product_row_count": _text_row_count(destination / "Products.txt"),
    }
    return {"US": zip_path}


def _fetch_hsa(destination: Path, records: dict[str, dict[str, object]]) -> dict[str, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    fields: list[str] = []
    limit = 1000
    offset = 0
    total = None
    page_hashes: list[str] = []
    while total is None or offset < total:
        query = urllib.parse.urlencode(
            {"resource_id": HSA_RESOURCE_ID, "limit": limit, "offset": offset}
        )
        body, _, _ = _request(f"{HSA_DATA_URL}?{query}")
        page_hashes.append(_sha256(body))
        payload = json.loads(body)
        if not payload.get("success"):
            raise ValueError(f"HSA API returned success=false at offset {offset}")
        result = payload["result"]
        if not fields:
            fields = [field["id"] for field in result.get("fields", [])]
        page_rows = result.get("records", [])
        rows.extend(page_rows)
        total = int(result.get("total", len(rows)))
        if not page_rows:
            break
        offset += len(page_rows)
    if total is None or len(rows) != total:
        raise ValueError(f"HSA snapshot incomplete: expected {total}, fetched {len(rows)}")
    rows.sort(key=lambda row: int(row.get("_id", 0)))
    csv_path = destination / "hsa_registered_therapeutic_products.csv"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    _write_bytes_atomic(csv_path, buffer.getvalue().encode("utf-8"))

    metadata, _, metadata_final_url = _request(HSA_METADATA_URL)
    metadata_path = destination / "hsa_registered_therapeutic_products_metadata.json"
    _write_bytes_atomic(metadata_path, metadata)
    _write_bytes_atomic(
        destination / "fetch_metadata.json",
        (
            json.dumps(
                {
                    "row_count": len(rows),
                    "source_url": HSA_DATA_URL,
                    "metadata_url": metadata_final_url,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )
    records["SG"] = {
        "source_url": HSA_DATA_URL,
        "metadata_url": metadata_final_url,
        "row_count": len(rows),
        "sha256": _file_sha256(csv_path),
        "page_sha256": page_hashes,
    }
    return {"SG": csv_path}


def _fetch_bangladesh(
    destination: Path, records: dict[str, dict[str, object]]
) -> dict[str, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    pages_dir = destination / "pages"
    if pages_dir.exists():
        shutil.rmtree(pages_dir)
    pages_dir.mkdir(parents=True)

    first_body, first_headers, first_final_url = _request(BD_FIRST_PAGE_URL)
    page_count = int(first_headers.get("pages", "1"))
    page_urls = {
        page_number: (
            BD_FIRST_PAGE_URL
            if page_number == 1
            else f"{BD_FIRST_PAGE_URL}&page={page_number}"
        )
        for page_number in range(1, page_count + 1)
    }
    fetched: dict[int, tuple[bytes, dict[str, str], str]] = {
        1: (first_body, first_headers, first_final_url)
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_request, url): page_number
            for page_number, url in page_urls.items()
            if page_number != 1
        }
        for future in concurrent.futures.as_completed(futures):
            fetched[futures[future]] = future.result()

    all_concepts: list[dict[str, object]] = []
    page_records: list[dict[str, object]] = []
    declared_total: int | None = None
    for page_number in range(1, page_count + 1):
        request_url = page_urls[page_number]
        body, headers, final_url = fetched[page_number]
        concepts = json.loads(body)
        if not isinstance(concepts, list):
            raise ValueError(f"Bangladesh page {page_number} body is not an array")
        returned = int(headers.get("num_returned", len(concepts)))
        if returned != len(concepts):
            raise ValueError(
                f"Bangladesh page {page_number} declared {returned} but returned {len(concepts)}"
            )
        header_page = str(headers.get("page_number", "")).strip()
        if header_page and int(header_page) != page_number:
            raise ValueError(
                f"Bangladesh requested page {page_number} but received page {header_page}"
            )
        header_pages = str(headers.get("pages", "")).strip()
        if header_pages and int(header_pages) != page_count:
            raise ValueError("Bangladesh page count changed during pagination")
        page_total = int(headers.get("num_found", len(concepts)))
        if declared_total is None:
            declared_total = page_total
        elif page_total != declared_total:
            raise ValueError("Bangladesh num_found changed during pagination")
        page_path = pages_dir / f"page-{page_number:04d}.json"
        header_path = pages_dir / f"page-{page_number:04d}.headers.json"
        _write_bytes_atomic(page_path, body)
        captured_headers = {
            key: headers.get(key, "")
            for key in ("num_found", "num_returned", "pages", "page_number", "next")
        }
        _write_bytes_atomic(
            header_path,
            (json.dumps(captured_headers, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        page_records.append(
            {
                "page_number": page_number,
                "request_url": request_url,
                "final_url": final_url,
                "body_sha256": _sha256(body),
                "headers": captured_headers,
            }
        )
        all_concepts.extend(concepts)
    if declared_total is None or len(all_concepts) != declared_total:
        raise ValueError(
            f"Bangladesh snapshot incomplete: expected {declared_total}, fetched {len(all_concepts)}"
        )
    concept_ids = [str(concept.get("id") or "") for concept in all_concepts]
    if any(not concept_id for concept_id in concept_ids):
        raise ValueError("Bangladesh response contains a concept without an OCL id")
    if len(set(concept_ids)) != len(concept_ids):
        raise ValueError("Bangladesh response contains duplicate OCL concept ids")

    output = {
        "metadata": {
            "country_code": "BD",
            "num_found": declared_total,
            "num_returned": len(all_concepts),
            "source_url": BD_FIRST_PAGE_URL,
            "pages": page_records,
        },
        "concepts": all_concepts,
    }
    output_path = destination / "dgda_concepts.json"
    _write_bytes_atomic(
        output_path,
        (json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    records["BD"] = {
        "source_url": BD_FIRST_PAGE_URL,
        "row_count": len(all_concepts),
        "page_count": len(page_records),
        "sha256": _file_sha256(output_path),
    }
    return {"BD": output_path}


def _fetch_bhutan(
    destination: Path, records: dict[str, dict[str, object]]
) -> dict[str, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    products_body, _, products_final_url = _request(BT_PRODUCTS_URL)
    actions_body, _, actions_final_url = _request(BT_ACTIONS_URL)
    products_path = destination / "registered_products.csv"
    actions_path = destination / "regulatory_actions.csv"
    _write_bytes_atomic(products_path, products_body)
    _write_bytes_atomic(actions_path, actions_body)
    product_rows = _csv_row_count(products_body)
    action_rows = _csv_row_count(actions_body)
    records["BT"] = {
        "products_url": products_final_url,
        "actions_url": actions_final_url,
        "product_row_count": product_rows,
        "action_row_count": action_rows,
        "products_sha256": _sha256(products_body),
        "actions_sha256": _sha256(actions_body),
    }
    return {"BT_PRODUCTS": products_path, "BT_ACTIONS": actions_path}


def _fetch_eeml(destination: Path, records: dict[str, dict[str, object]]) -> dict[str, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    body, headers, final_url = _request(EEML_URL)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        if "xl/worksheets/sheet1.xml" not in archive.namelist():
            raise ValueError("eEML response is not the expected XLSX workbook")
    path = destination / "eeml_2025.xlsx"
    _write_bytes_atomic(path, body)
    records["WHO_EML_2025"] = {
        "source_url": EEML_URL,
        "final_url": final_url,
        "sha256": _sha256(body),
        "content_type": headers.get("content-type", ""),
        "license": "CC BY 3.0 IGO",
        "edition": "24th list (2025)",
        "publication_year": 2025,
    }
    return {"WHO_EML_2025": path}


def _request(url: str, attempts: int = 3, timeout: int = 60) -> tuple[bytes, dict[str, str], str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "drug-access-atlas-poc/1.0 (+source snapshot fetch)",
            "Accept": "*/*",
        },
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return (
                    response.read(),
                    {key.lower(): value for key, value in response.headers.items()},
                    response.geturl(),
                )
        except Exception as error:  # network boundary; re-raised with URL context below.
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


def _write_bytes_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as temporary:
            temporary.write(body)
            temporary.flush()
        Path(temporary_name).replace(path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _replace_directory_pointer(pointer: Path, target: Path) -> None:
    temporary = pointer.parent / f".{pointer.name}.next-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(os.path.relpath(target, pointer.parent), target_is_directory=True)
    os.replace(temporary, pointer)


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _csv_row_count(body: bytes) -> int:
    return max(sum(1 for _ in csv.reader(io.StringIO(body.decode("utf-8-sig")))) - 1, 0)


def _text_row_count(path: Path) -> int:
    with path.open("rb") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--extraction-date", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--countries",
        nargs="+",
        default=["US", "SG", "BD", "BT"],
        choices=["US", "SG", "BD", "BT"],
    )
    parser.add_argument(
        "--atc-path",
        type=Path,
        help="required local WHO ATC CSV; defaults to data/raw/who/atc.csv",
    )
    parser.add_argument(
        "--rare-drugs-path",
        type=Path,
        help="required local FDA Rare Drugs export; defaults to data/raw/Rare Drugs.xls",
    )
    args = parser.parse_args()
    artifact = fetch_sources(
        args.root,
        args.extraction_date,
        tuple(args.countries),
        atc_path=args.atc_path,
        rare_drugs_path=args.rare_drugs_path,
    )
    print(f"manifest: {artifact.manifest_path}")
    for name, path in sorted(artifact.artifacts.items()):
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
