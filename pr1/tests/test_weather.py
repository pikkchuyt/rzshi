"""Перевірка модуля інтеграції без звернення до мережі.

Замість справжніх HTTP-запитів підставляються збережені відповіді з папки
`samples/` і штучні збої. Запуск із папки роботи:

    python -m unittest discover -s tests -v
"""

import json
import unittest
from pathlib import Path
from unittest import mock

import requests

from app import weather

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def sample(name: str):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


class FakeResponse:
    """Мінімальна заміна requests.Response: статус і тіло."""

    def __init__(self, status_code=200, body=None, not_json=False):
        self.status_code = status_code
        self._body = body
        self._not_json = not_json

    def json(self):
        if self._not_json:
            raise ValueError("not json")
        return self._body


def fake_get(*responses):
    """Підмінити requests.get: кожен виклик повертає наступну відповідь."""
    return mock.patch.object(weather.requests, "get", side_effect=list(responses))


class FindCityTests(unittest.TestCase):
    def test_city_found(self):
        with fake_get(FakeResponse(body=sample("geocoding-kyiv.json"))) as get:
            location = weather.find_city("  Київ ")

        self.assertEqual(location.name, "Київ")
        self.assertEqual(location.country, "Україна")
        self.assertEqual((location.latitude, location.longitude), (50.45466, 30.5238))

        # Параметри передано словником, timeout задано, назву обрізано.
        _, kwargs = get.call_args
        self.assertEqual(kwargs["params"]["name"], "Київ")
        self.assertEqual(kwargs["timeout"], weather.TIMEOUT)

    def test_city_not_found_has_no_results_key(self):
        with fake_get(FakeResponse(body=sample("geocoding-not-found.json"))):
            with self.assertRaises(weather.CityNotFoundError):
                weather.find_city("Абвгдеж")

    def test_empty_name_does_not_call_api(self):
        for name in ("", "   ", None):
            with fake_get() as get:
                with self.assertRaises(weather.InvalidCityError):
                    weather.find_city(name)
                get.assert_not_called()

    def test_too_long_name(self):
        with fake_get() as get:
            with self.assertRaises(weather.InvalidCityError):
                weather.find_city("я" * 500)
            get.assert_not_called()

    def test_result_without_coordinates(self):
        with fake_get(FakeResponse(body={"results": [{"name": "Київ"}]})):
            with self.assertRaises(weather.UnexpectedResponseError):
                weather.find_city("Київ")

    def test_results_of_wrong_type(self):
        with fake_get(FakeResponse(body={"results": "oops"})):
            with self.assertRaises(weather.UnexpectedResponseError):
                weather.find_city("Київ")


class GetCurrentWeatherTests(unittest.TestCase):
    def test_success(self):
        with fake_get(
            FakeResponse(body=sample("geocoding-kyiv.json")),
            FakeResponse(body=sample("forecast-kyiv.json")),
        ) as get:
            result = weather.get_current_weather("Київ")

        self.assertEqual(result["city"], "Київ")
        self.assertEqual(result["temperature"], 23.6)
        self.assertEqual(result["temperature_unit"], "°C")
        self.assertEqual(result["wind_speed"], 5.2)
        self.assertEqual(result["wind_speed_unit"], "km/h")
        # Координати — з геокодування, а не вузол сітки з прогнозу.
        self.assertEqual(result["latitude"], 50.45466)

        _, kwargs = get.call_args  # другий виклик — прогноз
        self.assertEqual(kwargs["params"]["latitude"], 50.45466)
        self.assertEqual(kwargs["params"]["current"], "temperature_2m,wind_speed_10m")
        self.assertEqual(kwargs["timeout"], weather.TIMEOUT)

    def test_forecast_without_current(self):
        with fake_get(
            FakeResponse(body=sample("geocoding-kyiv.json")),
            FakeResponse(body={"latitude": 50.4375}),
        ):
            with self.assertRaises(weather.UnexpectedResponseError):
                weather.get_current_weather("Київ")

    def test_forecast_with_null_value(self):
        body = sample("forecast-kyiv.json")
        body["current"]["wind_speed_10m"] = None
        with fake_get(FakeResponse(body=sample("geocoding-kyiv.json")), FakeResponse(body=body)):
            with self.assertRaises(weather.UnexpectedResponseError):
                weather.get_current_weather("Київ")

    def test_missing_units_fall_back_to_defaults(self):
        body = sample("forecast-kyiv.json")
        del body["current_units"]
        with fake_get(FakeResponse(body=sample("geocoding-kyiv.json")), FakeResponse(body=body)):
            result = weather.get_current_weather("Київ")
        self.assertEqual(result["temperature_unit"], "°C")
        self.assertEqual(result["wind_speed_unit"], "km/h")

    def test_forecast_400_is_our_bug_not_users(self):
        with fake_get(
            FakeResponse(body=sample("geocoding-kyiv.json")),
            FakeResponse(status_code=400, body=sample("forecast-error-400.json")),
        ):
            with self.assertRaises(weather.UnexpectedResponseError) as caught:
                weather.get_current_weather("Київ")
        # Текст причини від сервісу користувачеві не показується.
        self.assertNotIn("latitude", str(caught.exception))


class TransportFailureTests(unittest.TestCase):
    def assert_raises_for(self, failure, expected):
        with mock.patch.object(weather.requests, "get", side_effect=failure):
            with self.assertRaises(expected):
                weather.find_city("Київ")

    def test_read_timeout(self):
        self.assert_raises_for(requests.ReadTimeout(), weather.ServiceTimeoutError)

    def test_connect_timeout(self):
        self.assert_raises_for(requests.ConnectTimeout(), weather.ServiceTimeoutError)

    def test_connection_error(self):
        with mock.patch.object(weather.requests, "get", side_effect=requests.ConnectionError()):
            with self.assertRaises(weather.ServiceUnavailableError) as caught:
                weather.find_city("Київ")
        self.assertNotIsInstance(caught.exception, weather.ServiceTimeoutError)

    def test_server_error_statuses(self):
        for status in (500, 502, 503, 429):
            with fake_get(FakeResponse(status_code=status, not_json=True)):
                with self.assertRaises(weather.ServiceUnavailableError):
                    weather.find_city("Київ")

    def test_body_is_not_json(self):
        with fake_get(FakeResponse(not_json=True)):
            with self.assertRaises(weather.UnexpectedResponseError):
                weather.find_city("Київ")

    def test_body_is_json_but_not_object(self):
        with fake_get(FakeResponse(body=[1, 2, 3])):
            with self.assertRaises(weather.UnexpectedResponseError):
                weather.find_city("Київ")


if __name__ == "__main__":
    unittest.main()
