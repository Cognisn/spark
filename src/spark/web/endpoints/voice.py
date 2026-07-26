"""Voice endpoints: config, voice list, synthesis proxy, and usage.

Synthesis is proxied server-side so the ElevenLabs key never reaches the
browser. Any failure returns 503 with a reason, which the client reads as
"use the browser synthesiser for this utterance".
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from spark.database import voice_usage
from spark.voice.engine import VoiceEngine, load_config
from spark.voice.errors import VoiceUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])


def _user_guid(request: Request) -> str:
    return getattr(request.app.state, "user_guid", "default")


def _engine(request: Request) -> VoiceEngine:
    """The app's voice engine, built on first use. Tests may pre-set it."""
    engine = getattr(request.app.state, "voice_engine", None)
    if engine is not None:
        return engine

    ctx = request.app.state.ctx
    config = load_config(ctx)
    db = request.app.state.conversation_manager._db
    cache_root = Path(str(getattr(ctx, "data_dir", "."))) / "voice-cache"
    engine = VoiceEngine(config, db, cache_root)
    request.app.state.voice_engine = engine
    return engine


@router.get("/api/config")
async def voice_config(request: Request) -> JSONResponse:
    """What the frontend needs to pick an engine. Never includes the key."""
    engine = _engine(request)
    cfg = engine.config
    return JSONResponse(
        {
            "enabled": engine.available(),
            "has_key": bool(cfg.api_key),
            "model_id": cfg.model_id,
            "default_voice_id": cfg.default_voice_id,
            "interaction_mode": cfg.interaction_mode,
        }
    )


@router.get("/api/voices")
async def voice_list(request: Request) -> JSONResponse:
    """Premade voices for the pickers. Empty when ElevenLabs is off."""
    return JSONResponse(_engine(request).voices())


@router.post("/api/speak")
async def voice_speak(request: Request) -> Response:
    """Synthesise one utterance, or explain why the browser should do it."""
    data = await request.json()
    text = (data.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)

    try:
        audio = _engine(request).synthesise(
            text,
            data.get("voice_id") or None,
            user_guid=_user_guid(request),
            conversation_id=data.get("conversation_id"),
        )
    except VoiceUnavailable as e:
        # 503 is the client's signal to fall back to the browser synthesiser.
        return JSONResponse({"reason": e.reason}, status_code=503)
    except Exception as e:  # noqa: BLE001 - never fail the utterance outright
        logger.warning("Voice synthesis failed: %s", e)
        return JSONResponse({"reason": "server"}, status_code=503)

    return Response(content=audio, media_type="audio/mpeg")


@router.post("/api/test")
async def voice_test(request: Request) -> JSONResponse:
    """Test the ElevenLabs connection by synthesising a short phrase."""
    # Rebuild the engine so a key just saved in Settings is picked up.
    request.app.state.voice_engine = None
    engine = _engine(request)
    try:
        engine.synthesise("Spark voice is connected.", None, user_guid=_user_guid(request))
    except VoiceUnavailable as e:
        return JSONResponse({"ok": False, "reason": e.reason})
    except Exception as e:  # noqa: BLE001
        logger.warning("Voice test failed: %s", e)
        return JSONResponse({"ok": False, "reason": "server"})
    return JSONResponse({"ok": True})


@router.get("/api/usage")
async def voice_usage_summary(request: Request) -> JSONResponse:
    """Characters billed this month, against the configured cap."""
    engine = _engine(request)
    db = request.app.state.conversation_manager._db
    summary = voice_usage.usage_summary(db, _user_guid(request))
    summary["cap"] = engine.config.monthly_character_cap
    return JSONResponse(summary)
