import csv
import os
import re
import time
from typing import List, Dict, Optional, Tuple

from geopy.geocoders import Nominatim

from geocodeCache import GeocodeCache, GeocodeResult
from importICS import IcsEvent
from loggingTools import Logger, LogAgg


def _safe_get_country_code(geocode_raw: dict) -> str:
    """
    Extract a country code if available; else return empty string.
    Nominatim 'raw' structure may vary.
    """
    addr = geocode_raw.get("address") or {}
    # Sometimes 'country_code' exists; sometimes only 'country' exists.
    cc = (addr.get("country_code") or "").strip().upper()
    return cc


# URL - not geocodable address (don't waste a rate-limited call)
_ONLINE_RE = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*://|www\.)", re.I)
# UK postcode fallback
_UK_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)
_COUNTRY_ALIASES = {"UK": "GB", "GB": "UK"}


def _country_allowed(cc: str, allowed_set: set) -> bool:
    """default: allow"""
    if not cc or not allowed_set:
        return True
    cc = cc.upper()
    return cc in allowed_set or _COUNTRY_ALIASES.get(cc) in allowed_set


def geocode_events(
    events: List[IcsEvent],
    allowed_country_codes: List[str],
    rate_limit_seconds: float,
    geocoder_language: str,
    geocode_cache: Optional[GeocodeCache],
    failed_csv_path: str,
    logger: Logger,
    agg: LogAgg,
    dedupe_by_location_text: bool = True
) -> Tuple[List[dict], List[dict]]:
    """
    Main geocoding loop:
    - geocode only unique LOCATION strings (if dedupe_by_location_text=True)
    - reuse cached successful results
    - write per-event failures to geocodeFailed.csv (human readable)
    - return map rows (per event, not per location)

    Output row schema used by makeMap/pipelineTools:
    summary, uid, date, date_end, location_text, lat, lon, display_name, country_code
    """
    # Ensure failed csv exists and has header
    os.makedirs(os.path.dirname(failed_csv_path) or ".", exist_ok=True)
    file_exists = os.path.exists(failed_csv_path)
    if not file_exists:
        with open(failed_csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["location_text", "event_date", "event_end", "event_summary", "error"],
                quotechar='"',
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()

    geolocator = Nominatim(user_agent="ics-location-to-map")

    # NOTE: user-agent can be overridden in a follow-up step.
    allowed_set = set([c.upper() for c in allowed_country_codes])

    # Per LOCATION cache
    loc_to_success: Dict[str, GeocodeResult] = {}
    loc_to_error: Dict[str, str] = {}

    # Deduplicate location texts to reduce API calls
    if dedupe_by_location_text:
        unique_locs = sorted({ev.location_text for ev in events if ev.location_text})
    else:
        unique_locs = [ev.location_text for ev in events if ev.location_text]

    # Geocode unique locations (or attempts)
    for loc_text in unique_locs:
        if not loc_text:
            continue

        # Check geocode cache first
        if geocode_cache:
            cached = geocode_cache.get(loc_text)
            if cached:
                loc_to_success[loc_text] = cached
                continue

        if _ONLINE_RE.match(loc_text):
            loc_to_error[loc_text] = "url_not_a_location (online event)"
            agg.totalGeocodeFailed += 1
            logger.warn(f"Geocode SKIP location='{loc_text}' reason=online_event_url")
            continue

        # query as-is; fallback to postcode
        candidates = [loc_text]
        m = _UK_POSTCODE_RE.search(loc_text)
        if m:
            pc = re.sub(r"\s+", " ", m.group(1))
            if pc.upper() != loc_text.strip().upper():
                candidates.append(pc)

        # Geocode with throttling
        last_error = "not_found"
        for query in candidates:
            try:
                r = geolocator.geocode(query, timeout=20, language=geocoder_language)
                time.sleep(rate_limit_seconds)
            except Exception as ex:
                last_error = f"exception:{type(ex).__name__}"
                logger.error(f"Geocode EX location='{loc_text}' query='{query}' exception={last_error}")
                continue

            if not r:
                continue

            raw = (r.raw or {})
            cc = _safe_get_country_code(raw)

            # Country filtering
            if not _country_allowed(cc, allowed_set):
                last_error = f"outside_allowed_countries (cc={cc})"
                continue

            result = GeocodeResult(
                lat=float(r.latitude),
                lon=float(r.longitude),
                display_name=r.address or "",
                country_code=cc
            )

            loc_to_success[loc_text] = result
            if geocode_cache:
                geocode_cache.set(loc_text, result)

            agg.totalGeocodeSucceeded += 1
            logger.info(
                f"Geocode OK location='{loc_text}' (via '{query}') -> ({result.lat},{result.lon}) cc={result.country_code}"
            )
            break
        else:
            loc_to_error[loc_text] = last_error
            agg.totalGeocodeFailed += 1
            logger.warn(f"Geocode FAIL location='{loc_text}' reason={last_error}")

    # Build map rows per event
    map_rows: List[dict] = []
    failed_rows: List[dict] = []

    for ev in events:
        if not ev.location_text:
            agg.totalGeocodeMissingLocation += 1
            logger.warn(f"Missing LOCATION in event uid={ev.uid} date={ev.date}")

            failed_rows.append({
                "location_text": "",
                "event_date": ev.date,
                "event_end": ev.date_end,
                "event_summary": ev.summary,
                "error": "missing_location_text"
            })
            continue

        loc = ev.location_text

        if loc in loc_to_success:
            s = loc_to_success[loc]
            map_rows.append({
                "summary": ev.summary,
                "uid": ev.uid or "",
                "date": ev.date,
                "date_end": ev.date_end,
                "location_text": ev.location_text,
                "lat": s.lat,
                "lon": s.lon,
                "display_name": s.display_name,
                "country_code": s.country_code
            })
            continue

        # not in success means failure/ambiguous review
        err = loc_to_error.get(loc, "unknown_error")
        failed_rows.append({
            "location_text": loc,
            "event_date": ev.date,
            "event_end": ev.date_end,
            "event_summary": ev.summary,
            "error": err
        })

    # Append failures to CSV
    with open(failed_csv_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["location_text", "event_date", "event_end", "event_summary", "error"],
            quotechar='"',
            quoting=csv.QUOTE_ALL,
        )
        for row in failed_rows:
            writer.writerow(row)

    return map_rows, failed_rows