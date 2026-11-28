"""The working marker: a conversation where a turn of the user's is running.

``GET /stream/running`` lists the conversations (tested in test_background_turns_web);
projects.js reads the answer (``workingIds``, under Node) and marks each conversation in
the projects tree and each row of the Conversations page, which carries
``data-conversation-id``. The marker is a labelled image for a screen reader, and it
does not pulse under reduced motion.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.test_projects_ui_frontend import NODE, PROJECTS_JS, TEMPLATES, needs_node

CSS = Path(__file__).resolve().parents[1] / "src" / "spark" / "web" / "static" / "css"

_SCRIPT = r"""
const p = require(process.argv[1]);
console.log(JSON.stringify({
  ids: Array.from(p.workingIds({conversations: [3, '7', 'x', null, 2.5]})),
  none: Array.from(p.workingIds(null)),
  junk: Array.from(p.workingIds({conversations: 'nope'})),
  mark: p.WORKING_MARK,
}));
"""


@needs_node
def test_the_running_list_is_read_as_conversation_ids() -> None:
    assert NODE is not None
    out = subprocess.run(
        [NODE, "-e", _SCRIPT, str(PROJECTS_JS)], capture_output=True, text=True, check=True
    )
    found = json.loads(out.stdout)
    assert found["ids"] == [3, 7]
    assert found["none"] == [] and found["junk"] == []
    assert 'class="turn-working-marker"' in found["mark"]
    assert 'role="img"' in found["mark"] and "aria-label=" in found["mark"]


def test_every_row_of_the_conversations_page_can_carry_the_marker() -> None:
    page = (TEMPLATES / "conversations.html").read_text(encoding="utf-8")
    assert page.count('data-conversation-id="${c.id}"') == 2  # yours, and shared with you
    assert page.count("SparkProjects.showWorking()") == 2  # after each list is drawn
    assert "/static/css/chat_status.css" in page


def test_the_marker_is_still_under_reduced_motion() -> None:
    css = (CSS / "chat_status.css").read_text(encoding="utf-8")
    block = css[css.index("@media (prefers-reduced-motion: reduce) {\n    .turn-working-marker") :]
    assert "animation: none" in block.split("}")[0]
