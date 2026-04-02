# Tool: read_powerpoint_document

## Purpose

Extracts content from PowerPoint presentations (.pptx files). Retrieves slide text, speaker notes, and basic structure information.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| path | string | Yes | - | Path to the PowerPoint file |
| include_notes | boolean | No | true | Include speaker notes |
| include_metadata | boolean | No | true | Include presentation properties |

## Return Value

```json
{
    "path": "/presentations/quarterly_review.pptx",
    "metadata": {
        "title": "Q1 2026 Review",
        "author": "Marketing Team",
        "created": "2026-01-20T09:00:00",
        "modified": "2026-03-01T14:30:00",
        "slide_count": 25
    },
    "slides": [
        {
            "number": 1,
            "title": "Q1 2026 Quarterly Review",
            "content": ["Marketing Department", "March 2026"],
            "notes": "Welcome slide - introduce team members"
        },
        {
            "number": 2,
            "title": "Agenda",
            "content": ["Performance Overview", "Key Achievements", "Challenges", "Q2 Outlook"],
            "notes": "Keep to 2 minutes on this slide"
        }
    ]
}
```

## Handling Large Presentations

For presentations with many slides:

1. **Check metadata first**: Note `slide_count` for planning
2. **Most content is text**: Presentations are typically not as data-heavy as Excel
3. **Speaker notes add volume**: Disable if not needed
4. **Images are not extracted**: Only text content is returned

## Examples

### Read Full Presentation

```json
{
    "tool": "read_powerpoint_document",
    "input": {
        "path": "/presentations/sales_pitch.pptx"
    }
}
```

### Without Speaker Notes

```json
{
    "tool": "read_powerpoint_document",
    "input": {
        "path": "/presentations/training.pptx",
        "include_notes": false
    }
}
```

### Content Only

```json
{
    "tool": "read_powerpoint_document",
    "input": {
        "path": "/presentations/overview.pptx",
        "include_notes": false,
        "include_metadata": false
    }
}
```

## Best Practices

- Use for extracting text content from presentations
- Speaker notes often contain valuable context
- Check slide count to estimate content volume
- Works only with .pptx (not legacy .ppt)

## Common Pitfalls

- Images and charts are not extracted
- Complex SmartArt may have limited text extraction
- Embedded objects are not processed
- Legacy .ppt files require conversion

## Related Tools

- `create_powerpoint_document` - Create new presentations
- `read_word_document` - For Word documents
- `read_pdf_document` - For PDF files
