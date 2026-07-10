"""Panel orchestrator: drives the persisted state machine turn by turn.

Panels reuse the debate tables and state machine. The RULING state carries
the moderator's synthesis. A human panellist's turn is a persisted pause:
the run returns, and the prompt endpoint stores the contribution before the
stream re-runs the orchestrator to resume the rotation.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

from spark.core.agent_executor import AgentExecutor
from spark.core.cancellation import CancellationToken
from spark.core.debate.state import DebateState, assert_transition
from spark.core.debate.tools import MEMORY_TOOL_NAMES
from spark.core.panel import prompts
from spark.core.panel.history import build_panel_transcript
from spark.core.panel.tools import (
    DELIVER_SYNTHESIS_TOOL,
    REQUEST_NEXT_ROUND_TOOL,
    SET_PANEL_ORDER_TOOL,
    SUBMIT_CONTRIBUTION_TOOL,
    normalise_order,
    validate_exhibits,
)
from spark.database import debates

logger = logging.getLogger(__name__)

_PANELLIST_EXCLUDED = MEMORY_TOOL_NAMES | frozenset(
    {"spawn_agent", "list_provider_models"}
)


def _panellist_roles(cfg: dict) -> list[str]:
    """The panellist roles in numeric order (moderator excluded)."""
    roles = [r for r in cfg["agents"] if r.startswith("panellist:")]
    return sorted(roles, key=lambda r: int(r.split(":")[1]))


def _speaking_order(cfg: dict) -> list[str]:
    """The stored speaking order, falling back to numeric order."""
    roles = _panellist_roles(cfg)
    try:
        stored = json.loads(cfg.get("opening_speaker") or "")
    except (ValueError, TypeError):
        stored = None
    return normalise_order(stored, roles)


class PanelOrchestrator:
    """Runs a panel conversation from its persisted state."""

    def __init__(
        self,
        db: Any,
        service_factory: Callable[[str], Any],
        embedded_tools_config: dict[str, Any],
        *,
        mcp_manager: Any | None = None,
        mcp_loop: Any | None = None,
        status_callback: Callable[[str, dict], None] | None = None,
        tool_permission_callback: Callable | None = None,
        context_limit_resolver: Any | None = None,
    ) -> None:
        self._db = db
        self._factory = service_factory
        self._config = embedded_tools_config
        self._mcp_manager = mcp_manager
        self._mcp_loop = mcp_loop
        self._emit_cb = status_callback
        self._permission_cb = tool_permission_callback
        self._limits = context_limit_resolver

    # ------------------------------------------------------------------ events

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._emit_cb:
            self._emit_cb(event_type, data)

    def _compute_skills_block(self, user_guid: str, allowed: list[str] | None = None) -> str:
        """Advertise enabled skills; failure must never break the panel."""
        try:
            from spark.database import skills as skills_db
            from spark.skills.manager import get_skills_manager
            from spark.skills.prompt import build_skills_block

            enabled = skills_db.resolve_enabled(self._db, get_skills_manager(), user_guid)
            if allowed is not None:
                enabled = [s for s in enabled if s["name"] in allowed]
            return build_skills_block(enabled)
        except Exception:  # noqa: BLE001
            logger.warning("Skills block unavailable", exc_info=True)
            return ""

    def _set_state(self, cid: int, cfg: dict, new: DebateState, **kwargs: Any) -> None:
        assert_transition(DebateState(cfg["state"]), new)
        debates.update_debate_state(self._db, cid, new.value, **kwargs)
        cfg["state"] = new.value
        cfg.update({k: v for k, v in kwargs.items() if v is not None})
        self._emit(
            "panel_state",
            {"role": "system", "state": new.value, "round": cfg.get("current_round", 0)},
        )

    def _display_name(self, cfg: dict, role: str) -> str:
        agent = cfg["agents"].get(role) or {}
        return agent.get("display_name") or role

    # ------------------------------------------------------------------ public

    def run(
        self,
        conversation_id: int,
        user_guid: str,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        """Drive the panel until QA, cancellation, a failed turn, or a human pause."""
        cfg = debates.get_debate(self._db, conversation_id)
        if not cfg:
            raise ValueError(f"No panel for conversation {conversation_id}")

        self._skills_block = self._compute_skills_block(user_guid)
        self._skills_user_guid = user_guid

        def cancelled() -> bool:
            return cancel_token is not None and cancel_token.is_cancelled()

        while not cancelled():
            state = DebateState(cfg["state"])
            if state == DebateState.SETUP:
                if not self._opening(conversation_id, cfg):
                    return  # moderator turn failed, paused
                self._set_state(conversation_id, cfg, DebateState.OPENING)
                self._set_state(conversation_id, cfg, DebateState.ROUND, current_round=1)
            elif state == DebateState.OPENING:
                # Interrupted between opening and round 1: resume into round 1.
                self._set_state(conversation_id, cfg, DebateState.ROUND, current_round=1)
            elif state == DebateState.ROUND:
                outcome = self._round(conversation_id, cfg, user_guid, cancel_token)
                if outcome in ("failed", "paused") or cancelled():
                    return
                if outcome == "synthesise":
                    self._set_state(conversation_id, cfg, DebateState.RULING)
                else:
                    self._set_state(
                        conversation_id,
                        cfg,
                        DebateState.ROUND,
                        current_round=cfg["current_round"] + 1,
                    )
            elif state == DebateState.RULING:
                if not self._synthesis(conversation_id, cfg):
                    return
                self._set_state(conversation_id, cfg, DebateState.QA)
            else:  # QA: nothing to drive; answered on demand via answer_qa
                return

    def submit_user_prompt(self, conversation_id: int, text: str) -> int:
        """Queue user input as a turn; typed by current panel state."""
        cfg = debates.get_debate(self._db, conversation_id)
        turn_type = "qa_question" if cfg and cfg["state"] == "qa" else "user_prompt"
        round_no = cfg["current_round"] if cfg else 0
        return debates.add_turn(self._db, conversation_id, round_no, "user", turn_type, text)

    def awaiting_human(self, conversation_id: int, user_guid: str) -> dict | None:
        """The pending human slot the rotation is paused at, or None."""
        cfg = debates.get_debate(self._db, conversation_id)
        if not cfg or cfg["state"] != "round":
            return None
        round_no = cfg["current_round"]
        done = {
            t["role"]
            for t in debates.get_turns(self._db, conversation_id)
            if t["turn_type"] == "contribution"
            and t["round"] == round_no
            and t["status"] == "complete"
        }
        for role in _speaking_order(cfg):
            if role in done:
                continue
            agent = cfg["agents"][role]
            if agent.get("is_human"):
                return {
                    "role": role,
                    "name": self._display_name(cfg, role),
                    "round": round_no,
                }
            return None  # next slot is an AI turn, not a human pause
        return None

    def submit_contribution(
        self, conversation_id: int, user_guid: str, text: str
    ) -> int | None:
        """Store the human panellist's contribution for the awaiting slot."""
        pending = self.awaiting_human(conversation_id, user_guid)
        if not pending:
            return None
        return debates.add_turn(
            self._db,
            conversation_id,
            pending["round"],
            pending["role"],
            "contribution",
            text,
        )

    def answer_qa(self, conversation_id: int, user_guid: str, question_turn_id: int) -> str:
        """Ask the moderator to answer a QA question about the discussion."""
        cfg = debates.get_debate(self._db, conversation_id)
        self._skills_block = self._compute_skills_block(user_guid)
        turns = debates.get_turns(self._db, conversation_id)
        exhibits = debates.get_exhibits(self._db, conversation_id)
        transcript = build_panel_transcript(
            turns, exhibits, cfg["agents"], for_moderator=True, include_qa=True
        )
        self._emit("floor", {"role": "moderator"})
        try:
            response = self._moderator_invoke(
                cfg, transcript, prompts.moderator_phase_instruction("qa"), tools=None
            )
        finally:
            self._emit("floor", {"role": "none"})
        if response is None:
            return ""
        answer = response.get("content", "")
        debates.add_turn(self._db, conversation_id, 0, "moderator", "qa_answer", answer)
        self._emit("moderator_text", {"role": "moderator", "text": answer, "phase": "qa"})
        return answer

    # ------------------------------------------------------------------ phases

    def _opening(self, cid: int, cfg: dict) -> bool:
        self._emit("floor", {"role": "moderator"})
        try:
            return self._opening_inner(cid, cfg)
        finally:
            self._emit("floor", {"role": "none"})

    def _opening_inner(self, cid: int, cfg: dict) -> bool:
        roles = _panellist_roles(cfg)
        response = self._moderator_invoke(
            cfg,
            "(the discussion has not started)",
            prompts.moderator_phase_instruction("opening", order_roles=roles),
            tools=[SET_PANEL_ORDER_TOOL],
        )
        if response is None:
            return False
        raw_order = None
        for tc in response.get("tool_use") or []:
            if tc.get("name") == "set_speaking_order":
                raw_order = tc.get("input", {}).get("order")
        order = normalise_order(raw_order, roles)
        names = ", ".join(self._display_name(cfg, r) for r in order)
        framing = response.get("content", "") or f"The discussion begins with {names}."
        debates.update_debate_state(
            self._db, cid, cfg["state"], opening_speaker=json.dumps(order)
        )
        cfg["opening_speaker"] = json.dumps(order)
        debates.add_turn(
            self._db,
            cid,
            0,
            "moderator",
            "announcement",
            f"{framing}\n\nSpeaking order: {names}.",
        )
        self._emit(
            "moderator_text", {"role": "moderator", "text": framing, "phase": "opening"}
        )
        return True

    def _round(
        self,
        cid: int,
        cfg: dict,
        user_guid: str,
        cancel_token: CancellationToken | None,
    ) -> str:
        """Run one round. Returns 'next', 'synthesise', 'failed', or 'paused'."""
        order = _speaking_order(cfg)
        round_no = cfg["current_round"]

        done_roles = {
            t["role"]
            for t in debates.get_turns(self._db, cid)
            if t["turn_type"] == "contribution"
            and t["round"] == round_no
            and t["status"] == "complete"
        }
        for role in order:
            if role in done_roles:
                continue  # resuming a partially complete round
            if cancel_token and cancel_token.is_cancelled():
                return "failed"
            agent = cfg["agents"][role]
            if agent.get("is_human"):
                # Persisted pause: the human writes their contribution via the
                # prompt endpoint, then the stream re-runs the orchestrator.
                self._emit("floor", {"role": role})
                self._emit(
                    "human_turn",
                    {
                        "role": role,
                        "name": self._display_name(cfg, role),
                        "round": round_no,
                    },
                )
                return "paused"
            if not self._panellist_turn(cid, cfg, role, user_guid, cancel_token):
                return "failed"

        # Round complete: the cap forces synthesis, otherwise the moderator decides.
        if cfg["rounds_mode"] == "fixed" and round_no >= (cfg["max_rounds"] or 1):
            return "synthesise"
        turns = debates.get_turns(self._db, cid)
        exhibits = debates.get_exhibits(self._db, cid)
        transcript = build_panel_transcript(turns, exhibits, cfg["agents"], for_moderator=True)
        self._emit("floor", {"role": "moderator"})
        try:
            response = self._moderator_invoke(
                cfg,
                transcript,
                prompts.moderator_phase_instruction(
                    "interim", current_round=round_no, max_rounds=cfg["max_rounds"]
                ),
                tools=[REQUEST_NEXT_ROUND_TOOL, DELIVER_SYNTHESIS_TOOL],
            )
        finally:
            self._emit("floor", {"role": "none"})
        if response is None:
            return "failed"
        decision = "next"
        for tc in response.get("tool_use") or []:
            if tc.get("name") == "deliver_synthesis":
                decision = "synthesise"
        remarks = response.get("content", "")
        if remarks:
            debates.add_turn(self._db, cid, round_no, "moderator", "interim", remarks)
            self._emit(
                "moderator_text",
                {"role": "moderator", "text": remarks, "phase": "interim"},
            )
        return decision

    def _synthesis(self, cid: int, cfg: dict) -> bool:
        self._emit("floor", {"role": "moderator"})
        try:
            return self._synthesis_inner(cid, cfg)
        finally:
            self._emit("floor", {"role": "none"})

    def _synthesis_inner(self, cid: int, cfg: dict) -> bool:
        turns = debates.get_turns(self._db, cid)
        exhibits = debates.get_exhibits(self._db, cid)
        transcript = build_panel_transcript(turns, exhibits, cfg["agents"], for_moderator=True)
        response = self._moderator_invoke(
            cfg, transcript, prompts.moderator_phase_instruction("synthesis"), tools=None
        )
        if response is None:
            return False
        synthesis = response.get("content", "")
        debates.add_turn(self._db, cid, 0, "moderator", "synthesis", synthesis)
        self._emit(
            "moderator_text",
            {"role": "moderator", "text": synthesis, "phase": "synthesis"},
        )
        return True

    def _available_tool_names(self) -> set[str]:
        """Every tool name a panellist could see (builtin plus MCP), failure-safe."""
        names: set[str] = set()
        try:
            from spark.tools.registry import get_builtin_tools

            names |= {t["name"] for t in get_builtin_tools(self._config)}
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._mcp_manager is not None:
                names |= {
                    t.get("name", "") for t in (self._mcp_manager._tools_cache or [])
                }
        except Exception:  # noqa: BLE001
            pass
        names.discard("")
        return names

    # ------------------------------------------------------------------ agents

    def _moderator_invoke(
        self, cfg: dict, transcript: str, instruction: str, tools: list[dict] | None
    ) -> dict | None:
        """One moderator model invocation. Returns None on failure (panel pauses)."""
        moderator = cfg["agents"]["moderator"]
        try:
            service = self._factory(moderator["model_id"])
            service.set_model(moderator["model_id"])
            mod_allowed = moderator.get("allowed_skills")
            if mod_allowed is not None:
                mod_block = self._compute_skills_block(
                    getattr(self, "_skills_user_guid", "default"), mod_allowed
                )
            else:
                mod_block = getattr(self, "_skills_block", "")
            names = [
                self._display_name(cfg, r) for r in _panellist_roles(cfg)
            ]
            system = prompts.moderator_system(
                cfg["topic"],
                moderator.get("brief"),
                rounds_mode=cfg["rounds_mode"],
                max_rounds=cfg["max_rounds"],
                panellist_names=names,
                skills_block=mod_block,
            )
            from spark.skills.tools import get_read_tools

            if moderator.get("allowed_skills") == []:
                mod_tools = list(tools or [])
            else:
                mod_tools = get_read_tools() + list(tools or [])
            response = service.invoke_model(
                [
                    {
                        "role": "user",
                        "content": (
                            f"## Panel transcript\n\n{transcript}\n\n"
                            f"## Your task\n{instruction}"
                        ),
                    }
                ],
                max_tokens=4096,
                temperature=0.4,
                tools=mod_tools,
                system=system,
            )
            usage = response.get("usage", {})
            debates.add_agent_tokens(
                self._db,
                cfg["conversation_id"],
                "moderator",
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
            )
            return response
        except Exception as e:  # noqa: BLE001 - any provider failure pauses the panel
            logger.error("Moderator invocation failed: %s", e)
            self._emit("turn_failed", {"role": "moderator", "error": str(e)})
            return None

    def _panellist_turn(
        self,
        cid: int,
        cfg: dict,
        role: str,
        user_guid: str,
        cancel_token: CancellationToken | None,
    ) -> bool:
        round_no = cfg["current_round"]
        agent = cfg["agents"][role]
        name = self._display_name(cfg, role)
        self._emit(
            "panellist_turn_start", {"role": role, "name": name, "round": round_no}
        )
        self._emit("floor", {"role": role})
        try:
            return self._panellist_turn_inner(
                cid, cfg, role, user_guid, cancel_token, round_no, agent, name
            )
        finally:
            self._emit("floor", {"role": "none"})

    def _panellist_turn_inner(
        self,
        cid: int,
        cfg: dict,
        role: str,
        user_guid: str,
        cancel_token: CancellationToken | None,
        round_no: int,
        agent: dict,
        name: str,
    ) -> bool:
        turns = debates.get_turns(self._db, cid)
        exhibits = debates.get_exhibits(self._db, cid)
        transcript = build_panel_transcript(turns, exhibits, cfg["agents"])
        task = (
            f"## Discussion transcript so far\n\n{transcript}\n\n"
            f"## Your task\nResearch as needed, then submit your round {round_no} "
            f"contribution with `submit_contribution`."
        )
        agent_run_id = f"panel-{cid}-{role.replace(':', '-')}-r{round_no}-{uuid.uuid4().hex[:6]}"
        try:
            from spark.database.agents import create_agent_run

            create_agent_run(
                self._db,
                agent_run_id,
                cid,
                f"{name} panellist",
                f"Round {round_no} contribution",
                "orchestrator",
                agent["model_id"],
                user_guid,
            )
        except Exception:  # noqa: BLE001 - audit failure must not stop the panel
            logger.warning("Could not record agent run for %s", agent_run_id)

        try:
            exclude = set(_PANELLIST_EXCLUDED)
            allowed_tools = agent.get("allowed_tools")
            if allowed_tools is not None:
                exclude |= self._available_tool_names() - set(allowed_tools)
                exclude.discard("submit_contribution")

            allowed_skills = agent.get("allowed_skills")
            if allowed_skills is not None:
                panellist_block = self._compute_skills_block(user_guid, allowed_skills)
                if not panellist_block:
                    exclude |= {"use_skill", "read_skill_resource"}
            else:
                panellist_block = getattr(self, "_skills_block", "")

            turn_config = dict(self._config)
            if allowed_skills is not None:
                turn_config["_skills_allowlist"] = allowed_skills

            executor = AgentExecutor(
                self._factory(agent["model_id"]),
                self._db,
                turn_config,
                mcp_manager=self._mcp_manager,
                mcp_loop=self._mcp_loop,
                user_guid=user_guid,
                tool_permission_callback=self._permission_cb,
                status_callback=lambda t, d: self._emit(t, {**d, "role": role}),
            )
            result = executor.execute(
                agent_run_id,
                f"{name} panellist",
                task,
                agent["model_id"],
                system_override=prompts.panellist_system(
                    name,
                    cfg["topic"],
                    agent.get("brief"),
                    skills_block=panellist_block,
                ),
                exclude_tools=frozenset(exclude),
                extra_tools=[SUBMIT_CONTRIBUTION_TOOL],
                terminal_tool="submit_contribution",
                cancel_token=cancel_token,
            )
        except Exception as e:  # noqa: BLE001 - any provider failure fails the turn
            logger.error("Panellist %s turn failed: %s", role, e)
            debates.add_turn(self._db, cid, round_no, role, "contribution", "", status="failed")
            self._emit("turn_failed", {"role": role, "round": round_no, "error": str(e)})
            return False

        debates.add_agent_tokens(
            self._db,
            cid,
            role,
            result.get("input_tokens", 0),
            result.get("output_tokens", 0),
        )
        try:
            from spark.database.agents import complete_agent_run

            complete_agent_run(
                self._db,
                agent_run_id,
                status=result.get("status", "completed"),
                result_text="",
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not complete agent run for %s", agent_run_id)

        if result.get("status") == "cancelled":
            debates.add_turn(
                self._db, cid, round_no, role, "contribution", "", status="cancelled"
            )
            return False

        terminal = result.get("terminal_call")
        if not terminal:
            # The model finished without submitting: fail the turn (retryable).
            debates.add_turn(
                self._db,
                cid,
                round_no,
                role,
                "contribution",
                result.get("content", ""),
                status="failed",
            )
            self._emit(
                "turn_failed",
                {"role": role, "round": round_no, "error": "No submit_contribution call"},
            )
            return False

        contribution = terminal["input"].get("contribution_markdown", "")
        exhibits_in = validate_exhibits(terminal["input"].get("exhibits"))
        turn_id = debates.add_turn(
            self._db,
            cid,
            round_no,
            role,
            "contribution",
            contribution,
            token_count=result.get("output_tokens", 0),
        )
        if exhibits_in:
            debates.add_exhibits(self._db, turn_id, exhibits_in)
        self._emit(
            "contribution",
            {"role": role, "name": name, "round": round_no, "text": contribution},
        )
        self._emit("exhibits", {"role": role, "round": round_no, "items": exhibits_in})
        return True
