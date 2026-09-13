import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import birdnet_trmnl as adapter


def config(cache_dir: Path, **overrides):
    values = {
        "birdnet_url": "http://birdnet.test:8080",
        "webhook_url": "https://trmnl.test/api/custom_plugins/secret",
        "station_name": "Backyard Birds",
        "timezone": "America/New_York",
        "timeout_seconds": 5.0,
        "cache_dir": cache_dir,
        "max_payload_bytes": 2000,
        "min_confidence": 0.0,
        "images_enabled": True,
    }
    values.update(overrides)
    return adapter.Config(**values)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = config(Path(self.tempdir.name))
        self.now = datetime(2026, 9, 13, 12, 0, tzinfo=ZoneInfo("UTC"))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_builds_daily_totals_and_filters_false_positive_recent_detection(self):
        daily = [
            {
                "common_name": "Fish Crow",
                "scientific_name": "Corvus ossifragus",
                "count": 8,
                "latest_heard": "07:59:00",
                "hourly_counts": [0] * 7 + [8] + [0] * 16,
            },
            {
                "common_name": "Blue Jay",
                "scientific_name": "Cyanocitta cristata",
                "count": 2,
                "latest_heard": "07:30:00",
                "hourly_counts": [0] * 7 + [2] + [0] * 16,
            },
        ]
        detections = [
            {
                "date": "2026-09-13",
                "timestamp": "2026-09-13T07:59:00-04:00",
                "commonName": "Mockingbird",
                "scientificName": "Mimus polyglottos",
                "confidence": 0.99,
                "verified": "false_positive",
            },
            {
                "date": "2026-09-13",
                "timestamp": "2026-09-13T07:58:00-04:00",
                "commonName": "Fish Crow",
                "scientificName": "Corvus ossifragus",
                "confidence": 0.91,
                "verified": "unverified",
            },
            {
                "date": "2026-09-12",
                "timestamp": "2026-09-12T23:58:00-04:00",
                "commonName": "Barn Owl",
                "scientificName": "Tyto alba",
                "confidence": 0.88,
                "verified": "unverified",
            },
        ]

        result = adapter.build_snapshot(daily, detections, self.config, self.now)

        self.assertEqual(result["date"], "2026-09-13")
        self.assertEqual(result["detections"], 10)
        self.assertEqual(result["species"], 2)
        self.assertEqual(result["featured"]["name"], "Fish Crow")
        self.assertEqual(result["featured"]["confidence"], 91)
        self.assertEqual(len(result["recent"]), 1)
        self.assertEqual(result["activity"][7], 10)
        self.assertEqual(result["activity_levels"][7], 10)

    def test_empty_day_is_online_with_zero_counts(self):
        result = adapter.build_snapshot([], [], self.config, self.now)
        self.assertEqual(result["status"], "online")
        self.assertEqual(result["detections"], 0)
        self.assertEqual(result["species"], 0)
        self.assertEqual(result["featured"]["name"], "No detections yet")
        self.assertEqual(result["activity"], [0] * 24)

    def test_payload_trims_optional_content_but_keeps_required_totals(self):
        snapshot = {
            "schema_version": 1,
            "status": "online",
            "station": "A" * 42,
            "date": "2026-09-13",
            "date_label": "Sunday, September 13",
            "checked_at": "8:00 AM EDT",
            "detections": 100,
            "species": 12,
            "featured": {
                "name": "B" * 42,
                "scientific": "C" * 52,
                "confidence": 99,
                "heard": "8:00 AM",
                "image_url": "https://example.test/" + "x" * 800,
                "image_credit": "Y" * 110,
                "image_source": "https://example.test/" + "z" * 500,
            },
            "top_species": [
                {"name": "D" * 38, "scientific": "E" * 48, "count": index, "last": "8:00 AM"}
                for index in range(5)
            ],
            "recent": [{"name": "F" * 34, "confidence": 99, "heard": "8:00 AM"} for _ in range(5)],
            "activity": list(range(24)),
            "activity_levels": list(range(11)) + [10] * 13,
            "activity_max": 23,
        }

        body = adapter.fit_webhook_body(snapshot, 2000)
        payload = json.loads(body)["merge_variables"]

        self.assertLessEqual(len(body), 2000)
        self.assertEqual(payload["detections"], 100)
        self.assertEqual(payload["species"], 12)

    def test_timezone_controls_daily_boundary(self):
        just_after_midnight_utc = datetime(2026, 9, 14, 0, 30, tzinfo=ZoneInfo("UTC"))
        result = adapter.build_snapshot([], [], self.config, just_after_midnight_utc)
        self.assertEqual(result["date"], "2026-09-13")


class WikimediaTests(unittest.TestCase):
    def test_cleans_html_metadata(self):
        metadata = {"Artist": {"value": '<a href="https://example.test">Jane&nbsp;Doe</a>'}}
        self.assertEqual(adapter._metadata_text(metadata, "Artist"), "Jane Doe")

    def test_reads_fresh_cached_portrait_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            client = adapter.WikimediaClient(adapter.HttpClient(1), Path(directory))
            portrait = {"url": "https://example.test/bird.jpg", "credit": "Photo: Jane · CC0", "source": "x"}
            client._write_cache("Testus birdus", {"portrait": portrait, "cached_at": int(adapter.time.time())})
            self.assertEqual(client.portrait("Testus birdus"), portrait)


class FakeHttp:
    def __init__(self):
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        if "/analytics/species/daily" in url:
            return []
        return {"data": []}


class CollectionTests(unittest.TestCase):
    def test_collect_uses_explicit_station_date(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeHttp()
            cfg = config(Path(directory), images_enabled=False)
            now = datetime(2026, 9, 14, 0, 30, tzinfo=ZoneInfo("UTC"))
            result = adapter.collect(cfg, now=now, http=fake)
            self.assertIn("date=2026-09-13", fake.urls[0])
            self.assertEqual(result["date"], "2026-09-13")


if __name__ == "__main__":
    unittest.main()
