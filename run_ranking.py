#!/usr/bin/env python3
"""Orquestrador principal do Ranking Unificado BR (Company of Heroes 3 — Steam -> Discord)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from arranged_teams import (
    DEFAULT_BOOTSTRAP_DAYS,
    DEFAULT_INACTIVE_DAYS,
    Coh3StatsOpenDataClient,
    build_side_teams_from_matches,
    compute_unified_2v2_arranged_teams,
    export_arranged_teams_files,
    sync_brazilian_2v2_arranged_matches,
)
from avatar_cache import RankingCacheDB
from discord_ranking import (
    DiscordRankingClient,
    render_arranged_teams_attachments,
    render_ranking_attachments,
)
from relic_ranking import (
    RelicRankingClient,
    compute_merged_mode_rankings,
    export_merged_top_rankings,
    export_players_files,
    fetch_custom_players_stats,
    merge_catalog_with_custom_players,
)
from roster import (
    CORE_BR_ROSTER,
    count_unique_roster_players,
    ensure_core_roster_in_config,
    extract_steam_id,
    parse_profile_id_input,
    parse_steam_id_input,
)
from security import atomic_write_text, redact_secrets, validate_discord_webhook_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("coh3_ranking.main")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
SAFE_WEBHOOK_PLACEHOLDER = "COLE_AQUI_VIA_ENV_DISCORD_RANKING_WEBHOOK_URL"
CURRENT_PANEL_VERSION = "v2_0_veteranos_unificado_br"
CURRENT_ARRANGED_PANEL_VERSION = "v1_0_arranged_2v2_br"


def load_config(config_path: Path) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "discord_webhook_url": SAFE_WEBHOOK_PLACEHOLDER,
        "bot_username": "CoH3 Brasil • Ranking Unificado",
        "top_n_per_mode": 20,
        "arranged_teams_inactive_days": DEFAULT_INACTIVE_DAYS,
        "arranged_teams_bootstrap_days": DEFAULT_BOOTSTRAP_DAYS,
        "cache_db_path": "./ranking_cache.db",
        "custom_players": [],
    }
    if config_path.is_file():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cfg.update(loaded)

    ensure_core_roster_in_config(cfg)

    env_webhook = os.environ.get("DISCORD_RANKING_WEBHOOK_URL", "").strip()
    if env_webhook:
        cfg["discord_webhook_url"] = env_webhook
    elif not validate_discord_webhook_url(str(cfg.get("discord_webhook_url") or "")):
        cfg["discord_webhook_url"] = ""
    return cfg


def save_config(config_path: Path, cfg: dict[str, Any]) -> None:
    """Salva config.json garantindo que NUNCA nenhum Webhook real seja gravado em disco."""
    sanitized = dict(cfg)
    sanitized["discord_webhook_url"] = SAFE_WEBHOOK_PLACEHOLDER
    atomic_write_text(config_path, json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n")


def compute_rankings_digest(rankings_by_mode: dict[str, list[dict[str, Any]]]) -> str:
    payload: dict[str, Any] = {}
    for mode in sorted(rankings_by_mode.keys()):
        payload[mode] = [
            (
                int(e.get("profile_id", 0)),
                int(e.get("br_rank", 0)),
                round(float(e.get("avg_elo", 0)), 1),
                int(e.get("wins", 0)),
                int(e.get("losses", 0)),
                str(e.get("alias") or ""),
                bool(e.get("inactive_fallback")),
            )
            for e in rankings_by_mode.get(mode, [])
        ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def compute_arranged_teams_digest(teams: list[dict[str, Any]]) -> str:
    payload = [
        (
            str(t.get("pair_key") or ""),
            int(t.get("br_rank", 0)),
            round(float(t.get("avg_elo", 0)), 1),
            list(t.get("sides") or []),
            int(t.get("wins", 0)),
            int(t.get("losses", 0)),
            int(t.get("streak", 0)),
            int(t.get("last_match_date", 0)),
            [str(p.get("alias") or "") for p in (t.get("players") or [])],
            bool(t.get("has_veterano")),
        )
        for t in teams
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def add_extra_player(config_path: Path, raw_input: str) -> None:
    cfg = load_config(config_path)
    relic = RelicRankingClient()
    sid = parse_steam_id_input(raw_input)
    pid = parse_profile_id_input(raw_input)
    if not sid and not pid:
        raise ValueError(f"Entrada inválida para --add-player: informe SteamID64, profile_id ou link coh3stats.com.")

    fetched = fetch_custom_players_stats(relic, [pid] if pid else [], steam_ids=[sid] if sid else None)
    if not fetched:
        raise RuntimeError(f"Jogador não encontrado na API da Relic para '{raw_input}'.")

    pl = fetched[0]
    new_pid = int(pl["profile_id"])
    new_sid = str(pl.get("steam_id") or sid or "")
    new_alias = str(pl.get("original_alias") or pl.get("alias") or f"Player {new_pid}")

    custom_players = list(cfg.get("custom_players") or [])
    for existing in custom_players:
        if int(existing.get("profile_id", 0)) == new_pid or (new_sid and extract_steam_id(existing.get("steam_id")) == new_sid):
            logger.info("Jogador %s (#%d) já está na lista do Ranking Unificado.", new_alias, new_pid)
            return

    custom_players.append(
        {
            "profile_id": new_pid,
            "steam_id": new_sid,
            "original_alias": new_alias,
            "alias": new_alias,
        }
    )
    cfg["custom_players"] = custom_players
    save_config(config_path, cfg)
    logger.info("Jogador extra adicionado ao Ranking Unificado: %s (#%d | Steam %s).", new_alias, new_pid, new_sid or "N/D")


def remove_extra_player(config_path: Path, raw_input: str) -> None:
    cfg = load_config(config_path)
    sid = parse_steam_id_input(raw_input)
    pid = parse_profile_id_input(raw_input)
    if not sid and not pid:
        raise ValueError("Entrada inválida para --remove-player: informe SteamID64, profile_id ou link coh3stats.com.")

    core_pids = {int(c["profile_id"]) for c in CORE_BR_ROSTER}
    core_sids = {str(c["steam_id"]) for c in CORE_BR_ROSTER}
    if (pid and pid in core_pids) or (sid and sid in core_sids):
        raise ValueError("Jogador faz parte do elenco fixo (CORE_BR_ROSTER em roster.py); remova via PR em roster.py.")

    before = list(cfg.get("custom_players") or [])
    after = [
        cp
        for cp in before
        if not ((pid and int(cp.get("profile_id", 0)) == pid) or (sid and extract_steam_id(cp.get("steam_id")) == sid))
    ]
    if len(after) == len(before):
        logger.warning("Nenhum jogador extra encontrado para remoção (%s).", raw_input)
        return
    cfg["custom_players"] = after
    save_config(config_path, cfg)
    logger.info("Jogador extra removido com sucesso (%s).", raw_input)


def run_ranking_sync(
    config_path: Path,
    force: bool = False,
    recreate_message: bool = False,
    dry_run: bool = False,
    verify_discord: bool = False,
    preview_dir: Path | None = None,
) -> int:
    cfg = load_config(config_path)
    save_config(config_path, cfg)

    top_n = max(10, min(50, int(cfg.get("top_n_per_mode", 20))))
    inactive_days = max(1, min(365, int(cfg.get("arranged_teams_inactive_days", DEFAULT_INACTIVE_DAYS))))
    bootstrap_days = max(inactive_days, min(180, int(cfg.get("arranged_teams_bootstrap_days", DEFAULT_BOOTSTRAP_DAYS))))
    custom_players: list[dict[str, Any]] = list(cfg.get("custom_players") or [])
    unique_cnt = count_unique_roster_players(custom_players)
    db_path = (config_path.parent / str(cfg.get("cache_db_path", "./ranking_cache.db"))).resolve()
    data_dir = config_path.parent

    logger.info(
        "Iniciando varredura do Ranking Unificado BR (Veteranos: %d jogadores únicos / %d contas Steam)...",
        unique_cnt,
        len(custom_players),
    )

    relic = RelicRankingClient()
    merged_players = merge_catalog_with_custom_players([], custom_players, relic)
    merged_players.sort(
        key=lambda x: (
            -int(x.get("max_elo", 0)),
            -int(x.get("total_wins", 0)),
            str(x.get("alias") or "").lower(),
        )
    )
    export_players_files(
        merged_players,
        data_dir / "jogadores_br_steam.json",
        data_dir / "jogadores_br_steam.csv",
    )

    custom_ids = {int(cp["profile_id"]) for cp in custom_players if int(cp.get("profile_id", 0)) > 0}
    custom_sids = {extract_steam_id(cp.get("steam_id")) for cp in custom_players if extract_steam_id(cp.get("steam_id"))}

    full_rankings = compute_merged_mode_rankings(
        merged_players,
        custom_ids=custom_ids,
        custom_steam_ids=custom_sids,
        only_custom=True,
    )
    exported = export_merged_top_rankings(
        full_rankings,
        top_n=top_n,
        json_path=data_dir / f"top{top_n}_br_por_modo.json",
        csv_path=data_dir / f"top{top_n}_br_por_modo.csv",
        custom_players=custom_players,
        only_custom=True,
    )
    save_config(config_path, cfg)

    for mode, rows in exported.items():
        active_cnt = sum(1 for r in rows if not r.get("inactive_fallback"))
        inactive_cnt = len(rows) - active_cnt
        logger.info(
            "Modo %-11s: %d veteranos no Ranking Unificado (%d ativos, %d inativos).",
            mode,
            len(rows),
            active_cnt,
            inactive_cnt,
        )

    new_digest = compute_rankings_digest(exported)

    with RankingCacheDB(db_path) as db:
        db.prune_expired_avatars()

        # Coleta incremental de Times Brasileiros 2v2 Arranjados via COH3 Stats Open Data
        arranged_teams: list[dict[str, Any]] | None = None
        try:
            open_data = Coh3StatsOpenDataClient()
            br_match_rows = sync_brazilian_2v2_arranged_matches(
                client=open_data,
                db=db,
                custom_players=custom_players,
                bootstrap_days=bootstrap_days,
            )
            raw_arranged = build_side_teams_from_matches(br_match_rows)
            arranged_teams = compute_unified_2v2_arranged_teams(
                raw_teams=raw_arranged,
                custom_players=custom_players,
                inactive_days=inactive_days,
            )
            export_arranged_teams_files(
                arranged_teams,
                json_path=data_dir / "times_arranjados_2v2_br.json",
                csv_path=data_dir / "times_arranjados_2v2_br.csv",
                inactive_days=inactive_days,
            )
            vet_teams_cnt = sum(1 for t in arranged_teams if t.get("has_veterano"))
            logger.info(
                "Times Arranjados 2v2 BR (ativos <= %dd): %d times (%d com Veteranos).",
                inactive_days,
                len(arranged_teams),
                vet_teams_cnt,
            )
        except Exception as exc:
            logger.warning(
                "Falha ao atualizar Times Arranjados 2v2 (COH3 Stats Open Data); mantendo painel anterior se existir: %s",
                redact_secrets(exc),
            )

        new_arranged_digest = compute_arranged_teams_digest(arranged_teams) if arranged_teams is not None else ""

        prev_digest = db.get_state("ranking_digest") or ""
        prev_msg_id = db.get_state("summary_message_id") or ""
        prev_ver = db.get_state("panel_version") or ""

        prev_arr_digest = db.get_state("arranged_teams_digest") or ""
        prev_arr_msg_id = db.get_state("arranged_teams_message_id") or ""
        prev_arr_ver = db.get_state("arranged_teams_panel_version") or ""

        discord = DiscordRankingClient(
            webhook_url=str(cfg.get("discord_webhook_url") or ""),
            bot_username=str(cfg.get("bot_username") or "CoH3 Brasil • Ranking Unificado"),
            db=db,
            catalog_json_path=data_dir / "jogadores_br_steam.json",
        )

        if preview_dir is not None:
            preview_dir.mkdir(parents=True, exist_ok=True)
            ap = discord.get_avatar_provider(allow_network=not dry_run)
            atts = render_ranking_attachments(
                rankings_by_mode=exported,
                avatar_provider=ap,
                top_n=top_n,
                unique_roster_count=unique_cnt,
            )
            if arranged_teams is not None:
                atts.extend(
                    render_arranged_teams_attachments(
                        teams=arranged_teams,
                        avatar_provider=ap,
                        inactive_days=inactive_days,
                    )
                )
            for fname, png_bytes, _ in atts:
                (preview_dir / fname).write_bytes(png_bytes)
            logger.info("Prévia renderizada com sucesso (%d PNGs) em %s.", len(atts), preview_dir)

        if dry_run:
            logger.info("Modo --dry-run ativo: envio ao Discord ignorado.")
            return 0

        if not discord.is_configured:
            logger.warning(
                "Secret DISCORD_RANKING_WEBHOOK_URL ainda não configurado; dados JSON/CSV atualizados sem postagem no Discord."
            )
            # Ainda valida a renderização em memória para garantir integridade do Pillow e fontes em CI
            ap_off = discord.get_avatar_provider(allow_network=False)
            atts = render_ranking_attachments(
                rankings_by_mode=exported,
                avatar_provider=ap_off,
                top_n=top_n,
                unique_roster_count=unique_cnt,
            )
            if arranged_teams is not None:
                atts.extend(
                    render_arranged_teams_attachments(
                        teams=arranged_teams,
                        avatar_provider=ap_off,
                        inactive_days=inactive_days,
                    )
                )
            logger.info("Validação offline de renderização concluída (%d PNGs prontos).", len(atts))
            return 0

        if verify_discord:
            wh = discord.verify_webhook()
            logger.info("Webhook Discord validado: '%s' (canal #%s).", wh.get("name") or "Webhook", wh.get("channel_id") or "N/D")

        # 1) Painel fixo 1: Ranking Unificado Individual (Veteranos)
        should_publish = force or recreate_message or not prev_msg_id or (new_digest != prev_digest) or (prev_ver != CURRENT_PANEL_VERSION)
        if not should_publish:
            logger.info("Nenhuma alteração no Ranking Unificado desde a última publicação; Painel Fixo mantido (ID %s).", prev_msg_id)
            db.set_state("last_check_ts", str(int(time.time())))
        else:
            new_msg_id = discord.upsert_ranking_panel(
                rankings_by_mode=exported,
                custom_players=custom_players,
                existing_message_id=prev_msg_id,
                top_n=top_n,
                recreate_message=recreate_message,
            )
            if new_msg_id:
                db.set_state("summary_message_id", new_msg_id)
                db.set_state("ranking_digest", new_digest)
                db.set_state("panel_version", CURRENT_PANEL_VERSION)
                db.set_state("last_publish_ts", str(int(time.time())))
                logger.info("Painel Fixo do Ranking Unificado BR atualizado no Discord (ID %s).", new_msg_id)
            else:
                logger.error("Falha ao obter ID da mensagem após envio do Painel Individual ao Discord.")
                return 1

        # 2) Painel fixo 2: Times Brasileiros 2v2 Arranjados (COH3 Stats)
        if arranged_teams is not None:
            should_publish_arr = (
                force
                or recreate_message
                or not prev_arr_msg_id
                or (new_arranged_digest != prev_arr_digest)
                or (prev_arr_ver != CURRENT_ARRANGED_PANEL_VERSION)
            )
            if not should_publish_arr:
                logger.info(
                    "Nenhuma alteração nos Times Arranjados 2v2 desde a última publicação; Painel Fixo 2 mantido (ID %s).",
                    prev_arr_msg_id,
                )
            else:
                new_arr_msg_id = discord.upsert_arranged_teams_panel(
                    teams=arranged_teams,
                    existing_message_id=prev_arr_msg_id,
                    inactive_days=inactive_days,
                    recreate_message=recreate_message,
                )
                if new_arr_msg_id:
                    db.set_state("arranged_teams_message_id", new_arr_msg_id)
                    db.set_state("arranged_teams_digest", new_arranged_digest)
                    db.set_state("arranged_teams_panel_version", CURRENT_ARRANGED_PANEL_VERSION)
                    db.set_state("arranged_teams_publish_ts", str(int(time.time())))
                    logger.info("Painel Fixo 2 (Times Arranjados 2v2 BR) atualizado no Discord (ID %s).", new_arr_msg_id)
                else:
                    logger.warning("Falha ao obter ID da mensagem após envio dos Times Arranjados 2v2 ao Discord.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CoH3 Brasil — Ranking Unificado BR (Discord)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Caminho para config.json")
    parser.add_argument("--force", action="store_true", help="Força atualização do painel mesmo sem mudança de ELO")
    parser.add_argument("--recreate-message", action="store_true", help="Recria a mensagem fixa do zero no Discord")
    parser.add_argument("--dry-run", action="store_true", help="Executa varredura e exportação sem enviar ao Discord")
    parser.add_argument("--verify-discord", action="store_true", help="Valida o Webhook antes de publicar")
    parser.add_argument("--preview-dir", type=Path, default=None, help="Diretório opcional para salvar os PNGs gerados")
    parser.add_argument("--add-player", type=str, default="", help="Adiciona jogador extra (SteamID64 ou URL coh3stats)")
    parser.add_argument("--remove-player", type=str, default="", help="Remove jogador extra (SteamID64 ou URL coh3stats)")
    args = parser.parse_args()

    try:
        if args.add_player.strip():
            add_extra_player(args.config, args.add_player.strip())
        if args.remove_player.strip():
            remove_extra_player(args.config, args.remove_player.strip())
        return run_ranking_sync(
            config_path=args.config,
            force=args.force,
            recreate_message=args.recreate_message,
            dry_run=args.dry_run,
            verify_discord=args.verify_discord,
            preview_dir=args.preview_dir,
        )
    except Exception as exc:
        logger.error("Erro fatal no Ranking Unificado BR: %s", redact_secrets(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
