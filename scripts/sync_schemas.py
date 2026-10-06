"""Vendor or check a pinned RDE schema release using Python's standard library.

Copy this script into a consumer alongside its committed rde-schemas.lock.json.
Normal sync downloads the archive named by that lock; --check is offline.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
from urllib.request import urlopen


PACKAGE = "time-atlas-rde-schemas"
REPOSITORY = "https://github.com/epfl-timemachine/time-atlas-json-schema"
BASE_URI = "https://epfl-timemachine.github.io/time-atlas-json-schema/rde/"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SCHEMA_PATH = re.compile(r"rde/[a-zA-Z0-9][a-zA-Z0-9_-]*\.schema\.json")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def _json(data):
    return json.loads(data, object_pairs_hook=_unique_object)


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def validate_manifest(manifest):
    """Reject unsupported or ambiguous locks before downloading or writing."""
    fields = {
        "manifest_version", "package", "version", "source_repository",
        "source_commit", "source_dirty", "schema_dialect", "schema_base_uri",
        "files", "archive",
    }
    if not isinstance(manifest, dict) or set(manifest) != fields:
        raise ValueError("Manifest must contain exactly the manifest v1 fields")
    if type(manifest["manifest_version"]) is not int or manifest["manifest_version"] != 1:
        raise ValueError("Unsupported manifest_version (expected 1)")
    if manifest["package"] != PACKAGE or manifest["source_repository"] != REPOSITORY:
        raise ValueError("Manifest is not for the Time Atlas RDE schemas repository")
    if not _matches(SEMVER, manifest["version"]):
        raise ValueError("Manifest version must be a stable major.minor.patch version")
    if not _matches(COMMIT, manifest["source_commit"]):
        raise ValueError("source_commit must be a full lowercase Git commit SHA")
    if manifest["source_dirty"] is not False:
        raise ValueError("A consumer must pin a clean release (source_dirty must be false)")
    if manifest["schema_dialect"] != DIALECT or manifest["schema_base_uri"] != BASE_URI:
        raise ValueError("Unsupported schema dialect or base URI")

    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("Manifest files must be a nonempty object")
    names = set()
    for path, entry in files.items():
        if not _matches(SCHEMA_PATH, path):
            raise ValueError(f"Invalid schema path: {path!r}")
        # Files must remain distinct on case-insensitive consumer filesystems.
        if path.casefold() in names:
            raise ValueError(f"Case-colliding schema path: {path}")
        names.add(path.casefold())
        if not isinstance(entry, dict) or set(entry) != {"id", "sha256"}:
            raise ValueError(f"Invalid manifest entry: {path}")
        if entry["id"] != BASE_URI + path.removeprefix("rde/"):
            raise ValueError(f"Incorrect schema ID in manifest: {path}")
        if not _matches(SHA256, entry["sha256"]):
            raise ValueError(f"Invalid SHA-256 for {path}")

    archive = manifest["archive"]
    if not isinstance(archive, dict) or set(archive) != {"name", "url", "sha256"}:
        raise ValueError("Invalid archive metadata")
    name = f"{PACKAGE}-{manifest['version']}.tar.gz"
    url = f"{REPOSITORY}/releases/download/v{manifest['version']}/{name}"
    if archive["name"] != name or archive["url"] != url:
        raise ValueError("Archive name and URL must match the pinned release version")
    if not _matches(SHA256, archive["sha256"]):
        raise ValueError("Invalid archive SHA-256")
    return manifest


def load_manifest(path):
    return validate_manifest(_json(Path(path).read_bytes()))


def _verify_schema(data, entry, name):
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError(f"Schema SHA-256 mismatch: {name}")
    schema = _json(data)
    if not isinstance(schema, dict) or schema.get("$id") != entry["id"]:
        raise ValueError(f"Schema $id mismatch: {name}")
    if schema.get("$schema") != DIALECT:
        raise ValueError(f"Schema dialect mismatch: {name}")


def read_archive(manifest, archive_path=None):
    """Verify the archive in memory, returning only validated schema bytes."""
    if archive_path is None:
        with urlopen(manifest["archive"]["url"], timeout=30) as response:
            data = response.read()
    else:
        data = Path(archive_path).read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest["archive"]["sha256"]:
        raise ValueError("Archive SHA-256 mismatch; no schemas were written")

    expected_names = {"manifest.json", *manifest["files"]}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError("Archive contains duplicate paths")
        if set(names) != expected_names:
            raise ValueError("Archive members do not match the pinned manifest")
        if any(member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} for member in members):
            raise ValueError("Archive may contain only regular files (no links or directories)")

        internal = _json(archive.extractfile("manifest.json").read())
        if internal != {key: value for key, value in manifest.items() if key != "archive"}:
            raise ValueError("Internal archive manifest does not match the pinned manifest")
        # JSON booleans and numbers can compare equal in Python; apply the same
        # type checks to the internal manifest as to the consumer's lock.
        validate_manifest({**internal, "archive": manifest["archive"]})
        schemas = {}
        for name, entry in manifest["files"].items():
            content = archive.extractfile(name).read()
            _verify_schema(content, entry, name)
            schemas[name.removeprefix("rde/")] = content
    return schemas


def _destination_schemas(destination):
    """Find generated files without following links out of the destination."""
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError("The schema destination must not be a symbolic link")
    if destination.exists() and not destination.is_dir():
        raise ValueError("The schema destination must be a directory")
    paths = {}
    for path in destination.rglob("*.schema.json"):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Schema destination contains a nonregular file: {path}")
        paths[path.relative_to(destination).as_posix()] = path
    return paths


def check_destination(manifest, destination):
    """Check a complete vendored set against its lock without network access."""
    actual = _destination_schemas(destination)
    expected = {name.removeprefix("rde/"): entry for name, entry in manifest["files"].items()}
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    changed = []
    for name in expected.keys() & actual.keys():
        if hashlib.sha256(actual[name].read_bytes()).hexdigest() != expected[name]["sha256"]:
            changed.append(name)
    problems = []
    for label, names in (("missing", missing), ("unexpected", extra), ("changed", sorted(changed))):
        if names:
            problems.append(f"{label}: {', '.join(names)}")
    if problems:
        raise ValueError("Vendored schemas differ from their lock (" + "; ".join(problems) + ")")


def sync_destination(schemas, destination):
    """Stage all files first, then replace schemas and remove stale schemas."""
    destination = Path(destination)
    existing = _destination_schemas(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rde-schemas-", dir=destination.parent) as staging:
        staging = Path(staging)
        for name, content in schemas.items():
            (staging / name).write_bytes(content)
        destination.mkdir(exist_ok=True)
        for name in schemas:
            os.replace(staging / name, destination / name)
        for name in existing.keys() - schemas.keys():
            existing[name].unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Committed release lock JSON")
    parser.add_argument("--destination", type=Path, required=True, help="Dedicated schema directory")
    parser.add_argument("--check", action="store_true", help="Check vendored files offline; never write")
    parser.add_argument("--archive", type=Path, help="Use a local release archive instead of downloading")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.check:
            if args.archive is not None:
                read_archive(manifest, args.archive)
            check_destination(manifest, args.destination)
            print(f"Checked {len(manifest['files'])} schemas against release {manifest['version']}.")
        else:
            lock = args.manifest.resolve()
            if lock.name.endswith(".schema.json") and lock.is_relative_to(args.destination.resolve()):
                raise ValueError("The lock file inside the destination must not end in .schema.json")
            schemas = read_archive(manifest, args.archive)
            sync_destination(schemas, args.destination)
            print(f"Synced {len(schemas)} schemas from release {manifest['version']} to {args.destination}.")
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
