"""Ollama HTTP client for structured PT-BR content generation.

The service is conservative: it always returns a *graceful fallback* if Ollama is
unreachable, disabled, or returns malformed output. Callers never have to catch
network errors and we never block the critical path (metadata generation) on the
LLM being available.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class OllamaContent:
    """Structured output returned by the LLM for a single video."""

    title: str
    description: str
    tags: list[str]
    thumbnail_prompt: str
    chapters: list[dict[str, Any]]
    used_fallback: bool = False
    model: str | None = None


class OllamaClient:
    """Thin async/sync wrapper over the `/api/generate` endpoint."""

    def __init__(self, settings: Settings):
        self.settings = settings

    # ---- public ---------------------------------------------------------
    def is_enabled(self) -> bool:
        return bool(self.settings.ollama_enabled and self.settings.ollama_base_url)

    def health(self) -> dict[str, Any]:
        if not self.is_enabled():
            return {"ok": False, "reason": "disabled"}
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self.settings.ollama_base_url.rstrip('/')}/api/tags")
                resp.raise_for_status()
                tags = [m.get("name") for m in resp.json().get("models", []) if m.get("name")]
                return {"ok": True, "models": tags, "configured_model": self.settings.ollama_model}
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("ollama_health_failed", extra={"err": str(exc)})
            return {"ok": False, "reason": str(exc)}

    def generate_video_content(
        self,
        *,
        game_name: str,
        episode_number: int,
        duration_minutes: int | None,
        recorded_at_label: str | None,
        channel_language: str,
        pc_config: str,
        default_description_block: str,
        default_tags: list[str],
        per_game_tags: list[str],
        achievements: list[str],
        transcript_excerpt: str | None,
        thumbnail_hint: str | None,
    ) -> OllamaContent:
        """Ask Ollama for a PT-BR title/description/tags/thumbnail prompt.

        Always returns a populated :class:`OllamaContent`. If Ollama is disabled or
        fails, the returned object has ``used_fallback=True`` and sensible defaults
        so the caller can decide whether to swap in the legacy template output.
        """
        prompt = self._build_prompt(
            game_name=game_name,
            episode_number=episode_number,
            duration_minutes=duration_minutes,
            recorded_at_label=recorded_at_label,
            channel_language=channel_language,
            pc_config=pc_config,
            default_description_block=default_description_block,
            default_tags=default_tags,
            per_game_tags=per_game_tags,
            achievements=achievements,
            transcript_excerpt=transcript_excerpt,
            thumbnail_hint=thumbnail_hint,
        )

        fallback = self._fallback_content(
            game_name=game_name,
            episode_number=episode_number,
            default_description_block=default_description_block,
            default_tags=default_tags,
            per_game_tags=per_game_tags,
            achievements=achievements,
            thumbnail_hint=thumbnail_hint,
        )

        if not self.is_enabled():
            return fallback

        raw = self._call(prompt)
        if not raw:
            return fallback

        parsed = self._parse_json(raw)
        if not parsed:
            logger.warning("ollama_parse_failed", extra={"raw_preview": raw[:200]})
            return fallback

        return OllamaContent(
            title=str(parsed.get("title") or fallback.title).strip()[:100],
            description=str(parsed.get("description") or fallback.description).strip(),
            tags=_dedupe_tags(parsed.get("tags") or fallback.tags),
            thumbnail_prompt=str(parsed.get("thumbnail_prompt") or fallback.thumbnail_prompt).strip(),
            chapters=_normalize_chapters(parsed.get("chapters") or []),
            used_fallback=False,
            model=self.settings.ollama_model,
        )

    # ---- focused per-part generators (faster perceived latency) -------
    def gen_title(self, *, game_name: str, episode_number: int, transcript_excerpt: str | None,
                  achievements: list[str] | None = None) -> str | None:
        if not self.is_enabled():
            return None
        ctx = (transcript_excerpt or "").strip()[:1200]
        ach = ", ".join((achievements or [])[:6])
        prompt = (
            "Voce e um redator de YouTube em portugues do Brasil. "
            f"Crie um titulo curto e clickbait moderado para o episodio {episode_number} de {game_name}. "
            "Maximo 95 caracteres. Sem emojis. Inclua o nome do jogo. "
            f"Achievements recentes: {ach or 'nenhum'}. "
            f"Trecho do video: {ctx or '(sem transcricao)'}.\n"
            'Responda JSON: {"title": "..."}'
        )
        raw = self._call(prompt, temperature=0.5)
        parsed = self._parse_json(raw) if raw else None
        title = (parsed or {}).get("title") if isinstance(parsed, dict) else None
        return str(title).strip()[:100] if title else None

    def gen_tags(self, *, game_name: str, default_tags: list[str], per_game_tags: list[str],
                 transcript_excerpt: str | None) -> list[str] | None:
        if not self.is_enabled():
            return None
        ctx = (transcript_excerpt or "").strip()[:1000]
        prompt = (
            f"Liste 12 tags em portugues do Brasil para um video de gameplay de {game_name}. "
            "Apenas palavras curtas e frases de 1-3 palavras, sem #, sem virgulas dentro da tag. "
            f"Tags base: {', '.join(default_tags + per_game_tags)}. "
            f"Trecho do video: {ctx or '(sem transcricao)'}.\n"
            'Responda JSON: {"tags": ["...","..."]}'
        )
        raw = self._call(prompt, temperature=0.3)
        parsed = self._parse_json(raw) if raw else None
        items = (parsed or {}).get("tags") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            return None
        return _dedupe_tags(items)

    def gen_description(self, *, game_name: str, episode_number: int, transcript_excerpt: str | None,
                        achievements: list[str] | None = None,
                        chapters: list[dict[str, Any]] | None = None,
                        default_block: str = "") -> str | None:
        if not self.is_enabled():
            return None
        ctx = (transcript_excerpt or "").strip()[:2000]
        ach = "; ".join((achievements or [])[:8])
        chap_lines = format_chapters_for_description(chapters or [])
        prompt = (
            "Escreva uma descricao de YouTube em portugues do Brasil. "
            "Tom: descontraido e direto. Sem emojis em excesso. "
            f"Jogo: {game_name}. Episodio: {episode_number}. "
            f"Conquistas relevantes: {ach or 'nenhuma'}. "
            f"Trecho: {ctx or '(sem transcricao)'}. "
            "Inclua um paragrafo curto (3-4 frases). "
            "Depois, deixe linha em branco e SE houver capitulos abaixo, mantenha-os literalmente como estao. "
            f"Capitulos:\n{chap_lines or '(sem capitulos)'}.\n"
            'Responda JSON: {"description": "..."}'
        )
        raw = self._call(prompt, temperature=0.6)
        parsed = self._parse_json(raw) if raw else None
        desc = (parsed or {}).get("description") if isinstance(parsed, dict) else None
        return str(desc).strip() if desc else None

    def gen_thumbnail_prompt(self, *, game_name: str, episode_number: int,
                             hint: str | None) -> str | None:
        if not self.is_enabled():
            return None
        prompt = (
            f"Sugira um prompt curto (max 200 chars) para gerar uma thumbnail de YouTube do "
            f"episodio {episode_number} de {game_name}. Estilo cinematografico, alto contraste. "
            f"Tema sugerido pelo usuario: {hint or 'livre'}. "
            'Responda JSON: {"thumbnail_prompt": "..."}'
        )
        raw = self._call(prompt, temperature=0.6)
        parsed = self._parse_json(raw) if raw else None
        out = (parsed or {}).get("thumbnail_prompt") if isinstance(parsed, dict) else None
        return str(out).strip()[:240] if out else None

    def generate_chapters(
        self,
        *,
        game_name: str,
        episode_number: int,
        duration_seconds: int,
        transcript_segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Generate YouTube-style chapter markers from transcript segments."""
        if not self.is_enabled() or not transcript_segments:
            return []

        compact = [
            {
                "start": round(float(seg.get("start", 0) or 0), 1),
                "text": str(seg.get("text") or "").strip()[:180],
            }
            for seg in transcript_segments[:250]
            if seg.get("text")
        ]

        prompt = (
            "Voce recebe segmentos transcritos (em portugues) de um gameplay e "
            f"precisa criar capitulos no formato YouTube (max 8). Jogo: {game_name}. "
            f"Episodio: {episode_number:02d}. Duracao total (s): {duration_seconds}. "
            "Cada capitulo deve ter 'start_seconds' (int, multiplo de 1s, crescente) e "
            "'title' (curto, pt-BR, sem emojis). O primeiro capitulo DEVE comecar em 0. "
            "Responda APENAS com JSON: {\"chapters\": [{\"start_seconds\": 0, \"title\": \"...\"}, ...]}.\n\n"
            f"Segmentos: {json.dumps(compact, ensure_ascii=False)}"
        )
        raw = self._call(prompt)
        if not raw:
            return []
        data = self._parse_json(raw) or {}
        return _normalize_chapters(data.get("chapters") or [])

    def generate_thumbnail_prompt(self, *, game_name: str, episode_number: int, hint: str | None) -> str:
        if not self.is_enabled():
            return self._default_thumbnail_prompt(game_name, episode_number, hint)

        prompt = (
            "Gere um prompt de imagem (em portugues) para thumbnail de YouTube de gameplay "
            f"do jogo {game_name}, episodio {episode_number:02d}. "
            "Formato 16:9, cinematografico, alto contraste, iluminacao dramatica, "
            "texto grande 'EP {:02d}' e subtitulo curto de alto CTR. ".format(episode_number)
            + (f"Dica do usuario: {hint.strip()}. " if hint else "")
            + "Responda APENAS em JSON: {\"prompt\": \"...\"}."
        )
        raw = self._call(prompt)
        if not raw:
            return self._default_thumbnail_prompt(game_name, episode_number, hint)
        data = self._parse_json(raw) or {}
        value = str(data.get("prompt") or "").strip()
        return value or self._default_thumbnail_prompt(game_name, episode_number, hint)

    # ---- internal -------------------------------------------------------
    def _call(self, prompt: str, *, fmt: str | None = "json", temperature: float = 0.4) -> str | None:
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/generate"
        body: dict[str, Any] = {
            "model": self.settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "top_p": 0.9, "num_ctx": 8192},
        }
        if fmt:
            body["format"] = fmt
        try:
            with httpx.Client(timeout=self.settings.ollama_timeout_seconds) as client:
                resp = client.post(url, json=body)
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "ollama_request_failed",
                extra={"err": str(exc)[:200], "model": self.settings.ollama_model},
            )
            return None

        return str(payload.get("response") or "").strip() or None

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any] | None:
        # Ollama with format=json returns valid JSON; still be defensive.
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _default_thumbnail_prompt(game_name: str, episode_number: int, hint: str | None) -> str:
        base = (
            f"Use esta imagem como base para thumbnail de YouTube do episodio {episode_number:02d} "
            f"de {game_name}. Adicionar texto grande e legivel: EP {episode_number:02d} e "
            "subtitulo curto de alto CTR. Manter estilo cinematografico, alto contraste, "
            "iluminacao dramatica e visual limpo em 16:9."
        )
        if hint:
            return f"{base} Foco sugerido: {hint.strip()}."
        return base

    def _fallback_content(
        self,
        *,
        game_name: str,
        episode_number: int,
        default_description_block: str,
        default_tags: list[str],
        per_game_tags: list[str],
        achievements: list[str],
        thumbnail_hint: str | None,
    ) -> OllamaContent:
        title = f"{game_name} Gameplay PT-BR | Episodio {episode_number:02d}"
        tags = _dedupe_tags(
            [game_name.lower(), "gameplay", "sem comentarios", "pt-br", *default_tags, *per_game_tags]
        )
        achievements_line = (
            "Conquistas desbloqueadas: " + ", ".join(achievements)
            if achievements
            else "Conquistas desbloqueadas: sem novas conquistas registradas"
        )
        description = "\n".join(
            [
                f"Serie: {game_name}",
                f"Episodio: {episode_number:02d}",
                "Formato: gameplay sem comentarios",
                achievements_line,
                "",
                default_description_block,
            ]
        )
        return OllamaContent(
            title=title[:100],
            description=description,
            tags=tags,
            thumbnail_prompt=self._default_thumbnail_prompt(game_name, episode_number, thumbnail_hint),
            chapters=[],
            used_fallback=True,
            model=None,
        )

    def _build_prompt(
        self,
        *,
        game_name: str,
        episode_number: int,
        duration_minutes: int | None,
        recorded_at_label: str | None,
        channel_language: str,
        pc_config: str,
        default_description_block: str,
        default_tags: list[str],
        per_game_tags: list[str],
        achievements: list[str],
        transcript_excerpt: str | None,
        thumbnail_hint: str | None,
    ) -> str:
        context_bits = {
            "jogo": game_name,
            "episodio": episode_number,
            "duracao_minutos": duration_minutes,
            "gravado_em": recorded_at_label,
            "idioma_canal": channel_language,
            "pc": pc_config,
            "tags_padrao": default_tags,
            "tags_do_jogo": per_game_tags,
            "conquistas_desbloqueadas": achievements,
            "bloco_padrao_descricao": default_description_block,
            "dica_thumbnail": thumbnail_hint,
            "trecho_transcricao": (transcript_excerpt or "")[:2400],
        }
        schema = (
            "{\n"
            '  "title": "string (<=100 chars, pt-BR, inclua EP NN)",\n'
            '  "description": "string (<=3500 chars, pt-BR, paragrafos e bullets)",\n'
            '  "tags": ["10 a 15 tags pt-BR, minusculas, sem # e sem aspas"],\n'
            '  "thumbnail_prompt": "string pt-BR, 16:9 cinematografico",\n'
            '  "chapters": [{"start_seconds": 0, "title": "intro curto"}]\n'
            "}"
        )
        return (
            "Voce e um produtor de conteudo do YouTube focado em gameplay pt-BR sem comentarios. "
            "Sua saida DEVE ser apenas JSON valido (sem markdown). "
            "Use sempre titulo terminando com 'Episodio NN' (dois digitos). "
            "Titulos devem ser cativantes mas honestos (sem clickbait enganoso). "
            "Descricao deve ter 3 a 6 paragrafos curtos + bullets com conteudo do episodio "
            "+ bloco final obrigatorio com o 'bloco_padrao_descricao'. "
            "Tags curtas, em pt-BR, sem repetir, sem hashtags.\n\n"
            f"SCHEMA:\n{schema}\n\n"
            f"CONTEXTO:\n{json.dumps(context_bits, ensure_ascii=False, indent=2)}"
        )


