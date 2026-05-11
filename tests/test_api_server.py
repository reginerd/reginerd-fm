import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mac"))
import api_server  # noqa: E402


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 5, 5, 17, 0)


class ApiServerTests(unittest.TestCase):
    def test_schedule_upcoming_uses_exact_next_airings(self):
        with patch.object(api_server, "datetime", FixedDatetime):
            info = api_server.get_schedule_info()

        self.assertEqual(info["current"]["show_id"], "crosswire")
        self.assertEqual(info["upcoming"][0]["show_id"], "sonic_archaeology")
        self.assertEqual(info["upcoming"][0]["starts_around"], "18:00")
        self.assertEqual(info["upcoming"][0]["starts_at"], "2026-05-05T18:00:00")

    def test_station_prefixed_now_playing_route_is_resolved(self):
        self.assertEqual(
            api_server.parse_station_route("/cdex-fm/now-playing"),
            ("cdex-fm", "/now-playing"),
        )
        self.assertEqual(
            api_server.parse_station_route("/stations/klod-fm/diary"),
            ("klod-fm", "/diary"),
        )
        self.assertEqual(
            api_server.parse_station_route("/klod-fm/message"),
            ("klod-fm", "/message"),
        )

    def test_unknown_non_station_route_is_ignored(self):
        self.assertIsNone(api_server.parse_station_route("/assets/app.js"))
        self.assertIsNone(api_server.parse_station_route("/now-playing"))

    def test_standalone_api_health_does_not_require_local_streamer(self):
        with (
            patch.object(api_server, "_api_mode", "standalone"),
            patch.object(api_server, "_encoder_getter", lambda: None),
            patch.object(api_server, "check_url", return_value=True),
            patch.object(api_server, "check_process", return_value=True),
        ):
            info = api_server.get_health_status()

        self.assertEqual(info["status"], "healthy")
        self.assertEqual(info["mode"], "standalone")
        self.assertEqual(info["components"]["streamer"]["status"], "not_attached")

    def test_stream_api_health_requires_local_streamer(self):
        with (
            patch.object(api_server, "_api_mode", "stream"),
            patch.object(api_server, "_encoder_getter", lambda: None),
            patch.object(api_server, "check_url", return_value=True),
            patch.object(api_server, "check_process", return_value=True),
        ):
            info = api_server.get_health_status()

        self.assertEqual(info["status"], "degraded")
        self.assertEqual(info["components"]["streamer"]["status"], "down")


if __name__ == "__main__":
    unittest.main()
