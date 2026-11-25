"""The document preview (CS37 to CS39): show a document beside the conversation.

* :mod:`spark.preview.kinds`: the renderer a document is shown with, and the check of
  its declared type against its content.
* :mod:`spark.preview.guard`: the fetch guard, the rules under which SPARK's server
  fetches a link.
* :mod:`spark.preview.sources`: the hosts a link may come from (the sender's MCP
  servers).
* :mod:`spark.preview.service`: a link or a file made into a file of the conversation,
  and the document card that reopens it.

The ``show_document`` tool (:mod:`spark.tools.show_document`) runs the service; the
web routes (:mod:`spark.web.endpoints.preview`) serve the file to members of the
conversation and the sandboxed frame that renders it.
"""
