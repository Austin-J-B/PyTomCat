"""Local LLM parser for structured fallback routing decisions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..logger import log_action


_ALLOWED_ROUTES = {"none", "dispatch_existing", "cat_query"}
_ALLOWED_INTENTS = {"show_photo", "who_is"}
_ALLOWED_QUERY_OPS = {"count_all_cats", "count_by_filters", "list_names_by_filters"}
_ALLOWED_COLOR_FAMILIES = {"brown", "gray", "orange", "black_white", "tabby"}


def _clean_text(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _clamp01(value: Any) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    txt = _clean_text(value).lower()
    if txt in {"true", "yes", "y", "1"}:
        return True
    if txt in {"false", "no", "n", "0"}:
        return False
    return None


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    txt = _clean_text(text)
    if not txt:
        return None
    try:
        data = json.loads(txt)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start = txt.find("{")
    end = txt.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(txt[start : end + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


@dataclass
class LocalParseResult:
    route: str = "none"
    confidence: float = 0.0
    intent: Optional[str] = None
    cat_name: Optional[str] = None
    query: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


class LocalLLMParser:
    """Wrapper around llama.cpp-style local model for strict JSON routing output."""

    def __init__(self, llm: Any, *, max_tokens: int, timeout_sec: float):
        self._llm = llm
        self._max_tokens = max(64, int(max_tokens))
        self._timeout_sec = max(0.5, float(timeout_sec))

    @staticmethod
    def maybe_load(settings: Any) -> Optional["LocalLLMParser"]:
        if not bool(getattr(settings, "local_llm_enabled", False)):
            return None
        runtime = str(getattr(settings, "local_llm_runtime", "llama_cpp") or "").strip().lower()
        if runtime != "llama_cpp":
            log_action("local_llm_disabled", "runtime", f"unsupported={runtime}")
            return None

        model_path = str(getattr(settings, "local_llm_gguf_path", "") or "").strip()
        if not model_path or not os.path.exists(model_path):
            log_action("local_llm_disabled", "model_path", f"missing={model_path or '(empty)'}")
            return None

        try:
            from llama_cpp import Llama  #type: ignore
        except Exception as e:
            log_action("local_llm_disabled", "import", str(e))
            return None

        try:
            llm = Llama(
                model_path=model_path,
                n_ctx=int(getattr(settings, "local_llm_ctx", 2048) or 2048),
                n_gpu_layers=int(getattr(settings, "local_llm_n_gpu_layers", 0) or 0),
                verbose=False,
            )
        except Exception as e:
            log_action("local_llm_disabled", "init", str(e))
            return None

        log_action("local_llm_loaded", "runtime=llama_cpp", os.path.basename(model_path))
        return LocalLLMParser(
            llm,
            max_tokens=int(getattr(settings, "local_llm_max_tokens", 220) or 220),
            timeout_sec=float(getattr(settings, "local_llm_timeout_sec", 4.0) or 4.0),
        )

    async def parse(self, text: str) -> LocalParseResult:
        cleaned = _clean_text(text)
        if not cleaned:
            return LocalParseResult()
        started = time.perf_counter()
        try:
            out = await asyncio.wait_for(asyncio.to_thread(self._parse_sync, cleaned), timeout=self._timeout_sec)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log_action("local_llm_parse", "status=ok", f"ms={elapsed_ms}; route={out.route}; conf={out.confidence:.2f}")
            return out
        except asyncio.TimeoutError:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log_action("local_llm_parse", "status=timeout", f"ms={elapsed_ms}")
            return LocalParseResult(route="none", confidence=0.0, reason="timeout")
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log_action("local_llm_parse", "status=error", f"ms={elapsed_ms}; err={e}")
            log_action("local_llm_parse_error", "exception", str(e))
            return LocalParseResult(route="none", confidence=0.0, reason="exception")

    def _parse_sync(self, text: str) -> LocalParseResult:
        system_prompt = (
            "You are a routing parser for TomCat. Return only one JSON object.\n"
            "No markdown, no explanation.\n"
            "Schema:\n"
            "{\n"
            '  "route": "none|dispatch_existing|cat_query",\n'
            '  "confidence": 0.0,\n'
            '  "intent": "show_photo|who_is|null",\n'
            '  "cat_name": "string|null",\n'
            '  "query": {\n'
            '    "op": "count_all_cats|count_by_filters|list_names_by_filters",\n'
            '    "location": "string|null",\n'
            '    "tnrd": true|false|null,\n'
            '    "color_family": "brown|gray|orange|black_white|tabby|null"\n'
            "  },\n"
            '  "reason": "short string"\n'
            "}\n"
            "Rules:\n"
            "- If user asks for photo/show image of a cat -> route=dispatch_existing intent=show_photo.\n"
            "- If user asks who a cat is -> route=dispatch_existing intent=who_is.\n"
            "- If user asks count/list/filter question about cats/catabase -> route=cat_query.\n"
            "- If user asks for total cat count -> query.op=count_all_cats.\n"
            "- For brown semantics, brown may include tabby/tan/buff (but not explicit orange requests).\n"
            "- Keep orange distinct from brown when user explicitly asks for orange.\n"
            "- Map black-and-white/tuxedo requests to color_family=black_white.\n"
            "- Map tabby/tabbies requests to color_family=tabby.\n"
            "- If unclear or unrelated -> route=none confidence<=0.4.\n"
        )
        user_prompt = f"Message: {text}"

        raw = self._run_model(system_prompt, user_prompt)
        parsed = _extract_json_object(raw)
        if not parsed:
            return LocalParseResult(route="none", confidence=0.0, reason="invalid_json")
        return self._normalize(parsed)

    def _run_model(self, system_prompt: str, user_prompt: str) -> str:
        #Prefer chat completion with JSON mode; fall back to plain generation if unavailable.
        try:
            out = self._llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=self._max_tokens,
                response_format={"type": "json_object"},
            )
            return (
                (out or {})
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        except Exception:
            pass

        prompt = (
            "<|im_start|>system\n"
            f"{system_prompt}\n"
            "<|im_end|>\n"
            "<|im_start|>user\n"
            f"{user_prompt}\n"
            "<|im_end|>\n"
            "<|im_start|>assistant\n"
        )
        out = self._llm(
            prompt,
            temperature=0.0,
            max_tokens=self._max_tokens,
            stop=["<|im_end|>"],
        )
        return (out or {}).get("choices", [{}])[0].get("text", "")

    def _normalize(self, data: Dict[str, Any]) -> LocalParseResult:
        route = _clean_text(data.get("route")).lower()
        if route not in _ALLOWED_ROUTES:
            return LocalParseResult(route="none", confidence=0.0, reason="bad_route")

        confidence = _clamp01(data.get("confidence"))
        reason = _clean_text(data.get("reason"))

        if route == "none":
            return LocalParseResult(route="none", confidence=confidence, reason=reason)

        if route == "dispatch_existing":
            intent = _clean_text(data.get("intent")).lower()
            cat_name = _clean_text(data.get("cat_name"))
            if intent not in _ALLOWED_INTENTS or not cat_name:
                return LocalParseResult(route="none", confidence=0.0, reason="bad_dispatch")
            return LocalParseResult(
                route="dispatch_existing",
                confidence=confidence,
                intent=intent,
                cat_name=cat_name,
                reason=reason,
            )

        query_raw = data.get("query")
        query = query_raw if isinstance(query_raw, dict) else {}
        op = _clean_text(query.get("op")).lower()
        if op not in _ALLOWED_QUERY_OPS:
            op = "list_names_by_filters"

        location = _clean_text(query.get("location")) or None
        tnrd = _coerce_bool(query.get("tnrd"))
        color_family = _clean_text(query.get("color_family")).lower() or None
        if color_family not in _ALLOWED_COLOR_FAMILIES:
            color_family = None

        out_query = {
            "op": op,
            "location": location,
            "tnrd": tnrd,
            "color_family": color_family,
        }
        return LocalParseResult(
            route="cat_query",
            confidence=confidence,
            query=out_query,
            reason=reason,
        )
