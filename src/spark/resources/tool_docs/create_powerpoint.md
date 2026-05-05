# Tool: create_powerpoint

## Purpose

Creates a Microsoft PowerPoint (.pptx) presentation. Use for generating slide decks, briefings, training materials, and visual presentations.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| path | string | Yes | Output file path (must end in .pptx, within allowed paths) |
| slides | array | Yes | Array of slide definitions |
| title | string | No | Presentation title metadata |
| author | string | No | Presentation author metadata |

### Slide Layouts

Each slide requires a `layout` field. Available layouts:

#### title
Title slide with large title and subtitle. Use as the first slide.

| Property | Type | Description |
|----------|------|-------------|
| layout | "title" | Required |
| title | string | Main title text |
| subtitle | string | Subtitle or author line |
| notes | string | Speaker notes |

#### content
Standard slide with title and bullet points. The most common layout.

| Property | Type | Description |
|----------|------|-------------|
| layout | "content" | Required |
| title | string | Slide title |
| bullets | array | Array of bullet point strings |
| notes | string | Speaker notes |

#### two_column
Slide with title and two columns of bullet points.

| Property | Type | Description |
|----------|------|-------------|
| layout | "two_column" | Required |
| title | string | Slide title |
| left_bullets | array | Left column bullet points |
| right_bullets | array | Right column bullet points |
| notes | string | Speaker notes |

#### table
Slide with a title and data table.

| Property | Type | Description |
|----------|------|-------------|
| layout | "table" | Required |
| title | string | Slide title |
| rows | array | Table rows as arrays of cell strings. First row is typically headers |
| notes | string | Speaker notes |

#### image
Slide with a title and full-width image.

| Property | Type | Description |
|----------|------|-------------|
| layout | "image" | Required |
| title | string | Slide title |
| image_path | string | Path to image file (must be within allowed paths) |
| notes | string | Speaker notes |

#### blank
Empty slide. Use for custom content or as a separator.

| Property | Type | Description |
|----------|------|-------------|
| layout | "blank" | Required |
| notes | string | Speaker notes |

## Examples

### Executive Briefing

```json
{
  "tool": "create_powerpoint",
  "input": {
    "path": "/presentations/q1_briefing.pptx",
    "title": "Q1 2026 Executive Briefing",
    "author": "Spark",
    "slides": [
      {
        "layout": "title",
        "title": "Q1 2026 Executive Briefing",
        "subtitle": "Prepared by Spark — 13 April 2026"
      },
      {
        "layout": "content",
        "title": "Agenda",
        "bullets": [
          "Financial Performance",
          "Product Updates",
          "Market Analysis",
          "Q2 Priorities"
        ]
      },
      {
        "layout": "table",
        "title": "Financial Performance",
        "rows": [
          ["Metric", "Target", "Actual", "Variance"],
          ["Revenue", "$1.0M", "$1.15M", "+15%"],
          ["Gross Margin", "70%", "72%", "+2%"],
          ["New Customers", "50", "63", "+26%"]
        ],
        "notes": "Key talking point: revenue growth driven by enterprise segment"
      },
      {
        "layout": "two_column",
        "title": "Product Updates",
        "left_bullets": [
          "v2.0 launched in March",
          "API redesign complete",
          "Mobile app in beta"
        ],
        "right_bullets": [
          "99.9% uptime achieved",
          "Response time < 200ms",
          "Zero security incidents"
        ]
      },
      {
        "layout": "content",
        "title": "Q2 Priorities",
        "bullets": [
          "Launch enterprise self-service portal",
          "Expand into APAC market",
          "Achieve SOC 2 Type II certification",
          "Reduce infrastructure costs by 20%"
        ]
      }
    ]
  }
}
```

### Security Report Presentation

```json
{
  "tool": "create_powerpoint",
  "input": {
    "path": "/reports/security_review.pptx",
    "slides": [
      {
        "layout": "title",
        "title": "Weekly Security Review",
        "subtitle": "Week of 7 April 2026"
      },
      {
        "layout": "content",
        "title": "Threat Summary",
        "bullets": [
          "142 blocked attacks (down 12% from last week)",
          "3 new attack signatures detected",
          "SQL Injection remains the top attack vector",
          "No successful breaches"
        ]
      },
      {
        "layout": "table",
        "title": "Top Attack Vectors",
        "rows": [
          ["Vector", "Count", "Blocked", "Success Rate"],
          ["SQL Injection", "68", "68", "0%"],
          ["XSS", "42", "42", "0%"],
          ["Path Traversal", "21", "21", "0%"],
          ["Brute Force", "11", "11", "0%"]
        ]
      }
    ]
  }
}
```

## Best Practices

- Start with a "title" layout slide
- Use "content" layout for most informational slides
- Keep bullet points concise — 4-6 items maximum per slide
- Use "table" layout for data comparisons (keep tables small — max 6-8 rows)
- Add speaker notes for context the presenter needs
- Use "two_column" for comparisons (before/after, pros/cons)
- Limit presentations to 10-15 slides for executive audiences

## Common Pitfalls

- Path must be within allowed_paths
- Image paths must also be within allowed paths
- Very long bullet text may overflow the slide area
- The "two_column" layout requires both left_bullets and right_bullets
- Table cells are plain text — no formatting within cells

## Related Tools

- `read_powerpoint` — Read existing presentations
- `create_word` — Create Word documents for detailed reports
- `create_pdf` — Create PDF documents
