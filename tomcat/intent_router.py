# tomcat/intent_router.py
from __future__ import annotations
import asyncio
import io
import json
import re
import os
from collections import deque, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Any, Deque, Dict, List, Optional, Tuple

import discord

# ---- config / logging --------------------------------------------------------
from .config import settings
from .logger import log_action, log_intent
try:
    # Use the common safe sender that respects silent mode
    from .utils.sender import safe_send as _safe_send
except Exception:
    _safe_send = None

# ---- handlers we’ll dispatch to ----------------------------------------------
# Cats: “show me …” and “who is …”
from .handlers.cats import handle_cat_photo, handle_cat_show
# Vision CV: detect / crop / identify (we already added these earlier)
from .handlers.vision import handle_cv_detect, handle_cv_crop, handle_cv_identify
# Feeding: import the handlers module inside this package
from .handlers import feeding  # type: ignore
#BALLS5
from .handlers.misc import (
    handle_profiles_create,
    handle_profile_update_one,
    handle_profiles_update_all,
)

# ---- Aliases and optional NLP ------------------------------------------------
from .aliases import resolve_station_or_cat, alias_vocab
from .nlp.model import NLPModel  # returns None if not available

# ---- Time zone handling (America/Chicago) -----------------------------------
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None  # type: ignore

CENTRAL_TZ = ZoneInfo("America/Chicago") if ZoneInfo else None

# Optional bot mention pattern (uses configured bot_user_id)
try:
    _BOT_ID_INT = int(getattr(settings, "bot_user_id", 0) or 0)
except Exception:
    _BOT_ID_INT = 0
BOT_MENTION_RE = re.compile(rf"<@!?{_BOT_ID_INT}>") if _BOT_ID_INT else None

# ==============================================================================
# Intent event and router
# ==============================================================================


class Intent:
    def __init__(self, type: str, data: Dict[str, Any]):
        self.type = type
        self.data = data

@dataclass
class IntentEvent:
    type: str                      # "show_photo" | "who_is" | "cv_identify" | "cv_detect" | "cv_crop" | "feed_update" | "sub_request" | "sub_accept" | "none"
    confidence: float
    channel_id: int
    user_id: int
    message_id: int
    text: str
    has_image: bool
    attachment_ids: List[int]
    # slots:
    cat_name: Optional[str] = None         # canonical cat or station display name
    station: Optional[str] = None          # canonical station key (same string space as cat if shared)
    stations: Optional[List[str]] = None   # optional multi-station list for feed updates
    dates: Optional[List[str]] = None      # ISO "YYYY-MM-DD"
    # evidence pointers (message ids) used when pairing
    paired_messages: Optional[List[int]] = None

# Simple ring buffer per (channel_id, user_id)
MachineRow = Dict[str, Any]

TOMCAT_PREFIX = re.compile(r"^\s*(tom\s*cat|tomcat|tom-kat|tom\s*kat)[\s,:-]*", re.I)
SHOW_PAT = re.compile(r"\b(show\s*me|show)\b", re.I)
WHO_PAT  = re.compile(r"\b(who\s+is|who\s*’s|who\s*s|whois)\b", re.I)
IDENT_PAT= re.compile(r"\b(identify|id|classify|classification)\b", re.I)
DETECT_PAT = re.compile(r"\bdetect\b", re.I)
CROP_PAT   = re.compile(r"\bcrop\b", re.I)

FEED_REQUEST_RE = re.compile(r"\b(can|could|would)\s+(someone|anyone)\s+feed\b", re.I)
FEED_VERB = re.compile(r"\b(fed|feed(?:ed)?|filled|topped(?:\s*off)?)\b", re.I)
SUB_VERB  = re.compile(r"\b(sub|cover|cover\s+me|can\s+someone|anyone\s+able)\b", re.I)
# Accept patterns in feeding channels (broad but channel-gated)
ACCEPT_PAT= re.compile(
    r"\b(" 
    r"sure|"
    r"i(?:’|')?ll\s+(?:cover|take(?:\s+(?:it|this|[a-z]+))?|do\s+it)|"
    r"i\s+can(?:\s+(?:cover|take|do\s+it))?|"
    r"i\s*'?ve?\s*got\s+(?:it|this)|"
    r"i\s+got\s+it"
    r")\b",
    re.I,
)
FEEDING_CHECK_RE = re.compile(
    r"^(?:(?:who(?:'s|\s+is|\s+has|\s+have|\s+hasn'?t|\s+haven'?t)\s*(?:been\s+)?fed(?:\s+today)?)|(?:which\s+stations?\s+(?:have|has|haven'?t|hasn'?t)\s*(?:been\s+)?fed(?:\s+today)?))\s*[?.!]*$",
    re.I
)
SILENT_CMD = re.compile(r"\bsilent\s*mode\s+(on|off)\b", re.I)
WHO_THIS_RE = re.compile(r"(?:^|\b)(?:who(?:'s|\s+is)|what(?:'s|\s+is))\s+(?:this|that)\s*(?:cat)?\??$", re.I)
FEEDING_UPDATE_RE = re.compile(r"^feeding\s+update\s*$", re.I)
MANUAL_8PM_RE = re.compile(r"^manual\s+8\s*pm\s+update\s*$", re.I)

CREATE_PROFILES_RE = re.compile(r"^create\s+profiles?\s+(\d+)(?:\s+through\s+(\d+))?$", re.I)
UPDATE_PROFILE_RE  = re.compile(r"^update\s+profile\s+(\d+)$", re.I)
UPDATE_ALL_PROFILES_RE = re.compile(r"^update\s+all\s+profiles$", re.I)




# quick weekday map
WEEKDAYS = {w.lower(): i for i, w in enumerate(["Mon","Tue","Wed","Thu","Fri","Sat","Sun"])}

