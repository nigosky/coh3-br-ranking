#!/usr/bin/env python3
"""Orquestrador semanal dos Campeões Brasileiros por Battlegroup (CoH3 — todo sábado às 12:00 BRT)."""

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
from battlegroup_champions import (
    DEFAULT_BG_CACHE_TTL_DAYS,
    DEFAULT_BG_LOOKBACK_DAYS,
    compute_battlegroup_champions_digest,
    export_battlegroup_champions_files,
    sync_battlegroup_champions,
)
from discord_ranking import (
    DiscordRankingClient,
    render_battlegroup_champions_attachments,
)
from relic_ranking import RelicRankingClient
from roster import refresh_core_br_roster
from run_ranking import CURRENT_BG_PANEL_VERSION, DEFAULT_CONFIG_PATH, load_config
from schedule_guard import get_expected_battlegroups_slot
from security import (
    atomic_write_bytes,
    configure_secure_logging,
    redact_secrets,
    resolve_safe_db_path,
    validate_discord_webhook_url,
)

configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("coh3_ranking.battlegroups_main")


def _resolve_battlegroups_webhook(cfg: dict[str, Any]) -> str:
    """Usa DISCORD_BATTLEGROUPS_WEBHOOK_URL se configurado; senão faz fallback para DISCORD_RANKING_WEBHOOK_URL."""
    dedicated = os.environ.get("DISCORD_BATTLEGROUPS_WEBHOOK_URL", "").strip()
    if dedicated and validate_discord_webhook_url(dedicated):
        return dedicated
    fallback = str(cfg.get("discord_webhook_url") or "").strip()
    return fallback if validate_discord_webhook_url(fallback) else ""


