"""Модуль інтеграції із зовнішнім API погоди (Open-Meteo).

Це єдине місце застосунку, яке знає про HTTP: адреси сервісів, параметри
запиту, коди відповіді й формат JSON. Веб-рівень (`app/main.py`) отримує
звідси готовий словник із погодою або один із винятків `WeatherError`
і нічого не знає про `requests`.

Ухвалені рішення (розділ 4 практичної роботи):

* параметри запиту передаються словником через `params=` — `requests` сам
  екранує значення (пробіли, кирилицю, `&`), URL вручну не склеюється;
* кожен запит має timeout: 3 с на встановлення з'єднання і 10 с на
  очікування відповіді. Коли він спрацював — запит не повторюється, а
  користувач отримує зрозуміле повідомлення «спробуйте пізніше»;
* 4xx і 5xx обробляються по-різному: 5xx (і 429) — тимчасова проблема
  сервісу, 4xx — помилка в нашому власному запиті, тобто дефект застосунку;
* відсутнє поле `results` означає «місто не знайдено», будь-яка інша
  несподівана форма відповіді — окрема помилка формату;
* назовні при успіху віддається плоский словник, а помилка позначається
  винятком конкретного типу — веб-рівень сам вирішує, який HTTP-статус
  йому відповідає.
"""

import logging
from dataclasses import dataclass

import requests

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# (час на з'єднання, час на очікування відповіді) у секундах.
# Сервіс зазвичай відповідає за частки секунди, тож 10 с — це вже збій,
# а не «повільна мережа»; тримати користувача довше немає сенсу.
TIMEOUT = (3.05, 10)

# Назва міста довша за це значення — точно не назва міста.
MAX_CITY_LENGTH = 100

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Помилки
# --------------------------------------------------------------------------

class WeatherError(Exception):
    """Базова помилка отримання погоди, зрозуміла веб-рівню.

    Одного типу недостатньо: «місто не знайдено» — це помилка вводу
    користувача, «сервіс недоступний» — тимчасовий збій, «відповідь не та» —
    проблема на межі з API. Їм відповідають різні HTTP-статуси й різні
    повідомлення, тому кожен випадок — окремий підклас.

    Текст винятку написаний для користувача: у ньому немає ані кодів
    відповіді Open-Meteo, ані тексту його помилок.
    """


class InvalidCityError(WeatherError):
    """Назву міста не задано або вона некоректна (порожній рядок тощо)."""


class CityNotFoundError(WeatherError):
    """Сервіс геокодування не знає міста з такою назвою."""


class ServiceUnavailableError(WeatherError):
    """Сервіс погоди тимчасово недоступний: немає мережі, 5xx або 429."""


class ServiceTimeoutError(ServiceUnavailableError):
    """Сервіс погоди не відповів за відведений час."""


class UnexpectedResponseError(WeatherError):
    """Сервіс відповів, але не так, як ми очікували.

    Сюди потрапляють: тіло, що не є JSON; JSON без потрібних полів; а також
    4xx — сервіс відхилив запит, який склав наш код, а не користувач.
    """


# --------------------------------------------------------------------------
# Результат геокодування
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Location:
    """Знайдене місто: назва, під якою його знає сервіс, і координати."""

    name: str
    country: str
    latitude: float
    longitude: float


# --------------------------------------------------------------------------
# Допоміжні функції
# --------------------------------------------------------------------------

def _get_json(url: str, params: dict) -> dict:
    """Виконати GET-запит і повернути розібране JSON-тіло як словник.

    Усі можливі збої HTTP-рівня перетворюються тут на `WeatherError`,
    тому вище цієї функції про `requests` уже ніхто не згадує.
    """
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.Timeout as exc:
        # Timeout перевіряємо першим: ConnectTimeout є водночас і
        # ConnectionError, а нам важливо розрізнити «довго» і «недоступно».
        logger.warning("Таймаут запиту до %s: %s", url, exc)
        raise ServiceTimeoutError(
            "Сервіс погоди не відповів вчасно. Спробуйте ще раз пізніше."
        ) from exc
    except requests.RequestException as exc:
        # Немає мережі, не розв'язалося ім'я, обірвалося з'єднання тощо.
        logger.warning("Немає зв'язку з %s: %s", url, exc)
        raise ServiceUnavailableError(
            "Сервіс погоди зараз недоступний. Спробуйте ще раз пізніше."
        ) from exc

    status = response.status_code

    if status >= 500 or status == 429:
        # Проблема на боці сервісу (або він просить зачекати) — наш запит
        # правильний, виправляти нічого, лишається спробувати пізніше.
        logger.warning("Сервіс %s повернув статус %s", url, status)
        raise ServiceUnavailableError(
            "Сервіс погоди тимчасово не працює. Спробуйте ще раз пізніше."
        )

    if status >= 400:
        # Сервіс відхилив запит. Параметри складає наш код, тож це дефект
        # застосунку, а не помилка користувача. Причину (`reason`) пишемо
        # в журнал для розробника, користувачеві її не показуємо.
        logger.error(
            "Сервіс %s відхилив запит: статус %s, причина: %s",
            url, status, _error_reason(response),
        )
        raise UnexpectedResponseError(
            "Не вдалося отримати погоду: сервіс відхилив запит застосунку."
        )

    if status != 200:
        logger.error("Несподіваний статус %s від %s", status, url)
        raise UnexpectedResponseError(
            "Не вдалося отримати погоду: несподівана відповідь сервісу."
        )

    try:
        data = response.json()
    except ValueError as exc:
        # Статус 200, але тіло — не JSON (наприклад, HTML-сторінка проксі).
        logger.error("Відповідь %s не є JSON: %s", url, exc)
        raise UnexpectedResponseError(
            "Не вдалося отримати погоду: сервіс повернув незрозумілу відповідь."
        ) from exc

    if not isinstance(data, dict):
        logger.error("Відповідь %s — не JSON-об'єкт: %r", url, type(data))
        raise UnexpectedResponseError(
            "Не вдалося отримати погоду: сервіс повернув незрозумілу відповідь."
        )

    return data