# Tight fuzzy thresholds
FUZZY_ACCEPT = 88
FUZZY_LEN_BIAS = 82
FUZZY_LEN_DELTA = 3

# confidence gates
CONF_HIGH = 0.88
CONF_MID  = 0.75

# optional: rapidfuzz fallback to difflib
try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz  # type: ignore
    def _fuzzy_one(q: str, choices: List[str]) -> Tuple[str, float]:
        if not choices:
            return ("", 0.0)
        name, score, _ = rf_process.extractOne(q, choices, scorer=rf_fuzz.token_set_ratio)  # type: ignore
        return (name, float(score) / 100.0)
except Exception:
    import difflib
    def _fuzzy_one(q: str, choices: List[str]) -> Tuple[str, float]:
        if not choices:
            return ("", 0.0)
        name = difflib.get_close_matches(q, choices, n=1, cutoff=0.0)
        if not name:
            return ("", 0.0)
        # difflib ratio ~ [0,1]
        return (name[0], difflib.SequenceMatcher(None, q, name[0]).ratio())

# ------------------------------------------------------------------------------
# Clarification UI: Yes/No that only the original author can click
# ------------------------------------------------------------------------------

class ClarifyView(discord.ui.View):
    def __init__(self, author_id: int, on_yes):
        super().__init__(timeout=120)  # 2 minutes is plenty; you can set None to keep forever
        self.author_id = author_id
        self.on_yes = on_yes  # async callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        # Politely reject others
        try:
            await interaction.response.send_message("This confirmation is for the original requester.", ephemeral=True)
        except Exception:
            pass
        return False

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success, custom_id="clarify_yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.on_yes(interaction)
        finally:
            self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger, custom_id="clarify_no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Just acknowledge and close
        try:
            await interaction.response.send_message("Okay, ignored.", ephemeral=True)
        except Exception:
            pass
        self.stop()

# ------------------------------------------------------------------------------
# IntentRouter
# ------------------------------------------------------------------------------

