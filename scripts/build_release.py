"""Build a reproducible, language-neutral RDE schema release bundle."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

if __package__:
    from .build_pages import BASE_URL, check_schemas
else:
    from build_pages import BASE_URL, check_schemas


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "time-atlas-rde-schemas"
REPOSITORY = "https://github.com/epfl-timemachine/time-atlas-json-schema"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
VERSION_PATTERN = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def git(root, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True
    ).strip()


def build_release(root, output, *, tag=None, allow_dirty=False):
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if re.fullmatch(VERSION_PATTERN, version) is None:
        raise ValueError("VERSION must contain a stable version such as 1.0.0")

    commit = git(root, "rev-parse", "HEAD")
    dirty = bool(git(root, "status", "--porcelain", "--untracked-files=all"))
    if dirty and (not allow_dirty or tag is not None):
        raise ValueError("Release builds require a clean checkout; use --allow-dirty for a local preview")
    if tag is not None:
        if tag != "v" + version:
            raise ValueError(f"Tag {tag!r} does not match VERSION ({version})")
        if git(root, "rev-parse", f"{tag}^{{commit}}") != commit:
            raise ValueError(f"Tag {tag!r} does not identify the checked-out commit")

    paths = sorted((root / "rde").glob("*.schema.json"))
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("Schemas must be regular files, not symbolic links")
    check_schemas(paths)

    contents = {}
    files = {}
    for path in paths:
        name = "rde/" + path.name
        payload = path.read_bytes()
        schema = json.loads(payload)
        if schema.get("$schema") != DIALECT:
            raise ValueError(f"{path.name}: expected $schema {DIALECT}")
        contents[name] = payload
        files[name] = {"id": schema["$id"], "sha256": sha256(payload)}

    manifest = {
        "manifest_version": 1,
        "package": PACKAGE,
        "version": version,
        "source_repository": REPOSITORY,
        "source_commit": commit,
        "source_dirty": dirty,
        "schema_dialect": DIALECT,
        "schema_base_uri": BASE_URL + "rde/",
        "files": files,
    }
    contents["manifest.json"] = json_bytes(manifest)

    archive_buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=archive_buffer, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, payload in sorted(contents.items()):
                entry = tarfile.TarInfo(name)
                entry.size = len(payload)
                entry.mode = 0o644
                entry.mtime = entry.uid = entry.gid = 0
                entry.uname = entry.gname = ""
                archive.addfile(entry, io.BytesIO(payload))

    archive_name = f"{PACKAGE}-{version}.tar.gz"
    archive_payload = archive_buffer.getvalue()
    manifest["archive"] = {
        "name": archive_name,
        "url": f"{REPOSITORY}/releases/download/v{version}/{archive_name}",
        "sha256": sha256(archive_payload),
    }
    manifest_name = f"{PACKAGE}-{version}.manifest.json"
    manifest_payload = json_bytes(manifest)
    output.mkdir(parents=True, exist_ok=True)
    (output / archive_name).write_bytes(archive_payload)
    (output / manifest_name).write_bytes(manifest_payload)
    (output / "SHA256SUMS").write_text(
        f"{sha256(archive_payload)}  {archive_name}\n"
        f"{sha256(manifest_payload)}  {manifest_name}\n",
        encoding="utf-8",
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--tag", help="Require this existing tag to match VERSION and HEAD")
    parser.add_argument("--allow-dirty", action="store_true", help="Build a local preview marked source_dirty")
    args = parser.parse_args()
    try:
        manifest = build_release(ROOT, args.output, tag=args.tag, allow_dirty=args.allow_dirty)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release build failed: {error}\n")
    print(f"Built {manifest['archive']['name']} in {args.output}")
    print(f"SHA-256: {manifest['archive']['sha256']}")
    if manifest["source_dirty"]:
        print("Local preview only: uncommitted source; consumer sync refuses this manifest.")


if __name__ == "__main__":
    main()
