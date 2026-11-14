"""Веб-рівень застосунку: сторінка з формою і JSON-ендпоінт.

Цей файл не знає про `requests`, адреси сервісів і коди їхніх відповідей —
усе це лишається в `app/weather.py`. Тут вирішується інше: що застосунок
віддає клієнтові та з яким HTTP-статусом.

Запуск із папки роботи:

    uvicorn app.main:app --reload

Далі відкрийте http://127.0.0.1:8000
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import weather

logger = logging.getLogger(__name__)

app = FastAPI(title="Погода — ПР1")

INDEX_PAGE = Path(__file__).parent / "templates" / "index.html"

# Яка помилка модуля інтеграції якому HTTP-статусу відповідає.
# Порядок важливий: підкласи стоять перед своїми базовими класами.
#
#   400 — клієнт надіслав некоректний запит (порожня назва міста);
#   404 — запит коректний, але такого міста немає;
#   504 — наш сервер не дочекався відповіді зовнішнього сервісу;
#   503 — зовнішній сервіс тимчасово недоступний, варто спробувати пізніше;
#   502 — зовнішній сервіс відповів, але відповідь непридатна.
#
# Збої зовнішнього сервісу — це 5xx, а не 4xx: користувач зробив усе
# правильно, проблема на нашому боці ланцюжка.
ERROR_STATUSES = (
    (weather.InvalidCityError, 400, "invalid_city"),
    (weather.CityNotFoundError, 404, "city_not_found"),
    (weather.ServiceTimeoutError, 504, "service_timeout"),
    (weather.ServiceUnavailableError, 503, "service_unavailable"),
    (weather.UnexpectedResponseError, 502, "bad_service_response"),
)


@app.exception_handler(weather.WeatherError)
def handle_weather_error(request: Request, exc: weather.WeatherError) -> JSONResponse:
    """Перетворити помилку модуля інтеграції на JSON-відповідь зі статусом."""
    for error_type, status, code in ERROR_STATUSES:
        if isinstance(exc, error_type):
            break
    else:
        status, code = 500, "weather_error"

    return JSONResponse(
        status_code=status,
        content={"error": code, "message": str(exc)},
    )


@app.exception_handler(Exception)
def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Остання лінія захисту: будь-яка непередбачена помилка — теж JSON.

    Подробиці йдуть у журнал сервера, клієнт бачить лише загальний текст.
    """
    logger.error(
        "Непередбачена помилка під час обробки %s", request.url.path, exc_info=exc
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "Внутрішня помилка застосунку. Спробуйте ще раз пізніше.",
        },
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Віддати сторінку з формою вводу міста."""
    return INDEX_PAGE.read_text(encoding="utf-8")


@app.get("/api/weather")
def api_weather(city: str = ""):
    """Повернути поточну погоду в місті у форматі JSON.

    Успіх — статус 200 і словник із погодою. Будь-який збій модуль
    інтеграції позначає винятком `WeatherError`; обробник вище перетворює
    його на відповідь виду {"error": "<код>", "message": "<текст>"}.

    Параметр `city` має значення за замовчуванням, щоб і відсутній, і
    порожній параметр давали однакову відповідь 400 у нашому форматі,
    а не стандартну 422 від FastAPI.
    """
    return weather.get_current_weather(city)
