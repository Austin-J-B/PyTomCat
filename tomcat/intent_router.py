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
from .logger import log_action
try:
    from .handlers.misc import safe_send as _safe_send
except Exception:
    _safe_send = None

# ---- handlers we’ll dispatch to ----------------------------------------------
# Cats: “show me …” and “who is …”
from .handlers.cats import handle_cat_photo, handle_cat_show
# Vision CV: detect / crop / identify (we already added these earlier)
from .handlers.vision import handle_cv_detect, handle_cv_crop, handle_cv_identify
# Feeding: we’ll call a single entrypoint; add the helper below if you don’t have one
import tomcat.feeding as feeding  # type: ignore

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
    dates: Optional[List[str]] = None      # ISO "YYYY-MM-DD"
    # evidence pointers (message ids) used when pairing
    paired_messages: Optional[List[int]] = None

# Simple ring buffer per (channel_id, user_id)
MachineRow = Dict[str, Any]

TOMCAT_PREFIX = re.compile(r"^\s*(tom\s*cat|tomcat|tom-kat|tom\s*kat)[\s,:-]*", re.I)
SHOW_PAT = re.compile(r"\b(show\s*me|show)\b", re.I)
WHO_PAT  = re.compile(r"\b(who\s+is|who\s*’s|who\s*s|whois)\b", re.I)
IDENT_PAT= re.compile(r"\b(identify|id)\b", re.I)

