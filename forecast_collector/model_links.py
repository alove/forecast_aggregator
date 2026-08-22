from __future__ import annotations

"""Human-facing published model URLs for exported forecast observations."""


def model_web_url_for(vendor: str, metric_type: str) -> str:
    vendor = (vendor or "").strip()
    metric_type = (metric_type or "").strip()

    if vendor == "ElectIndex":
        return "https://electindex.com/forecasts/"

    if vendor == "Grant Williams":
        return "https://grantbw4.github.io/2026-midterms-forecast/"

    if vendor == "Election StatSheet":
        if metric_type.startswith("US Senate"):
            return "https://www.electionstatsheet.com/senate"
        if metric_type.startswith("US House District"):
            return "https://www.electionstatsheet.com/districts"
        if metric_type.startswith("US House"):
            return "https://www.electionstatsheet.com/house"
        return "https://www.electionstatsheet.com/"

    if vendor == "Race to the WH":
        if metric_type.startswith("US Senate"):
            return "https://www.racetothewh.com/senate/26"
        return "https://www.racetothewh.com/house"

    if vendor == "Kalshi":
        if metric_type.startswith("US Senate"):
            return "https://kalshi.com/category/elections/midterms"
        return "https://kalshi.com/category/elections/midterms/house"

    return ""
