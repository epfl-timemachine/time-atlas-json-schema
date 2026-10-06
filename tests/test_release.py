"""Exercise release provenance, archive reproducibility and package contents."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from scripts.build_release import build_release


ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        shutil.copytree(ROOT / "rde", self.root / "rde")
        (self.root / "VERSION").write_text("1.0.0\n")
        self.run_git("init", "-q")
        self.run_git("add", ".")
        self.run_git("-c", "user.name=Release Test", "-c", "user.email=test@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "-qm", "Schema fixture")
        self.output = Path(self.temporary.name) / "dist"

    def run_git(self, *arguments):
        return subprocess.check_output(
            ["git", "-C", str(self.root), *arguments], text=True, stderr=subprocess.STDOUT
        ).strip()

    def test_bundle_is_reproducible_and_checksums_cover_exact_source(self):
        manifest = build_release(self.root, self.output)
        self.assertFalse(manifest["source_dirty"])
        self.assertEqual(manifest["source_commit"], self.run_git("rev-parse", "HEAD"))
        self.assertEqual(manifest["version"], "1.0.0")
        archive_path = self.output / manifest["archive"]["name"]
        self.assertEqual(hashlib.sha256(archive_path.read_bytes()).hexdigest(), manifest["archive"]["sha256"])
        self.assertIn("/releases/download/v1.0.0/", manifest["archive"]["url"])

        with tarfile.open(archive_path, "r:gz") as archive:
            self.assertEqual(set(archive.getnames()), {"manifest.json", *manifest["files"]})
            internal = json.load(archive.extractfile("manifest.json"))
            self.assertEqual(internal, {key: value for key, value in manifest.items() if key != "archive"})
            for name, details in manifest["files"].items():
                payload = archive.extractfile(name).read()
                self.assertEqual(payload, (self.root / name).read_bytes())
                self.assertEqual(hashlib.sha256(payload).hexdigest(), details["sha256"])

        for line in (self.output / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), expected)

        second = self.output.parent / "second"
        build_release(self.root, second)
        for path in self.output.iterdir():
            self.assertEqual(path.read_bytes(), (second / path.name).read_bytes())

    def test_tag_must_match_version_and_source_commit(self):
        self.run_git("-c", "tag.gpgsign=false", "tag", "v1.0.0")
        build_release(self.root, self.output, tag="v1.0.0")
        with self.assertRaisesRegex(ValueError, "does not match VERSION"):
            build_release(self.root, self.output, tag="v2.0.0")
        self.run_git("-c", "user.name=Release Test", "-c", "user.email=test@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "Another commit")
        with self.assertRaisesRegex(ValueError, "does not identify"):
            build_release(self.root, self.output, tag="v1.0.0")

    def test_dirty_builds_are_rejected_or_explicitly_marked(self):
        (self.root / "uncommitted.txt").write_text("not in the source commit")
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            build_release(self.root, self.output)
        self.assertFalse(self.output.exists())
        preview = build_release(self.root, self.output, allow_dirty=True)
        self.assertTrue(preview["source_dirty"])
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            build_release(self.root, self.output, tag="v1.0.0", allow_dirty=True)

    def test_invalid_version_is_rejected(self):
        (self.root / "VERSION").write_text("01.0.0\n")
        with self.assertRaisesRegex(ValueError, "stable version"):
            build_release(self.root, self.output, allow_dirty=True)

    def test_broken_references_are_rejected_before_packaging(self):
        path = self.root / "rde/file.schema.json"
        schema = json.loads(path.read_text())
        schema["$ref"] = "missing.schema.json"
        path.write_text(json.dumps(schema))
        with self.assertRaisesRegex(ValueError, "unresolved"):
            build_release(self.root, self.output, allow_dirty=True)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