FEED_VERB = re.compile(r"\b(fed|feed(?:ed)?|filled|topped(?:\s*off)?)\b", re.I)
SUB_VERB  = re.compile(r"\b(sub|cover|cover\s+me|can\s+someone|anyone\s+able)\b", re.I)
ACCEPT_PAT= re.compile(r"\b(sure|i(?:’|')?ll\s+cover|i\s+can\s+cover|i\s+got\s+it)\b", re.I)
FEEDING_CHECK_RE = re.compile(
    r"^(?:(?:who(?:'s|\s+is)\s+(?:been\s+)?fed(?:\s+today)?)|(?:which\s+stations?\s+(?:have|has|haven'?t|hasn'?t)\s*(?:been\s+)?fed(?:\s+today)?))\s*[?.!]*$",
    re.I
)
SILENT_CMD = re.compile(r"\bsilent\s*mode\s+(on|off)\b", re.I)
WHO_THIS_RE = re.compile(r"^who(?:'s|\s+is)\s+this\??$", re.I)
FEEDING_UPDATE_RE = re.compile(r"^feeding\s+update\s*$", re.I)

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

    # ---------- public entry ----------
    async def handle_message(self, message: Any, ctx: Dict[str, Any]) -> None:

        """Log -> analyze (possibly with short context) -> dispatch or do nothing."""
        try:
            row = self._machine_row_from_message(message)
            self._buf[(row["channel_id"], row["user_id"])].append(row)

            event = await self._analyze_with_context(row, message)
            if not event or event.type == "none":
                return

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
        text = row["text_norm"]
        has_image = row["has_image"]

        # 1) TomCat commands first (show / who / identify)
        if TOMCAT_PREFIX.search(text):
            # strip prefix
            text_wo = TOMCAT_PREFIX.sub("", text, count=1).strip()
            # Silent mode command: requires TomCat prefix
            m = SILENT_CMD.search(text_wo)
            if m:
                return IntentEvent(
                    type="silent_mode", confidence=1.0,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )

            # "who is this?" → cv_identify on last/attached image
            if WHO_THIS_RE.search(text_wo):
                return IntentEvent(
                   type="cv_identify", confidence=0.95,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )

            # "feeding update" → status listing
            if FEEDING_UPDATE_RE.search(text_wo):
                return IntentEvent(
                    type="feeding_status", confidence=0.95,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )

            # Profile management (admin-only later in handler)
            m = CREATE_PROFILES_RE.search(text_wo)
            if m:
                return IntentEvent(
                    type="profiles_create", confidence=0.99,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                    cat_name=None, station=None,
                    dates=None, paired_messages=None
                ).__class__(**{**locals()["return"].__dict__, "station": None, "cat_name": None})

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





            # Feeding inquiry: allow with or without prefix; prefer prefix path
            if FEEDING_CHECK_RE.search(text_wo) or FEEDING_CHECK_RE.search(text):
                return IntentEvent(
                    type="feeding_status", confidence=0.95,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )
            if SHOW_PAT.search(text_wo):
                cat = self._extract_best_entity(text_wo, want="cat")
                if cat:
                    return IntentEvent(
                        type="show_photo", confidence=1.0,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                        cat_name=cat, station=cat
                    )
                # no cat? low confidence; ignore
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"],
                                   message_id=row["message_id"], text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"])

            if WHO_PAT.search(text_wo):
                cat = self._extract_best_entity(text_wo, want="cat")
                if cat:
                    return IntentEvent(
                        type="who_is", confidence=1.0,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                        cat_name=cat, station=cat
                    )
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"],
                                   message_id=row["message_id"], text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"])

            if IDENT_PAT.search(text_wo):
                # cv identify needs an image; if absent, look back for last image by the same user in this channel
                if has_image:
                    return IntentEvent(
                        type="cv_identify", confidence=1.0,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=True, attachment_ids=row["attachment_ids"]
                    )
                # look back in ring buffer only now
                pm = self._last_image_for_user(row["channel_id"], row["user_id"], within_minutes=10)
                if pm:
                    return IntentEvent(
                        type="cv_identify", confidence=0.95,
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=True, attachment_ids=pm.get("attachment_ids", []),
                        paired_messages=[pm["message_id"]]
                    )
                # ask for image only if not in silent mode; otherwise log
                return IntentEvent(type="none", confidence=0.0, channel_id=row["channel_id"], user_id=row["user_id"],
                                   message_id=row["message_id"], text=row["text"], has_image=False, attachment_ids=[])

        # 2) Feeding updates (high traffic). Rules first.
        # Case A: “mike fed” (station + verb) → immediate feed_update
        if FEED_VERB.search(text):
            station = self._extract_best_entity(text, want="station")
            if station:
                dates = self._extract_dates(text)
                if not dates:
                    dates = [self._today()]
                return IntentEvent(
                    type="feed_update", confidence=0.95,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                    station=station, dates=dates
                )

        # Case B: only station name, use image context if needed
        station_only = self._extract_best_entity(text, want="station")
        if station_only:
            # If they included "fed" above we already returned. This is the “mike” alone case.
            # If there’s an image now, accept. Else, look back for last image.
            if has_image:
                return IntentEvent(
                    type="feed_update", confidence=0.9,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=True, attachment_ids=row["attachment_ids"],
                    station=station_only, dates=[self._today()]
                )
            pm = self._last_image_for_user(row["channel_id"], row["user_id"], within_minutes=10)
            if pm:
                return IntentEvent(
                    type="feed_update", confidence=0.85,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=True, attachment_ids=pm.get("attachment_ids", []),
                    station=station_only, dates=[self._today()],
                    paired_messages=[pm["message_id"]]
                )
            # Low-confidence: we could ask “Did you mean mark Microwave fed today?” via buttons.
            # We'll trigger a clarification in dispatch if not silent.
            return IntentEvent(
                type="feed_update", confidence=0.72,
                channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                text=row["text"], has_image=False, attachment_ids=[],
                station=station_only, dates=[self._today()]
            )

        # 3) Sub requests / accepts
        if SUB_VERB.search(text):
            stations = self._extract_all_entities(text, want="station")
            dates = self._extract_dates(text)
            conf = 0.9 if stations and dates else 0.75
            return IntentEvent(
                type="sub_request", confidence=conf,
                channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                station=stations[0] if stations else None, dates=dates or None
            )

        if ACCEPT_PAT.search(text):
            # Acknowledge only if replying to a sub_request or if the immediately previous sub_request exists in buffer
            ref_id = row.get("reply_to_id")
            if ref_id:
                return IntentEvent(
                    type="sub_accept", confidence=0.9,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )
            # else try a quick look-back for last sub_request in channel (not just same user)
            if self._recent_sub_request_in_channel(row["channel_id"]):
                return IntentEvent(
                    type="sub_accept", confidence=0.8,
                    channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                    text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"]
                )

        # 4) If needed, run NLP fallback (intent + station scorer)
        if self._nlp and len(text) >= 3:
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
                if nlp_intent == "sub_request":
                    dates = self._extract_dates(text) or None
                    return IntentEvent(
                        type="sub_request", confidence=max(nlp_prob, 0.8),
                        channel_id=row["channel_id"], user_id=row["user_id"], message_id=row["message_id"],
                        text=row["text"], has_image=has_image, attachment_ids=row["attachment_ids"],
                        station=station, dates=dates
                    )

        # Default: none
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
            await handle_cv_identify(_intent("cv_identify", {}), {**ctx, "message": message})
            return

        if event.type == "sub_request":
            await feeding.handle_sub_request_event(event, ctx)
            return

        if event.type == "sub_accept":
            await feeding.handle_sub_accept_event(event, ctx)
            return

        if event.type == "feed_update":
            if event.confidence < CONF_MID and event.station:
                await self._maybe_clarify_feed(event, message)
            elif event.station:
                await feeding.handle_feed_update_event(event, ctx)
            return
        
        if event.type == "feeding_status":
            await feeding.handle_feeding_inquiry(_intent("feeding_inquiry", {}), ctx)
            return

        if event.type == "silent_mode":
            # Admin-only; no chatter on success, because silent mode rules.
            author = message.author
            perms = getattr(getattr(author, "guild_permissions", None), "administrator", False)
            m = SILENT_CMD.search(TOMCAT_PREFIX.sub("", event.text, count=1))
        on_str = m.group(1).lower() if m else ""
        on = (on_str == "on")  # <- always a bool now

        if event.type == "feeding_status":
            await feeding.handle_feeding_inquiry(_intent("feeding_inquiry", {}), ctx)
            return

        if event.type == "cv_identify":
            await handle_cv_identify(_intent("cv_identify", {}), ctx)
            return

        if event.type == "profiles_create":
            # extract ids from the regex now
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


        if perms:
            settings.silent_mode = on
            log_action("silent_mode", f"by={author.id}", "on" if on else "off")
        else:
            log_action("silent_mode_denied", f"by={author.id}", "not_admin")
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
        names: List[str] = []
        # gather alias hits
        for nm in (alias_vocab()[f"{want}s"]):
            if re.search(rf"\b{re.escape(nm.lower())}\b", text.lower()):
                names.append(nm)
        # unique, preserve order
        seen = set()
        out = []
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

        if "yesterday" in text or "last night" in text:
            out.append(today - timedelta(days=1))

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

def _intent(name: str, data: Dict[str, Any]) -> Intent:
    return Intent(name, data)
