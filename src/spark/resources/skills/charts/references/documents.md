# Titles for documents

The document tools make files the user can open and download, and each one is an artefact of the conversation, listed in the conversation panel by its title. A chart, a diagram and a document are named the same way.

## The tools

| Tool | Makes | Top-level parameters |
|---|---|---|
| `create_word` | a Word document (.docx) | `path`, `title`, `content`, `author` |
| `create_excel` | an Excel workbook (.xlsx) | `path`, `title`, `sheets`, `author` |
| `create_powerpoint` | a PowerPoint presentation (.pptx) | `path`, `title`, `slides`, `author` |
| `create_pdf` | a PDF document (.pdf) | `path`, `title`, `content`, `author`, `page_size` |

Each tool's own documentation (`get_tool_documentation`) holds the full formatting guide for `content`, `sheets` and `slides`. This page covers only what is shared.

## The title

- `title` is required on every one of these tools. A missing or blank title is refused before anything is written.
- Make it short and specific, such as "Quarterly sales review" or "Onboarding checklist": no file extension, and never "Untitled", "Document" or "New file".
- The title is a name for people, not a path. The file's name comes from `path`.
- `author` is optional metadata and is not shown as the title.

## Charts in documents

A chart made with `create_chart` is an artefact on its own and is drawn in the conversation. The document tools do not draw charts: they take tables, text and image files. When the user wants figures in a spreadsheet, put the rows in `create_excel`; when they want the picture, make the chart and tell them its card can save it as PNG or SVG.
