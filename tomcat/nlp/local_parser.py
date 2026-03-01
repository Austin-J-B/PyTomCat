"""Local LLM parser for structured fallback routing decisions."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..logger import log_action


_ALLOWED_ROUTES = {"none", "dispatch_existing", "cat_query"}
_ALLOWED_INTENTS = {"show_photo", "who_is"}
_ALLOWED_QUERY_OPS = {"count_all_cats", "count_by_filters", "list_names_by_filters"}
_ALLOWED_COLOR_FAMILIES = {"brown", "gray", "orange", "black_white", "tabby"}
_ALLOWED_RECENT_SCOPES = {"active", "inactive", "all"}
_ALLOWED_QUERY_RESULTS = {"list", "count"}
_ALLOWED_LOGICAL = {"and", "or"}


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


def _coerce_int(
    value: Any,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float):
            parsed = int(value)
        else:
            txt = _clean_text(value)
            if not txt:
                return None
            m = re.search(r"-?\d[\d,]*", txt)
            if not m:
                return None
            parsed = int(m.group(0).replace(",", ""))
    except Exception:
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


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


def _csv_columns_for_prompt() -> str:
    """Best-effort snapshot of currently queryable CatDatabase columns."""
    try:
        from ..services.cat_query import _load_rows  #lazy import to avoid heavy cycles
        header, _rows = _load_rows()
    except Exception:
        return "(unknown)"
    cols = [_clean_text(c) for c in (header or []) if _clean_text(c)]
    if not cols:
        return "(unknown)"
    #Keep prompt small; all needed columns should fit here.
    return ", ".join(cols[:32])


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
        #Single-flight executor: llama.cpp calls are heavy and not reliably safe under concurrent calls.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tomcat_local_llm",
        )
        self._submit_lock = threading.Lock()
        self._inflight: Optional[concurrent.futures.Future] = None

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
        timeout_config = float(getattr(settings, "local_llm_timeout_sec", 4.0) or 4.0)
        timeout_cap = float(getattr(settings, "local_llm_timeout_cap_sec", 1.2) or 1.2)
        timeout_eff = max(0.2, min(timeout_config, timeout_cap if timeout_cap > 0 else timeout_config))
        return LocalLLMParser(
            llm,
            max_tokens=int(getattr(settings, "local_llm_max_tokens", 220) or 220),
            timeout_sec=timeout_eff,
        )

    async def parse(self, text: str) -> LocalParseResult:
        cleaned = _clean_text(text)
        if not cleaned:
            return LocalParseResult()
        with self._submit_lock:
            if self._inflight is not None and not self._inflight.done():
                log_action("local_llm_parse", "status=busy", "skip=1")
                return LocalParseResult(route="none", confidence=0.0, reason="busy")
            self._inflight = self._executor.submit(self._parse_sync, cleaned)
            job = self._inflight
        started = time.perf_counter()
        try:
            out = await asyncio.wait_for(asyncio.wrap_future(job), timeout=self._timeout_sec)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log_action("local_llm_parse", "status=ok", f"ms={elapsed_ms}; route={out.route}; conf={out.confidence:.2f}")
            return out
        except asyncio.TimeoutError:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log_action("local_llm_parse", "status=timeout", f"ms={elapsed_ms}; cap={self._timeout_sec:.2f}")
            return LocalParseResult(route="none", confidence=0.0, reason="timeout")
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log_action("local_llm_parse", "status=error", f"ms={elapsed_ms}; err={e}")
            log_action("local_llm_parse_error", "exception", str(e))
            return LocalParseResult(route="none", confidence=0.0, reason="exception")

    def _parse_sync(self, text: str) -> LocalParseResult:
        csv_columns = _csv_columns_for_prompt()
        system_prompt = (
            "You are a routing parser for TomCat. Return only one JSON object.\n"
            "No markdown, no explanation.\n"
            f"Available CatDatabase columns: {csv_columns}\n"
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
            '    "color_family": "brown|gray|orange|black_white|tabby|null",\n'
            '    "recent_scope": "active|inactive|all|null",\n'
            '    "birth_year": 4-digit-year|null,\n'
            '    "photo_count_min": integer|null,\n'
            '    "photo_count_max": integer|null,\n'
            '    "photo_count_extreme": "max|min|null",\n'
            '    "result": "list|count|null",\n'
            '    "logical": "and|or|null",\n'
            '    "select_column": "string|null",\n'
            '    "limit": integer|null,\n'
            '    "filters": [\n'
            "      {\n"
            '        "column": "exact column name from available columns",\n'
            '        "op": "eq|neq|gt|gte|lt|lte|contains|not_contains|month_eq|year_eq|is_true|is_false|is_empty|is_not_empty",\n'
            '        "value": "any|null",\n'
            '        "value2": "any|null"\n'
            "      }\n"
            "    ]\n"
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
            "- If user asks for recently seen/active cats -> query.recent_scope=active.\n"
            "- If user asks for inactive/not recently seen cats -> query.recent_scope=inactive.\n"
            "- If user explicitly asks to include inactive/all cats -> query.recent_scope=all.\n"
            "- If user asks for cats born in a year, set query.birth_year.\n"
            "- If user asks for photo-count thresholds, set query.photo_count_min/max.\n"
            "- If user asks who has the most/fewest photos, set query.photo_count_extreme=max|min.\n"
            "- Prefer query.filters with exact column names for nuanced asks.\n"
            "- For date phrases like 'in october', use month_eq on a date column.\n"
            "- For 'exactly X' use eq, for 'X or more' use gte.\n"
            "- Never invent a column that is not in Available CatDatabase columns.\n"
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
        recent_scope = _clean_text(query.get("recent_scope")).lower() or None
        if recent_scope not in _ALLOWED_RECENT_SCOPES:
            recent_scope = None
        birth_year = _coerce_int(query.get("birth_year"), minimum=1900, maximum=2100)
        photo_count_min = _coerce_int(query.get("photo_count_min"), minimum=0)
        photo_count_max = _coerce_int(query.get("photo_count_max"), minimum=0)
        photo_count_extreme = _clean_text(
            query.get("photo_count_extreme") or query.get("photo_extreme")
        ).lower() or None
        if photo_count_extreme not in {"max", "min"}:
            photo_count_extreme = None
        if (
            photo_count_min is not None
            and photo_count_max is not None
            and photo_count_min > photo_count_max
        ):
            photo_count_min, photo_count_max = photo_count_max, photo_count_min
        result_mode = _clean_text(query.get("result") or query.get("result_mode")).lower() or None
        if result_mode not in _ALLOWED_QUERY_RESULTS:
            result_mode = None
        logical = _clean_text(query.get("logical") or query.get("join")).lower() or None
        if logical not in _ALLOWED_LOGICAL:
            logical = "and"
        select_column = _clean_text(query.get("select_column")) or None
        limit = _coerce_int(query.get("limit"), minimum=1, maximum=500)
        filters: list[Dict[str, Any]] = []
        filters_raw = query.get("filters")
        if isinstance(filters_raw, list):
            for item in filters_raw:
                if not isinstance(item, dict):
                    continue
                col = _clean_text(item.get("column"))
                op_txt = _clean_text(item.get("op") or item.get("operator")).lower()
                if not col or not op_txt:
                    continue
                filters.append(
                    {
                        "column": col,
                        "op": op_txt,
                        "value": item.get("value"),
                        "value2": item.get("value2"),
                    }
                )

        out_query = {
            "op": op,
            "location": location,
            "tnrd": tnrd,
            "color_family": color_family,
            "recent_scope": recent_scope,
            "birth_year": birth_year,
            "photo_count_min": photo_count_min,
            "photo_count_max": photo_count_max,
            "photo_count_extreme": photo_count_extreme,
            "result": result_mode,
            "logical": logical,
            "select_column": select_column,
            "limit": limit,
            "filters": filters,
        }
        return LocalParseResult(
            route="cat_query",
            confidence=confidence,
            query=out_query,
            reason=reason,
        )
