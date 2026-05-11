import unittest

from mac.station_config import load_station_config, station_ids


class StationConfigTests(unittest.TestCase):
    def test_configured_stations_have_isolated_runtime_and_api_ports(self):
        klod = load_station_config("klod-fm")
        cdex = load_station_config("cdex-fm")

        self.assertIn("klod-fm", station_ids())
        self.assertIn("cdex-fm", station_ids())
        self.assertEqual(klod.stream.mount, "/klod-fm")
        self.assertEqual(cdex.stream.mount, "/cdex-fm")
        self.assertNotEqual(klod.stream.api_port, cdex.stream.api_port)
        self.assertNotEqual(klod.output_dir, cdex.output_dir)
        self.assertNotEqual(klod.current_track_file, cdex.current_track_file)

    def test_agent_assignment_matches_station_owner(self):
        self.assertEqual(load_station_config("klod-fm").agent.kind, "claude")
        self.assertEqual(load_station_config("cdex-fm").agent.kind, "codex")

    def test_klod_inherits_writ_schedule_and_cdex_has_own_schedule(self):
        writ = load_station_config("writ-fm")
        klod = load_station_config("klod-fm")
        cdex = load_station_config("cdex-fm")

        self.assertEqual(klod.schedule_path, writ.schedule_path)
        self.assertNotEqual(cdex.schedule_path, writ.schedule_path)
        self.assertEqual(cdex.schedule_path.name, "cdex_schedule.yaml")


if __name__ == "__main__":
    unittest.main()
