"""Personal mode keeps today's behaviour: one person, sharing hidden."""

from __future__ import annotations

from typing import Any

from spark.database import conversation_members, conversations

MODEL = "stub/model-a"


def _call(client: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
    return client.app.state.sync_db.call(fn, *args, **kwargs)


def test_sharing_is_hidden_in_personal_mode(sqlite_settings: Any) -> None:
    from unittest.mock import MagicMock

    from spark.web.server import create_app
    from tests.db_helpers import app_client
    from tests.test_voice_endpoints import _mock_settings_get
    from tests.web_helpers import sign_in as personal_sign_in

    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    with app_client(app, sqlite_settings) as c:
        app.state.user_guid = "u1"
        cid = _call(c, conversations.create_conversation, "Mine", MODEL, "u1")
        app.state.conversation_manager = MagicMock()
        app.state.conversation_manager.get_conversation.return_value = {"id": cid, "name": "Mine"}
        personal_sign_in(c)
        assert c.get(f"/conversations/api/{cid}/members").status_code == 404
        assert c.get("/conversations/api/people?q=ab").status_code == 404
        assert c.get("/conversations/api/shared").json() == {"conversations": [], "unseen": 0}
        assert 'id="btn-share"' not in c.get(f"/chat/{cid}").text
        assert _call(c, conversation_members.unseen_count, "u1") == 0
