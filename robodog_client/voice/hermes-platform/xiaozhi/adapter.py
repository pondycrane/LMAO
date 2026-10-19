"""XiaoZhi voice-board platform adapter for the Hermes gateway.

The RoboDog board (Waveshare ESP32-S3-RLCD-4.2, XiaoZhi voice firmware) becomes a
first-class Hermes gateway client: the proven XiaoZhi WebSocket bridge
(``robodog_client/voice/voice_bridge.py``) stays the realtime transport (Opus
codec, energy VAD, WS/OTA on the board), and this adapter is the board's brain.

Transport split
---------------
* board <-> bridge (``:8000`` WS): Opus audio + hello/listen VAD — untouched.
* bridge <-> adapter (this file, ``:8101`` brain WS): ASR'd text in, spoken
  reply (auto-TTS audio or final text) out.  Each frame carries the bridge's
  ``session_id`` so a late frame from a stale turn is dropped on barge-in.

Identity
--------
The chat/session key is the board's stable Device-Id/MAC (``device_id`` from the
XiaoZhi ``hello``).  Reconnects (bridge restart, board reattach) reuse the same
``device_id`` so the Hermes session, multi-turn memory and allow-listing survive.

Spoken replies
--------------
Inbound user turns are submitted as VOICE-type messages, so the gateway's
auto-TTS path (`/voice all`, global ``voice.auto_tts``, or the binding
``tts.provider: openai`` -> our Kokoro-82M server) fires and delivers the reply
audio through ``send_voice``.  ``send()`` relays only the final text (interim
stream edits are dropped by declaring ``edit_message`` unsupported), and only
when no voice frame already covered this turn — so a turn is spoken exactly once.
The bridge decodes the audio (soundfile) to 24 kHz PCM and streams it to the
board as Opus frames; when only text arrives (auto-TTS off/failed) the bridge
synthesizes it with Kokoro itself, the exact voice the board has always used.

Config
------
gateway.platforms.xiaozhi.enabled: true   (plus ``plugins enabled platforms/xiaozhi``)
XIAOZHI_BRAIN_HOST / XIAOZHI_BRAIN_PORT  (default 127.0.0.1:8101)
XIAOZHI_ALLOWED_USERS / XIAOZHI_ALLOW_ALL_USERS  (authz; dev = allow-all on the LAN)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from typing import Any

try:
    import websockets
    _HAS_WEBSOCKETS = True
except Exception:  # pragma: no cover - import-safe when gated by check_fn
    websockets = None  # type: ignore[assignment]
    _HAS_WEBSOCKETS = False

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger("xiaozhi_adapter")

# Host/port the brain WebSocket server binds (the bridge connects here).
BRAIN_HOST = os.environ.get("XIAOZHI_BRAIN_HOST", "127.0.0.1").strip() or "127.0.0.1"
BRAIN_PORT = int(os.environ.get("XIAOZHI_BRAIN_PORT", "8101") or "8101")
BRAIN_PATH = os.environ.get("XIAOZHI_BRAIN_PATH", "/xiaozhi/brain")
# One spoken reply per turn: preferred audio (voice) suppresses the text reply.
_TURN_VOICE_SENT: dict[str, bool] = {}
_TURN_LOCK: dict[str, asyncio.Lock] = {}
# Live bridge sessions keyed by device_id — drives `is_connected` in gateway status.
_LIVE_DEVICES: set[str] = set()
_LIVE_DEVICES_LOCK = asyncio.Lock()
# device_id -> active WebSocket (reconnect replaces it; sends go to the newest).
_DEVICE_WS: dict[str, Any] = {}


def _make_turn_lock(chat_id: str) -> asyncio.Lock:
    lock = _TURN_LOCK.get(chat_id)
    if lock is None:
        lock = _TURN_LOCK[chat_id] = asyncio.Lock()
    return lock


async def relay_json(device_id: str, payload: dict) -> bool:
    """Best-effort push of a frame to the bridge that owns ``device_id``."""
    ws = _DEVICE_WS.get(device_id)
    if ws is None:
        return False
    try:
        await ws.send(json.dumps(payload))
        return True
    except Exception as e:
        logger.debug("xiaozhi relay %s failed: %s", device_id, e)
        _DEVICE_WS.pop(device_id, None)
        return False


def check_requirements() -> bool:
    """The brain server needs the ``websockets`` package (bundled with the gateway)."""
    return _HAS_WEBSOCKETS


def _is_connected(config) -> bool:
    """Config-level "connected" verdict for `hermes status`/`gateway status`.

    The brain server runs inside the gateway process whenever the platform is
    enabled (it has no external credentials to validate), so the enabled flag is
    the truthful cross-process verdict — the same way status judges token-based
    platforms. The live board session (a bridge attached right now) is tracked
    in the gateway's ``_LIVE_DEVICES`` and drives frame routing, not the CLI.
    """
    try:
        return bool(config is not None and bool(getattr(config, "enabled", False)))
    except Exception:
        return False


class XiaoZhiAdapter(BasePlatformAdapter):
    """Hermes platform adapter for the XiaoZhi voice board (via the local bridge)."""

    MAX_MESSAGE_LENGTH = 2000
    # Board is voice-first: any inbound turn (push-to-talk) is treated as voice input,
    # so auto-TTS spoken replies fire by default even without a `voice.auto_tts` flip.
    _auto_tts_default = True

    def __init__(self, config):
        super().__init__(config=config, platform=Platform("xiaozhi"))
        self._server: Any | None = None
        self._listen_task: asyncio.Task | None = None
        self.brain_host = BRAIN_HOST
        self.brain_port = BRAIN_PORT
        if config is not None:
            extra = getattr(config, "extra", {}) or {}
            self.brain_host = str(extra.get("brain_host") or BRAIN_HOST)
            try:
                self.brain_port = int(extra.get("brain_port") or BRAIN_PORT)
            except (TypeError, ValueError):
                self.brain_port = BRAIN_PORT

    # -- lifecycle -----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not _HAS_WEBSOCKETS:
            logger.error("xiaozhi: websockets unavailable — refusing to connect")
            return False
        self._server = await websockets.serve(
            self._handle_bridge_connection,
            self.brain_host, self.brain_port, ping_interval=30, ping_timeout=60,
            max_size=4 * 1024 * 1024,
        )
        logger.info("xiaozhi brain WS listening on ws://%s:%s%s",
                    self.brain_host, self.brain_port, BRAIN_PATH)
        return True

    async def disconnect(self) -> None:
        if self._server is not None:
            self._server.close()
            with __import__("contextlib").suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        _LIVE_DEVICES.clear()
        _DEVICE_WS.clear()

    # -- brain WebSocket server (bridge connects here) ------------------------

    async def _handle_bridge_connection(self, ws) -> None:
        device_id: str | None = None
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                mtype = msg.get("type")
                if mtype == "hello":
                    device_id = str(msg.get("device_id") or "").strip()
                    if not device_id:
                        device_id = "device-" + uuid.uuid4().hex[:12]
                    _DEVICE_WS[device_id] = ws
                    _LIVE_DEVICES.add(device_id)
                    logger.info("xiaozhi bridge attached device_id=%s", device_id)
                    await relay_json(device_id, {
                        "type": "welcome", "device_id": device_id, "ready": True,
                    })
                elif mtype == "user":
                    if device_id is None:
                        device_id = str(msg.get("device_id") or "").strip() or None
                        if not device_id:
                            continue
                    text = str(msg.get("text") or "").strip()
                    if not text:
                        continue
                    await self._on_user_turn(device_id, text, msg.get("session_id"), ws)
                elif mtype == "ping":
                    await relay_json(device_id or "", {"type": "pong"})
                # anything unknown is ignored — forward-compatible
        except Exception as e:
            logger.debug("xiaozhi brain connection error: %s", e)
        finally:
            if device_id:
                _LIVE_DEVICES.discard(device_id)
                _DEVICE_WS.pop(device_id, None)
                logger.info("xiaozhi bridge detached device_id=%s", device_id)

    async def _on_user_turn(self, device_id: str, text: str, session_id: Any, ws: Any) -> None:
        """A transcribed utterance from the bridge → a Hermes session turn."""
        chat_id = str(device_id)
        await relay_json(chat_id, {"type": "typing", "device_id": chat_id,
                                        "session_id": session_id})
        lock = _make_turn_lock(chat_id)
        async with lock:
            _TURN_VOICE_SENT[chat_id] = False
            event = MessageEvent(
                text=text,
                message_type=MessageType.VOICE,  # voice input → auto-TTS spoken replies
                user_id=chat_id,
                user_name=f"RoboDog:{chat_id[:8]}",
                source=self.build_source(
                    chat_id=chat_id, chat_name="RoboDog Voice Board", chat_type="dm",
                    user_id=chat_id, user_name=f"RoboDog:{chat_id[:8]}",
                    message_id=str(uuid.uuid4())),
                message_id=str(uuid.uuid4()),
            )
            try:
                await self.handle_message(event)
            except Exception as e:
                logger.exception("xiaozhi handle_message failed device=%s: %s",
                        chat_id, e)
                await relay_json(chat_id, {
                    "type": "error", "device_id": chat_id, "session_id": session_id,
                    "message": f"The voice assistant hit an error: {e}",
                })

    # -- outbound reply delivery to the board ---------------------------------

    def _should_auto_tts_for_chat(self, chat_id: str) -> bool:
        """Voice board replies aloud by default; an explicit ``/voice off`` still wins.

        Deliberately NOT driven by the global ``voice.auto_tts`` (which would also
        flip auto-TTS on for telegram/matrix/signal) — this adapter is voice-first.
        """
        return (chat_id in self._auto_tts_enabled_chats
                or chat_id not in self._auto_tts_disabled_chats)

    async def send(self, chat_id: str, content: str, reply_to: str | None = None,
                   metadata: dict[str, Any] | None = None) -> SendResult:
        chat_id = str(chat_id)
        # The board can't render text; if a voice frame already covered this turn,
        # the text relay is a no-op (the gateway's final text still ledgered OK).
        if _TURN_VOICE_SENT.get(chat_id, False):
            return SendResult(success=True, message_id="text-suppressed-by-voice")
        text = (content or "").strip()
        if not text:
            return SendResult(success=True, message_id="")
        session_id = (metadata or {}).get("session_id") if isinstance(metadata, dict) else None
        ok = await relay_json(chat_id, {
            "type": "reply", "device_id": chat_id, "session_id": session_id, "text": text,
        })
        mid = str(uuid.uuid4()) if ok else ""
        return SendResult(success=ok, message_id=mid,
                          error=None if ok else "no live bridge session")

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        # The board has no text preview to edit; declaring this unsupported makes the
        # gateway's stream consumer drop interim deltas and deliver the final only via
        # send(), so the bridge TTS's exactly one reply per turn.
        return SendResult(success=False, error="Not supported")

    async def send_voice(self, chat_id: str, audio_path: str, caption: str | None = None,
                         reply_to: str | None = None,
                         metadata: dict[str, Any] | None = None, **kwargs) -> SendResult:
        chat_id = str(chat_id)
        _TURN_VOICE_SENT[chat_id] = True
        try:
            with open(audio_path, "rb") as fh:
                audio_b64 = base64.b64encode(fh.read()).decode("ascii")
        except OSError as e:
            logger.warning("xiaozhi send_voice read failed %s: %s", audio_path, e)
            return SendResult(success=False, error=f"could not read audio: {e}")
        session_id = (metadata or {}).get("session_id") if isinstance(metadata, dict) else None
        ok = await relay_json(chat_id, {
            "type": "voice", "device_id": chat_id, "session_id": session_id,
            "audio": audio_b64, "mime": "audio/wav",
        })
        return SendResult(success=ok, message_id=str(uuid.uuid4()) if ok else "",
                          error=None if ok else "no live bridge session")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        await relay_json(str(chat_id), {"type": "typing", "device_id": str(chat_id)})

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": "RoboDog Voice Board", "type": "dm", "chat_id": str(chat_id)}


def register(ctx) -> None:
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="xiaozhi",
        label="XiaoZhi Voice Companion",
        adapter_factory=XiaoZhiAdapter,
        check_fn=check_requirements,
        install_hint="No extra packages needed (uses the gateway's bundled websockets)",
        is_connected=_is_connected,
        required_env=[],
        allowed_users_env="XIAOZHI_ALLOWED_USERS",
        allow_all_env="XIAOZHI_ALLOW_ALL_USERS",
        emoji="🎙️",
        max_message_length=2000,
        platform_hint=(
            "You are talking to the RoboDog voice board (XiaoZhi voice firmware) in "
            "the house. The user speaks and you answer out loud. Keep replies short, "
            "conversational and easy to speak (no markdown, no code fences, no "
            "tables); they are read aloud with TTS. The same session continues "
            "across turns and device reconnects, so remember the ongoing "
            "conversation."))
