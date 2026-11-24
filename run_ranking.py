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
    MAX_ARRANGED_TEAMS_TOP_N,
    MIN_TOTAL_GAMES_ARRANGED_TEAM,
    Coh3StatsOpenDataClient,
    build_side_teams_from_matches,
    compute_unified_2v2_arranged_teams,
    export_arranged_teams_files,
    sync_brazilian_2v2_arranged_matches,
    sync_recent_brazilian_2v2_matches_from_relic,
)
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
    render_arranged_teams_attachments,
    render_battlegroup_champions_attachments,
    render_ranking_attachments,
)
from relic_ranking import (
    UNIFIED_MIN_GAMES_PER_QUEUE,
    RelicRankingClient,
    compute_unified_skill_ranking,
    export_players_files,
    export_unified_skill_ranking,
    fetch_custom_players_stats,
    merge_catalog_with_custom_players,
)
from roster import (
    CORE_BR_ROSTER,
    EXCLUDED_VETERANO_PIDS,
    count_unique_roster_players,
    ensure_core_roster_in_config,
    extract_steam_id,
    parse_profile_id_input,
    parse_steam_id_input,
    refresh_core_br_roster,
)
from schedule_guard import get_current_ranking_slot
from security import (
    SAFE_WEBHOOK_PLACEHOLDER,
    atomic_write_bytes,
    atomic_write_text,
    configure_secure_logging,
    redact_secrets,
    resolve_safe_db_path,
    strip_unsafe_chars,
    validate_discord_webhook_url,
)

configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("coh3_ranking.main")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
CURRENT_PANEL_VERSION = "v3_2_skill_points_unificado_smurfs_no_merge"
CURRENT_ARRANGED_PANEL_VERSION = "v2_3_arranged_2v2_active_only_top32_smurfs_no_merge"
CURRENT_BG_PANEL_VERSION = "v1_0_battlegroup_champions_28bgs"


def load_config(config_path: Path) -> dict[str, Any]:
    if config_path.is_symlink():
        raise RuntimeError(f"Arquivo de configuração não pode ser symlink: {config_path}")
    cfg: dict[str, Any] = {
        "discord_webhook_url": SAFE_WEBHOOK_PLACEHOLDER,
        "bot_username": "CoH3 Brasil • Ranking Unificado",
        "arranged_teams_inactive_days": DEFAULT_INACTIVE_DAYS,
        "arranged_teams_bootstrap_days": DEFAULT_BOOTSTRAP_DAYS,
        "arranged_teams_min_total_games": MIN_TOTAL_GAMES_ARRANGED_TEAM,
        "arranged_teams_top_n": MAX_ARRANGED_TEAMS_TOP_N,
        "cache_db_path": "./ranking_cache.db",
        "custom_players": [],
    }
    if config_path.is_file():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cfg.update(loaded)
    cfg.pop("top_n_per_mode", None)

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


