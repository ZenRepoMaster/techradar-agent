"""Pluggable LLM client.

Provider is selected by ``TECHRADAR_LLM`` (anthropic | groq | gemini | ollama)
or, when unset, by the first credential found in the environment. All
providers implement one call: ``complete(system, prompt) -> str``. The agent
is provider-agnostic; briefs must generate identically (modulo model quality)
on any of them, which is what the assessment's free-tier constraint requires.

Anthropic uses the official SDK (streaming + adaptive thinking); the other
providers are thin raw-HTTP adapters to avoid three extra SDK dependencies.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx

DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-2.0-flash",
    "xai": "grok-3-mini",
    "ollama": "llama3.1",
}


class LLMError(RuntimeError):
    pass


@dataclass
class LLMClient:
    provider: str
    model: str

    @classmethod
    def from_env(cls) -> "LLMClient":
        provider = os.environ.get("TECHRADAR_LLM", "").strip().lower()
        if not provider:
            if os.environ.get("ANTHROPIC_API_KEY"):
                provider = "anthropic"
            elif os.environ.get("GROQ_API_KEY"):
                provider = "groq"
            elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
                provider = "gemini"
            elif os.environ.get("XAI_API_KEY"):
                provider = "xai"
            else:
                provider = "ollama"  # local fallback, no key needed
        if provider not in DEFAULT_MODELS:
            raise LLMError(f"unknown provider {provider!r}; "
                           f"expected one of {sorted(DEFAULT_MODELS)}")
        model = os.environ.get("TECHRADAR_LLM_MODEL", DEFAULT_MODELS[provider])
        return cls(provider=provider, model=model)

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        fn = getattr(self, f"_{self.provider}")
        return fn(system, prompt, max_tokens)

    def complete_json(self, system: str, prompt: str, max_tokens: int = 2048) -> dict | list:
        """Complete and parse a JSON object/array, tolerating code fences.

        A JSON parse failure is most often output truncation at max_tokens, so
        one retry with a doubled budget is attempted before giving up.
        """
        last_exc: Exception | None = None
        for budget in (max_tokens, max_tokens * 2):
            raw = self.complete(system + "\nRespond with valid JSON only.", prompt, budget)
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                text = text.removeprefix("json").strip()
            start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=0)
            end = max(text.rfind("}"), text.rfind("]")) + 1
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError as exc:
                last_exc = exc
        raise LLMError(f"model returned unparseable JSON after retry: {last_exc}") from last_exc

    # -- providers ---------------------------------------------------------

    def _anthropic(self, system: str, prompt: str, max_tokens: int) -> str:
        import anthropic
        client = anthropic.Anthropic()
        with client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
        return "".join(b.text for b in message.content if b.type == "text")

    def _groq(self, system: str, prompt: str, max_tokens: int) -> str:
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise LLMError("GROQ_API_KEY is not set")
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": self.model, "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": prompt}]},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _xai(self, system: str, prompt: str, max_tokens: int) -> str:
        key = os.environ.get("XAI_API_KEY")
        if not key:
            raise LLMError("XAI_API_KEY is not set")
        resp = httpx.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": self.model, "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": prompt}]},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _gemini(self, system: str, prompt: str, max_tokens: int) -> str:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY / GOOGLE_API_KEY is not set")
        resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            params={"key": key},
            json={"system_instruction": {"parts": [{"text": system}]},
                  "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "generationConfig": {"maxOutputTokens": max_tokens}},
            timeout=120,
        )
        resp.raise_for_status()
        candidates = resp.json().get("candidates") or []
        if not candidates:
            raise LLMError(f"gemini returned no candidates: {resp.text[:200]}")
        return "".join(p.get("text", "") for p in
                       candidates[0].get("content", {}).get("parts", []))

    def _ollama(self, system: str, prompt: str, max_tokens: int) -> str:
        base = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        try:
            resp = httpx.post(
                f"{base}/api/chat",
                json={"model": self.model, "stream": False,
                      "options": {"num_predict": max_tokens},
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": prompt}]},
                timeout=300,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(
                f"ollama unreachable at {base} — set ANTHROPIC_API_KEY / GROQ_API_KEY / "
                f"GEMINI_API_KEY or install ollama ({exc})") from exc
        return resp.json()["message"]["content"]
