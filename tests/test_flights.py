import json
import unittest

import lambda_api as api


class CarrierFromCallsignTests(unittest.TestCase):
    def test_known_prefix_maps_to_name(self):
        self.assertEqual(api.carrier_from_callsign("ASA123"), "Alaska Airlines")
        self.assertEqual(api.carrier_from_callsign(" dal456 "), "Delta Air Lines")

    def test_unknown_prefix_falls_back_to_code(self):
        self.assertEqual(api.carrier_from_callsign("ZZZ1"), "ZZZ")

    def test_general_aviation_or_empty_returns_none(self):
        self.assertIsNone(api.carrier_from_callsign("N12345"))
        self.assertIsNone(api.carrier_from_callsign(""))
        self.assertIsNone(api.carrier_from_callsign(None))


class GeometryTests(unittest.TestCase):
    def test_zero_distance(self):
        self.assertAlmostEqual(api.haversine_km(47.42, -122.30, 47.42, -122.30), 0.0, places=6)

    def test_one_degree_latitude_is_about_111km(self):
        self.assertAlmostEqual(api.haversine_km(47.0, -122.0, 48.0, -122.0), 111.19, delta=1.0)

    def test_bounding_box_brackets_the_point(self):
        lamin, lomin, lamax, lomax = api.flights_bounding_box(47.42, -122.30, 25)
        self.assertLess(lamin, 47.42)
        self.assertGreater(lamax, 47.42)
        self.assertLess(lomin, -122.30)
        self.assertGreater(lomax, -122.30)


class ParseStatesTests(unittest.TestCase):
    def test_parses_filters_by_radius_and_derives_carrier(self):
        lat, lon = 47.42, -122.30
        raw = {"states": [
            ["abc123", "ASA123 ", "United States", 0, 0, lon + 0.01, lat + 0.01,
             1000.0, False, 180.0, 90.0, 0.0, None, 1050.0, "1200", False, 0, 1],
            ["def456", "DAL9", "United States", 0, 0, lon + 2.0, lat + 2.0,
             2000.0, False, 200.0, 180.0, 0.0, None, 2050.0, "1300", False, 0, 1],
            ["ghost", "XXX", "US", 0, 0, None, None, None, False, 0, 0, 0],
        ]}
        flights = api.parse_opensky_states(raw, lat, lon, 25.0)
        self.assertEqual(len(flights), 1)
        flight = flights[0]
        self.assertEqual(flight["callsign"], "ASA123")
        self.assertEqual(flight["carrier"], "Alaska Airlines")
        self.assertLessEqual(flight["distance_km"], 25.0)
        self.assertEqual(flight["baro_altitude_m"], 1000.0)


class SampleFlightsTests(unittest.TestCase):
    def test_sample_flights_within_radius_and_labeled(self):
        flights = api.sample_flights(47.42, -122.30, 25.0)
        self.assertTrue(flights)
        for flight in flights:
            self.assertEqual(flight["icao24"], "sample")
            self.assertLessEqual(flight["distance_km"], 25.0)
            self.assertIsNotNone(flight["carrier"])


class GetFlightsFallbackTests(unittest.TestCase):
    def test_falls_back_to_labeled_sample_when_upstream_fails(self):
        def _boom(_bounding_box):
            raise RuntimeError("no network")

        original = api.fetch_opensky_states
        api.fetch_opensky_states = _boom
        api._flights_cache.clear()
        try:
            result = api.get_flights(
                {"queryStringParameters": {"lat": "47.42", "lon": "-122.30", "radius_km": "25"}}
            )
        finally:
            api.fetch_opensky_states = original
            api._flights_cache.clear()

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertTrue(body["is_sample"])
        self.assertEqual(body["source"], "sample")
        self.assertGreater(body["count"], 0)
        self.assertIn("attribution", body)


if __name__ == "__main__":
    unittest.main()
