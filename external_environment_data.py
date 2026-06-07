import json
import os
import re
from datetime import datetime

import requests


ORENBURG_BBOX = "50.0,50.0,62.0,54.5"
OVERPASS_URL = os.getenv(
    "OVERPASS_URL",
    "https://overpass-api.de/api/interpreter"
)
OPENAQ_API_URL = "https://api.openaq.org/v3"
OPEN_METEO_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

ORENBURG_AIR_POINTS = [
    ("Оренбург", 51.7682, 55.0970),
    ("Орск", 51.2293, 58.4752),
    ("Новотроицк", 51.2030, 58.3267),
    ("Бузулук", 52.7881, 52.2623),
    ("Бугуруслан", 53.6523, 52.4326),
    ("Гай", 51.4649, 58.4436),
    ("Соль-Илецк", 51.1631, 54.9918),
    ("Медногорск", 51.4128, 57.5950),
    ("Кувандык", 51.4781, 57.3612),
    ("Абдулино", 53.6778, 53.6470),
    ("Сорочинск", 52.4269, 53.1542),
    ("Ясный", 51.0369, 59.8743),
    ("Акбулак", 51.0019, 55.6172),
    ("Илек", 51.5271, 53.3831),
    ("Тюльган", 52.3405, 56.1665),
]


