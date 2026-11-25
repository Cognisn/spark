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

## Document preview and Markdown libraries

The document preview renders documents in the browser with the libraries below, and SPARK's
pages render Markdown with marked and DOMPurify. Each is taken unmodified from the package
named, kept under `src/spark/web/static/vendor/` with its licence text beside it, pinned by
version and SHA-256 in `scripts/licence_audit.py`, and served from SPARK itself: no page
asks a content delivery network for them.

### PDF.js 4.10.38

- Files: `vendor/pdfjs/pdf.min.mjs` and `pdf.worker.min.mjs`, from the npm package
  `pdfjs-dist` 4.10.38.
- Copyright Mozilla Foundation and the PDF.js contributors (https://github.com/mozilla/pdf.js).
- Licence: Apache License 2.0 (Apache-2.0). The licence text is in `vendor/pdfjs/LICENSE.txt`.

### docx-preview 0.4.1

- Files: `vendor/docx-preview/docx-preview.min.js`, from the npm package `docx-preview` 0.4.1.
- Copyright (c) 2016-2023 Volodymyr Baydalka (https://github.com/VolodymyrBaydalka/docxjs).
- Licence: Apache License 2.0 (Apache-2.0). The licence text is in
  `vendor/docx-preview/LICENSE.txt`.

### JSZip 3.10.2

- Files: `vendor/jszip/jszip.min.js`, from the npm package `jszip` 3.10.2 (used by
  docx-preview).
- Copyright (c) 2009-2016 Stuart Knightley, David Duponchel, Franz Buchinger, António Afonso
  (https://github.com/Stuk/jszip).
- Licence: offered as MIT or GPL-3.0-or-later; SPARK uses it under the MIT License (MIT).
  The licence text, with both offers, is in `vendor/jszip/LICENSE.txt`.

### SheetJS Community Edition 0.20.3

- Files: `vendor/sheetjs/xlsx.full.min.js`, from `xlsx-0.20.3.tgz` published by SheetJS at
  https://cdn.sheetjs.com/ (fetched once, when it was added to SPARK).
- Copyright (C) 2012-present SheetJS LLC (https://sheetjs.com).
- Licence: Apache License 2.0 (Apache-2.0). The licence text is in
  `vendor/sheetjs/LICENSE.txt`.

### marked 18.3.0

- Files: `vendor/marked/marked.umd.js`, from the npm package `marked` 18.3.0.
- Copyright (c) 2018-2026 MarkedJS; copyright (c) 2011-2018 Christopher Jeffrey
  (https://github.com/markedjs/marked).
- Licence: MIT License (MIT). The licence text is in `vendor/marked/LICENSE.txt`.

### DOMPurify 3.5.0

- Files: `vendor/dompurify/purify.min.js`, from the npm package `dompurify` 3.5.0.
- Copyright Dr.-Ing. Mario Heiderich, Cure53 (https://github.com/cure53/DOMPurify).
- Licence: offered as MPL-2.0 or Apache-2.0; SPARK uses it under the Apache License 2.0
  (Apache-2.0). The licence text is in `vendor/dompurify/LICENSE.txt`.