class IntentRouter:
    def __init__(self):
        # ring buffer: per (channel_id, user_id) last ~100 rows
        self._buf: Dict[Tuple[int,int], Deque[MachineRow]] = defaultdict(lambda: deque(maxlen=100))
        self._nlp: Optional[NLPModel] = NLPModel.maybe_load(settings)  # returns None if disabled
        self._alias_vocab = alias_vocab()  # {"stations":[names...], "cats":[names...], "all":[...]}
        # ephemeral memory for clarify actions: msg_id -> payload
        self._pending_clarify: Dict[int, Dict[str, Any]] = {}
        # pending CV follow-ups: (channel_id,user_id) -> {intent, requested_ts_iso, expires_ts_iso, message_id}
        self._pending_cv: Dict[Tuple[int,int], Dict[str, Any]] = {}
        # pending FEED follow-ups: station mention ↔ image pairing
        self._pending_feed: Dict[Tuple[int,int], Dict[str, Any]] = {}
        # decision traces for logging: message_id -> [steps]
        self._traces: Dict[int, List[str]] = {}

    # ---------- public entry ----------
    async def handle_message(self, message: Any, ctx: Dict[str, Any]) -> None:

        """Log -> analyze (possibly with short context) -> dispatch or do nothing."""
        try:
            # 0) If user just sent an image and has a pending CV request, fulfill it first
            attachments = getattr(message, "attachments", []) or []
            has_image_now = any((a.content_type or "").startswith("image/") for a in attachments)
            if has_image_now:
                key = (message.channel.id, message.author.id)
                pend = self._pending_cv.get(key)
                if pend:
                    # Check expiry
                    try:
                        expires = datetime.fromisoformat(pend.get("expires_ts_iso"))
                    except Exception:
                        expires = None
                    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
                    if not expires or now <= expires:
                        itype = pend.get("intent", "cv_identify")
                        # Dispatch straight to the vision handler
                        if itype == "cv_crop":
                            await handle_cv_crop(_intent("cv_crop", {}), ctx)
                        elif itype == "cv_detect":
                            await handle_cv_detect(_intent("cv_detect", {}), ctx)
                        else:
                            await handle_cv_identify(_intent("cv_identify", {}), ctx)
                        log_action("cv_pending_fulfilled", f"ch={message.channel.id}; user={message.author.id}", itype)
                        self._pending_cv.pop(key, None)
                        return
                    else:
                        # expired
                        self._pending_cv.pop(key, None)
                        log_action("cv_pending_expired", f"ch={message.channel.id}; user={message.author.id}", pend.get("intent",""))

                # Feed pending fulfilment in #feeding-team
                ft_ch = getattr(settings, "ch_feeding_team", None)
                if ft_ch and int(message.channel.id) == int(ft_ch):
                    fpend = self._pending_feed.get(key)
                    now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
                    if fpend:
                        try:
                            expires = datetime.fromisoformat(fpend.get("expires_ts_iso"))
                        except Exception:
                            expires = None
                        if not expires or now <= expires:
                            stations = fpend.get("stations") or ([fpend.get("station")] if fpend.get("station") else [])
                            from .handlers import feeding
                            for st in stations:
                                ev = IntentEvent(
                                    type="feed_update", confidence=0.95,
                                    channel_id=message.channel.id, user_id=message.author.id, message_id=message.id,
                                    text=message.content or "", has_image=True, attachment_ids=[a.id for a in attachments],
                                    station=st, dates=[self._today()]
                                )
                                await feeding.handle_feed_update_event(ev, ctx)
                            log_action("feed_pending_fulfilled", f"ch={message.channel.id}; user={message.author.id}", ",".join(stations))
                            self._pending_feed.pop(key, None)
                            return
                        else:
                            self._pending_feed.pop(key, None)
                            log_action("feed_pending_expired", f"ch={message.channel.id}; user={message.author.id}", "")

                    # No pending record; try recent station mention (5m) by this user in this channel
                    evs = self._feed_events_from_recent_station_mention(message)
                    if evs:
                        from .handlers import feeding
                        for ev in evs:
                            await feeding.handle_feed_update_event(ev, ctx)
                        log_action("feed_pair_recent", f"ch={message.channel.id}; user={message.author.id}", ",".join(e.station or "" for e in evs))
                        return

            # ------- Phase 1: Preprocess & buffer -------
            row = self._machine_row_from_message(message)
            self._buf[(row["channel_id"], row["user_id"])].append(row)

            # ------- Phase 2: Analyze (addressing + intent + slots + policy) -------
            event = await self._analyze_with_context(row, message)
            if not event or event.type == "none":
                return

            # ------- Phase 3: Log decision trace -------
            trace = self._traces.pop(row["message_id"], [])
            log_intent(event.type, event.confidence,
                       channel_id=event.channel_id, user_id=event.user_id,
                       message_id=event.message_id, has_image=event.has_image,
                       slots={"cat": event.cat_name, "station": event.station, "dates": event.dates},
                       decision=trace)

            # ------- Phase 4: Dispatch -------
            await self._dispatch(event, message, ctx)

        except Exception as e:
            log_action("intent_router_error", f"type={type(e).__name__}", str(e))

    # ---------- log shape for buffer ----------
    def _machine_row_from_message(self, message: discord.Message) -> MachineRow:
        attachments = getattr(message, "attachments", []) or []
        has_image = any((a.content_type or "").startswith("image/") for a in attachments)
        att_ids = [a.id for a in attachments if (a.content_type or "").startswith("image/")]

        return {
            "ts": datetime.now(CENTRAL_TZ).isoformat() if CENTRAL_TZ else datetime.now().isoformat(),
            "channel_id": message.channel.id,
            "channel_name": getattr(message.channel, "name", str(message.channel.id)),
            "user_id": message.author.id,
            "user_name": getattr(message.author, "name", "unknown"),
            "message_id": message.id,
            "reply_to_id": getattr(getattr(message, "reference", None), "message_id", None),
            "text": message.content or "",
            "text_norm": self._normalize_text(message.content or ""),
            "has_image": has_image,
            "attachment_ids": att_ids,
        }

    # ---------- core analysis pipeline ----------
    async def _analyze_with_context(self, row: MachineRow, message: discord.Message) -> Optional[IntentEvent]:
        trace: List[str] = []
        text = row["text_norm"]
        has_image = row["has_image"]
        # Feeding channels: union of ch_feeding_team and allowed_feeding_channel_ids
        feed_ids = set(int(x) for x in (getattr(settings, "allowed_feeding_channel_ids", []) or []))
        ft_id = getattr(settings, "ch_feeding_team", None)
        if ft_id:
            try:
                feed_ids.add(int(ft_id))
            except Exception:
                pass
        in_feeding = int(row["channel_id"]) in feed_ids if feed_ids else False

        # Treat wake signals: mention, wake word, or DM as addressed to the bot
        addressed = bool(TOMCAT_PREFIX.search(text) or self._is_dm(message) or self._is_bot_mentioned(message))
        if self._is_dm(message):
            trace.append("wake:dm")
        elif TOMCAT_PREFIX.search(text):
            trace.append("wake:prefix")
        elif self._is_bot_mentioned(message):
            trace.append("wake:mention")

        # 1) TomCat commands first (show / who / identify) when addressed
        if addressed:
            # strip wake tokens
            text_wo = self._strip_wake_tokens(text, message)
            # Silent mode command: requires TomCat prefix
            m = SILENT_CMD.search(text_wo)
            if m:
                return IntentEvent(
                    type="silent_mode", confidence=1.0,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )

            # "who is this?" → prefer attached/reply image; else last 30s; else set pending and stay quiet
            if WHO_THIS_RE.search(text_wo):
                if has_image or getattr(message, "reference", None):
                    ev = IntentEvent(
                       type="cv_identify", confidence=0.95,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                    )
                    trace.append("rule:who_is_this")
                    self._traces[row["message_id"]] = trace
                    return ev
                pm = self._last_image_for_user_seconds(row["channel_id"], row["user_id"], within_seconds=30)
                if pm:
                    ev = IntentEvent(
                       type="cv_identify", confidence=0.95,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=True, attachment_ids=pm.get("attachment_ids", []),
                        paired_messages=[pm["message_id"]]
                    )
                    trace.append("context:image_user_30s")
                    trace.append("rule:who_is_this")
                    self._traces[row["message_id"]] = trace
                    return ev
                # Set pending and be quiet until an image arrives
                self._set_pending_cv(row["channel_id"], row["user_id"], "cv_identify", row["message_id"])
                self._traces[row["message_id"]] = trace + ["pending:cv_identify"]
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"], text=row["text"], has_image=False, attachment_ids=[])

            # "feeding update" → status listing (requires addressing)
            if FEEDING_UPDATE_RE.search(text_wo):
                ev = IntentEvent(
                    type="feeding_status", confidence=0.95,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )
                trace.append("rule:feeding_status")
                self._traces[row["message_id"]] = trace
                return ev

            # Admin-only manual 8pm preview
            if MANUAL_8PM_RE.search(text_wo):
                ev = IntentEvent(
                    type="manual_8pm", confidence=0.99,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )
                trace.append("rule:manual_8pm")
                self._traces[row["message_id"]] = trace
                return ev

            # Profile management (admin-only later in handler)
            m = CREATE_PROFILES_RE.search(text_wo)
            if m:
                return IntentEvent(
                    type="profiles_create", confidence=0.99,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                    cat_name=None, station=None,
                    dates=None, paired_messages=None
                )
            m = UPDATE_PROFILE_RE.search(text_wo)
            if m:
                return IntentEvent(
                    type="profile_update_one", confidence=0.99,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                    cat_name=None, station=None
                )

            if UPDATE_ALL_PROFILES_RE.search(text_wo):
                return IntentEvent(
                   type="profiles_update_all", confidence=0.99,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )





            # Feeding inquiry: now requires addressing (wake/mention/DM)
            if FEEDING_CHECK_RE.search(text_wo):
                return IntentEvent(
                    type="feeding_status", confidence=0.95,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )
            if SHOW_PAT.search(text_wo):
                cat = self._extract_best_entity(text_wo, want="cat")
                if cat:
                    ev = IntentEvent(
                        type="show_photo", confidence=1.0,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                        cat_name=cat
                    )
                    trace.append(f"slot:cat={cat}")
                    trace.append("intent:show_photo")
                    self._traces[row["message_id"]] = trace
                    return ev
                # no cat? low confidence; ignore
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"],
                                   message_id=row["message_id"], text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"])

            if WHO_PAT.search(text_wo):
                cat = self._extract_best_entity(text_wo, want="cat")
                if cat:
                    ev = IntentEvent(
                        type="who_is", confidence=1.0,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                        cat_name=cat
                    )
                    trace.append(f"slot:cat={cat}")
                    trace.append("intent:who_is")
                    self._traces[row["message_id"]] = trace
                    return ev
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"],
                                   message_id=row["message_id"], text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"])

            # cv: identify
            if IDENT_PAT.search(text_wo):
                # cv identify/detect/crop need an image. Accept if:
                # - attachment present now
                # - message is a reply (handler will resolve image from the referenced message)
                # - last image by same user in the same channel within 30 seconds
                if has_image:
                    ev = IntentEvent(
                        type="cv_identify", confidence=1.0,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=True, attachment_ids=row["attachment_ids"]
                    )
                    trace.append("intent:cv_identify")
                    self._traces[row["message_id"]] = trace
                    return ev
                # allow replies to other people's images regardless of age (handler enforces image presence)
                if getattr(message, "reference", None):
                    return IntentEvent(
                        type="cv_identify", confidence=0.95,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                    )
                # look back for user's own image within the last 30 seconds
                pm = self._last_image_for_user_seconds(
                    row["channel_id"], row["user_id"], within_seconds=int(getattr(settings, "cv_lookback_seconds_before", 30) or 30)
                )
                if pm:
                    ev = IntentEvent(
                        type="cv_identify", confidence=0.95,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=True, attachment_ids=pm.get("attachment_ids", []),
                        paired_messages=[pm["message_id"]]
                    )
                    trace.append("context:image_user_30s")
                    trace.append("intent:cv_identify")
                    self._traces[row["message_id"]] = trace
                    return ev
                # otherwise, create a pending CV follow-up (5 minutes window) and stay silent
                self._set_pending_cv(row["channel_id"], row["user_id"], "cv_identify", row["message_id"])
                trace.append("pending:cv_identify")
                self._traces[row["message_id"]] = trace
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"],
                                   message_id=row["message_id"], text=row["text"], has_image=False, attachment_ids=[])

            # cv: detect
            if DETECT_PAT.search(text_wo):
                if has_image:
                    ev = IntentEvent(
                        type="cv_detect", confidence=1.0,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=True, attachment_ids=row["attachment_ids"]
                    )
                    trace.append("intent:cv_detect")
                    self._traces[row["message_id"]] = trace
                    return ev
                if getattr(message, "reference", None):
                    ev = IntentEvent(
                        type="cv_detect", confidence=0.95,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                    )
                    trace.append("context:reply_image")
                    trace.append("intent:cv_detect")
                    self._traces[row["message_id"]] = trace
                    return ev
                pm = self._last_image_for_user_seconds(
                    row["channel_id"], row["user_id"], within_seconds=int(getattr(settings, "cv_lookback_seconds_before", 30) or 30)
                )
                if pm:
                    ev = IntentEvent(
                        type="cv_detect", confidence=0.95,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=True, attachment_ids=pm.get("attachment_ids", []),
                        paired_messages=[pm["message_id"]]
                    )
                    trace.append("context:image_user_30s")
                    trace.append("intent:cv_detect")
                    self._traces[row["message_id"]] = trace
                    return ev
                self._set_pending_cv(row["channel_id"], row["user_id"], "cv_detect", row["message_id"])
                trace.append("pending:cv_detect")
                self._traces[row["message_id"]] = trace
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"],
                                   message_id=row["message_id"], text=row["text"], has_image=False, attachment_ids=[])

            # cv: crop
            if CROP_PAT.search(text_wo):
                if has_image:
                    ev = IntentEvent(
                        type="cv_crop", confidence=1.0,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=True, attachment_ids=row["attachment_ids"]
                    )
                    trace.append("intent:cv_crop")
                    self._traces[row["message_id"]] = trace
                    return ev
                if getattr(message, "reference", None):
                    ev = IntentEvent(
                        type="cv_crop", confidence=0.95,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                    )
                    trace.append("context:reply_image")
                    trace.append("intent:cv_crop")
                    self._traces[row["message_id"]] = trace
                    return ev
                pm = self._last_image_for_user_seconds(
                    row["channel_id"], row["user_id"], within_seconds=int(getattr(settings, "cv_lookback_seconds_before", 30) or 30)
                )
                if pm:
                    ev = IntentEvent(
                        type="cv_crop", confidence=0.95,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=True, attachment_ids=pm.get("attachment_ids", []),
                        paired_messages=[pm["message_id"]]
                    )
                    trace.append("context:image_user_30s")
                    trace.append("intent:cv_crop")
                    self._traces[row["message_id"]] = trace
                    return ev
                self._set_pending_cv(row["channel_id"], row["user_id"], "cv_crop", row["message_id"])
                trace.append("pending:cv_crop")
                self._traces[row["message_id"]] = trace
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"],
                                   message_id=row["message_id"], text=row["text"], has_image=False, attachment_ids=[])

        # 2) Feeding-team flows (high traffic). Sub-requests first.
        if in_feeding and (SUB_VERB.search(text) or FEED_REQUEST_RE.search(text)):
            stations = self._extract_all_entities(text, want="station")
            dates = self._extract_dates(text)
            if not stations:
                stations = self._stations_from_schedule(row["user_id"], dates)
            conf = 0.9 if stations and dates else 0.75
            ev = IntentEvent(
                type="sub_request", confidence=conf,
                channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                station=(stations[0] if stations else None), stations=(stations or None), dates=dates or None
            )
            trace.append("intent:sub_request")
            self._traces[row["message_id"]] = trace
            return ev

        # Then feed updates
        # Case A: feed verb with possibly multiple stations
        if FEED_VERB.search(text):
            stations = self._extract_all_entities(text, want="station")
            if not stations:
                best = self._extract_best_entity(text, want="station")
                if best:
                    stations = [best]
            if stations:
                dates = self._extract_dates(text)
                if not dates:
                    dates = [self._today()]
                ev = IntentEvent(
                    type="feed_update", confidence=0.95,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                    station=stations[0], stations=stations, dates=dates
                )
                trace.append(f"slot:stations={','.join(stations)}")
                trace.append("intent:feed_update")
                self._traces[row["message_id"]] = trace
                return ev

        # Case B: only station name(s), use image context if needed
        station_only_list = self._extract_all_entities(text, want="station")
        if not station_only_list:
            best = self._extract_best_entity(text, want="station")
            if best:
                station_only_list = [best]
        if station_only_list and in_feeding:
            # If they included "fed" above we already returned. This is the “mike” alone case.
            # If there’s an image now, accept. Else, look back (5m). Else set pending.
            if has_image:
                return IntentEvent(
                    type="feed_update", confidence=0.9,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=True, attachment_ids=row["attachment_ids"],
                    station=station_only_list[0], stations=station_only_list, dates=[self._today()]
                )
            pm = self._last_image_for_user(row["channel_id"], row["user_id"], within_minutes=int(getattr(settings, "feed_lookback_minutes_before", 5) or 5))
            if pm:
                return IntentEvent(
                    type="feed_update", confidence=0.85,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=True, attachment_ids=pm.get("attachment_ids", []),
                    station=station_only_list[0], stations=station_only_list, dates=[self._today()],
                    paired_messages=[pm["message_id"]]
                )
            # Set pending and stay silent
            self._set_pending_feed(row["channel_id"], row["user_id"], station_only_list, row["message_id"])
            trace.append("pending:feed_update")
            self._traces[row["message_id"]] = trace
            return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"], text=row["text"], has_image=False, attachment_ids=[])

        # 3) Sub requests / accepts
        if SUB_VERB.search(text):
            # Only treat as a sub request in feeding channels
            if not in_feeding:
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"], text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]) 
            stations = self._extract_all_entities(text, want="station")
            dates = self._extract_dates(text)
            conf = 0.9 if stations and dates else 0.75
            ev = IntentEvent(
                type="sub_request", confidence=conf,
                channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                station=stations[0] if stations else None, dates=dates or None
            )
            trace.append("intent:sub_request")
            self._traces[row["message_id"]] = trace
            return ev

        if ACCEPT_PAT.search(text):
            if not in_feeding:
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"], text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]) 
            # Acknowledge only if replying to a sub_request or if the immediately previous sub_request exists in buffer
            ref_id = row.get("reply_to_id")
            if ref_id:
                ev = IntentEvent(
                    type="sub_accept", confidence=0.9,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )
                trace.append("intent:sub_accept")
                self._traces[row["message_id"]] = trace
                return ev
            # else try a quick look-back for last sub_request in channel (not just same user)
            if self._recent_sub_request_in_channel(row["channel_id"]):
                ev = IntentEvent(
                    type="sub_accept", confidence=0.8,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )
                trace.append("intent:sub_accept")
                self._traces[row["message_id"]] = trace
                return ev

        # 4) If needed, run NLP fallback (intent + station scorer)
        # Guard: only consult NLP if addressed OR in feeding-team (to avoid false positives on general chatter).
        if self._nlp and len(text) >= 3 and (addressed or in_feeding):
            nlp_intent, nlp_prob = self._nlp.predict_intent(text)
            if nlp_intent in {"feed_update","sub_request"} and nlp_prob >= CONF_MID:
                station = self._extract_best_entity(text, want="station", allow_model=True)
                if nlp_intent == "feed_update" and station:
                    return IntentEvent(
                        type="feed_update", confidence=max(nlp_prob, 0.8),
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                        station=station, dates=[self._today()]
                    )
                if nlp_intent == "sub_request" and in_feeding:
                    dates = self._extract_dates(text) or None
                    st_list = [station] if station else self._stations_from_schedule(row["user_id"], dates)
                    return IntentEvent(
                        type="sub_request", confidence=max(nlp_prob, 0.8),
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                        station=(st_list[0] if st_list else None), stations=(st_list or None), dates=dates
                    )

        # Default: none
        self._traces[row["message_id"]] = trace
        return IntentEvent(type="none", confidence=0.0,
                           channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                           text=row["text"], has_image=row["has_image"], attachment_ids=row["attachment_ids"])

    # ---------- dispatch ----------
    async def _dispatch(self, event: IntentEvent, message: discord.Message, ctx: Dict[str, Any]) -> None:
        # Confidence gates and clarification

        # Commands
        if event.type == "show_photo" and event.cat_name:
            # reuse your cats handler
            await handle_cat_photo(_intent("cat_photo", {"name": event.cat_name}), ctx)
            return

        if event.type == "who_is" and event.cat_name:
            await handle_cat_show(_intent("cat_show", {"name": event.cat_name}), ctx)
            return

        if event.type == "cv_identify":
            # If event.attachment_ids came from context pairing, we’ll “replay” the other message by forging message.attachments.
            # Easiest path: just call the existing handler; it already checks current or referenced message.
            # If this came via a reply, suppress the handler's "attach an image" prompt when empty
            via_reply = bool(getattr(message, "reference", None))
            await handle_cv_identify(_intent("cv_identify", {}), {**ctx, "message": message, "silent_on_no_image": via_reply})
            return

        if event.type == "sub_request":
            if getattr(event, 'stations', None) and len(event.stations) > 1:
                for st in event.stations:
                    e2 = IntentEvent(
                        type="sub_request", confidence=event.confidence,
                        channel_id=event.channel_id, user_id=event.user_id, message_id=event.message_id,
                        text=event.text, has_image=event.has_image, attachment_ids=event.attachment_ids,
                        station=st, dates=event.dates
                    )
                    await feeding.handle_sub_request_event(e2, ctx)
            else:
                await feeding.handle_sub_request_event(event, ctx)
            return

        if event.type == "sub_accept":
            await feeding.handle_sub_accept_event(event, ctx)
            return

        if event.type == "feed_update":
            if event.stations and len(event.stations) > 1:
                for st in event.stations:
                    e2 = IntentEvent(
                        type="feed_update", confidence=event.confidence,
                        channel_id=event.channel_id, user_id=event.user_id, message_id=event.message_id,
                        text=event.text, has_image=event.has_image, attachment_ids=event.attachment_ids,
                        station=st, dates=event.dates
                    )
                    await feeding.handle_feed_update_event(e2, ctx)
            elif event.station:
                await feeding.handle_feed_update_event(event, ctx)
            return
        
        if event.type == "feeding_status":
            await feeding.handle_feeding_inquiry(_intent("feeding_inquiry", {}), ctx)
            return

        if event.type == "cv_detect":
            via_reply = bool(getattr(message, "reference", None))
            await handle_cv_detect(_intent("cv_detect", {}), {**ctx, "message": message, "silent_on_no_image": via_reply})
            return

        if event.type == "cv_crop":
            via_reply = bool(getattr(message, "reference", None))
            await handle_cv_crop(_intent("cv_crop", {}), {**ctx, "message": message, "silent_on_no_image": via_reply})
            return
        
        if event.type == "profiles_create":
            m = CREATE_PROFILES_RE.search(TOMCAT_PREFIX.sub("", event.text, count=1).strip())
            if m:
                start_id = int(m.group(1))
                end_id = int(m.group(2) or m.group(1))
                await handle_profiles_create(_intent("profiles_create", {"start_id": start_id, "end_id": end_id}), ctx)
                return

        if event.type == "profile_update_one":
            m = UPDATE_PROFILE_RE.search(TOMCAT_PREFIX.sub("", event.text, count=1).strip())
            if m:
                await handle_profile_update_one(_intent("profile_update_one", {"cat_id": m.group(1)}), ctx)
                return

        if event.type == "profiles_update_all":
            await handle_profiles_update_all(_intent("profiles_update_all", {}), ctx)
            return

        if event.type == "silent_mode":
            # Admin-only; no chatter on success, because silent mode rules.
            author = message.author
            perms = getattr(getattr(author, "guild_permissions", None), "administrator", False)
            m = SILENT_CMD.search(TOMCAT_PREFIX.sub("", event.text, count=1))
            on_str = m.group(1).lower() if m else ""
            on = (on_str == "on")
            if perms:
                settings.silent_mode = on
                log_action("silent_mode", f"by={author.id}", "on" if on else "off")
            else:
                log_action("silent_mode_denied", f"by={author.id}", "not_admin")
            return

        if event.type == "manual_8pm":
            # Admin-only via settings.admin_ids or guild admin
            author = message.author
            is_admin = int(getattr(author,'id',0)) in (getattr(settings,'admin_ids',[]) or []) or getattr(getattr(author, 'guild_permissions', None), 'administrator', False)
            if not is_admin:
                log_action("manual_8pm_denied", f"by={getattr(author,'id',0)}", "not_admin")
                return
            from .handlers.feeding import handle_manual_8pm_preview
            await handle_manual_8pm_preview(_intent("manual_8pm", {}), {**ctx, "bot": ctx.get("bot")})
            return
        



    # ---------- clarify feed (author-locked) ----------
    async def _maybe_clarify_feed(self, event: IntentEvent, message: discord.Message) -> None:
        station = event.station or "unknown"
        title = "Did you mean?"
        desc = f"Mark **{station}** as fed today?"
        embed = discord.Embed(title=title, description=desc, color=0x2F3136)

        async def on_yes(interaction: discord.Interaction):
            # call feeding directly with high confidence
            strong = IntentEvent(
                type="feed_update", confidence=1.0,
                channel_id=event.channel_id, user_id=event.user_id, message_id=event.message_id,
                text=event.text, has_image=event.has_image, attachment_ids=event.attachment_ids,
                station=station, dates=event.dates or [self._today()]
            )
            await feeding.handle_feed_update_event(strong, {"channel": message.channel, "message": message})
            try:
                await interaction.response.edit_message(content="Marked.", embed=None, view=None)
            except Exception:
                pass

        view = ClarifyView(author_id=message.author.id, on_yes=on_yes)

        if _safe_send:
            await _safe_send(message.channel, "", embed=embed, view=view)
            log_action("clarify", f"user={message.author.name}; station={station}", "clarification message sent or suppressed")
        else:
            # fallback
            try:
                await message.channel.send(embed=embed, view=view)
            finally:
                log_action("clarify", f"user={message.author.name}; station={station}", "clarification message sent (no safe_send)")

    # ---------- helpers: entity, context, dates ----------
    def _normalize_text(self, s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip().lower())

    def _extract_best_entity(self, text: str, want: str, allow_model: bool=False) -> Optional[str]:
        """want in {'cat','station'}. Try aliases, then fuzzy, then optional NLP scorer."""
        # 1) alias exact/normalized
        found = resolve_station_or_cat(text, want=want)
        if found:
            return found

        # 2) fuzzy over union
        vocab = self._alias_vocab["cats"] if want == "cat" else self._alias_vocab["stations"]
        token = self._best_token_for_fuzzy(text)
        if token:
            name, score = _fuzzy_one(token, vocab)
            if score >= CONF_HIGH:
                return name
            if score >= 0.82 and abs(len(token) - len(name)) <= FUZZY_LEN_DELTA:
                return name

        # 3) optional model scoring
        if allow_model and self._nlp is not None:
            best, prob = self._nlp.score_entity(text, vocab)
            if prob >= CONF_HIGH:
                return best

        return None

    def _extract_all_entities(self, text: str, want: str) -> List[str]:
        # Stations: use alias resolver so aliases like "west" → "West Hall" work
        if want == "station":
            try:
                from .aliases import resolve_stations as _resolve_stations
                stations = _resolve_stations(text)
                # resolve_stations returns display names already; ensure unique preserve order
                out: List[str] = []
                seen = set()
                for s in stations:
                    if s not in seen:
                        seen.add(s); out.append(s)
                return out
            except Exception:
                pass
        # Default cat path: match against display-name vocab (catch simple mentions like "Twix")
        names: List[str] = []
        for nm in (alias_vocab()[f"{want}s"]):
            if re.search(rf"\b{re.escape(nm.lower())}\b", text.lower()):
                names.append(nm)
        # unique, preserve order
        seen = set(); out = []
        for n in names:
            if n not in seen:
                out.append(n); seen.add(n)
        return out

    def _best_token_for_fuzzy(self, text: str) -> Optional[str]:
        # pick the longest token-ish word as candidate
        toks = [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]
        if not toks:
            return None
        return max(toks, key=len)

    def _last_image_for_user(self, channel_id: int, user_id: int, within_minutes: int=10) -> Optional[MachineRow]:
        dq = self._buf.get((channel_id, user_id))
        if not dq:
            return None
        cutoff = datetime.now(CENTRAL_TZ) - timedelta(minutes=within_minutes) if CENTRAL_TZ else datetime.now() - timedelta(minutes=within_minutes)
        for row in reversed(dq):
            if row["has_image"]:
                try:
                    ts = datetime.fromisoformat(row["ts"])
                except Exception:
                    ts = datetime.now()
                if ts >= cutoff:
                    return row
        return None

    def _last_image_for_user_seconds(self, channel_id: int, user_id: int, within_seconds: int=30) -> Optional[MachineRow]:
        dq = self._buf.get((channel_id, user_id))
        if not dq:
            return None
        delta = timedelta(seconds=max(1, int(within_seconds)))
        cutoff = (datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()) - delta
        for row in reversed(dq):
            if row.get("has_image"):
                try:
                    ts = datetime.fromisoformat(row["ts"]) if isinstance(row.get("ts"), str) else datetime.now()
                except Exception:
                    ts = datetime.now()
                if ts >= cutoff:
                    return row
        return None

    def _last_image_in_channel(self, channel_id: int, within_minutes: int=10) -> Optional[MachineRow]:
        cutoff = datetime.now(CENTRAL_TZ) - timedelta(minutes=within_minutes) if CENTRAL_TZ else datetime.now() - timedelta(minutes=within_minutes)
        for (cid, _uid), dq in self._buf.items():
            if cid != channel_id:
                continue
            for row in reversed(dq):
                if row.get("has_image"):
                    try:
                        ts = datetime.fromisoformat(row["ts"]) if isinstance(row.get("ts"), str) else datetime.now()
                    except Exception:
                        ts = datetime.now()
                    if ts >= cutoff:
                        return row
        return None

    def _recent_sub_request_in_channel(self, channel_id: int) -> bool:
        # naive: scan last few buffers for this channel; cheap and good enough
        for (cid, _uid), dq in self._buf.items():
            if cid != channel_id: 
                continue
            for row in reversed(dq):
                if SUB_VERB.search(row["text_norm"]):
                    return True
        return False

    def _today(self) -> str:
        dt = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
        return dt.date().isoformat()

    def _extract_dates(self, text: str) -> List[str]:
        """Basic rules you requested: yesterday/last night, this/next weekday, 21st-28th."""
        text = text.lower()
        today = datetime.now(CENTRAL_TZ).date() if CENTRAL_TZ else date.today()
        out: List[date] = []

        if "today" in text:
            out.append(today)
        if "tomorrow" in text:
            out.append(today + timedelta(days=1))

        if "yesterday" in text or "last night" in text:
            out.append(today - timedelta(days=1))

        # on <weekday> -> previous occurrence (most recent in past)
        m_on = re.search(r"\bon\s+(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b", text)
        if m_on:
            word = m_on.group(1)[:3]
            out.append(self._prev_weekday(today, WEEKDAYS[word]))

        # this/next weekday, or bare weekday -> next
        m = re.search(r"\b(this|next)?\s*(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b", text)
        if m:
            word = m.group(2)[:3]
            target = self._next_weekday(today, WEEKDAYS[word])
            # force “this friday” to mean next occurrence, per your rule
            out.append(target)

        # numeric range “21st to 28th”, “21-28”
        m2 = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:to|-)\s*(\d{1,2})(?:st|nd|rd|th)?\b", text)
        if m2:
            d1 = int(m2.group(1)); d2 = int(m2.group(2))
            # if today ≤ 20 assume this month; if today ≥ 22 assume next month; 21/22 edge okay
            base = today
            if today.day >= 22:
                # roll to next month
                year = today.year + (1 if today.month == 12 else 0)
                month = 1 if today.month == 12 else today.month + 1
                base = date(year, month, 1)
            for d in range(d1, d2 + 1):
                try:
                    out.append(date(base.year, base.month, d))
                except Exception:
                    continue

        # If someone says “I fed microwave saturday before I left vacation”
        if "saturday" in text and "fed" in text:
            # interpret as last Saturday
            out.append(self._prev_weekday(today, WEEKDAYS["sat"]))

        # dedupe and sort
        iso = sorted({d.isoformat() for d in out})
        return iso

    def _next_weekday(self, today: date, tgt: int) -> date:
        days_ahead = (tgt - today.weekday() + 7) % 7
        days_ahead = 7 if days_ahead == 0 else days_ahead  # always next occurrence
        return today + timedelta(days=days_ahead)

    def _prev_weekday(self, today: date, tgt: int) -> date:
        days_back = (today.weekday() - tgt + 7) % 7
        days_back = 7 if days_back == 0 else days_back
        return today - timedelta(days=days_back)

    # ---------- pending FEED helpers ----------
    def _set_pending_feed(self, channel_id: int, user_id: int, stations: List[str], message_id: int) -> None:
        now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
        expires = now + timedelta(minutes=int(getattr(settings, "feed_pending_minutes_after", 5) or 5))
        self._pending_feed[(channel_id, user_id)] = {
            "stations": stations,
            "requested_ts_iso": now.isoformat(),
            "expires_ts_iso": expires.isoformat(),
            "message_id": message_id,
        }
        log_action("feed_pending_set", f"ch={channel_id}; user={user_id}", ",".join(stations))

    def _feed_events_from_recent_station_mention(self, message: discord.Message) -> List[IntentEvent]:
        key = (message.channel.id, message.author.id)
        dq = self._buf.get(key)
        if not dq:
            return []
        look = int(getattr(settings, "feed_lookback_minutes_before", 5) or 5)
        cutoff = datetime.now(CENTRAL_TZ) - timedelta(minutes=look) if CENTRAL_TZ else datetime.now() - timedelta(minutes=look)
        stations: List[str] = []
        for row in reversed(dq):
            try:
                ts = datetime.fromisoformat(row.get("ts"))
            except Exception:
                ts = datetime.now()
            if ts < cutoff:
                break
            text = row.get("text_norm") or ""
            if SUB_VERB.search(text) or FEED_REQUEST_RE.search(text):
                continue
            if FEED_VERB.search(text):
                continue
            sts = self._extract_all_entities(text, want="station")
            if not sts:
                best = self._extract_best_entity(text, want="station")
                if best:
                    sts = [best]
            if sts:
                stations = sts
                break
        evs: List[IntentEvent] = []
        for st in stations:
            evs.append(IntentEvent(
                type="feed_update", confidence=0.9,
                channel_id=message.channel.id, user_id=message.author.id, message_id=message.id,
                text=message.content or "", has_image=True, attachment_ids=[a.id for a in getattr(message, "attachments", []) or []],
                station=st, dates=[self._today()]
            ))
        return evs

    # ---------- pending CV helpers ----------
    def _set_pending_cv(self, channel_id: int, user_id: int, intent: str, message_id: int) -> None:
        now = datetime.now(CENTRAL_TZ) if CENTRAL_TZ else datetime.now()
        after_min = int(getattr(settings, "cv_pending_minutes_after", 5) or 5)
        expires = now + timedelta(minutes=after_min)
        payload = {
            "intent": intent,
            "requested_ts_iso": now.isoformat(),
            "expires_ts_iso": expires.isoformat(),
            "message_id": message_id,
        }
        self._pending_cv[(channel_id, user_id)] = payload
        log_action("cv_pending_set", f"ch={channel_id}; user={user_id}", intent)

    # ---------- addressing helpers ----------
    def _is_dm(self, message: discord.Message) -> bool:
        return isinstance(getattr(message, "channel", None), discord.DMChannel)

    def _is_bot_mentioned(self, message: discord.Message) -> bool:
        if _BOT_ID_INT:
            try:
                for u in getattr(message, "mentions", []) or []:
                    if int(getattr(u, "id", 0)) == _BOT_ID_INT:
                        return True
            except Exception:
                pass
            # fallback string pattern
            try:
                if BOT_MENTION_RE and BOT_MENTION_RE.search(message.content or ""):
                    return True
            except Exception:
                pass
        return False

    def _strip_wake_tokens(self, text_norm: str, message: discord.Message) -> str:
        s = TOMCAT_PREFIX.sub("", text_norm, count=1).strip()
        if _BOT_ID_INT:
            try:
                s = re.sub(rf"\s*<@!?{_BOT_ID_INT}>\s*[:,\-]*\s*", " ", s).strip()
            except Exception:
                pass
        s = re.sub(r"\s+", " ", s).strip()
        return s

def _intent(name: str, data: Dict[str, Any]) -> Intent:
    return Intent(name, data)