def compute_rankings_digest(entries: list[dict[str, Any]]) -> str:
    payload = [
        (
            int(e.get("profile_id", 0)),
            int(e.get("br_rank", 0)),
            round(float(e.get("skill_points", 0)), 1),
            str(e.get("anchor_mode") or ""),
            sorted((str(k), int(v)) for k, v in (e.get("mode_max_elos") or {}).items()),
            int(e.get("wins", 0)),
            int(e.get("losses", 0)),
            str(e.get("alias") or ""),
            str(e.get("smurf_of") or ""),
            bool(e.get("inactive_fallback")),
        )
        for e in entries
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
            [(str(p.get("alias") or ""), str(p.get("smurf_of") or "")) for p in (t.get("players") or [])],
            bool(t.get("has_veterano")),
            bool(t.get("inactive_fallback")),
        )
        for t in teams
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def add_extra_player(config_path: Path, raw_input: str) -> None:
    refresh_core_br_roster()
    cfg = load_config(config_path)
    relic = RelicRankingClient()
    sid = parse_steam_id_input(raw_input)
    pid = parse_profile_id_input(raw_input)
    if not sid and not pid:
        raise ValueError("Entrada inválida para --add-player: informe SteamID64, profile_id ou link coh3stats.com.")

    fetched = fetch_custom_players_stats(relic, [pid] if pid else [], steam_ids=[sid] if sid else None)
    if not fetched:
        raise RuntimeError(f"Jogador não encontrado na API da Relic para '{raw_input}'.")

    pl = fetched[0]
    new_pid = int(pl["profile_id"])
    if new_pid in EXCLUDED_VETERANO_PIDS:
        raise ValueError(f"Perfil #{new_pid} pertence a Outros Brasileiros e não deve entrar nos Veteranos.")
    new_sid = extract_steam_id(pl.get("steam_id") or sid or "")
    new_alias = strip_unsafe_chars(pl.get("original_alias") or pl.get("alias") or "")[:40] or f"Player {new_pid}"

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
    refresh_core_br_roster()
    cfg = load_config(config_path)
    sid = parse_steam_id_input(raw_input)
    pid = parse_profile_id_input(raw_input)
    if not sid and not pid:
        raise ValueError("Entrada inválida para --remove-player: informe SteamID64, profile_id ou link coh3stats.com.")

    core_pids = {int(c["profile_id"]) for c in CORE_BR_ROSTER}
    core_sids = {str(c["steam_id"]) for c in CORE_BR_ROSTER}
    if (pid and pid in core_pids) or (sid and sid in core_sids):
        raise ValueError(
            "Jogador faz parte da base compartilhada de Veteranos (players_roster.json em nigosky/coh3-br-lobby-radar); "
            "remova por lá para refletir nos 4 sistemas."
        )

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


def _render_all_panel_attachments(
    *,
    exported: list[dict[str, Any]],
    arranged_teams: list[dict[str, Any]] | None,
    bg_report: dict[str, Any] | None = None,
    avatar_provider: Any,
    unique_cnt: int,
    max_arranged_teams: int,
    inactive_days: int,
    min_arranged_games: int,
) -> list[tuple[str, bytes, str]]:
    atts = render_ranking_attachments(
        entries=exported,
        avatar_provider=avatar_provider,
        unique_roster_count=unique_cnt,
    )
    if arranged_teams is not None:
        atts.extend(
            render_arranged_teams_attachments(
                teams=arranged_teams,
                avatar_provider=avatar_provider,
                max_teams=max_arranged_teams,
                inactive_days=inactive_days,
                min_total_games=min_arranged_games,
            )
        )
    if bg_report is not None:
        atts.extend(
            render_battlegroup_champions_attachments(
                report=bg_report,
                avatar_provider=avatar_provider,
            )
        )
    return atts


