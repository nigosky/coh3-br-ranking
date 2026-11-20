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

from daily_recap_renderer import (
    render_daily_recap_table_png,
    render_match_of_the_day_png,
)
from ranking_renderer import (
    SteamAvatarProvider,
    render_arranged_teams_table_png,
    render_unified_ranking_table_png,
)
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
    entries: list[dict[str, Any]],
    avatar_provider: SteamAvatarProvider,
    unique_roster_count: int | None = None,
) -> list[tuple[str, bytes, str]]:
    """Gera as imagens PNG paginadas do Ranking Unificado BR em Skill Points (somente Veteranos)."""
    selected = [dict(e) for e in entries if bool(e.get("is_custom"))]
    if not selected:
        return [
            (
                "ranking_unificado_p1.png",
                render_unified_ranking_table_png(
                    entries=[],
                    avatar_provider=avatar_provider,
                    max_rows=1,
                    subtitle_override="0 Veteranos calibrados",
                    unique_roster_count=unique_roster_count,
                ),
                "image/png",
            )
        ]

    for pos, entry in enumerate(selected, start=1):
        entry["br_rank"] = pos

    active_cnt = sum(1 for e in selected if not e.get("inactive_fallback"))
    inactive_cnt = len(selected) - active_cnt
    status_summary = (
        f"{active_cnt} Ativos • {inactive_cnt} Inativos"
        if inactive_cnt > 0
        else f"{active_cnt} Ativos"
    )

    max_per_page = 25
    total_sel = len(selected)
    num_pages = max(1, min(10, (total_sel + max_per_page - 1) // max_per_page))
    chunk_size = (total_sel + num_pages - 1) // num_pages

    files: list[tuple[str, bytes, str]] = []
    for p_idx in range(num_pages):
        page_rows = selected[p_idx * chunk_size : (p_idx + 1) * chunk_size]
        if not page_rows:
            break
        fname = f"ranking_unificado_p{p_idx + 1}.png"
        sub = (
            f"Parte {p_idx + 1}/{num_pages} • {status_summary}"
            if num_pages > 1
            else f"Veteranos • {status_summary}"
        )
        files.append(
            (
                fname,
                render_unified_ranking_table_png(
                    entries=page_rows,
                    avatar_provider=avatar_provider,
                    max_rows=len(page_rows),
                    subtitle_override=sub,
                    unique_roster_count=unique_roster_count,
                ),
                "image/png",
            )
        )

    return files[:10]


def build_ranking_panel_payload(
    entries: list[dict[str, Any]],
    custom_players: list[dict[str, Any]],
    attachment_names: list[str],
    bot_username: str = "CoH3 Brasil • Ranking Unificado",
) -> dict[str, Any]:
    """Monta o payload do Painel Fixo dedicado exclusivamente ao Ranking Unificado BR (Skill Points)."""
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    unique_roster_count = count_unique_roster_players(custom_players)

    ranked_pids = {int(e["profile_id"]) for e in entries if bool(e.get("is_custom"))}
    unranked_roster = [
        sanitize_discord_text(cp.get("alias") or cp.get("original_alias"), 28)
        for cp in custom_players
        if not cp.get("merged_into_profile_id") and int(cp.get("profile_id", 0)) not in ranked_pids
    ]

    embeds_list: list[dict[str, Any]] = []
    base_title = "🏆 RANKING UNIFICADO BR — SKILL POINTS"
    total_pages = len(attachment_names[:10])

    for idx, fname in enumerate(attachment_names[:10]):
        title = base_title if idx == 0 else f"{base_title} (Continuação • Parte {idx + 1}/{total_pages})"
        r_embed: dict[str, Any] = {
            "title": title,
            "color": COLOR_RANKING,
            "image": {"url": f"attachment://{fname}"},
        }
        if idx == 0:
            desc = (
                f"🎯 **Ranking Unificado • {unique_roster_count} Veteranos (Skill Points)**\n"
                f"📐 **Fórmula:** melhor modo ponderado (`1v1 ×2,0` • `2v2 ×1,5` • `3v3/4v4 ×1,0`) + **15% bônus** nos demais modos válidos *(mín. 10 jogos/fila)*\n"
                f"🏅 *Modo âncora destacado em dourado • Jogadores 100% inativos com fundo diferenciado abaixo dos ativos*\n"
                f"🔄 Atualização automática **2x ao dia** (`00:01` e `12:00 BRT`) via API oficial da Relic"
            )
            if unranked_roster:
                escaped_unranked = [g.replace("*", "\\*") for g in unranked_roster[:10]]
                desc += "\n⭐ *Aguardando calibração (mín. 10 jogos em uma fila):* " + ", ".join(
                    f"**{g}**" for g in escaped_unranked
                )
            r_embed["description"] = desc
        embeds_list.append(r_embed)

    if embeds_list:
        active_cnt = sum(1 for e in entries if bool(e.get("is_custom")) and not e.get("inactive_fallback"))
        inactive_cnt = sum(1 for e in entries if bool(e.get("is_custom")) and bool(e.get("inactive_fallback")))
        has_nick_change = any("*" in str(e.get("alias") or "") for e in entries if bool(e.get("is_custom")))
        footer_text = (
            f"CoH3 Brasil • Ranking Unificado Skill Points (1v1/2v2/3v3/4v4) • "
            f"{active_cnt} ativos, {inactive_cnt} inativos"
        )
        if has_nick_change:
            footer_text += " • * = mudou de nick"
        embeds_list[-1]["footer"] = {"text": footer_text}
        embeds_list[-1]["timestamp"] = now_iso

    return {
        "username": sanitize_discord_text(bot_username, max_len=80),
        "avatar_url": BOT_AVATAR_URL,
        "allowed_mentions": SAFE_ALLOWED_MENTIONS,
        "embeds": embeds_list[:10],
    }


def render_arranged_teams_attachments(
    teams: list[dict[str, Any]],
    avatar_provider: SteamAvatarProvider,
    max_teams: int = 60,
    inactive_days: int = 30,
    min_total_games: int = 10,
) -> list[tuple[str, bytes, str]]:
    """Gera as imagens PNG paginadas do Ranking de Times Brasileiros 2v2 Arranjados (COH3 Stats)."""
    if len(teams) <= max_teams:
        selected = list(teams)
    else:
        selected = [t for idx, t in enumerate(teams) if idx < max_teams or bool(t.get("has_veterano"))]

    total_sel = len(selected)
    active_cnt = sum(1 for t in selected if not t.get("inactive_fallback"))
    inactive_vet_cnt = total_sel - active_cnt
    vet_cnt = sum(1 for t in selected if t.get("has_veterano"))
    if inactive_vet_cnt > 0:
        summary_txt = f"{active_cnt} Ativas • {inactive_vet_cnt} Vet. Inativas • {vet_cnt} c/ Vet."
    else:
        summary_txt = f"{active_cnt} Duplas Ativas • {vet_cnt} c/ Veteranos"

    if not selected:
        return [
            (
                "arranged_2v2_p1.png",
                render_arranged_teams_table_png(
                    teams=[],
                    avatar_provider=avatar_provider,
                    max_rows=1,
                    subtitle_override=f"0 Duplas Qualificadas (mín. {int(min_total_games)} jogos)",
                    inactive_days=inactive_days,
                    min_total_games=min_total_games,
                ),
                "image/png",
            )
        ]

    # Paginação equilibrada (até 18 duplas por página; máximo de 10 anexos no Discord)
    max_per_page = 18
    num_pages = max(1, min(10, (total_sel + max_per_page - 1) // max_per_page))
    chunk_size = (total_sel + num_pages - 1) // num_pages

    files: list[tuple[str, bytes, str]] = []
    for p_idx in range(num_pages):
        page_rows = selected[p_idx * chunk_size : (p_idx + 1) * chunk_size]
        if not page_rows:
            break
        fname = f"arranged_2v2_p{p_idx + 1}.png"
        sub = (
            f"Parte {p_idx + 1}/{num_pages} • {summary_txt}"
            if num_pages > 1
            else summary_txt
        )
        files.append(
            (
                fname,
                render_arranged_teams_table_png(
                    teams=page_rows,
                    avatar_provider=avatar_provider,
                    max_rows=len(page_rows),
                    subtitle_override=sub,
                    inactive_days=inactive_days,
                    min_total_games=min_total_games,
                ),
                "image/png",
            )
        )

    return files[:10]


def build_arranged_teams_panel_payload(
    teams: list[dict[str, Any]],
    attachment_names: list[str],
    bot_username: str = "CoH3 Brasil • Ranking Unificado",
    inactive_days: int = 30,
    min_total_games: int = 10,
) -> dict[str, Any]:
    """Monta o payload da segunda mensagem fixa dedicada aos Times Brasileiros 2v2 Arranjados."""
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    active_teams = sum(1 for t in teams if not t.get("inactive_fallback"))
    inactive_vet_teams = len(teams) - active_teams
    vet_active = sum(1 for t in teams if t.get("has_veterano") and not t.get("inactive_fallback"))

    embeds_list: list[dict[str, Any]] = []
    base_title = "🤝 RANKING BR — TIMES ARRANJADOS 2v2"
    total_pages = len(attachment_names)

    for idx, fname in enumerate(attachment_names[:10]):
        title = base_title if idx == 0 else f"{base_title} (Continuação • Parte {idx + 1}/{total_pages})"
        embed: dict[str, Any] = {
            "title": title,
            "color": COLOR_RANKING,
            "image": {"url": f"attachment://{fname}"},
        }
        if idx == 0:
            embed["description"] = (
                f"🎯 **Times Brasileiros 2v2 Arranjados • Histórico Completo (Média ELO Eixo + Aliados)**\n"
                f"🇧🇷 **Ativos (últimos {int(inactive_days)}d)** • ⭐ **Veteranos ativos em dourado** • 🍷 **Duplas 100% Veteranas inativas (>{int(inactive_days)}d) ao fim em destaque vinho**\n"
                f"⚠️ **Aviso:** somente duplas com **+{int(min_total_games)} jogos no geral** (mín. {int(min_total_games)} partidas) são contadas por causa da calibragem de ELO. "
                f"Após **{int(inactive_days)} dias** sem jogar, saem do ranking todas as duplas que não forem **dupla veterana (ambos Veteranos)** • Fonte: **COH3 Stats Open Data**"
            )
        embeds_list.append(embed)

    if embeds_list:
        has_nick_change = any(
            "*" in str(p.get("alias") or "")
            for t in teams
            for p in (t.get("players") or [])
        )
        inact_part = (
            f" + {inactive_vet_teams} Veteranos inativas (>{int(inactive_days)}d)"
            if inactive_vet_teams > 0
            else ""
        )
        footer_text = (
            f"CoH3 Brasil • Times Arranjados 2v2 (Histórico Completo, mín. {int(min_total_games)} jogos) • "
            f"{active_teams} ativas ({vet_active} c/ Veteranos){inact_part}"
        )
        if has_nick_change:
            footer_text += " • * = mudou de nick"
        embeds_list[-1]["footer"] = {"text": footer_text}
        embeds_list[-1]["timestamp"] = now_iso

    return {
        "username": sanitize_discord_text(bot_username, max_len=80),
        "avatar_url": BOT_AVATAR_URL,
        "allowed_mentions": SAFE_ALLOWED_MENTIONS,
        "embeds": embeds_list[:10],
    }


def render_daily_recap_attachments(
    recap: dict[str, Any],
    avatar_provider: SteamAvatarProvider,
    max_per_page: int = 22,
) -> list[tuple[str, bytes, str]]:
    """Gera as imagens PNG do Boletim Diário dos Veteranos + Card do 'Jogo do Dia'."""
    players = list(recap.get("players") or [])
    day_str = str(recap.get("target_date_brt") or "")
    try:
        date_br = datetime.strptime(day_str, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        date_br = day_str

    files: list[tuple[str, bytes, str]] = []
    if not players:
        files.append(
            (
                "boletim_diario_p1.png",
                render_daily_recap_table_png(
                    recap=recap,
                    player_rows=[],
                    avatar_provider=avatar_provider,
                    subtitle_override=f"{date_br} (BRT) • 0 Veteranos em campo",
                    include_kpi_header=True,
                    include_awards_footer=False,
                ),
                "image/png",
            )
        )
    else:
        num_pages = max(1, min(8, (len(players) + max_per_page - 1) // max_per_page))
        chunk_size = (len(players) + num_pages - 1) // num_pages
        for p_idx in range(num_pages):
            chunk = players[p_idx * chunk_size : (p_idx + 1) * chunk_size]
            if not chunk:
                break
            sub = (
                f"Parte {p_idx + 1}/{num_pages} • Resumo de {date_br} (BRT)"
                if num_pages > 1
                else f"Resumo de {date_br} (BRT)"
            )
            files.append(
                (
                    f"boletim_diario_p{p_idx + 1}.png",
                    render_daily_recap_table_png(
                        recap=recap,
                        player_rows=chunk,
                        avatar_provider=avatar_provider,
                        subtitle_override=sub,
                        include_kpi_header=(p_idx == 0),
                        include_awards_footer=(p_idx == num_pages - 1),
                    ),
                    "image/png",
                )
            )

    motd = recap.get("match_of_the_day")
    if isinstance(motd, dict):
        files.append(
            (
                "jogo_do_dia.png",
                render_match_of_the_day_png(
                    match=motd,
                    target_date_brt=day_str,
                    avatar_provider=avatar_provider,
                ),
                "image/png",
            )
        )

    return files[:10]


def build_daily_recap_payload(
    recap: dict[str, Any],
    attachment_names: list[str],
    bot_username: str = "CoH3 Brasil • Boletim Diário",
) -> dict[str, Any]:
    """Monta o payload Discord do Boletim Diário (nova mensagem por dia com links seguros de replay)."""
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    day_str = str(recap.get("target_date_brt") or "")
    try:
        date_br = datetime.strptime(day_str, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        date_br = day_str

    summary = recap.get("summary") or {}
    motd = recap.get("match_of_the_day")
    recap_atts = [f for f in attachment_names if f != "jogo_do_dia.png"]
    has_motd_img = "jogo_do_dia.png" in attachment_names

    embeds_list: list[dict[str, Any]] = []
    for idx, fname in enumerate(recap_atts[:8]):
        title = (
            f"📰 BOLETIM DIÁRIO DOS VETERANOS — {date_br}"
            if idx == 0
            else f"📰 BOLETIM DIÁRIO DOS VETERANOS — {date_br} (Parte {idx + 1}/{len(recap_atts)})"
        )
        emb: dict[str, Any] = {
            "title": title,
            "color": COLOR_RANKING,
            "image": {"url": f"attachment://{fname}"},
        }
        if idx == 0:
            uniq_m = int(summary.get("total_unique_matches", 0))
            act_v = int(summary.get("active_veteranos", 0))
            tw = int(summary.get("total_wins", 0))
            tl = int(summary.get("total_losses", 0))
            owr = int(summary.get("overall_win_rate", 0))
            net_elo = int(summary.get("net_community_elo", 0))
            net_s = f"+{net_elo}" if net_elo > 0 else str(net_elo)
            emb["description"] = (
                f"☀️ **Resumo completo de ontem ({date_br} • BRT)** — todos os jogos PvP ranqueados dos Veteranos\n"
                f"📊 **{uniq_m} partidas** • **{act_v} veteranos em campo** • **{tw}V-{tl}D ({owr}%)** • Saldo coletivo: **`{net_s} ELO`**"
            )
        embeds_list.append(emb)

    if isinstance(motd, dict) and has_motd_img:
        mode_u = sanitize_discord_text(str(motd.get("mode") or "").upper(), 10)
        map_u = sanitize_discord_text(motd.get("map_name") or "", 48)
        score_v = int(motd.get("epicness_score", 0))
        badges_txt = " • ".join(f"**{sanitize_discord_text(b, 32)}**" for b in (motd.get("badges") or [])[:4])
        mid = int(motd.get("match_id", 0) or 0)
        coh3_url = f"https://coh3stats.com/matches/{mid}" if mid > 0 else ""
        cohdb_url = f"https://cohdb.com/matches/{mid}" if (mid > 0 and motd.get("cohdb_verified")) else "https://cohdb.com/matches"

        motd_lines = [f"🏅 **Critérios:** {badges_txt} • Índice Épico: **`{score_v}/100`**"]
        top5_sum = sanitize_discord_text(str(motd.get("unified_top5_summary") or ""), 120)
        if top5_sum:
            motd_lines.append(f"🏆 **Top 5 Unificado:** {top5_sum}")
        if coh3_url:
            motd_lines.append(f"🔎 [Ver estatísticas no COH3 Stats]({coh3_url})  •  🎬 [Replays no cohdb]({cohdb_url})")
        embeds_list.append(
            {
                "title": f"🔥 JOGO DO DIA — {mode_u} em {map_u}",
                "description": "\n".join(motd_lines),
                "color": 0xDC2626,
                "image": {"url": "attachment://jogo_do_dia.png"},
            }
        )

    if embeds_list:
        has_nick_change = any(
            "*" in str(p.get("alias") or "")
            for p in (recap.get("players") or [])
        )
        footer_text = "CoH3 Brasil • Boletim Diário Automático (06:00 BRT) • Relic API + COH3 Stats + cohdb"
        if has_nick_change:
            footer_text += " • * = mudou de nick"
        embeds_list[-1]["footer"] = {"text": footer_text}
        embeds_list[-1]["timestamp"] = now_iso

    return {
        "username": sanitize_discord_text(bot_username, max_len=80),
        "avatar_url": BOT_AVATAR_URL,
        "allowed_mentions": SAFE_ALLOWED_MENTIONS,
        "embeds": embeds_list[:10],
    }


class DiscordRankingClient:
    """Envia e atualiza in-place as mensagens fixas do Ranking Unificado BR no Discord."""

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

    def _upsert_message(
        self,
        payload: dict[str, Any],
        files: list[tuple[str, bytes, str]],
        existing_message_id: str | None = None,
        recreate_message: bool = False,
    ) -> str | None:
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
                # Substitui os anexos antigos pelos novos em vez de acumular (evita erro Discord 30015 > 10 anexos)
                patch_payload = {**payload, "attachments": []}
                status, data = self._request("PATCH", msg_url, patch_payload, files=files)
                if status != 404:
                    patched_id = self._extract_snowflake(data)
                    if patched_id:
                        return patched_id
                logger.info("Painel anterior (%s) não encontrado (404); publicando novo.", clean_msg_id)

        _, data = self._request("POST", self._append_wait_param(self._webhook_url), payload, files=files)
        return self._extract_snowflake(data)

    def upsert_ranking_panel(
        self,
        entries: list[dict[str, Any]],
        custom_players: list[dict[str, Any]],
        existing_message_id: str | None = None,
        recreate_message: bool = False,
    ) -> str | None:
        if not self.is_configured:
            return None

        unique_cnt = count_unique_roster_players(custom_players)
        files = render_ranking_attachments(
            entries=entries,
            avatar_provider=self.get_avatar_provider(allow_network=True),
            unique_roster_count=unique_cnt,
        )
        if not files:
            logger.warning("Nenhuma tabela de ranking gerada; ignorando envio ao Discord.")
            return existing_message_id

        payload = build_ranking_panel_payload(
            entries=entries,
            custom_players=custom_players,
            attachment_names=[f[0] for f in files],
            bot_username=self.bot_username,
        )
        return self._upsert_message(
            payload=payload,
            files=files,
            existing_message_id=existing_message_id,
            recreate_message=recreate_message,
        )

    def upsert_arranged_teams_panel(
        self,
        teams: list[dict[str, Any]],
        existing_message_id: str | None = None,
        inactive_days: int = 30,
        min_total_games: int = 10,
        recreate_message: bool = False,
    ) -> str | None:
        if not self.is_configured:
            return None

        files = render_arranged_teams_attachments(
            teams=teams,
            avatar_provider=self.get_avatar_provider(allow_network=True),
            inactive_days=inactive_days,
            min_total_games=min_total_games,
        )
        if not files:
            return existing_message_id

        payload = build_arranged_teams_panel_payload(
            teams=teams,
            attachment_names=[f[0] for f in files],
            bot_username=self.bot_username,
            inactive_days=inactive_days,
            min_total_games=min_total_games,
        )
        return self._upsert_message(
            payload=payload,
            files=files,
            existing_message_id=existing_message_id,
            recreate_message=recreate_message,
        )

    def post_daily_recap(self, recap: dict[str, Any]) -> str | None:
        """Publica uma nova mensagem no Discord com o Boletim Diário dos Veteranos + Jogo do Dia."""
        if not self.is_configured:
            return None

        files = render_daily_recap_attachments(
            recap=recap,
            avatar_provider=self.get_avatar_provider(allow_network=True),
        )
        if not files:
            return None

        payload = build_daily_recap_payload(
            recap=recap,
            attachment_names=[f[0] for f in files],
            bot_username=self.bot_username,
        )
        _, data = self._request("POST", self._append_wait_param(self._webhook_url), payload, files=files)
        return self._extract_snowflake(data)
