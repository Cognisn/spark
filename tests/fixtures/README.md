# Test fixtures

## SQLite databases made by earlier SPARK releases

- `spark_0_2_0.db`: created by `initialise_schema` from the `v0.2.0` tag (its own
  `schema.py`, `connection.py` and `backends.py`, SQLite backend), the reference for
  "the baseline reproduces the 0.2.0 schema exactly" and for the legacy upgrade.
- `spark_0_1_0.db`: created the same way from the `v0.1.0` tag; it lacks the
  tables and columns 0.2.0 added, so the legacy upgrade's 0.2.0 ALTERs run on it.

Each holds the same synthetic rows: one conversation (`user-example-0001`), two
messages mentioning only example.com and documentation addresses (192.0.2.10,
198.51.100.7), and one memory. The write-ahead log was checkpointed and removed
before the files were committed.

They were made by loading the tagged modules with `git show <tag>:<path>`, so the
current code played no part. Do not regenerate them from the current tree.

## User bundles

- `bundles/synthetic-v1.zip`: a user bundle in format version 1, exported by
  `spark.bundles.exporter` from the synthetic personal user of `tests/bundle_seed.py`
  (a garden-planning chat, a tea-or-coffee debate, two memories, a knowledge graph,
  the `note-taker` skill, one scheduled action, two small files and two preferences;
  invented names and example.org only, no credential of any kind). It pins the
  format: `tests/test_bundles_fixture.py` reads and imports it as committed, so a
  change that stops version 1 bundles importing fails. Regenerate it only for a new
  format version, under a new name.
