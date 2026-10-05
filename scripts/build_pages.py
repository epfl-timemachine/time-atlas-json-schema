"""Check schema URLs and build the GitHub Pages artifact using only the stdlib."""

import json
import shutil
from html import escape
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://epfl-timemachine.github.io/time-atlas-json-schema/"


def references(value):
    """Yield references from the current schemas (which use root-level IDs)."""
    if isinstance(value, dict):
        if "$ref" in value:
            yield value["$ref"]
        for child in value.values():
            yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def check_schemas(paths):
    schemas = {}
    for path in paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        expected_id = BASE_URL + "rde/" + path.name
        if schema.get("$id") != expected_id:
            raise ValueError(f"{path.name}: expected $id {expected_id}")
        schemas[expected_id] = schema

    if not schemas:
        raise ValueError("No schemas found in rde/")

    reference_count = 0
    for schema_id, schema in schemas.items():
        for reference in references(schema):
            target_id, fragment = urldefrag(urljoin(schema_id, reference))
            if target_id not in schemas:
                raise ValueError(f"{schema_id}: unresolved $ref {reference}")
            target = schemas[target_id]
            if fragment:
                pointer = unquote(fragment)
                if not pointer.startswith("/"):
                    raise ValueError(f"{schema_id}: unsupported fragment {reference}")
                try:
                    for token in pointer[1:].split("/"):
                        token = token.replace("~1", "/").replace("~0", "~")
                        target = target[int(token)] if isinstance(target, list) else target[token]
                except (KeyError, IndexError, TypeError, ValueError) as error:
                    raise ValueError(f"{schema_id}: unresolved $ref {reference}") from error
            reference_count += 1
    print(f"Checked {len(schemas)} schema IDs and {reference_count} references.")


def main():
    paths = sorted((ROOT / "rde").glob("*.schema.json"))
    check_schemas(paths)

    output = ROOT / "_site"
    if output.exists():
        shutil.rmtree(output)
    (output / "rde").mkdir(parents=True)
    for path in paths:
        shutil.copyfile(path, output / "rde" / path.name)

    links = "\n".join(
        f'    <li><a href="rde/{escape(path.name)}">{escape(path.name)}</a></li>'
        for path in paths
    )
    (output / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <title>Time Atlas RDE JSON Schemas</title>\n</head>\n<body>\n'
        '  <h1>Time Atlas RDE JSON Schemas</h1>\n'
        '  <p>Current schemas from the main branch.</p>\n'
        f'  <ul>\n{links}\n  </ul>\n</body>\n</html>\n',
        encoding="utf-8",
    )
    print(f"Built Pages artifact in {output}")


if __name__ == "__main__":
    main()
