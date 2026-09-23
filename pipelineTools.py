import os
import csv
import json
import re
import tomllib  # Python 3.11+

from importICS import load_events_from_ics
from loggingTools import Logger, LogAgg
from geocodeCache import GeocodeCache
from geocode import geocode_events
from htmlTools import (
    write_map_html,
    copy_map_assets_to_output_dir,
)


def compile_location_replacements(cfg, logger):
    """
    Compile [importICS.locationReplacements] into a list of (regex, replacement) rules.
    Invalid patterns are logged and skipped; rules keep config order.
    """
    rules = []
    for pattern, replacement in cfg.get("importICS", {}).get("locationReplacements", {}).items():
        try:
            rules.append((re.compile(pattern), replacement))
        except re.error as e:
            logger.warn(f"Location replacement skipped pattern={pattern!r} error={e}")
    return rules


def apply_location_replacements(events, rules, logger):
    """
    Rewrite ev.location_text in place; first rule matching wins.
    """
    for ev in events:
        for regex, replacement in rules:
            new, n = regex.subn(replacement, ev.location_text, count=1)
            if n:
                logger.info(f"Location replaced '{ev.location_text}' -> '{new}' (pattern {regex.pattern!r})")
                ev.location_text = new
                break


def load_config_and_init_logging(logger_output_dir: str):
    """
    Load config.toml and set up logger and aggregates.
    Returns (cfg, output_dir, logger, agg).
    """
    # CAREFUL: could fail if config.toml missing/invalid; keep it obvious at top-level.
    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)

    output_dir = cfg["outputDir"]
    os.makedirs(output_dir, exist_ok=True)

    # Use the provided logger_output_dir for logs; fall back to output_dir if empty/None.
    if not logger_output_dir:
        logger_output_dir = output_dir

    os.makedirs(logger_output_dir, exist_ok=True)
    log_path = os.path.join(logger_output_dir, "log.txt")
    logger = Logger(log_path)
    agg = LogAgg()
    return cfg, output_dir, logger, agg


def optionally_load_geocode_credentials(_logger):
    """
    Load geocodeCredentialsFile.toml if present.
    (Kept for future use; currently the credentials are not wired into geocode.py.)
    """
    if not os.path.exists("geocodeCredentialsFile.toml"):
        return

    # Marked: currently loaded but not used; still safe to keep.
    with open("geocodeCredentialsFile.toml", "rb") as f:
        _ = tomllib.load(f)


def load_selected_ics_events(cfg, output_dir, agg, logger):
    """
    Load ICS events filtered by configured date range and location constraints.
    Returns selected events list.
    """
    import_failed_csv_path = os.path.join(
        output_dir,
        cfg["importICS"]["importFailedCsv"]
    )

    ics_path = cfg["icsPath"]
    date_from = cfg["importICS"]["dateFrom"]
    date_to = cfg["importICS"]["dateTo"]
    require_location = cfg["importICS"].get("requireLocation", True)

    agg.totalSelectedEvents = 0
    agg.totalImportFailed = 0

    events = load_events_from_ics(
        ics_path=ics_path,
        date_from=date_from,
        date_to=date_to,
        require_location=require_location,
        failed_csv_path=import_failed_csv_path,
        logger=logger,
        agg=agg,
    )

    rules = compile_location_replacements(cfg, logger)
    if rules:
        apply_location_replacements(events, rules, logger)

    agg.totalSelectedEvents = len(events)

    logger.info(
        f"Imported events selected by date range: {len(events)} from '{ics_path}'"
    )
    return events


def run_geocoding_and_write_map_data(cfg, events, agg, logger, output_dir):
    """
    Geocode events with caching + rate limiting, then write mapData.csv.
    Returns map_rows count and/or failed rows.
    """
    geo_cfg = cfg["geoCode"]
    countries = cfg["importICS"]["countries"]
    rate_limit_seconds = float(geo_cfg["rateLimitSeconds"])
    geocoder_language = geo_cfg.get("geocoderLanguage", "en")
    use_cache = bool(geo_cfg.get("useGeocodeCache", True))

    geocode_cache_csv = os.path.join(output_dir, geo_cfg["geocodeCacheCsv"])
    failed_csv_path = os.path.join(output_dir, geo_cfg["geocodeFailedCsv"])
    map_data_csv_path = os.path.join(output_dir, geo_cfg["mapDataCsv"])

    geocode_cache = GeocodeCache(geocode_cache_csv, logger=logger) if use_cache else None
    if geocode_cache:
        geocode_cache.load()

    dedupe = bool(geo_cfg.get("dedupeByLocationText", True))

    fail_cache_csv = geo_cfg.get("failCacheCsv", "geocodeFailCache.csv")
    fail_cache_ttl_hours = float(geo_cfg.get("failCacheTtlHours", 720))

    # Marked: external web access. Keep this try/except for better logs later.
    try:
        map_rows, _ = geocode_events(
            events=events,
            allowed_country_codes=countries,
            rate_limit_seconds=rate_limit_seconds,
            geocoder_language=geocoder_language,
            geocode_cache=geocode_cache,
            failed_csv_path=failed_csv_path,
            logger=logger,
            agg=agg,
            dedupe_by_location_text=dedupe,
            fail_cache_path=os.path.join(output_dir, fail_cache_csv),
            fail_cache_ttl_hours=fail_cache_ttl_hours,
        )
    except Exception as e:
        logger.error(f"geocode_events failed: {type(e).__name__}: {e}")
        raise

    if geocode_cache:
        geocode_cache.save()

    # Write mapData.csv (this drives map.html)
    with open(map_data_csv_path, "w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "summary",
            "uid",
            "date",
            "date_end",
            "location_text",
            "lat",
            "lon",
            "display_name",
            "country_code",
        ]
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            quotechar='"',
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        for r in map_rows:
            writer.writerow(r)

        logger.info(f"Wrote map data rows: {len(map_rows)} to {map_data_csv_path}")

    map_data_json_path = os.path.join(output_dir, geo_cfg["mapDataCsv"]).replace(".csv", ".json")
    with open(map_data_json_path, "w", encoding="utf-8") as f:
        json.dump(map_rows, f, ensure_ascii=False)

    logger.info(f"Wrote map data rows: {len(map_rows)} to {map_data_json_path}")
    return map_rows


def create_map_html_with_assets(cfg, output_dir, logger):
    """Create HTML map output and copy required assets into the output directory."""
    # CAREFUL: keep the call order identical to the old main() sequence.
    copy_map_assets_to_output_dir(output_dir, logger)
    write_map_html(cfg, output_dir, logger)