# ------ helpers ----------------------------------------------------------
def _dedupe_tags(tags: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags or []:
        value = " ".join(str(raw).split()).strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out[:15]


def _normalize_chapters(items: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for entry in items or []:
        if not isinstance(entry, dict):
            continue
        try:
            start = int(entry.get("start_seconds") or entry.get("start") or 0)
        except (TypeError, ValueError):
            continue
        title = str(entry.get("title") or "").strip()
        if start < 0 or not title:
            continue
        normalized.append({"start_seconds": start, "title": title[:80]})

    normalized.sort(key=lambda c: c["start_seconds"])
    # ensure first chapter at 0
    if normalized and normalized[0]["start_seconds"] != 0:
        normalized[0] = {"start_seconds": 0, "title": normalized[0]["title"]}
    return normalized[:8]


def format_chapters_for_description(chapters: list[dict[str, Any]]) -> str:
    """Render YouTube-style chapter markers."""
    if not chapters:
        return ""

    lines = ["Capitulos:"]
    for chap in chapters:
        try:
            start = int(chap.get("start_seconds", 0))
        except (TypeError, ValueError):
            continue
        title = str(chap.get("title") or "").strip()
        if not title:
            continue
        hh = start // 3600
        mm = (start % 3600) // 60
        ss = start % 60
        stamp = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        lines.append(f"{stamp} {title}")
    return "\n".join(lines)
