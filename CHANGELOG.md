# Changelog

## 1.0.0

First shared RDE schema release, replacing independently maintained copies.

- Publish all 13 schemas as one versioned archive, with a source commit,
  per-file SHA-256 hashes, a consumer manifest and archive checksums.
- Declare JSON Schema Draft 2020-12 explicitly.
- Use the resolvable GitHub Pages IDs.
- Fix the file envelope: `name` is a string and `type_in_file` is a required,
  nonempty array of recognized types at the top level. Canonical long names are
  preferred; `hr`, `obs`, `poi` and `geom` remain accepted for compatibility.
- Preserve optional `related_dataset_slugs`; when supplied, it must be a
  nonempty list of strings.

Migration: old copies did not all enforce the same rules. Test existing data
against this release, update old URI resolver mappings, and load all schemas
locally from the same pinned bundle. Empty or unrecognized `type_in_file` values
are rejected. The list is not cross-checked against the types of individual
`rde_objects`; that existing behavior is unchanged.
