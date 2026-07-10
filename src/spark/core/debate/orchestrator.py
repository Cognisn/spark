"""Debate orchestrator: drives the persisted state machine turn by turn."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from spark.core.agent_executor import AgentExecutor
from spark.core.cancellation import CancellationToken
from spark.core.debate import history, prompts
from spark.core.debate.state import DebateState, assert_transition
from spark.core.debate.tools import (
    DELIVER_RULING_TOOL,
    MEMORY_TOOL_NAMES,
    REQUEST_NEXT_ROUND_TOOL,
    SET_SPEAKING_ORDER_TOOL,
    SUBMIT_ARGUMENT_TOOL,
    validate_exhibits,
)
from spark.database import debates

logger = logging.getLogger(__name__)

_DEBATER_EXCLUDED = MEMORY_TOOL_NAMES | frozenset({"spawn_agent", "list_provider_models"})


class DebateOrchestrator:
    """Runs a debate conversation from its persisted state."""

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
        """Advertise enabled skills; failure must never break the debate."""
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
            "debate_state",
            {"role": "system", "state": new.value, "round": cfg.get("current_round", 0)},
        )

    # ------------------------------------------------------------------ public

    def run(
        self,
        conversation_id: int,
        user_guid: str,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        """Drive the debate until QA, cancellation, or a failed turn."""
        cfg = debates.get_debate(self._db, conversation_id)
        if not cfg:
            raise ValueError(f"No debate for conversation {conversation_id}")

        self._skills_block = self._compute_skills_block(user_guid)
        self._skills_user_guid = user_guid

        def cancelled() -> bool:
            return cancel_token is not None and cancel_token.is_cancelled()

        while not cancelled():
            state = DebateState(cfg["state"])
            if state == DebateState.SETUP:
                if not self._opening(conversation_id, cfg):
                    return  # judge turn failed, paused
                self._set_state(conversation_id, cfg, DebateState.OPENING)
                self._set_state(conversation_id, cfg, DebateState.ROUND, current_round=1)
            elif state == DebateState.OPENING:
                # Interrupted between opening and round 1: resume into round 1.
                self._set_state(conversation_id, cfg, DebateState.ROUND, current_round=1)
            elif state == DebateState.ROUND:
                outcome = self._round(conversation_id, cfg, user_guid, cancel_token)
                if outcome == "failed" or cancelled():
                    return
                if outcome == "rule":
                    self._set_state(conversation_id, cfg, DebateState.RULING)
                else:
                    self._set_state(
                        conversation_id,
                        cfg,
                        DebateState.ROUND,
                        current_round=cfg["current_round"] + 1,
                    )
            elif state == DebateState.RULING:
                if not self._ruling(conversation_id, cfg):
                    return
                self._set_state(conversation_id, cfg, DebateState.QA)
            else:  # QA: nothing to drive; answered on demand via answer_qa
                return

    def submit_user_prompt(self, conversation_id: int, text: str) -> int:
        """Queue user input as a turn; typed by current debate state."""
        cfg = debates.get_debate(self._db, conversation_id)
        turn_type = "qa_question" if cfg and cfg["state"] == "qa" else "user_prompt"
        round_no = cfg["current_round"] if cfg else 0
        return debates.add_turn(self._db, conversation_id, round_no, "user", turn_type, text)

    def answer_qa(self, conversation_id: int, user_guid: str, question_turn_id: int) -> str:
        """Ask the judge to answer a QA question about its ruling."""
        cfg = debates.get_debate(self._db, conversation_id)
        self._skills_block = self._compute_skills_block(user_guid)
        turns = debates.get_turns(self._db, conversation_id)
        exhibits = debates.get_exhibits(self._db, conversation_id)
        transcript = history.build_judge_transcript(turns, exhibits, include_qa=True)
        self._emit("floor", {"role": "judge"})
        try:
            response = self._judge_invoke(
                cfg, transcript, prompts.judge_phase_instruction("qa"), tools=None
            )
        finally:
            self._emit("floor", {"role": "none"})
        if response is None:
            return ""
        answer = response.get("content", "")
        debates.add_turn(self._db, conversation_id, 0, "judge", "qa_answer", answer)
        self._emit("judge_text", {"role": "judge", "text": answer, "phase": "qa"})
        return answer

    # ------------------------------------------------------------------ phases

    def _opening(self, cid: int, cfg: dict) -> bool:
        self._emit("floor", {"role": "judge"})
        try:
            return self._opening_inner(cid, cfg)
        finally:
            self._emit("floor", {"role": "none"})

    def _opening_inner(self, cid: int, cfg: dict) -> bool:
        transcript = "(the debate has not started)"
        response = self._judge_invoke(
            cfg,
            transcript,
            prompts.judge_phase_instruction("opening"),
            tools=[SET_SPEAKING_ORDER_TOOL],
        )
        if response is None:
            return False
        first = "pro"
        for tc in response.get("tool_use") or []:
            if tc.get("name") == "set_speaking_order":
                first = tc.get("input", {}).get("first_speaker", "pro")
        framing = response.get("content", "") or f"The debate begins. {first} argues first."
        debates.update_debate_state(self._db, cid, cfg["state"], opening_speaker=first)
        cfg["opening_speaker"] = first
        debates.add_turn(
            self._db,
            cid,
            0,
            "judge",
            "announcement",
            f"{framing}\n\nSpeaking order: {first} first.",
        )
        self._emit("judge_text", {"role": "judge", "text": framing, "phase": "opening"})
        return True

    def _round(
        self,
        cid: int,
        cfg: dict,
        user_guid: str,
        cancel_token: CancellationToken | None,
    ) -> str:
        """Run one round. Returns 'next', 'rule', or 'failed'."""
        order = [cfg["opening_speaker"] or "pro"]
        order.append("con" if order[0] == "pro" else "pro")
        round_no = cfg["current_round"]

        done_roles = {
            t["role"]
            for t in debates.get_turns(self._db, cid)
            if t["turn_type"] == "argument"
            and t["round"] == round_no
            and t["status"] == "complete"
        }
        for role in order:
            if role in done_roles:
                continue  # resuming a partially complete round
            if cancel_token and cancel_token.is_cancelled():
                return "failed"
            if not self._debater_turn(cid, cfg, role, user_guid, cancel_token):
                return "failed"

        # Round complete: the cap forces the ruling, otherwise the judge decides.
        if cfg["rounds_mode"] == "fixed" and round_no >= (cfg["max_rounds"] or 1):
            return "rule"
        turns = debates.get_turns(self._db, cid)
        exhibits = debates.get_exhibits(self._db, cid)
        transcript = history.build_judge_transcript(turns, exhibits)
        self._emit("floor", {"role": "judge"})
        try:
            response = self._judge_invoke(
                cfg,
                transcript,
                prompts.judge_phase_instruction(
                    "interim", current_round=round_no, max_rounds=cfg["max_rounds"]
                ),
                tools=[REQUEST_NEXT_ROUND_TOOL, DELIVER_RULING_TOOL],
            )
        finally:
            self._emit("floor", {"role": "none"})
        if response is None:
            return "failed"
        decision = "next"
        for tc in response.get("tool_use") or []:
            if tc.get("name") == "deliver_ruling":
                decision = "rule"
        remarks = response.get("content", "")
        if remarks:
            debates.add_turn(self._db, cid, round_no, "judge", "interim", remarks)
            self._emit("judge_text", {"role": "judge", "text": remarks, "phase": "interim"})
        return decision

    def _ruling(self, cid: int, cfg: dict) -> bool:
        self._emit("floor", {"role": "judge"})
        try:
            return self._ruling_inner(cid, cfg)
        finally:
            self._emit("floor", {"role": "none"})

    def _ruling_inner(self, cid: int, cfg: dict) -> bool:
        turns = debates.get_turns(self._db, cid)
        exhibits = debates.get_exhibits(self._db, cid)
        transcript = history.build_judge_transcript(turns, exhibits)
        response = self._judge_invoke(
            cfg, transcript, prompts.judge_phase_instruction("ruling"), tools=None
        )
        if response is None:
            return False
        ruling = response.get("content", "")
        debates.add_turn(self._db, cid, 0, "judge", "ruling", ruling)
        self._emit("judge_text", {"role": "judge", "text": ruling, "phase": "ruling"})
        return True

    def _available_tool_names(self) -> set[str]:
        """Every tool name a debater could see (builtin plus MCP), failure-safe."""
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

    def _judge_invoke(
        self, cfg: dict, transcript: str, instruction: str, tools: list[dict] | None
    ) -> dict | None:
        """One judge model invocation. Returns None on failure (debate pauses)."""
        judge = cfg["agents"]["judge"]
        try:
            service = self._factory(judge["model_id"])
            service.set_model(judge["model_id"])
            judge_allowed = judge.get("allowed_skills")
            if judge_allowed is not None:
                judge_block = self._compute_skills_block(
                    getattr(self, "_skills_user_guid", "default"), judge_allowed
                )
            else:
                judge_block = getattr(self, "_skills_block", "")
            system = prompts.judge_system(
                cfg["topic"],
                judge.get("brief"),
                rounds_mode=cfg["rounds_mode"],
                max_rounds=cfg["max_rounds"],
                skills_block=judge_block,
            )
            from spark.skills.tools import get_read_tools

            if judge.get("allowed_skills") == []:
                judge_tools = list(tools or [])
            else:
                judge_tools = get_read_tools() + list(tools or [])
            response = service.invoke_model(
                [
                    {
                        "role": "user",
                        "content": (
                            f"## Debate transcript\n\n{transcript}\n\n"
                            f"## Your task\n{instruction}"
                        ),
                    }
                ],
                max_tokens=4096,
                temperature=0.4,
                tools=judge_tools,
                system=system,
            )
            usage = response.get("usage", {})
            debates.add_agent_tokens(
                self._db,
                cfg["conversation_id"],
                "judge",
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
            )
            return response
        except Exception as e:  # noqa: BLE001 - any provider failure pauses the debate
            logger.error("Judge invocation failed: %s", e)
            self._emit("turn_failed", {"role": "judge", "error": str(e)})
            return None

    def _debater_turn(
        self,
        cid: int,
        cfg: dict,
        role: str,
        user_guid: str,
        cancel_token: CancellationToken | None,
    ) -> bool:
        round_no = cfg["current_round"]
        agent = cfg["agents"][role]
        self._emit("debater_turn_start", {"role": role, "round": round_no})
        self._emit("floor", {"role": role})
        try:
            return self._debater_turn_inner(cid, cfg, role, user_guid, cancel_token, round_no, agent)
        finally:
            self._emit("floor", {"role": "none"})

    def _debater_turn_inner(
        self,
        cid: int,
        cfg: dict,
        role: str,
        user_guid: str,
        cancel_token: CancellationToken | None,
        round_no: int,
        agent: dict,
    ) -> bool:

        turns = debates.get_turns(self._db, cid)
        exhibits = debates.get_exhibits(self._db, cid)
        transcript = history.build_debater_transcript(role, turns, exhibits)
        task = (
            f"## Debate transcript so far\n\n{transcript}\n\n"
            f"## Your task\nResearch as needed, then submit your round {round_no} "
            f"argument with `submit_argument`."
        )
        agent_run_id = f"debate-{cid}-{role}-r{round_no}-{uuid.uuid4().hex[:6]}"
        try:
            from spark.database.agents import create_agent_run

            create_agent_run(
                self._db,
                agent_run_id,
                cid,
                f"{role} debater",
                f"Round {round_no} argument",
                "orchestrator",
                agent["model_id"],
                user_guid,
            )
        except Exception:  # noqa: BLE001 - audit failure must not stop the debate
            logger.warning("Could not record agent run for %s", agent_run_id)

        try:
            exclude = set(_DEBATER_EXCLUDED)
            allowed_tools = agent.get("allowed_tools")
            if allowed_tools is not None:
                exclude |= self._available_tool_names() - set(allowed_tools)
                exclude.discard("submit_argument")

            allowed_skills = agent.get("allowed_skills")
            if allowed_skills is not None:
                debater_block = self._compute_skills_block(user_guid, allowed_skills)
                if not debater_block:
                    exclude |= {"use_skill", "read_skill_resource"}
            else:
                debater_block = getattr(self, "_skills_block", "")

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
                f"{role} debater",
                task,
                agent["model_id"],
                system_override=prompts.debater_system(
                    role,
                    cfg["topic"],
                    agent.get("brief"),
                    skills_block=debater_block,
                ),
                exclude_tools=frozenset(exclude),
                extra_tools=[SUBMIT_ARGUMENT_TOOL],
                terminal_tool="submit_argument",
                cancel_token=cancel_token,
            )
        except Exception as e:  # noqa: BLE001 - any provider failure fails the turn
            logger.error("Debater %s turn failed: %s", role, e)
            debates.add_turn(self._db, cid, round_no, role, "argument", "", status="failed")
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
            debates.add_turn(self._db, cid, round_no, role, "argument", "", status="cancelled")
            return False

        terminal = result.get("terminal_call")
        if not terminal:
            # The model finished without submitting: fail the turn (retryable).
            debates.add_turn(
                self._db,
                cid,
                round_no,
                role,
                "argument",
                result.get("content", ""),
                status="failed",
            )
            self._emit(
                "turn_failed",
                {"role": role, "round": round_no, "error": "No submit_argument call"},
            )
            return False

        argument = terminal["input"].get("argument_markdown", "")
        exhibits_in = validate_exhibits(terminal["input"].get("exhibits"))
        turn_id = debates.add_turn(
            self._db,
            cid,
            round_no,
            role,
            "argument",
            argument,
            token_count=result.get("output_tokens", 0),
        )
        if exhibits_in:
            debates.add_exhibits(self._db, turn_id, exhibits_in)
        self._emit("argument", {"role": role, "round": round_no, "text": argument})
        self._emit("exhibits", {"role": role, "round": round_no, "items": exhibits_in})
        return True
