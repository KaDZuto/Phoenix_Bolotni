"""
Клиент метеорологических данных для экологического контекста аудиозаписей.
Использует бесплатный открытый API Open-Meteo (без API ключей).

ВАЖНО: Погода подтягивается исключительно как внешний физический контекст для отчетов.
Нейросеть Phoenix_Bolotni НЕ прогнозирует погоду и никак не связана с метеопрогнозами.
"""

from datetime import datetime
from typing import Any, Dict, Optional
import requests

OPEN_METEO_HISTORICAL_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_weather_context(
    latitude: float,
    longitude: float,
    date_str: Optional[str] = None,
    timeout: int = 10,
) -> Dict[str, Any]:
    """
    Получение метеоданных (температура, осадки, влажность, ветер)
    для точки записи по координатам и дате (YYYY-MM-DD).
    """
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

    # Если дата сегодняшняя или в будущем — берем forecast, если прошлая — archive
    target_dt = datetime.strptime(date_str, "%Y-%m-%d")
    is_past = (datetime.utcnow() - target_dt).days > 5

    endpoint = OPEN_METEO_HISTORICAL_URL if is_past else OPEN_METEO_FORECAST_URL
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": date_str,
        "end_date": date_str,
        "hourly": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m",
        "timezone": "UTC",
    }

    try:
        resp = requests.get(endpoint, params=params, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            hourly = data.get("hourly", {})
            temps = hourly.get("temperature_2m", [])
            precip = hourly.get("precipitation", [])
            humidity = hourly.get("relative_humidity_2m", [])
            wind = hourly.get("wind_speed_10m", [])

            return {
                "status": "success",
                "source": "Open-Meteo API",
                "disclaimer": "Внешние физические метеоданные, не прогноз нейросети",
                "latitude": latitude,
                "longitude": longitude,
                "date": date_str,
                "summary": {
                    "avg_temperature_c": round(sum(temps) / len(temps), 1) if temps else None,
                    "max_temperature_c": max(temps) if temps else None,
                    "min_temperature_c": min(temps) if temps else None,
                    "total_precipitation_mm": round(sum(precip), 2) if precip else 0.0,
                    "avg_humidity_pct": round(sum(humidity) / len(humidity), 1) if humidity else None,
                    "avg_wind_speed_kmh": round(sum(wind) / len(wind), 1) if wind else None,
                },
                "hourly_data": hourly,
            }
        else:
            return {
                "status": "api_error",
                "code": resp.status_code,
                "message": f"Ошибка ответа Open-Meteo API: HTTP {resp.status_code}",
            }
    except Exception as e:
        return {
            "status": "network_error",
            "message": f"Не удалось получить метеоданные: {e}",
        }
