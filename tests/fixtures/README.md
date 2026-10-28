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
