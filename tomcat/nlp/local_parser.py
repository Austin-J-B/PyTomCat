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
_ALLOWED_COLOR_FAMILIES = {"brown", "gray", "orange", "black_white", "tabby", "white"}
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
    # Truncate verbose column names to save prompt tokens for the small local model.
    trimmed = []
    for c in cols[:32]:
        # Strip parenthetical explanations: "Recently seen? (Automatically checked...)" -> "Recently seen?"
        short = re.sub(r"\s*\(.*\)\s*$", "", c).strip()
        if not short:
            short = c
        # Cap individual column name length
        if len(short) > 40:
            short = short[:37] + "..."
        trimmed.append(short)
    return ", ".join(trimmed)


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

        # CUDA builds of llama-cpp-python need cudart/cublas DLLs at load time.
        # PyTorch bundles these but only in its own lib dir, which isn't on the
        # default DLL search path. Add it before importing llama_cpp.
        try:
            import torch as _torch
            _torch_lib = os.path.join(os.path.dirname(_torch.__file__), "lib")
            if os.path.isdir(_torch_lib):
                os.add_dll_directory(_torch_lib)
        except Exception:
            pass

        try:
            from llama_cpp import Llama  #type: ignore
        except Exception as e:
            log_action("local_llm_disabled", "import", str(e))
            return None

        try:
            llm = Llama(
                model_path=model_path,
                n_ctx=int(getattr(settings, "local_llm_ctx", 2048) or 2048),
                n_gpu_layers=int(getattr(settings, "local_llm_n_gpu_layers", -1)),
                verbose=False,
            )
        except Exception as e:
            log_action("local_llm_disabled", "init", str(e))
            return None

        try:
            from llama_cpp import llama_cpp as _ll
            gpu_ok = bool(_ll.llama_supports_gpu_offload())
        except Exception:
            gpu_ok = False
        gpu_layers = int(getattr(settings, "local_llm_n_gpu_layers", -1))
        log_action("local_llm_loaded", "runtime=llama_cpp",
                   f"{os.path.basename(model_path)}; gpu_offload={'yes' if gpu_ok else 'NO (CPU only)'}; n_gpu_layers={gpu_layers}")
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

    def shutdown(self, *, wait: bool = False) -> None:
        """Release the single-flight executor during process shutdown."""
        with self._submit_lock:
            inflight = self._inflight
            self._inflight = None
        if inflight is not None and not inflight.done():
            try:
                inflight.cancel()
            except Exception:
                pass
        try:
            self._executor.shutdown(wait=wait, cancel_futures=True)
        except Exception:
            pass

    def _parse_sync(self, text: str) -> LocalParseResult:
        csv_columns = _csv_columns_for_prompt()
        # Keep the system prompt compact — a 1.7B model needs short, direct
        # instructions with concrete examples to produce valid JSON.
        # IMPORTANT: Small models copy pipe-separated options literally
        # (e.g. "route": "none|cat_query") instead of picking one.
        # Use examples instead of schema enums.
        system_prompt = (
            "You are a routing parser for TomCat, a cat database bot. "
            "Return one JSON object. No markdown.\n"
            f"CatDatabase columns: {csv_columns}\n\n"
            "EXAMPLES:\n"
            'User: "show me Gizmo" -> {"route":"dispatch_existing","confidence":0.9,"intent":"show_photo","cat_name":"Gizmo","query":{},"reason":"show photo"}\n'
            'User: "who is Patches" -> {"route":"dispatch_existing","confidence":0.9,"intent":"who_is","cat_name":"Patches","query":{},"reason":"cat profile"}\n'
            'User: "how many orange cats" -> {"route":"cat_query","confidence":0.9,"intent":null,"cat_name":null,"query":{"op":"count_by_filters","color_family":"orange"},"reason":"count by color"}\n'
            'User: "which cats are white" -> {"route":"cat_query","confidence":0.9,"intent":null,"cat_name":null,"query":{"op":"list_names_by_filters","color_family":"white"},"reason":"list by color"}\n'
            'User: "cats at the bookstore" -> {"route":"cat_query","confidence":0.9,"intent":null,"cat_name":null,"query":{"op":"list_names_by_filters","location":"bookstore"},"reason":"list by location"}\n'
            'User: "which cats have spots" -> {"route":"cat_query","confidence":0.9,"intent":null,"cat_name":null,"query":{"op":"list_names_by_filters","filters":[{"column":"Physical Description","op":"contains","value":"spots"}]},"reason":"physical trait"}\n'
            'User: "hello there" -> {"route":"none","confidence":0.1,"intent":null,"cat_name":null,"query":{},"reason":"not cat related"}\n\n'
            "RULES:\n"
            "- route is one of: none, dispatch_existing, cat_query\n"
            "- intent is one of: show_photo, who_is, or null\n"
            "- color_family is one of: brown, gray, orange, black_white, tabby, white, or null\n"
            "- op is one of: count_all_cats, count_by_filters, list_names_by_filters\n"
            "- For nuanced filters use: query.filters=[{\"column\":\"exact column name\",\"op\":\"contains\",\"value\":\"search term\"}]\n"
            "- black-and-white or tuxedo -> color_family=black_white. white/cream/snow -> color_family=white\n"
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
