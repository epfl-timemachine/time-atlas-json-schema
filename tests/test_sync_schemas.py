"""Consumer contract tests independent of the release builder."""

import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import sync_schemas


class SyncSchemasTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "validation" / "schemas"
        self.lock_path = self.root / "rde-schemas.lock.json"
        self.archive_path = self.root / "schemas.tar.gz"
        self.schemas = {}
        for name in ("file", "point_of_interest"):
            self.schemas[f"rde/{name}.schema.json"] = json.dumps({
                "$schema": sync_schemas.DIALECT,
                "$id": sync_schemas.BASE_URI + f"{name}.schema.json",
                "type": "object",
            }).encode()
        self.internal = {
            "manifest_version": 1,
            "package": sync_schemas.PACKAGE,
            "version": "1.0.0",
            "source_repository": sync_schemas.REPOSITORY,
            "source_commit": "a" * 40,
            "source_dirty": False,
            "schema_dialect": sync_schemas.DIALECT,
            "schema_base_uri": sync_schemas.BASE_URI,
            "files": {
                name: {
                    "id": sync_schemas.BASE_URI + name.removeprefix("rde/"),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for name, content in self.schemas.items()
            },
        }
        self.write_archive()

    def write_archive(self, extra_members=(), internal=None, schemas=None):
        """Repin crafted archives so checks reach the behavior under test."""
        contents = {"manifest.json": json.dumps(self.internal if internal is None else internal).encode()}
        contents.update(self.schemas if schemas is None else schemas)
        with tarfile.open(self.archive_path, "w:gz") as archive:
            for name, content in contents.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            for member, content in extra_members:
                archive.addfile(member, io.BytesIO(content))
        name = "time-atlas-rde-schemas-1.0.0.tar.gz"
        self.manifest = copy.deepcopy(self.internal)
        self.manifest["archive"] = {
            "name": name,
            "url": f"{sync_schemas.REPOSITORY}/releases/download/v1.0.0/{name}",
            "sha256": hashlib.sha256(self.archive_path.read_bytes()).hexdigest(),
        }
        self.lock_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def cli(self, *options):
        return subprocess.run(
            [sys.executable, str(Path(sync_schemas.__file__)),
             "--manifest", str(self.lock_path), "--destination", str(self.destination), *options],
            capture_output=True, text=True, check=False,
        )

    def sync(self):
        result = self.cli("--archive", str(self.archive_path))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_successful_sync_and_offline_check(self):
        self.sync()
        for name, content in self.schemas.items():
            self.assertEqual((self.destination / Path(name).name).read_bytes(), content)
        # The default check must work without either archive or network.
        self.archive_path.unlink()
        with patch.object(sync_schemas, "urlopen", side_effect=AssertionError("Unexpected network")):
            sync_schemas.main([
                "--manifest", str(self.lock_path), "--destination", str(self.destination), "--check",
            ])

    def test_sync_removes_stale_schemas_and_preserves_unrelated_files(self):
        self.destination.mkdir(parents=True)
        (self.destination / "nested").mkdir()
        (self.destination / "old.schema.json").write_text("old", encoding="utf-8")
        (self.destination / "nested" / "stale.schema.json").write_text("old", encoding="utf-8")
        readme = self.destination / "README.txt"
        readme.write_text("keep", encoding="utf-8")
        self.lock_path = self.destination / "rde-schemas.lock.json"
        lock_bytes = json.dumps(self.manifest).encode()
        self.lock_path.write_bytes(lock_bytes)
        self.sync()
        self.assertFalse((self.destination / "old.schema.json").exists())
        self.assertFalse((self.destination / "nested" / "stale.schema.json").exists())
        self.assertEqual(readme.read_text(), "keep")
        self.assertEqual(self.lock_path.read_bytes(), lock_bytes)
        self.assertEqual(self.cli("--check").returncode, 0)

    def test_check_detects_missing_extra_and_changed_files_without_writes(self):
        self.sync()
        (self.destination / "file.schema.json").write_text("{}", encoding="utf-8")
        (self.destination / "point_of_interest.schema.json").unlink()
        (self.destination / "extra.schema.json").write_text("{}", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.destination.iterdir()}
        result = self.cli("--check")
        self.assertNotEqual(result.returncode, 0)
        for message in ("missing: point_of_interest", "unexpected: extra", "changed: file"):
            self.assertIn(message, result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.destination.iterdir()})

    def test_corrupt_archive_is_rejected_before_parsing_or_writing(self):
        self.archive_path.write_bytes(b"not even a tar file")
        result = self.cli("--archive", str(self.archive_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Archive SHA-256 mismatch", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_archive_paths_duplicates_and_links_are_rejected_without_writes(self):
        traversal = tarfile.TarInfo("../outside.schema.json")
        traversal.size = 4
        duplicate = tarfile.TarInfo("rde/file.schema.json")
        duplicate.size = 4
        link = tarfile.TarInfo("rde/file.schema.json")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside.schema.json"
        for member, content, omit_file in (
            (traversal, b"evil", False),
            (duplicate, b"evil", False),
            (link, b"", True),
        ):
            with self.subTest(member=member.name, type=member.type):
                schemas = dict(self.schemas)
                if omit_file:
                    del schemas["rde/file.schema.json"]
                self.write_archive(extra_members=[(member, content)], schemas=schemas)
                result = self.cli("--archive", str(self.archive_path))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.destination.exists())
                self.assertFalse((self.root / "outside.schema.json").exists())

    def test_internal_manifest_mismatch_and_per_file_corruption_are_rejected(self):
        wrong_manifest = copy.deepcopy(self.internal)
        wrong_manifest["source_commit"] = "b" * 40
        self.write_archive(internal=wrong_manifest)
        result = self.cli("--archive", str(self.archive_path))
        self.assertIn("Internal archive manifest", result.stderr)
        self.assertFalse(self.destination.exists())

        corrupted = dict(self.schemas)
        corrupted["rde/file.schema.json"] = b"{}"
        self.write_archive(schemas=corrupted)
        result = self.cli("--archive", str(self.archive_path))
        self.assertIn("Schema SHA-256 mismatch", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_schema_id_and_dialect_must_match_even_when_hash_is_repinned(self):
        for key, value in (("$id", "https://example.com/schema"), ("$schema", "draft-07")):
            with self.subTest(key=key):
                schemas = dict(self.schemas)
                schema = json.loads(schemas["rde/file.schema.json"])
                schema[key] = value
                schemas["rde/file.schema.json"] = json.dumps(schema).encode()
                self.internal["files"]["rde/file.schema.json"]["sha256"] = hashlib.sha256(
                    schemas["rde/file.schema.json"]
                ).hexdigest()
                self.write_archive(schemas=schemas)
                result = self.cli("--archive", str(self.archive_path))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("mismatch", result.stderr)
                self.assertFalse(self.destination.exists())

    def test_default_sync_downloads_the_pinned_archive(self):
        with patch.object(sync_schemas, "urlopen", return_value=io.BytesIO(self.archive_path.read_bytes())) as download:
            sync_schemas.main([
                "--manifest", str(self.lock_path), "--destination", str(self.destination),
            ])
        download.assert_called_once_with(self.manifest["archive"]["url"], timeout=30)
        sync_schemas.check_destination(self.manifest, self.destination)

    def test_manifest_rejects_dirty_release_invalid_version_paths_and_urls(self):
        mutations = [
            {"source_dirty": True},
            {"version": "1.0.0-rc.1"},
            {"source_commit": "a" * 7},
            {"files": {"rde/../evil.schema.json": next(iter(self.internal["files"].values()))}},
            {"archive": {**self.manifest["archive"], "url": "https://example.com/schemas.tar.gz"}},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    sync_schemas.validate_manifest({**self.manifest, **mutation})

    def test_duplicate_json_keys_are_rejected(self):
        content = self.lock_path.read_text()
        self.lock_path.write_text(content[:-1] + ', "source_dirty": true}', encoding="utf-8")
        result = self.cli("--archive", str(self.archive_path))
        self.assertIn("Duplicate JSON key", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_existing_schema_symlink_is_not_followed_or_overwritten(self):
        self.destination.mkdir(parents=True)
        outside = self.root / "external.schema.json"
        outside.write_text("keep", encoding="utf-8")
        (self.destination / "file.schema.json").symlink_to(outside)
        result = self.cli("--archive", str(self.archive_path))
        self.assertIn("nonregular file", result.stderr)
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse((self.destination / "point_of_interest.schema.json").exists())


if __name__ == "__main__":
    unittest.main()
