"""Panel tool schemas and speaking-order normalisation."""

from __future__ import annotations

from spark.core.panel.tools import (
    DELIVER_SYNTHESIS_TOOL,
    REQUEST_NEXT_ROUND_TOOL,
    SET_PANEL_ORDER_TOOL,
    SUBMIT_CONTRIBUTION_TOOL,
    normalise_order,
)

ROLES = ["panellist:1", "panellist:2", "panellist:3"]


def test_tool_shapes() -> None:
    assert SUBMIT_CONTRIBUTION_TOOL["name"] == "submit_contribution"
    assert "contribution_markdown" in SUBMIT_CONTRIBUTION_TOOL["inputSchema"]["required"]
    assert SET_PANEL_ORDER_TOOL["name"] == "set_speaking_order"
    assert SET_PANEL_ORDER_TOOL["inputSchema"]["properties"]["order"]["type"] == "array"
    assert REQUEST_NEXT_ROUND_TOOL["name"] == "request_next_round"
    assert DELIVER_SYNTHESIS_TOOL["name"] == "deliver_synthesis"


class TestNormaliseOrder:
    def test_valid_order_kept(self) -> None:
        assert normalise_order(["panellist:2", "panellist:1", "panellist:3"], ROLES) == [
            "panellist:2",
            "panellist:1",
            "panellist:3",
        ]

    def test_partial_completed_and_unknown_dropped(self) -> None:
        assert normalise_order(["panellist:3", "ghost", "panellist:3"], ROLES) == [
            "panellist:3",
            "panellist:1",
            "panellist:2",
        ]

    def test_garbage_yields_numeric(self) -> None:
        assert normalise_order("nonsense", ROLES) == ROLES
        assert normalise_order(None, ROLES) == ROLES
