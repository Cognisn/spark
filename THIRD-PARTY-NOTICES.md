# Third-party notices

SPARK ships the following third-party files inside its wheel. Python dependencies are listed
by their own distributions and checked by `python scripts/licence_audit.py`.

## Inter

- Files: `src/spark/web/static/fonts/inter-latin-400.woff2`, `inter-latin-500.woff2`,
  `inter-latin-600.woff2` and `inter-latin-700.woff2` (the Latin subset, taken unmodified
  from the `@fontsource/inter` 5.3.0 package).
- Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter).
- Licence: SIL Open Font License, Version 1.1 (OFL-1.1). The licence text is in
  `src/spark/web/static/fonts/Inter-OFL.txt`.
- Used for the pages before sign-in, the loading page, the header and the footer only. It is
  served from SPARK itself: no page asks a third party for a font.