def run_ranking_sync(
    config_path: Path,
    force: bool = False,
    recreate_message: bool = False,
    dry_run: bool = False,
    verify_discord: bool = False,
    preview_dir: Path | None = None,
    arranged_scan_seconds: float = 240.0,
    delete_message_ids: list[str] | None = None,
    force_bg_refresh: bool = False,
    skip_battlegroups: bool = False,
) -> int:
    refresh_core_br_roster()
    cfg = load_config(config_path)
    save_config(config_path, cfg)

    min_games = max(1, min(100, int(cfg.get("unified_min_games_per_queue", UNIFIED_MIN_GAMES_PER_QUEUE))))
    inactive_days = max(1, min(365, int(cfg.get("arranged_teams_inactive_days", DEFAULT_INACTIVE_DAYS))))
    bootstrap_days = max(inactive_days, min(2000, int(cfg.get("arranged_teams_bootstrap_days", DEFAULT_BOOTSTRAP_DAYS))))
    min_arranged_games = max(1, min(100, int(cfg.get("arranged_teams_min_total_games", MIN_TOTAL_GAMES_ARRANGED_TEAM))))
    max_arranged_teams = max(1, min(100, int(cfg.get("arranged_teams_top_n", MAX_ARRANGED_TEAMS_TOP_N))))
    scan_budget_sec = max(10.0, min(1500.0, float(arranged_scan_seconds)))
    custom_players: list[dict[str, Any]] = list(cfg.get("custom_players") or [])
    unique_cnt = count_unique_roster_players(custom_players)
    db_path = resolve_safe_db_path(config_path.parent, str(cfg.get("cache_db_path", "./ranking_cache.db")))
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

    unified_ranking = compute_unified_skill_ranking(
        merged_players,
        custom_ids=custom_ids,
        custom_steam_ids=custom_sids,
        only_custom=True,
        min_games=min_games,
    )
    exported = export_unified_skill_ranking(
        unified_ranking,
        json_path=data_dir / "ranking_unificado_br.json",
        csv_path=data_dir / "ranking_unificado_br.csv",
        custom_players=custom_players,
        only_custom=True,
        min_games=min_games,
    )

    active_cnt = sum(1 for r in exported if not r.get("inactive_fallback"))
    inactive_cnt = len(exported) - active_cnt
    logger.info(
        "Ranking Unificado BR (Skill Points, mín. %d jogos/fila): %d veteranos (%d ativos, %d inativos).",
        min_games,
        len(exported),
        active_cnt,
        inactive_cnt,
    )

    new_digest = compute_rankings_digest(exported)

    with RankingCacheDB(db_path) as db:
        db.prune_expired_avatars()

        # Coleta incremental de TODO o histórico (COH3 Stats Open Data) + partidas recentes em tempo real (Relic Live API)
        arranged_teams: list[dict[str, Any]] | None = None
        try:
            open_data_ok = False
            try:
                open_data = Coh3StatsOpenDataClient()
                sync_brazilian_2v2_arranged_matches(
                    client=open_data,
                    db=db,
                    custom_players=custom_players,
                    bootstrap_days=bootstrap_days,
                    max_scan_seconds=scan_budget_sec,
                )
                open_data_ok = True
            except Exception as exc:
                logger.warning(
                    "Falha no COH3 Stats Open Data (seguindo com cache SQLite + Relic Live API): %s",
                    redact_secrets(exc),
                )

            relic_live_ok = False
            try:
                sync_recent_brazilian_2v2_matches_from_relic(
                    relic_client=relic,
                    db=db,
                    custom_players=custom_players,
                    inactive_days=inactive_days,
                )
                relic_live_ok = True
            except Exception as exc:
                logger.warning(
                    "Falha no complemento em tempo real 2v2 da Relic API (seguindo com histórico em cache): %s",
                    redact_secrets(exc),
                )

            br_match_rows = db.load_all_arranged_2v2_matches()
            if not open_data_ok and not relic_live_ok and not br_match_rows:
                raise RuntimeError("Nenhuma fonte de partidas 2v2 BR disponível e cache local vazio.")

            raw_arranged = build_side_teams_from_matches(br_match_rows)
            arranged_teams = compute_unified_2v2_arranged_teams(
                raw_teams=raw_arranged,
                custom_players=custom_players,
                inactive_days=inactive_days,
                min_total_games=min_arranged_games,
                max_teams=max_arranged_teams,
            )
            export_arranged_teams_files(
                arranged_teams,
                json_path=data_dir / "times_arranjados_2v2_br.json",
                csv_path=data_dir / "times_arranjados_2v2_br.csv",
                inactive_days=inactive_days,
                min_total_games=min_arranged_games,
                max_teams=max_arranged_teams,
            )
            vet_teams_cnt = sum(1 for t in arranged_teams if t.get("has_veterano"))
            logger.info(
                "Times Arranjados 2v2 BR (Top %d ativos ≤%dd, histórico completo + Relic Live, mín. %d jogos): %d duplas (%d c/ Veteranos).",
                max_arranged_teams,
                inactive_days,
                min_arranged_games,
                len(arranged_teams),
                vet_teams_cnt,
            )
        except Exception as exc:
            logger.warning(
                "Falha ao atualizar Times Arranjados 2v2; mantendo painel anterior se existir: %s",
                redact_secrets(exc),
            )

        new_arranged_digest = compute_arranged_teams_digest(arranged_teams) if arranged_teams is not None else ""

        # Coleta incremental e ranking dos 28 Battlegroups (Automatch 1v1 a 4v4 + cohdb cache)
        bg_report: dict[str, Any] | None = None
        if not skip_battlegroups:
            try:
                bg_ttl_days = max(1, min(30, int(cfg.get("battlegroup_cache_ttl_days", DEFAULT_BG_CACHE_TTL_DAYS))))
                bg_lookback_days = max(14, min(90, int(cfg.get("battlegroup_lookback_days", DEFAULT_BG_LOOKBACK_DAYS))))
                bg_open_data = Coh3StatsOpenDataClient()
                bg_report = sync_battlegroup_champions(
                    relic_client=relic,
                    open_data_client=bg_open_data,
                    db=db,
                    custom_players=custom_players,
                    cache_ttl_days=bg_ttl_days,
                    lookback_days=bg_lookback_days,
                    max_scan_seconds=min(scan_budget_sec, 180.0),
                    force_refresh=force_bg_refresh,
                )
                export_battlegroup_champions_files(
                    bg_report,
                    json_path=data_dir / "campeoes_battlegroups_br.json",
                    csv_path=data_dir / "campeoes_battlegroups_br.csv",
                )
                tally_log = ", ".join(
                    f"{t['alias']} ({t['titles']})" for t in (bg_report.get("title_tally") or [])[:6]
                )
                logger.info(
                    "Campeões BR por Battlegroup (%d BGs, base %d BRs): %s.",
                    int(bg_report.get("total_battlegroups") or 0),
                    int(bg_report.get("candidates_evaluated") or 0),
                    tally_log or "N/D",
                )
            except Exception as exc:
                logger.warning(
                    "Falha ao atualizar Campeões por Battlegroup; mantendo painel anterior se existir: %s",
                    redact_secrets(exc),
                )

        new_bg_digest = compute_battlegroup_champions_digest(bg_report) if bg_report is not None else ""

        prev_digest = db.get_state("ranking_digest") or ""
        prev_msg_id = db.get_state("summary_message_id") or ""
        prev_ver = db.get_state("panel_version") or ""

        prev_arr_digest = db.get_state("arranged_teams_digest") or ""
        prev_arr_msg_id = db.get_state("arranged_teams_message_id") or ""
        prev_arr_ver = db.get_state("arranged_teams_panel_version") or ""

        prev_bg_digest = db.get_state("bg_champions_digest") or ""
        prev_bg_msg_id = db.get_state("bg_champions_message_id") or ""
        prev_bg_ver = db.get_state("bg_champions_panel_version") or ""

        discord = DiscordRankingClient(
            webhook_url=str(cfg.get("discord_webhook_url") or ""),
            bot_username=str(cfg.get("bot_username") or "CoH3 Brasil • Ranking Unificado"),
            db=db,
            catalog_json_path=data_dir / "jogadores_br_steam.json",
        )

        if preview_dir is not None:
            if preview_dir.is_symlink():
                raise RuntimeError(f"Diretório de prévia não pode ser symlink: {preview_dir}")
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview_root = preview_dir.resolve()
            atts = _render_all_panel_attachments(
                exported=exported,
                arranged_teams=arranged_teams,
                bg_report=bg_report,
                avatar_provider=discord.get_avatar_provider(allow_network=not dry_run),
                unique_cnt=unique_cnt,
                max_arranged_teams=max_arranged_teams,
                inactive_days=inactive_days,
                min_arranged_games=min_arranged_games,
            )
            for fname, png_bytes, _ in atts:
                safe_name = Path(fname).name
                out_file = preview_root / safe_name
                if out_file.is_symlink() or out_file.resolve().parent != preview_root:
                    raise RuntimeError(f"Destino de prévia bloqueado por segurança: {fname}")
                atomic_write_bytes(out_file, png_bytes)
            logger.info("Prévia renderizada com sucesso (%d PNGs) em %s.", len(atts), preview_root)

        if dry_run:
            logger.info("Modo --dry-run ativo: envio ao Discord ignorado.")
            return 0

        if not discord.is_configured:
            logger.warning(
                "Secret DISCORD_RANKING_WEBHOOK_URL ainda não configurado; dados JSON/CSV atualizados sem postagem no Discord."
            )
            # Ainda valida a renderização em memória para garantir integridade do Pillow e fontes em CI
            atts = _render_all_panel_attachments(
                exported=exported,
                arranged_teams=arranged_teams,
                bg_report=bg_report,
                avatar_provider=discord.get_avatar_provider(allow_network=False),
                unique_cnt=unique_cnt,
                max_arranged_teams=max_arranged_teams,
                inactive_days=inactive_days,
                min_arranged_games=min_arranged_games,
            )
            logger.info("Validação offline de renderização concluída (%d PNGs prontos).", len(atts))
            return 0

        if verify_discord:
            wh = discord.verify_webhook()
            logger.info("Webhook Discord validado: '%s' (canal #%s).", wh.get("name") or "Webhook", wh.get("channel_id") or "N/D")

        # 1) Painel 1: Ranking Unificado BR (Skill Points — posta novo e apaga o antigo)
        current_slot_id, _ = get_current_ranking_slot()
        active_msg_id = prev_msg_id
        should_publish = force or recreate_message or not prev_msg_id or (new_digest != prev_digest) or (prev_ver != CURRENT_PANEL_VERSION)
        if not should_publish:
            logger.info("Nenhuma alteração no Ranking Unificado desde a última publicação; Painel mantido (ID %s).", prev_msg_id)
            db.set_state("last_check_ts", str(int(time.time())))
            db.set_state("last_ranking_schedule_slot", current_slot_id)
        else:
            new_msg_id = discord.upsert_ranking_panel(
                entries=exported,
                custom_players=custom_players,
                existing_message_id=prev_msg_id,
                recreate_message=recreate_message,
                replace_existing=True,
            )
            if new_msg_id:
                active_msg_id = new_msg_id
                db.set_state("summary_message_id", new_msg_id)
                db.set_state("ranking_digest", new_digest)
                db.set_state("panel_version", CURRENT_PANEL_VERSION)
                db.set_state("last_publish_ts", str(int(time.time())))
                db.set_state("last_ranking_schedule_slot", current_slot_id)
                logger.info("Painel do Ranking Unificado BR substituído no Discord (novo ID %s).", new_msg_id)
            else:
                logger.error("Falha ao obter ID da mensagem após envio do Painel Individual ao Discord.")
                return 1

        # 2) Painel 2: Times Brasileiros 2v2 Arranjados (posta novo e apaga o antigo; acompanha Painel 1 para manter ordem)
        active_arr_msg_id = prev_arr_msg_id
        should_publish_arr = False
        if arranged_teams is not None:
            should_publish_arr = (
                force
                or recreate_message
                or should_publish
                or not prev_arr_msg_id
                or (new_arranged_digest != prev_arr_digest)
                or (prev_arr_ver != CURRENT_ARRANGED_PANEL_VERSION)
            )
            if not should_publish_arr:
                logger.info(
                    "Nenhuma alteração nos Times Arranjados 2v2 desde a última publicação; Painel 2 mantido (ID %s).",
                    prev_arr_msg_id,
                )
            else:
                new_arr_msg_id = discord.upsert_arranged_teams_panel(
                    teams=arranged_teams,
                    existing_message_id=prev_arr_msg_id,
                    inactive_days=inactive_days,
                    min_total_games=min_arranged_games,
                    max_teams=max_arranged_teams,
                    recreate_message=recreate_message,
                    replace_existing=True,
                )
                if new_arr_msg_id:
                    active_arr_msg_id = new_arr_msg_id
                    db.set_state("arranged_teams_message_id", new_arr_msg_id)
                    db.set_state("arranged_teams_digest", new_arranged_digest)
                    db.set_state("arranged_teams_panel_version", CURRENT_ARRANGED_PANEL_VERSION)
                    db.set_state("arranged_teams_publish_ts", str(int(time.time())))
                    logger.info("Painel 2 (Times Arranjados 2v2 BR) substituído no Discord (novo ID %s).", new_arr_msg_id)
                else:
                    logger.warning("Falha ao obter ID da mensagem após envio dos Times Arranjados 2v2 ao Discord.")

        # 3) Painel 3: Campeões BR por Battlegroup (posta novo e apaga o antigo; acompanha Painéis 1 e 2)
        active_bg_msg_id = prev_bg_msg_id
        if bg_report is not None:
            should_publish_bg = (
                force
                or recreate_message
                or force_bg_refresh
                or should_publish
                or should_publish_arr
                or not prev_bg_msg_id
                or (new_bg_digest != prev_bg_digest)
                or (prev_bg_ver != CURRENT_BG_PANEL_VERSION)
            )
            if not should_publish_bg:
                logger.info(
                    "Nenhuma alteração nos Campeões por Battlegroup desde a última publicação; Painel 3 mantido (ID %s).",
                    prev_bg_msg_id,
                )
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
                    logger.info("Painel 3 (Campeões BR por Battlegroup) substituído no Discord (novo ID %s).", new_bg_msg_id)
                else:
                    logger.warning("Falha ao obter ID da mensagem após envio dos Campeões por Battlegroup ao Discord.")

        keep_ids = {
            mid
            for mid in (
                active_msg_id,
                active_arr_msg_id,
                active_bg_msg_id,
                db.get_state("last_daily_recap_message_id") or "",
            )
            if mid
        }
        discord.cleanup_pending_deletes(
            state_key="pending_delete_ranking_ids",
            extra_ids=delete_message_ids,
            keep_ids=keep_ids,
        )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CoH3 Brasil — Ranking Unificado BR (Discord)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Caminho para config.json")
    parser.add_argument("--force", action="store_true", help="Força atualização do painel mesmo sem mudança de ELO")
    parser.add_argument("--recreate-message", action="store_true", help="Recria a mensagem do zero no Discord")
    parser.add_argument("--dry-run", action="store_true", help="Executa varredura e exportação sem enviar ao Discord")
    parser.add_argument("--verify-discord", action="store_true", help="Valida o Webhook antes de publicar")
    parser.add_argument("--preview-dir", type=Path, default=None, help="Diretório opcional para salvar os PNGs gerados")
    parser.add_argument("--add-player", type=str, default="", help="Adiciona jogador extra (SteamID64 ou URL coh3stats)")
    parser.add_argument("--remove-player", type=str, default="", help="Remove jogador extra (SteamID64 ou URL coh3stats)")
    parser.add_argument(
        "--delete-message-ids",
        type=str,
        default="",
        help="IDs de mensagens antigas do Discord (separados por vírgula) para apagar com segurança",
    )
    parser.add_argument(
        "--arranged-scan-seconds",
        type=float,
        default=240.0,
        help="Orçamento máximo em segundos para backfill histórico de partidas 2v2 no Open Data (10..1500)",
    )
    parser.add_argument(
        "--force-bg-refresh",
        action="store_true",
        help="Força atualização completa do cache cohdb dos 28 Battlegroups ignorando TTL de 7 dias",
    )
    parser.add_argument(
        "--skip-battlegroups",
        action="store_true",
        help="Pula a coleta/publicação do Painel 3 (Campeões por Battlegroup)",
    )
    args = parser.parse_args()

    try:
        if args.add_player.strip():
            add_extra_player(args.config, args.add_player.strip())
        if args.remove_player.strip():
            remove_extra_player(args.config, args.remove_player.strip())
        extra_del_ids = [s.strip() for s in args.delete_message_ids.split(",") if s.strip()]
        return run_ranking_sync(
            config_path=args.config,
            force=args.force,
            recreate_message=args.recreate_message,
            dry_run=args.dry_run,
            verify_discord=args.verify_discord,
            preview_dir=args.preview_dir,
            arranged_scan_seconds=args.arranged_scan_seconds,
            delete_message_ids=extra_del_ids or None,
            force_bg_refresh=args.force_bg_refresh,
            skip_battlegroups=args.skip_battlegroups,
        )
    except Exception as exc:
        logger.error("Erro fatal no Ranking Unificado BR: %s", redact_secrets(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