def _error_reason(response) -> str:
    """Дістати поле `reason` з тіла помилки — лише для запису в журнал."""
    try:
        body = response.json()
    except ValueError:
        return "<тіло не є JSON>"
    if isinstance(body, dict):
        return str(body.get("reason", "<без пояснення>"))
    return "<без пояснення>"


def _is_number(value) -> bool:
    """Перевірити, що значення — число (bool у Python теж int, тож відсікаємо)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _bad_format(what: str) -> UnexpectedResponseError:
    """Записати в журнал, чого бракує у відповіді, і повернути помилку формату."""
    logger.error("Несподіваний формат відповіді: %s", what)
    return UnexpectedResponseError(
        "Не вдалося отримати погоду: сервіс повернув неповні дані."
    )


# --------------------------------------------------------------------------
# Публічні функції модуля
# --------------------------------------------------------------------------

def find_city(name: str) -> Location:
    """Знайти координати міста за його назвою.

    Повертає `Location`. Якщо назва порожня — `InvalidCityError`,
    якщо міста з такою назвою немає — `CityNotFoundError`.
    """
    name = (name or "").strip()
    if not name:
        raise InvalidCityError("Введіть назву міста.")
    if len(name) > MAX_CITY_LENGTH:
        raise InvalidCityError("Назва міста надто довга.")

    data = _get_json(
        GEOCODING_URL,
        params={
            "name": name,
            "count": 1,        # потрібен лише найкращий збіг
            "language": "uk",  # назви міста й країни українською
            "format": "json",
        },
    )

    # Коли місто не знайдено, ключа `results` у відповіді немає взагалі
    # (див. samples/geocoding-not-found.json), тому беремо його через .get().
    results = data.get("results")
    if results is None or results == []:
        raise CityNotFoundError(f"Місто «{name}» не знайдено. Перевірте назву.")

    if not isinstance(results, list) or not isinstance(results[0], dict):
        raise _bad_format("поле results не є списком об'єктів")

    first = results[0]
    latitude = first.get("latitude")
    longitude = first.get("longitude")
    if not _is_number(latitude) or not _is_number(longitude):
        raise _bad_format("у результаті геокодування немає координат")

    return Location(
        name=str(first.get("name") or name),
        country=str(first.get("country") or ""),
        latitude=float(latitude),
        longitude=float(longitude),
    )


def get_current_weather(city: str) -> dict:
    """Повернути поточну погоду в місті: температуру й швидкість вітру.

    Це функція, яку викликає веб-рівень. Вона поєднує геокодування і запит
    прогнозу та віддає звичайний словник, готовий до перетворення на JSON:

        {
            "city": "Київ", "country": "Україна",
            "latitude": 50.45466, "longitude": 30.5238,
            "temperature": 23.6, "temperature_unit": "°C",
            "wind_speed": 5.2, "wind_speed_unit": "km/h",
            "time": "2026-08-31T19:45",
        }

    У разі збою підіймає один із підкласів `WeatherError`.
    """
    location = find_city(city)

    data = _get_json(
        FORECAST_URL,
        params={
            "latitude": location.latitude,
            "longitude": location.longitude,
            "current": "temperature_2m,wind_speed_10m",
        },
    )

    current = data.get("current")
    if not isinstance(current, dict):
        raise _bad_format("у прогнозі немає об'єкта current")

    temperature = current.get("temperature_2m")
    wind_speed = current.get("wind_speed_10m")
    if not _is_number(temperature) or not _is_number(wind_speed):
        raise _bad_format("у current немає temperature_2m або wind_speed_10m")

    # Одиниці вимірювання лежать окремо, в current_units. Без них значення
    # лишаються правильними, тож їхня відсутність — не привід для помилки:
    # підставляємо одиниці, які Open-Meteo використовує за замовчуванням.
    units = data.get("current_units")
    if not isinstance(units, dict):
        units = {}

    # Координати беремо з геокодування, а не з прогнозу: прогноз повертає
    # вузол своєї сітки (50.4375, 30.5), а не точку, яку ми запитували.
    return {
        "city": location.name,
        "country": location.country,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "temperature": temperature,
        "temperature_unit": str(units.get("temperature_2m") or "°C"),
        "wind_speed": wind_speed,
        "wind_speed_unit": str(units.get("wind_speed_10m") or "km/h"),
        "time": current.get("time"),
    }
