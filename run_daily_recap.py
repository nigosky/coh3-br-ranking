#!/usr/bin/env python3
"""Orquestrador do Boletim Diário dos Veteranos (06:00 BRT) + 'Jogo do Dia' no Discord."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from arranged_teams import Coh3StatsOpenDataClient
from avatar_cache import RankingCacheDB
from daily_recap import (
    CohdbReplayClient,
    build_daily_recap,
    export_daily_recap_json,
    fetch_global_top_players,
    fetch_open_data_matches_for_window,
    fetch_veteranos_recent_matches_from_relic,
    get_brt_day_window,
    merge_and_filter_day_matches,
)
from discord_ranking import (
    DiscordRankingClient,
    render_daily_recap_attachments,
)
from relic_ranking import RelicRankingClient
from run_ranking import DEFAULT_CONFIG_PATH, load_config
from security import (
    configure_secure_logging,
    redact_secrets,
    resolve_safe_db_path,
    validate_discord_webhook_url,
)

configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("coh3_ranking.daily_main")


def _resolve_daily_webhook(cfg: dict[str, Any]) -> str:
    """Usa DISCORD_DAILY_RECAP_WEBHOOK_URL se configurado; senão faz fallback para DISCORD_RANKING_WEBHOOK_URL."""
    dedicated = os.environ.get("DISCORD_DAILY_RECAP_WEBHOOK_URL", "").strip()
    if dedicated and validate_discord_webhook_url(dedicated):
        return dedicated
    fallback = str(cfg.get("discord_webhook_url") or "").strip()
    return fallback if validate_discord_webhook_url(fallback) else ""


def run_daily_recap_sync(
    config_path: Path,
    target_date: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    verify_discord: bool = False,
    preview_dir: Path | None = None,
    skip_cohdb: bool = False,
) -> int:
    cfg = load_config(config_path)
    custom_players: list[dict[str, Any]] = list(cfg.get("custom_players") or [])
    db_path = resolve_safe_db_path(config_path.parent, str(cfg.get("cache_db_path", "./ranking_cache.db")))
    data_dir = config_path.parent

    day_str, start_ts, end_ts = get_brt_day_window(target_date)
    logger.info("Iniciando Boletim Diário dos Veteranos para %s (BRT)...", day_str)

    relic = RelicRankingClient()
    open_data = Coh3StatsOpenDataClient()

    # 1) Top 20 global (>1800 ELO) das 16 leaderboards oficiais da Relic
    top_global = fetch_global_top_players(relic, top_count=20)
    logger.info("Snapshot Top 20 global (>1800 ELO) carregado (%d jogadores únicos nas 16 filas).", len(top_global))

    # 2) Ingestão híbrida: histórico recente Relic (tempo real) + Open Data (captura maratonas >10 jogos)
    veterano_pids = {int(cp["profile_id"]) for cp in custom_players if int(cp.get("profile_id", 0) or 0) > 0}
    raw_matches = fetch_veteranos_recent_matches_from_relic(relic, custom_players=custom_players)
    open_data_matches = fetch_open_data_matches_for_window(open_data, start_ts, end_ts, veterano_pids)
    raw_matches.extend(open_data_matches)

    day_matches = merge_and_filter_day_matches(
        raw_matches,
        start_ts=start_ts,
        end_ts=end_ts,
        custom_players=custom_players,
    )
    logger.info(
        "Partidas PvP automatch encontradas em %s (BRT): %d partidas únicas envolvendo Veteranos.",
        day_str,
        len(day_matches),
    )

    cohdb_client = None if skip_cohdb else CohdbReplayClient()
    recap = build_daily_recap(
        day_matches=day_matches,
        target_date_str=day_str,
        top_global_by_pid=top_global,
        cohdb_client=cohdb_client,
    )
    export_daily_recap_json(recap, data_dir / "boletim_diario_br.json")

    summary = recap["summary"]
    motd = recap.get("match_of_the_day")
    if motd:
        motd_res = "Vitória BR" if motd.get("vet_won") else "Derrota BR"
        motd_badges = ", ".join(motd.get("badges") or [])
        motd_desc = (
            f"match #{motd['match_id']} ({motd['mode'].upper()} em {motd['map_name']}, "
            f"{motd_res}, tier {motd.get('selection_tier', 0)}/4, score {motd['epicness_score']}/100"
            f"{f', [{motd_badges}]' if motd_badges else ''})"
        )
    else:
        motd_desc = "nenhum"
    logger.info(
        "Boletim %s: %d jogos, %d veteranos ativos (%dV-%dD, saldo %+d ELO). Jogo do Dia: %s.",
        day_str,
        summary["total_unique_matches"],
        summary["active_veteranos"],
        summary["total_wins"],
        summary["total_losses"],
        summary["net_community_elo"],
        motd_desc,
    )

    webhook_url = _resolve_daily_webhook(cfg)

    with RankingCacheDB(db_path) as db:
        discord = DiscordRankingClient(
            webhook_url=webhook_url,
            bot_username=str(cfg.get("bot_username") or "CoH3 Brasil • Boletim Diário"),
            db=db,
            catalog_json_path=data_dir / "jogadores_br_steam.json",
        )

        if preview_dir is not None:
            if preview_dir.is_symlink():
                raise RuntimeError(f"Diretório de prévia não pode ser symlink: {preview_dir}")
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview_root = preview_dir.resolve()
            ap = discord.get_avatar_provider(allow_network=not dry_run)
            atts = render_daily_recap_attachments(recap, avatar_provider=ap)
            for fname, png_bytes, _ in atts:
                safe_name = Path(fname).name
                out_file = preview_root / safe_name
                if out_file.is_symlink() or out_file.resolve().parent != preview_root:
                    raise RuntimeError(f"Destino de prévia bloqueado por segurança: {fname}")
                out_file.write_bytes(png_bytes)
            logger.info("Prévia do Boletim Diário renderizada (%d PNGs) em %s.", len(atts), preview_root)

        if dry_run:
            logger.info("Modo --dry-run ativo: envio ao Discord ignorado.")
            return 0

        if summary["total_unique_matches"] == 0:
            logger.info("Nenhum Veterano jogou partidas PvP ranqueadas em %s (BRT); postagem silenciada.", day_str)
            return 0

        if not discord.is_configured:
            logger.warning("Nenhum Webhook Discord configurado; validando renderização offline do Boletim Diário.")
            ap_off = discord.get_avatar_provider(allow_network=False)
            atts = render_daily_recap_attachments(recap, avatar_provider=ap_off)
            logger.info("Validação offline concluída (%d PNGs prontos).", len(atts))
            return 0

        last_posted_day = db.get_state("last_daily_recap_date") or ""
        if not force and last_posted_day == day_str:
            logger.info("Boletim Diário de %s já foi publicado anteriormente; use --force para reenviar.", day_str)
            return 0

        if verify_discord:
            wh = discord.verify_webhook()
            logger.info("Webhook Discord validado: '%s' (canal #%s).", wh.get("name") or "Webhook", wh.get("channel_id") or "N/D")

        msg_id = discord.post_daily_recap(recap)
        if msg_id:
            db.set_state("last_daily_recap_date", day_str)
            db.set_state("last_daily_recap_message_id", msg_id)
            db.set_state("last_daily_recap_ts", str(int(time.time())))
            logger.info("Boletim Diário de %s publicado no Discord (ID %s).", day_str, msg_id)
            return 0

        logger.error("Falha ao obter ID da mensagem após publicar Boletim Diário no Discord.")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="CoH3 Brasil — Boletim Diário dos Veteranos (06:00 BRT)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Caminho para config.json")
    parser.add_argument("--date", type=str, default=None, help="Data alvo YYYY-MM-DD em BRT (padrão: ontem)")
    parser.add_argument("--force", action="store_true", help="Reenvia o boletim mesmo se a data já tiver sido publicada")
    parser.add_argument("--dry-run", action="store_true", help="Coleta, gera JSON e prévias sem enviar ao Discord")
    parser.add_argument("--verify-discord", action="store_true", help="Valida o Webhook antes de publicar")
    parser.add_argument("--preview-dir", type=Path, default=None, help="Diretório opcional para salvar os PNGs gerados")
    parser.add_argument("--skip-cohdb", action="store_true", help="Ignora consulta opcional de replay/VP no cohdb.com")
    args = parser.parse_args()

    try:
        return run_daily_recap_sync(
            config_path=args.config,
            target_date=args.date,
            force=args.force,
            dry_run=args.dry_run,
            verify_discord=args.verify_discord,
            preview_dir=args.preview_dir,
            skip_cohdb=args.skip_cohdb,
        )
    except Exception as exc:
        logger.error("Erro fatal no Boletim Diário dos Veteranos: %s", redact_secrets(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
