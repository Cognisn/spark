"""A QR code of a TOTP enrolment's ``otpauth://`` URI, rendered on the server.

Drawn with reportlab's QR widget (reportlab is already a dependency, for PDF
creation), as an SVG in a ``data:`` URI, so no script, no external service
and no new dependency sees the secret. Returns None if the code cannot be
drawn; the screens always show the secret and the URI as text as well.
"""

from __future__ import annotations

import base64
import logging

__all__ = ["qr_data_uri"]

logger = logging.getLogger(__name__)

# The drawn size, in SVG units.
_SIZE = 200


def qr_data_uri(text: str) -> str | None:
    """``text`` as a QR code: an ``image/svg+xml`` data URI, or None."""
    try:
        from reportlab.graphics import renderSVG
        from reportlab.graphics.barcode.qr import QrCodeWidget
        from reportlab.graphics.shapes import Drawing

        widget = QrCodeWidget(text, barLevel="M")
        x0, y0, x1, y1 = widget.getBounds()
        width, height = x1 - x0, y1 - y0
        drawing = Drawing(_SIZE, _SIZE, transform=[_SIZE / width, 0, 0, _SIZE / height, 0, 0])
        drawing.add(widget)
        svg = renderSVG.drawToString(drawing)
    except Exception:  # an unexpected reportlab failure must not break enrolment
        logger.warning("Could not draw the enrolment QR code; showing the secret only")
        return None
    data = svg.encode("utf-8") if isinstance(svg, str) else bytes(svg)
    return "data:image/svg+xml;base64," + base64.b64encode(data).decode("ascii")
