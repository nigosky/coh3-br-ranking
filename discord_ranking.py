"""Cliente endurecido de Webhook do Discord dedicado ao Painel Fixo do Ranking Unificado BR."""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ranking_renderer import SteamAvatarProvider, render_ranking_table_png
from roster import count_unique_roster_players
from security import (
    build_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    strip_unsafe_chars,
    validate_discord_webhook_url,
)

logger = logging.getLogger("coh3_ranking.discord")

COLOR_RANKING = 0xEAB308  # Ouro Ranking Unificado BR
BOT_AVATAR_URL = "https://coh3stats.com/logo/android-icon-192x192.png"
SNOWFLAKE_ID_RE = re.compile(r"^\d{15,22}$")
SAFE_ALLOWED_MENTIONS: dict[str, list[str]] = {"parse": []}
MAX_DISCORD_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MB

_SECURE_OPENER = build_no_redirect_https_opener()


def sanitize_discord_text(raw: Any, max_len: int = 80) -> str:
    txt = strip_unsafe_chars(raw)
    txt = txt.replace("@", "@\u200b").replace("[", "(").replace("]", ")").replace("`", "'")
    return (txt[: max_len - 1] + "…") if len(txt) > max_len else txt


def _encode_multipart_formdata(
    payload: dict[str, Any],
    files: list[tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----CoH3Ranking{secrets.token_hex(12)}"
    chunks: list[bytes] = []

    payload_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    chunks.append(f"--{boundary}\r\n".encode("ascii"))
    chunks.append(b'Content-Disposition: form-data; name="payload_json"\r\n')
    chunks.append(b"Content-Type: application/json; charset=utf-8\r\n\r\n")
    chunks.append(payload_json + b"\r\n")

    for idx, (filename, content_bytes, content_type) in enumerate(files):
        safe_fname = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or f"file_{idx}.png"
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(
            f'Content-Disposition: form-data; name="files[{idx}]"; filename="{safe_fname}"\r\n'.encode("ascii")
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
        chunks.append(content_bytes + b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def render_ranking_attachments(
    rankings_by_mode: dict[str, list[dict[str, Any]]],
    avatar_provider: SteamAvatarProvider,
    top_n: int = 20,
    unique_roster_count: int | None = None,
) -> list[tuple[str, bytes, str]]:
    """Gera as imagens PNG paginadas do Ranking Unificado BR (1v1, 2v2 e Jogos em Equipe)."""
    files: list[tuple[str, bytes, str]] = []
    for m_key, fname1, fname2 in (
        ("1v1", "ranking_1v1.png", "ranking_1v1_p2.png"),
        ("2v2", "ranking_2v2.png", "ranking_2v2_p2.png"),
        ("Team Games", "ranking_team.png", "ranking_team_p2.png"),
    ):
        entries = rankings_by_mode.get(m_key, [])
        if not entries:
            continue
        selected = [e for idx, e in enumerate(entries) if idx < top_n or bool(e.get("is_custom"))]
        if len(selected) <= 25:
            chunk_size = len(selected)
            page1, page2 = selected, []
        else:
            chunk_size = (len(selected) + 1) // 2
            page1, page2 = selected[:chunk_size], selected[chunk_size:]

        sub1 = (
            f"Parte 1/2 • {len(selected)} no Ranking"
            if page2
            else f"Top {top_n} + Monitorados ({len(selected)})"
        )
        files.append(
            (
                fname1,
                render_ranking_table_png(
                    entries=page1,
                    mode_title=m_key,
                    avatar_provider=avatar_provider,
                    max_rows=chunk_size,
                    subtitle_override=sub1,
                    unique_roster_count=unique_roster_count,
                ),
                "image/png",
            )
        )
        if page2:
            files.append(
                (
                    fname2,
                    render_ranking_table_png(
                        entries=page2,
                        mode_title=m_key,
                        avatar_provider=avatar_provider,
                        max_rows=len(page2),
                        subtitle_override=f"Parte 2/2 • Monitorados (+{len(page2)})",
                        unique_roster_count=unique_roster_count,
                    ),
                    "image/png",
                )
            )

    return files[:10]


def build_ranking_panel_payload(
    rankings_by_mode: dict[str, list[dict[str, Any]]],
    custom_players: list[dict[str, Any]],
    attachment_names: list[str],
    bot_username: str = "CoH3 Brasil • Ranking Unificado",
    top_n: int = 20,
) -> dict[str, Any]:
    """Monta o payload do Painel Fixo dedicado exclusivamente ao Ranking Unificado BR."""
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    att_set = set(attachment_names)
    unique_roster_count = count_unique_roster_players(custom_players)

    ranked_pids = {
        int(e["profile_id"])
        for entries in rankings_by_mode.values()
        for e in entries
    }
    unranked_roster = [
        sanitize_discord_text(cp.get("alias") or cp.get("original_alias"), 28)
        for cp in custom_players
        if not cp.get("merged_into_profile_id") and int(cp.get("profile_id", 0)) not in ranked_pids
    ]

    embeds_list: list[dict[str, Any]] = []
    first_embed = True

    for m_key, m_title, fname1, fname2 in (
        ("1v1", "🏆 RANKING UNIFICADO BR — 1v1", "ranking_1v1.png", "ranking_1v1_p2.png"),
        ("2v2", "🏆 RANKING UNIFICADO BR — 2v2", "ranking_2v2.png", "ranking_2v2_p2.png"),
        ("Team Games", "🏆 RANKING UNIFICADO BR — JOGOS EM EQUIPE (3v3 + 4v4)", "ranking_team.png", "ranking_team_p2.png"),
    ):
        if not rankings_by_mode.get(m_key) or fname1 not in att_set:
            continue
        r_embed: dict[str, Any] = {
            "title": m_title,
            "color": COLOR_RANKING,
            "image": {"url": f"attachment://{fname1}"},
        }
        if first_embed:
            first_embed = False
            desc = (
                f"🎯 **Ranking Unificado BR • Top {int(top_n)} + {unique_roster_count} jogadores brasileiros fixos**\n"
                f"🔄 Atualização automática **2x ao dia** (`00:01` e `12:00 BRT`) via API oficial da Relic"
            )
            if unranked_roster:
                desc += "\n⭐ *Aguardando 1ª partida ranqueada:* " + ", ".join(f"**{g}**" for g in unranked_roster[:10])
            r_embed["description"] = desc
        embeds_list.append(r_embed)

        if fname2 in att_set:
            embeds_list.append(
                {
                    "title": f"{m_title} (Continuação • Monitorados)",
                    "color": COLOR_RANKING,
                    "image": {"url": f"attachment://{fname2}"},
                }
            )

    if embeds_list:
        embeds_list[-1]["footer"] = {
            "text": f"CoH3 Brasil • Ranking Unificado (Média ELO ativa + fallback inativos) • {unique_roster_count} monitorados"
        }
        embeds_list[-1]["timestamp"] = now_iso

    return {
        "username": sanitize_discord_text(bot_username, max_len=80),
        "avatar_url": BOT_AVATAR_URL,
        "allowed_mentions": SAFE_ALLOWED_MENTIONS,
        "embeds": embeds_list[:10],
    }


class DiscordRankingClient:
    """Envia e atualiza in-place a mensagem fixa do Ranking Unificado BR no Discord."""

    def __init__(
        self,
        webhook_url: str,
        bot_username: str = "CoH3 Brasil • Ranking Unificado",
        db: Any | None = None,
        catalog_json_path: Path | None = None,
    ) -> None:
        self._webhook_url = (webhook_url or "").strip().rstrip("/")
        self.bot_username = bot_username
        self.db = db
        self.catalog_json_path = catalog_json_path
        self._avatar_provider: SteamAvatarProvider | None = None

    @property
    def is_configured(self) -> bool:
        return validate_discord_webhook_url(self._webhook_url)

    def get_avatar_provider(self, allow_network: bool = True) -> SteamAvatarProvider:
        if self._avatar_provider is None:
            self._avatar_provider = SteamAvatarProvider(
                db=self.db,
                catalog_json_path=self.catalog_json_path,
                allow_network=allow_network,
            )
        return self._avatar_provider

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        files: list[tuple[str, bytes, str]] | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        if not validate_discord_webhook_url(url, allow_message_subpath=True):
            raise ValueError("URL de destino bloqueada pela política estrita do DiscordRankingClient.")

        method_upper = method.upper()
        headers = {"User-Agent": "CoH3-BR-Ranking-Unificado/1.0", "Accept-Encoding": "gzip"}
        if method_upper in ("GET", "DELETE"):
            body = None
        elif files:
            body, content_type = _encode_multipart_formdata(payload or {}, files)
            headers["Content-Type"] = content_type
        else:
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        for attempt in range(3):
            req = urllib.request.Request(url, data=body, method=method_upper, headers=headers)
            try:
                with _SECURE_OPENER.open(req, timeout=25) as resp:
                    raw = read_bounded_http_body(resp, MAX_DISCORD_RESPONSE_BYTES).decode("utf-8")
                    return resp.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry_after = 2.0
                    try:
                        err_json = json.loads(exc.read(4096).decode("utf-8"))
                        retry_after = min(max(float(err_json.get("retry_after", 2.0)), 0.5), 30.0)
                    except Exception:
                        pass
                    logger.warning("Rate limit no Discord Webhook; aguardando %.1fs...", retry_after)
                    time.sleep(retry_after + 0.2)
                    continue
                if exc.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if exc.code == 404 and method_upper in ("PATCH", "DELETE"):
                    return 404, None
                err_body = redact_secrets(exc.read(1024).decode("utf-8", errors="replace"))
                raise RuntimeError(f"Erro HTTP {exc.code} no Discord Webhook: {err_body}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"Falha de rede ao contatar Discord Webhook: {redact_secrets(exc)}") from None

        raise RuntimeError("Falha no envio ao Discord Webhook após múltiplas tentativas.")

    def verify_webhook(self) -> dict[str, str]:
        if not self.is_configured:
            raise RuntimeError("Webhook do Discord (DISCORD_RANKING_WEBHOOK_URL) ausente ou inválido.")
        status, data = self._request("GET", self._webhook_url)
        if status != 200 or not isinstance(data, dict) or not data.get("id"):
            raise RuntimeError(f"Verificação do Discord Webhook falhou (HTTP {status}).")
        return {
            "id": str(data.get("id", "")),
            "channel_id": str(data.get("channel_id", "")),
            "name": strip_unsafe_chars(data.get("name", ""))[:60],
        }

    @staticmethod
    def _append_wait_param(base_url: str) -> str:
        parsed = urllib.parse.urlparse(base_url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        q["wait"] = "true"
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(q)))

    @staticmethod
    def _extract_snowflake(data: dict[str, Any] | None) -> str | None:
        msg_id = str(data["id"]) if data and "id" in data else None
        return msg_id if msg_id and SNOWFLAKE_ID_RE.fullmatch(msg_id) else None

    def upsert_ranking_panel(
        self,
        rankings_by_mode: dict[str, list[dict[str, Any]]],
        custom_players: list[dict[str, Any]],
        existing_message_id: str | None = None,
        top_n: int = 20,
        recreate_message: bool = False,
    ) -> str | None:
        if not self.is_configured:
            return None

        unique_cnt = count_unique_roster_players(custom_players)
        files = render_ranking_attachments(
            rankings_by_mode=rankings_by_mode,
            avatar_provider=self.get_avatar_provider(allow_network=True),
            top_n=top_n,
            unique_roster_count=unique_cnt,
        )
        if not files:
            logger.warning("Nenhuma tabela de ranking gerada; ignorando envio ao Discord.")
            return existing_message_id

        payload = build_ranking_panel_payload(
            rankings_by_mode=rankings_by_mode,
            custom_players=custom_players,
            attachment_names=[f[0] for f in files],
            bot_username=self.bot_username,
            top_n=top_n,
        )

        clean_msg_id = (existing_message_id or "").strip()
        if clean_msg_id and SNOWFLAKE_ID_RE.fullmatch(clean_msg_id):
            parsed = urllib.parse.urlparse(self._webhook_url)
            msg_url = urllib.parse.urlunparse(parsed._replace(path=f"{parsed.path.rstrip('/')}/messages/{clean_msg_id}"))
            if recreate_message:
                try:
                    self._request("DELETE", msg_url)
                except Exception as exc:
                    logger.warning("Não foi possível remover painel anterior (%s): %s", clean_msg_id, redact_secrets(exc))
            else:
                status, data = self._request("PATCH", msg_url, payload, files=files)
                if status != 404:
                    patched_id = self._extract_snowflake(data)
                    if patched_id:
                        return patched_id
                logger.info("Painel anterior (%s) não encontrado (404); publicando novo.", clean_msg_id)

        _, data = self._request("POST", self._append_wait_param(self._webhook_url), payload, files=files)
        return self._extract_snowflake(data)
