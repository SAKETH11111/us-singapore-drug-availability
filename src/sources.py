"""Canonical facts about the data sources this project draws from.

The fetcher (`fetch_sources`) downloads from these endpoints and the atlas records
them as provenance. Keeping one copy here stops the download URL and the recorded
provenance URL — or a licence label and its manifest copy — from drifting apart.

Only the values that are genuinely identical in both layers live here. Each
regulator's request-specific endpoint (HSA's API, Bhutan's CSV export links) stays
next to the code that calls it, because those differ from the human-facing URLs the
atlas cites as provenance.
"""

from __future__ import annotations

SUPPORTED_COUNTRIES = ("US", "SG", "BD", "BT")

# FDA publishes the full Drugs@FDA extract at one stable media link; the eEML print
# export is the only machine-readable route to the electronic list.
FDA_DRUGS_URL = "https://www.fda.gov/media/89850/download"
EEML_URL = "https://list.essentialmeds.org/print?format=xlsx"

WHO_ATC_LICENSE = {
    "license_name": "WHO ATC/DDD Index terms",
    "license_url": "https://atcddd.fhi.no/copyright_disclaimer/",
    "license_status": "human_review_required",
}

FDA_RARE_URL = "https://www.accessdata.fda.gov/scripts/opdlisting/oopd/"
FDA_RARE_LICENSE = {
    "license_name": "U.S. FDA public source",
    "license_url": FDA_RARE_URL,
    "license_status": "reviewed_public_government_source",
}