def run_battlegroups_sync(
    config_path: Path,
    force: bool = False,
    recreate_message: bool = False,
    dry_run: bool = False,
    verify_discord: bool = False,
    preview_dir: Path | None = None,
    force_bg_refresh: bool = False,
    lookback_days: int = DEFAULT_BG_LOOKBACK_DAYS,
    max_scan_seconds: float = 180.0,
    delete_message_ids: list[str] | None = None,
) -> int:
    """Sincroniza os 28 Battlegroups (Automatch), exporta JSON/CSV e publica o Painel 3 no Discord."""
    refresh_core_br_roster()
    cfg = load_config(config_path)
    custom_players: list[dict[str, Any]] = list(cfg.get("custom_players") or [])
    db_path = resolve_safe_db_path(config_path.parent, str(cfg.get("cache_db_path", "./ranking_cache.db")))
    data_dir = config_path.parent
    webhook_url = _resolve_battlegroups_webhook(cfg)

    relic = RelicRankingClient()
    open_data = Coh3StatsOpenDataClient()

    with RankingCacheDB(db_path) as db:
        prev_bg_msg_id = db.get_state("bg_champions_message_id") or ""
        prev_bg_digest = db.get_state("bg_champions_digest") or ""
        prev_bg_ver = db.get_state("bg_champions_panel_version") or ""

        slot_id, window_open, _ = get_expected_battlegroups_slot()
        logger.info("Iniciando sincronização semanal de Campeões BR por Battlegroup (28 BGs, Automatch)...")
        bg_report = sync_battlegroup_champions(
            relic_client=relic,
            open_data_client=open_data,
            db=db,
            custom_players=custom_players,
            cache_ttl_days=DEFAULT_BG_CACHE_TTL_DAYS,
            lookback_days=max(14, int(lookback_days)),
            max_scan_seconds=max(30.0, min(600.0, float(max_scan_seconds))),
            force_refresh=force_bg_refresh,
            slot_id=slot_id,
        )
        export_battlegroup_champions_files(
            bg_report,
            json_path=data_dir / "campeoes_battlegroups_br.json",
            csv_path=data_dir / "campeoes_battlegroups_br.csv",
        )
        new_bg_digest = compute_battlegroup_champions_digest(bg_report)
        tally_str = ", ".join(
            f"{t.get('alias')} ({t.get('titles')})"
            for t in (bg_report.get("title_tally") or [])[:10]
        )
        logger.info(
            "Campeões BR por Battlegroup (%d BGs, base %d BRs): %s.",
            int(bg_report.get("total_battlegroups") or 0),
            int(bg_report.get("candidates_evaluated") or 0),
            tally_str or "N/D",
        )

        discord = DiscordRankingClient(
            webhook_url=webhook_url,
            bot_username=str(cfg.get("bot_username") or "CoH3 Brasil • Ranking Unificado"),
            db=db,
            catalog_json_path=data_dir / "jogadores_br_steam.json",
        )

        if preview_dir is not None:
            if preview_dir.is_symlink():
                raise RuntimeError(f"Diretório de prévia não pode ser symlink: {preview_dir}")
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview_root = preview_dir.resolve()
            atts = render_battlegroup_champions_attachments(
                bg_report,
                avatar_provider=discord.get_avatar_provider(allow_network=not dry_run),
            )
            for fname, png_bytes, _ in atts:
                safe_name = Path(fname).name
                out_file = preview_root / safe_name
                if out_file.is_symlink() or out_file.resolve().parent != preview_root:
                    raise RuntimeError(f"Destino de prévia bloqueado por segurança: {fname}")
                atomic_write_bytes(out_file, png_bytes)
            logger.info("Prévia de Battlegroups renderizada com sucesso (%d PNGs) em %s.", len(atts), preview_root)

        if dry_run:
            logger.info("Modo --dry-run ativo: envio ao Discord ignorado.")
            return 0

        if not discord.is_configured:
            logger.warning(
                "Secret DISCORD_RANKING_WEBHOOK_URL ainda não configurado; dados JSON/CSV atualizados sem postagem no Discord."
            )
            atts = render_battlegroup_champions_attachments(
                bg_report,
                avatar_provider=discord.get_avatar_provider(allow_network=False),
            )
            logger.info("Validação offline de renderização de Battlegroups concluída (%d PNGs prontos).", len(atts))
            return 0

        if verify_discord:
            wh = discord.verify_webhook()
            logger.info("Webhook Discord validado: '%s' (canal #%s).", wh.get("name") or "Webhook", wh.get("channel_id") or "N/D")

        active_bg_msg_id = prev_bg_msg_id
        should_publish = (
            force
            or recreate_message
            or force_bg_refresh
            or not prev_bg_msg_id
            or (new_bg_digest != prev_bg_digest)
            or (prev_bg_ver != CURRENT_BG_PANEL_VERSION)
        )
        if not should_publish:
            logger.info(
                "Nenhuma alteração nos Campeões por Battlegroup desde a última publicação; Painel 3 mantido (ID %s).",
                prev_bg_msg_id,
            )
            if window_open:
                db.set_state("last_bg_schedule_slot", slot_id)
        else:
            new_bg_msg_id = discord.upsert_battlegroup_champions_panel(
                report=bg_report,
                existing_message_id=prev_bg_msg_id,
                recreate_message=recreate_message,
                replace_existing=True,
            )
            if new_bg_msg_id:
                active_bg_msg_id = new_bg_msg_id
                db.set_state("bg_champions_message_id", new_bg_msg_id)
                db.set_state("bg_champions_digest", new_bg_digest)
                db.set_state("bg_champions_panel_version", CURRENT_BG_PANEL_VERSION)
                db.set_state("bg_champions_publish_ts", str(int(time.time())))
                belt_rows = bg_report.get("_belt_rows_to_save") or []
                if belt_rows and hasattr(db, "save_bg_belt_state"):
                    db.save_bg_belt_state(belt_rows)
                if window_open:
                    db.set_state("last_bg_schedule_slot", slot_id)
                logger.info("Painel 3 (Campeões BR por Battlegroup) publicado/substituído no Discord (novo ID %s).", new_bg_msg_id)
            else:
                logger.error("Falha ao obter ID da mensagem após envio dos Campeões por Battlegroup ao Discord.")
                return 1

        keep_ids = {
            mid
            for mid in (
                active_bg_msg_id,
                db.get_state("summary_message_id") or "",
                db.get_state("arranged_teams_message_id") or "",
                db.get_state("last_daily_recap_message_id") or "",
            )
            if mid
        }
        discord.cleanup_pending_deletes(
            state_key="pending_delete_bg_ids",
            extra_ids=delete_message_ids,
            keep_ids=keep_ids,
        )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CoH3 Brasil — Campeões BR por Battlegroup (Sábado 12:00 BRT)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Caminho para config.json")
    parser.add_argument("--force", action="store_true", help="Força postagem no Discord mesmo sem mudança de digest")
    parser.add_argument("--recreate-message", action="store_true", help="Recria a mensagem do zero no Discord")
    parser.add_argument("--dry-run", action="store_true", help="Executa cálculo e exportação sem enviar ao Discord")
    parser.add_argument("--verify-discord", action="store_true", help="Valida o Webhook antes de publicar")
    parser.add_argument("--preview-dir", type=Path, default=None, help="Diretório opcional para salvar os PNGs gerados")
    parser.add_argument(
        "--force-bg-refresh",
        action="store_true",
        help="Força atualização completa do cache cohdb dos 28 Battlegroups ignorando TTL de 7 dias",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_BG_LOOKBACK_DAYS,
        help="Janela de dias do COH3 Stats Open Data para métricas Automatch (padrão: 45)",
    )
    parser.add_argument(
        "--scan-seconds",
        type=float,
        default=180.0,
        help="Orçamento máximo em segundos para varredura pendente do Open Data",
    )
    parser.add_argument(
        "--delete-message-ids",
        type=str,
        default="",
        help="IDs de mensagens antigas do Discord (separados por vírgula) para apagar com segurança",
    )
    args = parser.parse_args()

    try:
        extra_del_ids = [s.strip() for s in args.delete_message_ids.split(",") if s.strip()]
        return run_battlegroups_sync(
            config_path=args.config,
            force=args.force,
            recreate_message=args.recreate_message,
            dry_run=args.dry_run,
            verify_discord=args.verify_discord,
            preview_dir=args.preview_dir,
            force_bg_refresh=args.force_bg_refresh,
            lookback_days=args.lookback_days,
            max_scan_seconds=args.scan_seconds,
            delete_message_ids=extra_del_ids or None,
        )
    except Exception as exc:
        logger.error("Erro fatal nos Campeões BR por Battlegroup: %s", redact_secrets(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