def ensure_environment_columns(db):
    forest_columns = {row["name"] for row in db.execute("PRAGMA table_info(forest_areas)").fetchall()}
    pollution_columns = {row["name"] for row in db.execute("PRAGMA table_info(pollution_points)").fetchall()}

    if "source" not in forest_columns:
        db.execute("ALTER TABLE forest_areas ADD COLUMN source TEXT DEFAULT 'manual'")
    if "source_id" not in forest_columns:
        db.execute("ALTER TABLE forest_areas ADD COLUMN source_id TEXT")
    if "category" not in forest_columns:
        db.execute("ALTER TABLE forest_areas ADD COLUMN category TEXT")
    if "source" not in pollution_columns:
        db.execute("ALTER TABLE pollution_points ADD COLUMN source TEXT DEFAULT 'manual'")
    if "source_id" not in pollution_columns:
        db.execute("ALTER TABLE pollution_points ADD COLUMN source_id TEXT")
    if "measured_at" not in pollution_columns:
        db.execute("ALTER TABLE pollution_points ADD COLUMN measured_at TEXT")
    db.execute("""
        CREATE TABLE IF NOT EXISTS region_boundaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            coordinates TEXT NOT NULL,
            source TEXT DEFAULT 'overpass',
            source_id TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.commit()


def _overpass_query():
    return """
    [out:json][timeout:120];
    area["ISO3166-2"="RU-ORE"]["boundary"="administrative"]->.region;
    (
      way(area.region)["boundary"="protected_area"];
      relation(area.region)["boundary"="protected_area"];
      way(area.region)["boundary"="national_park"];
      relation(area.region)["boundary"="national_park"];
      way(area.region)["leisure"="nature_reserve"];
      relation(area.region)["leisure"="nature_reserve"];
      way(area.region)["landuse"="forest"];
      relation(area.region)["landuse"="forest"];
      way(area.region)["natural"="wood"];
      relation(area.region)["natural"="wood"];
    );
    out tags geom;
    """


def _boundary_query():
    return """
    [out:json][timeout:120];
    relation["ISO3166-2"="RU-ORE"]["boundary"="administrative"];
    out geom;
    """


def _element_geometry(element):
    if element.get("geometry"):
        return [[point["lat"], point["lon"]] for point in element["geometry"]]

    for member in element.get("members", []):
        if member.get("role") in ("outer", "") and member.get("geometry"):
            return [[point["lat"], point["lon"]] for point in member["geometry"]]

    return []


def _forest_status(tags):
    if (
        tags.get("boundary") in ("protected_area", "national_park")
        or tags.get("leisure") == "nature_reserve"
        or tags.get("protect_class")
    ):
        return "protected"
    return "forest"


def _forest_category(tags):
    if tags.get("boundary") == "national_park":
        return "Национальный парк"
    if tags.get("boundary") == "protected_area":
        return "Охраняемая природная территория"
    if tags.get("leisure") == "nature_reserve":
        return "Природный резерват"
    if tags.get("natural") == "wood":
        return "Природная лесная территория"
    return "Лесная территория"


def sync_forests_from_overpass(db, max_features=80):
    ensure_environment_columns(db)
    response = requests.post(
        OVERPASS_URL,
        data={"data": _overpass_query()},
        headers={"User-Agent": "OrenEco diploma project"},
        timeout=180,
    )
    response.raise_for_status()
    payload = response.json()

    imported = 0
    protected_imported = 0
    forest_imported = 0
    protected_limit = max_features // 2
    forest_limit = max_features - protected_limit
    db.execute("UPDATE forest_areas SET is_active = 0 WHERE source = 'overpass'")

    elements = sorted(
        payload.get("elements", []),
        key=lambda item: 0 if _forest_status(item.get("tags", {})) == "protected" else 1
    )

    for element in elements:
        tags = element.get("tags", {})
        coordinates = _element_geometry(element)
        if len(coordinates) < 4:
            continue

        name = tags.get("name") or _forest_category(tags)
        status = _forest_status(tags)
        if status == "protected":
            if protected_imported >= protected_limit:
                continue
        elif forest_imported >= forest_limit:
            continue

        category = _forest_category(tags)
        source_id = f"{element.get('type')}/{element.get('id')}"
        description = "Данные загружены из OpenStreetMap через Overpass API"

        db.execute(
            """
            INSERT INTO forest_areas
                (name, coordinates, status, description, area, is_active, source, source_id, category)
            VALUES (?, ?, ?, ?, ?, 1, 'overpass', ?, ?)
            """,
            (
                name,
                json.dumps(coordinates, ensure_ascii=False),
                status,
                description,
                tags.get("area"),
                source_id,
                category,
            ),
        )
        imported += 1
        if status == "protected":
            protected_imported += 1
        else:
            forest_imported += 1

        if protected_imported >= protected_limit and forest_imported >= forest_limit:
            break

    db.commit()
    return {
        "imported": imported,
        "protected_imported": protected_imported,
        "forest_imported": forest_imported,
        "source": "overpass"
    }


def sync_region_boundary_from_overpass(db):
    ensure_environment_columns(db)
    response = requests.post(
        OVERPASS_URL,
        data={"data": _boundary_query()},
        headers={"User-Agent": "OrenEco diploma project"},
        timeout=180,
    )
    response.raise_for_status()
    payload = response.json()

    segments = []
    source_id = None
    name = "Оренбургская область"

    for element in payload.get("elements", []):
        if element.get("type") != "relation":
            continue
        source_id = f"relation/{element.get('id')}"
        tags = element.get("tags", {})
        name = tags.get("name") or name
        for member in element.get("members", []):
            if member.get("role") not in ("outer", ""):
                continue
            geometry = member.get("geometry") or []
            coords = [[point["lat"], point["lon"]] for point in geometry if "lat" in point and "lon" in point]
            if len(coords) >= 2:
                segments.append(coords)

    if not segments:
        return {"imported": 0, "source": "overpass"}

    db.execute("UPDATE region_boundaries SET is_active = 0 WHERE source = 'overpass'")
    db.execute(
        """
        INSERT INTO region_boundaries (name, coordinates, source, source_id, is_active)
        VALUES (?, ?, 'overpass', ?, 1)
        """,
        (name, json.dumps(segments, ensure_ascii=False), source_id),
    )
    db.commit()
    return {"imported": len(segments), "source": "overpass", "name": name}


def _pollution_percent(parameter, value):
    thresholds = {
        "pm25": (12, 35),
        "pm10": (45, 100),
        "so2": (50, 125),
        "no2": (40, 100),
        "co": (4, 10),
        "o3": (100, 180),
    }
    low, high = thresholds.get(parameter.lower(), (50, 100))
    if value <= low:
        return 35
    if value >= high:
        return 90
    return round(35 + ((value - low) / (high - low)) * 55)


def _aqi_pollution_percent(aqi):
    if aqi is None:
        return 0
    return max(1, min(100, round(aqi)))


def _format_openmeteo_pollutants(current):
    labels = {
        "pm10": "PM10",
        "pm2_5": "PM2.5",
        "nitrogen_dioxide": "NO2",
        "sulphur_dioxide": "SO2",
        "carbon_monoxide": "CO",
        "ozone": "O3",
    }
    result = []
    for key, label in labels.items():
        value = current.get(key)
        if value is not None:
            result.append(f"{label}: {value}")
    return result


def _default_air_explanation(name, pollution, pollutants):
    return (
        f"Индекс качества воздуха для точки «{name}» составляет {pollution}. "
        "Чем выше индекс, тем хуже качество воздуха; перечисленные вещества показывают расчетные концентрации основных загрязнителей."
    )


def _build_air_prompt(name, pollution, pollutants):
    return (
        "Ты экологический эксперт. Дай строго одно короткое пояснение на русском языке для карты загрязнения. "
        "Без приветствия, без списков, без markdown, максимум 2 предложения. "
        "Объясни, что значит этот индекс качества воздуха и какие вещества важны. "
        f"Город: {name}. European AQI: {pollution}. Вещества: {', '.join(pollutants)}."
    )


def _clean_ai_explanation(text):
    text = re.sub(r"Оценка\s*:\s*\d{1,2}\s*/\s*10", "", text or "", flags=re.IGNORECASE)
    text = re.sub(r"\n+", " ", text)
    return text.strip()


def sync_pollution_from_openmeteo(db, ai_explainer=None):
    ensure_environment_columns(db)
    records = []

    for name, lat, lon in ORENBURG_AIR_POINTS:
        response = requests.get(
            OPEN_METEO_AIR_QUALITY_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "european_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone",
                "timezone": "Europe/Moscow",
            },
            timeout=30,
        )
        response.raise_for_status()
        current = response.json().get("current", {})
        aqi = current.get("european_aqi")
        if aqi is None:
            continue

        pollutants = _format_openmeteo_pollutants(current)
        if ai_explainer:
            explanation = _clean_ai_explanation(
                ai_explainer(_build_air_prompt(name, _aqi_pollution_percent(aqi), pollutants))
            )
        else:
            explanation = _default_air_explanation(name, _aqi_pollution_percent(aqi), pollutants)

        records.append({
            "name": name,
            "lat": lat,
            "lon": lon,
            "pollution": _aqi_pollution_percent(aqi),
            "pollutants": pollutants,
            "description": (
                f"{explanation} "
                "Данные качества воздуха получены из Open-Meteo Air Quality API."
            ),
            "measured_at": current.get("time"),
        })

    if not records:
        return {"imported": 0, "source": "openmeteo"}

    db.execute("UPDATE pollution_points SET is_active = 0")
    for record in records:
        db.execute(
            """
            INSERT INTO pollution_points
                (name, lat, lon, pollution, pollutants, description, is_active, source, source_id, measured_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'openmeteo', ?, ?)
            """,
            (
                record["name"],
                record["lat"],
                record["lon"],
                record["pollution"],
                json.dumps(record["pollutants"], ensure_ascii=False),
                record["description"],
                record["name"].lower(),
                record["measured_at"],
            ),
        )

    db.commit()
    return {"imported": len(records), "source": "openmeteo"}


def _openaq_headers():
    api_key = os.getenv("OPENAQ_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAQ_API_KEY is not set")
    return {"X-API-Key": api_key}


def _get_latest_measurements(location_id):
    response = requests.get(
        f"{OPENAQ_API_URL}/locations/{location_id}/latest",
        headers=_openaq_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def sync_pollution_from_openaq(db, max_locations=20):
    ensure_environment_columns(db)
    locations_response = requests.get(
        f"{OPENAQ_API_URL}/locations",
        headers=_openaq_headers(),
        params={"bbox": ORENBURG_BBOX, "limit": max_locations, "iso": "RU"},
        timeout=30,
    )
    locations_response.raise_for_status()
    locations = locations_response.json().get("results", [])

    imported = 0
    db.execute("UPDATE pollution_points SET is_active = 0 WHERE source = 'openaq'")

    for location in locations:
        coords = location.get("coordinates") or {}
        lat = coords.get("latitude")
        lon = coords.get("longitude")
        if lat is None or lon is None:
            continue

        latest = _get_latest_measurements(location["id"])
        valid = []
        for item in latest:
            parameter = (item.get("parameter") or {}).get("name") or item.get("parameter")
            value = item.get("value")
            if parameter and isinstance(value, (int, float)):
                valid.append((parameter, value, item))

        if not valid:
            continue

        main_parameter, main_value, main_item = max(
            valid,
            key=lambda row: _pollution_percent(row[0], row[1])
        )
        pollution = _pollution_percent(main_parameter, main_value)
        pollutants = [f"{parameter}: {value}" for parameter, value, _ in valid]
        measured_at = (main_item.get("datetime") or {}).get("utc") or datetime.utcnow().isoformat()

        db.execute(
            """
            INSERT INTO pollution_points
                (name, lat, lon, pollution, pollutants, description, is_active, source, source_id, measured_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'openaq', ?, ?)
            """,
            (
                location.get("name") or f"OpenAQ station {location['id']}",
                lat,
                lon,
                pollution,
                json.dumps(pollutants, ensure_ascii=False),
                "Данные качества воздуха загружены из OpenAQ",
                str(location["id"]),
                measured_at,
            ),
        )
        imported += 1

    db.commit()
    return {"imported": imported, "source": "openaq", "locations_found": len(locations)}
