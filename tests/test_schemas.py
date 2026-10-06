"""Compatibility checks for RDE envelopes with all references resolved offline."""

import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
DIALECT = "https://json-schema.org/draft/2020-12/schema"
CANONICAL_TYPES = (
    "historical_record", "observation", "point_of_interest", "geometry",
    "dataset", "map", "layer", "area", "dictionary",
)
ALIASES = ("hr", "obs", "poi", "geom")


def envelope(types, objects=()):
    # Field layout emitted by timeatlas.TimeAtlas.RDEFileWriter._envelope.
    return {
        "name": "example",
        "creation_time": "2026-10-05T12:00:00",
        "type_in_file": list(types),
        "rde_objects": copy.deepcopy(list(objects)),
    }


def representative_objects():
    # Representative serialized fields from the Python writer and its fixtures.
    # Keep these local so CI does not require another repository or network access.
    return {
        "historical_record": {
            "id": "record-1",
            "dataset": "dataset-1",
            "rde_type": "historical_record",
            "paradata": "m",
            "has_observations": ["observation-1"],
            "start_time": "1808-01-01T00:00:00",
            "end_time": "1808-12-31T23:59:59",
            "metadata": {"parcel_number": "42"},
            "rights_attribution": None,
        },
        "observation": {
            "id": "observation-1",
            "rde_type": "observation",
            "historical_record": "record-1",
            "has_geometries": ["geometry-1"],
            "part_of_point_of_interest": None,
            "geometry": {"type": "Point", "coordinates": [12.32, 45.43]},
        },
        "dataset": {
            "id": "dataset-1",
            "slug": "venice-1808",
            "rde_type": "dataset",
            "name": {"en": ["Land registers"], "it": ["Registri fondiari"]},
            "creation_time": "2026-10-05T12:00:00",
            "version": "1.0",
            "sources": ["source-1"],
            "start_time": "1808-01-01T00:00:00",
            "end_time": "1808-12-31T23:59:59",
            "has_areas": ["area-1"],
            "metadata": [{
                "type": "STRING",
                "label": {"en": ["Description"]},
                "value": {"en": ["Historical land registers."]},
            }],
            "configuration": {
                "main_label": "${parcel_number}",
                "sub_label": "${place}",
                "display_thumbnail": False,
                "external_source": False,
                "metadata_field_config": [{
                    "id": "parcel_number",
                    "type": "STRING",
                    "display_label": {"en": ["Parcel number"]},
                    "nullable": False,
                    "indexable": True,
                    "short_display": True,
                    "hidden": False,
                    "tag": "PLACE",
                    "paradata": "m",
                }],
            },
        },
    }


class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((ROOT / "rde").glob("*.schema.json"))
        }

        def forbid_remote(uri):
            raise AssertionError(f"Schema reference is not available offline: {uri}")

        registry = Registry(retrieve=forbid_remote).with_resources(
            (schema["$id"], Resource.from_contents(schema))
            for schema in cls.schemas.values()
        )
        cls.validator = Draft202012Validator(
            cls.schemas["file.schema.json"], registry=registry
        )

    def assert_valid(self, value):
        self.assertEqual([], list(self.validator.iter_errors(value)))

    def assert_invalid(self, value):
        self.assertTrue(list(self.validator.iter_errors(value)))

    def test_every_schema_declares_and_satisfies_draft_2020_12(self):
        for name, schema in self.schemas.items():
            with self.subTest(schema=name):
                self.assertEqual(DIALECT, schema["$schema"])
                Draft202012Validator.check_schema(schema)

    def test_canonical_type_names_and_legacy_aliases(self):
        for name in CANONICAL_TYPES + ALIASES:
            with self.subTest(type=name):
                self.assert_valid(envelope([name]))
        self.assert_valid(envelope(["historical_record", "observation", "dataset"]))

    def test_type_in_file_is_required(self):
        value = envelope(["dataset"])
        del value["type_in_file"]
        self.assert_invalid(value)

    def test_type_in_file_requires_nonempty_array_of_known_names(self):
        for types in ([], "dataset", None, {}, ["unknown"], [42], ["dataset", "unknown"]):
            with self.subTest(types=types):
                value = envelope(["dataset"])
                value["type_in_file"] = types
                self.assert_invalid(value)

    def test_name_is_a_string(self):
        value = envelope(["dataset"])
        value["name"] = {"type_in_file": ["dataset"]}
        self.assert_invalid(value)

    def test_representative_canonical_envelopes_validate_offline(self):
        objects = representative_objects()
        for name, obj in objects.items():
            with self.subTest(type=name):
                self.assert_valid(envelope([name], [obj]))
        self.assert_valid(envelope(objects, objects.values()))

    def test_invalid_rde_content_fails(self):
        for name, obj in representative_objects().items():
            with self.subTest(type=name):
                del obj["id"]
                self.assert_invalid(envelope([name], [obj]))
        self.assert_invalid(envelope(["observation"], ["not an object"]))

    def test_invalid_nested_content_fails_through_references(self):
        dataset = representative_objects()["dataset"]
        dataset["configuration"]["metadata_field_config"][0]["display_label"]["en"] = 42
        self.assert_invalid(envelope(["dataset"], [dataset]))

    def test_related_dataset_slugs_remains_optional_and_nonempty_when_present(self):
        self.assert_valid(envelope(["dataset"]))
        value = envelope(["dataset"])
        value["related_dataset_slugs"] = ["venice-1808", "venice-1848"]
        self.assert_valid(value)
        for slugs in ([], "venice-1808", [42], None):
            with self.subTest(slugs=slugs):
                value["related_dataset_slugs"] = slugs
                self.assert_invalid(value)


if __name__ == "__main__":
    unittest.main()
