"""User bundles: a personal-mode export of one user's data, imported into a server (A15).

* :mod:`.format`: the bundle layout (version 1), the table allow-list and the writer.
* :mod:`.secrets`: credential shapes, redacted on export and refused on import.
* :mod:`.reader`: opening a bundle with every check (size, containment, version, integrity).
* :mod:`.exporter`: writing a personal-mode user's bundle.
* :mod:`.importer`: importing a bundle for a server user, with its report.

The format is documented in ``docs/bundles.md``.
"""
