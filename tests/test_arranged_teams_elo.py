"""Testes unitários do cálculo de ELO, filtragem 2v2 BR, unificação Eixo+Aliados e cache SQLite."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from arranged_teams import (
    ELO_K_FACTOR,
    INITIAL_TEAM_ELO,
    MAX_CONSECUTIVE_HISTORICAL_404_DAYS,
    build_side_teams_from_matches,
    compute_unified_2v2_arranged_teams,
    export_arranged_teams_files,
    extract_brazilian_2v2_matches_from_dump,
    is_brazilian_2v2_team,
    is_brazilian_player,
    sync_brazilian_2v2_arranged_matches,
    sync_recent_brazilian_2v2_matches_from_relic,
)
from avatar_cache import RankingCacheDB


def _mk_match(
    mid: int,
    ts: int,
    t0_players: list[tuple[int, str, str, str]],
    t0_races: list[int],
    t0_won: bool,
    t1_players: list[tuple[int, str, str, str]],
    t1_races: list[int],
    t0_elo: float = 1200.0,
    t1_elo: float = 1200.0,
    mtype: int = 2,
    platform: str = "steam",
) -> dict:
    reports = []
    for idx, (pid, alias, country, sid) in enumerate(t0_players):
        reports.append(
            {
                "profile_id": pid,
                "resulttype": 1 if t0_won else 0,
                "teamid": 0,
                "race_id": t0_races[idx],
                "profile": {"alias": alias, "country": country, "name": f"/steam/{sid}"},
                "matchhistorymember": {"oldrating": t0_elo, "newrating": t0_elo, "outcome": 1 if t0_won else 0},
            }
        )
    for idx, (pid, alias, country, sid) in enumerate(t1_players):
        reports.append(
            {
                "profile_id": pid,
                "resulttype": 0 if t0_won else 1,
                "teamid": 1,
                "race_id": t1_races[idx],
                "profile": {"alias": alias, "country": country, "name": f"/steam/{sid}"},
                "matchhistorymember": {"oldrating": t1_elo, "newrating": t1_elo, "outcome": 0 if t0_won else 1},
            }
        )
    return {
        "id": mid,
        "matchtype_id": mtype,
        "platform": platform,
        "completiontime": ts,
        "matchhistoryreportresults": reports,
    }


class TestArrangedTeamsEloAndRules(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1795100000
        self.custom_players = [
            {"profile_id": 26910, "steam_id": "76561197963741792", "original_alias": "nigo_BR ᵀᴹ"},
            {"profile_id": 110078, "steam_id": "76561198024839791", "original_alias": "Bineto"},
            {"profile_id": 366826, "steam_id": "76561198014985380", "original_alias": "Lem22"},
        ]
        self.vet_pids = {26910, 110078, 366826}

        self.nigo = (26910, "nigo_BR ᵀᴹ", "br", "76561197963741792")
        self.bineto = (110078, "Bineto", "br", "76561198024839791")
        self.lem = (366826, "lem22", "us", "76561198014985380")
        self.br1 = (9999992, "OutroBR1", "br", "76561198000000012")
        self.br2 = (9999991, "OutroBR2", "br", "76561198000000011")
        self.ger1 = (8888881, "HansDE", "de", "76561198000000091")
        self.ger2 = (8888882, "FritzDE", "de", "76561198000000092")

    def test_brazilian_team_criteria_includes_veterano_with_us_flag_and_excludes_mixed(self) -> None:
        self.assertTrue(is_brazilian_player({"profile_id": 366826, "country": "us"}, self.vet_pids))
        self.assertFalse(is_brazilian_player({"profile_id": 8888881, "country": "de"}, self.vet_pids))
        # Possível estrangeiro usando bandeira BR via VPN (ex.: Nocturne 1142621) é bloqueado até verificação
        self.assertFalse(is_brazilian_player({"profile_id": 1142621, "country": "br"}, self.vet_pids))
        self.assertFalse(
            is_brazilian_2v2_team(
                {"type": "2v2", "players": [{"profile_id": 26910, "country": "br"}, {"profile_id": 1142621, "country": "br"}]},
                self.vet_pids,
            )
        )
        # Brasileiro emigrado confirmado (XAPA KENTE 212034, Canadá) continua incluído
        self.assertTrue(is_brazilian_player({"profile_id": 212034, "country": "ca"}, self.vet_pids))

        # Lem22 (US flag, Veterano) + nigo_BR (BR) -> aceito
        self.assertTrue(
            is_brazilian_2v2_team(
                {"type": "2v2", "players": [{"profile_id": 366826, "country": "us"}, {"profile_id": 26910, "country": "br"}]},
                self.vet_pids,
            )
        )
        # BR + estrangeiro não-Veterano -> rejeitado
        self.assertFalse(
            is_brazilian_2v2_team(
                {"type": "2v2", "players": [{"profile_id": 26910, "country": "br"}, {"profile_id": 8888881, "country": "de"}]},
                self.vet_pids,
            )
        )
        # 3v3 -> rejeitado
        self.assertFalse(
            is_brazilian_2v2_team(
                {
                    "type": "3v3",
                    "players": [
                        {"profile_id": 26910, "country": "br"},
                        {"profile_id": 110078, "country": "br"},
                        {"profile_id": 9999992, "country": "br"},
                    ],
                },
                self.vet_pids,
            )
        )

    def test_elo_math_exact_formula(self) -> None:
        # 3 vitórias contra adversários de 1200, 1600 e 1000
        rows = [
            {
                "match_id": 1, "side": "axis", "pair_key": "26910_110078",
                "p1_id": 26910, "p1_alias": "nigo", "p1_country": "br", "p1_steam_id": "",
                "p2_id": 110078, "p2_alias": "Bineto", "p2_country": "br", "p2_steam_id": "",
                "won": 1, "enemy_elo": 1200.0, "completion_ts": self.now - 300,
            },
            {
                "match_id": 2, "side": "axis", "pair_key": "26910_110078",
                "p1_id": 26910, "p1_alias": "nigo", "p1_country": "br", "p1_steam_id": "",
                "p2_id": 110078, "p2_alias": "Bineto", "p2_country": "br", "p2_steam_id": "",
                "won": 1, "enemy_elo": 1600.0, "completion_ts": self.now - 200,
            },
            {
                "match_id": 3, "side": "axis", "pair_key": "26910_110078",
                "p1_id": 26910, "p1_alias": "nigo", "p1_country": "br", "p1_steam_id": "",
                "p2_id": 110078, "p2_alias": "Bineto", "p2_country": "br", "p2_steam_id": "",
                "won": 0, "enemy_elo": 1000.0, "completion_ts": self.now - 100,
            },
        ]

        # Cálculo manual esperado
        e = INITIAL_TEAM_ELO
        for won, opp in ((1, 1200.0), (1, 1600.0), (0, 1000.0)):
            exp = 1.0 / (1.0 + 10.0 ** ((opp - e) / 400.0))
            e = round(e + ELO_K_FACTOR * (won - exp), 1)

        # Passar fora de ordem cronológica para validar ordenação interna
        built = build_side_teams_from_matches([rows[2], rows[0], rows[1]], min_games=3)
        self.assertEqual(len(built), 1)
        self.assertAlmostEqual(built[0]["elo"], e, places=1)
        self.assertEqual(built[0]["w"], 2)
        self.assertEqual(built[0]["l"], 1)
        self.assertEqual(built[0]["s"], -1)

    def test_br_vs_br_arranged_match_uses_arranged_team_elos(self) -> None:
        # 3 jogos entre dupla A (axis) e dupla B (allies), 2 vitórias A e 1 vitória B
        # Na partida 1, ambas começam com 1200 -> soma de ELOs se conserva em 2400!
        dump = [
            _mk_match(1, self.now - 500, [self.nigo, self.bineto], [137123, 198437], True, [self.br1, self.br2], [129494, 203852], t0_elo=900, t1_elo=1800),
            _mk_match(2, self.now - 400, [self.nigo, self.bineto], [137123, 198437], False, [self.br1, self.br2], [129494, 203852], t0_elo=900, t1_elo=1800),
            _mk_match(3, self.now - 300, [self.nigo, self.bineto], [137123, 198437], True, [self.br1, self.br2], [129494, 203852], t0_elo=900, t1_elo=1800),
        ]
        extracted = extract_brazilian_2v2_matches_from_dump(dump, self.vet_pids)
        built = build_side_teams_from_matches(extracted, min_games=3)
        self.assertEqual(len(built), 2)
        # Como cada dupla usa o ELO arranjado da dupla adversária (soma zero), ELO_A + ELO_B ≈ 2400.0
        self.assertAlmostEqual(built[0]["elo"] + built[1]["elo"], 2400.0, places=0)

    def test_extract_filters_non_2v2_custom_console_and_sanitizes_fields(self) -> None:
        evil_player = (9999992, "\u202eEvil\x00Name" + "X" * 100, "BR/../", "76561198000000012")
        dump = [
            _mk_match(1, self.now - 100, [evil_player, self.br2], [137123, 198437], True, [self.ger1, self.ger2], [129494, 203852], mtype=2),
            _mk_match(2, self.now - 100, [self.nigo, self.bineto], [137123, 198437], True, [self.ger1, self.ger2], [129494, 203852], mtype=0),  # custom
            _mk_match(3, self.now - 100, [self.nigo, self.bineto], [137123, 198437], True, [self.ger1, self.ger2], [129494, 203852], platform="xbox"),  # console
            _mk_match(4, self.now - 100, [self.nigo, self.ger1], [137123, 198437], True, [self.ger2, self.br1], [129494, 203852], mtype=21),  # mixed
        ]
        ext = extract_brazilian_2v2_matches_from_dump(dump, self.vet_pids)
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]["match_id"], 1)
        self.assertNotIn("\u202e", ext[0]["p1_alias"])
        self.assertNotIn("\x00", ext[0]["p1_alias"])
        self.assertLessEqual(len(ext[0]["p1_alias"]), 80)
        self.assertEqual(ext[0]["p1_country"], "br")

    def test_full_history_min10_active_only_excludes_all_inactive_and_csv_export(self) -> None:
        raw_teams = [
            # 1) Dupla Veteranos ativa em ambos os lados -> média (1460 + 1380) / 2 = 1420.0 (25 jogos)
            {"id": "ax1", "type": "2v2", "side": "axis", "elo": 1460.0, "w": 10, "l": 5, "s": 3, "lmTS": self.now - 2 * 86400,
             "players": [{"profile_id": 26910, "alias": "nigo_BR ᵀᴹ", "country": "br"}, {"profile_id": 110078, "alias": "Bineto", "country": "br"}]},
            {"id": "al1", "type": "2v2", "side": "allies", "elo": 1380.0, "w": 8, "l": 2, "s": -1, "lmTS": self.now - 5 * 86400,
             "players": [{"profile_id": 110078, "alias": "Bineto", "country": "br"}, {"profile_id": 26910, "alias": "nigo_BR ᵀᴹ", "country": "br"}]},
            # 2) Lem22 (US, Veterano) + BR não-Veterano: Axis recente (7j) + Allies >30d (8j) -> TODO histórico mantido (15 jogos, média 1300.0)
            {"id": "ax2", "type": "2v2", "side": "axis", "elo": 1500.0, "w": 6, "l": 1, "s": 4, "lmTS": (self.now - 4 * 86400) * 1000,
             "players": [{"profile_id": 9999992, "alias": "=SUM(A1)", "country": "br"}, {"profile_id": 366826, "alias": "lem22", "country": "us"}]},
            {"id": "al2_old", "type": "2v2", "side": "allies", "elo": 1100.0, "w": 3, "l": 5, "s": -2, "lmTS": self.now - 45 * 86400,
             "players": [{"profile_id": 9999992, "alias": "=SUM(A1)", "country": "br"}, {"profile_id": 366826, "alias": "lem22", "country": "us"}]},
            # 3) Dupla Veteranos ativa com apenas 9 jogos (< 10) -> deve ser excluída pela calibragem de ELO
            {"id": "ax_small", "type": "2v2", "side": "axis", "elo": 1650.0, "w": 7, "l": 2, "s": 3, "lmTS": self.now - 1 * 86400,
             "players": [{"profile_id": 26910, "alias": "nigo_BR ᵀᴹ", "country": "br"}, {"profile_id": 366826, "alias": "lem22", "country": "us"}]},
            # 4) Dupla 100% Veterana inativa (> 30d, >= 10 jogos, ELO alto 1750) -> REMOVIDA (> 30 dias sem jogar)
            {"id": "ax_vet2_inact", "type": "2v2", "side": "axis", "elo": 1750.0, "w": 11, "l": 2, "s": 5, "lmTS": self.now - 40 * 86400,
             "players": [{"profile_id": 110078, "alias": "Bineto", "country": "br"}, {"profile_id": 366826, "alias": "lem22", "country": "us"}]},
            # 5) Dupla mista (1 Veterano + 1 outro BR) inativa (> 30d, >= 10 jogos) -> REMOVIDA
            {"id": "ax_vet1_inact", "type": "2v2", "side": "axis", "elo": 1820.0, "w": 15, "l": 3, "s": 4, "lmTS": self.now - 35 * 86400,
             "players": [{"profile_id": 110078, "alias": "Bineto", "country": "br"}, {"profile_id": 9999991, "alias": "OutroBR2", "country": "br"}]},
            # 6) Dupla de outros brasileiros (0 Veteranos) inativa (> 30d, 22 jogos) -> REMOVIDA
            {"id": "ax3_old", "type": "2v2", "side": "axis", "elo": 1700.0, "w": 20, "l": 2, "s": 8, "lmTS": self.now - 31 * 86400,
             "players": [{"profile_id": 9999992, "alias": "OutroBR1", "country": "br"}, {"profile_id": 9999991, "alias": "OutroBR2", "country": "br"}]},
        ]
        unified = compute_unified_2v2_arranged_teams(
            raw_teams,
            custom_players=self.custom_players,
            inactive_days=30,
            now_ts=self.now,
            min_total_games=10,
        )
        self.assertEqual(len(unified), 2)

        # #1: Bineto + nigo_BR — ativa, unificada Eixo + Aliados (1420.0)
        self.assertEqual(unified[0]["br_rank"], 1)
        self.assertFalse(unified[0]["inactive_fallback"])
        self.assertEqual(unified[0]["avg_elo"], 1420.0)
        self.assertEqual(unified[0]["sides"], ["axis", "allies"])
        self.assertEqual(unified[0]["wins"], 18)
        self.assertEqual(unified[0]["losses"], 7)
        self.assertEqual(unified[0]["streak"], 3)
        self.assertTrue(unified[0]["all_veteranos"])

        # #2: Lem22 + =SUM(A1) — ativa, mantém lado Aliados > 30d no histórico completo -> média (1500+1100)/2 = 1300.0
        self.assertEqual(unified[1]["br_rank"], 2)
        self.assertFalse(unified[1]["inactive_fallback"])
        self.assertEqual(unified[1]["avg_elo"], 1300.0)
        self.assertEqual(unified[1]["sides"], ["axis", "allies"])
        self.assertEqual(unified[1]["allies_elo"], 1100)
        self.assertEqual(unified[1]["total"], 15)
        self.assertEqual(unified[1]["players"][0]["alias"], "Lem22")
        self.assertEqual(unified[1]["veteranos_count"], 1)

        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "arranged.json"
            cp = Path(td) / "arranged.csv"
            export_arranged_teams_files(unified, jp, cp, inactive_days=30, min_total_games=10, max_teams=32)
            doc = json.loads(jp.read_text(encoding="utf-8"))
            self.assertEqual(doc["total_teams"], 2)
            self.assertEqual(doc["active_teams"], 2)
            self.assertEqual(doc["top_n_limit"], 32)
            self.assertEqual(doc["min_total_games"], 10)
            self.assertNotIn("inactive_veteranos_teams", doc)
            with cp.open(encoding="utf-8") as fp:
                rows = list(csv.DictReader(fp))
            self.assertEqual([r["status"] for r in rows], ["ativo", "ativo"])
            self.assertTrue(rows[1]["player2_alias"].startswith("'="))

    def test_top32_hard_cutoff_excludes_active_teams_beyond_rank_32(self) -> None:
        raw_teams = []
        # 36 duplas ativas de outros brasileiros (ELO 1600 down to 1250) + 2 duplas Veteranas ativas com ELO abaixo do #32
        for i in range(36):
            p1 = 500000 + i * 2
            p2 = 500001 + i * 2
            raw_teams.append(
                {
                    "id": f"ax_br_{i}",
                    "type": "2v2",
                    "side": "axis",
                    "elo": 1600.0 - i * 10.0,
                    "w": 12,
                    "l": 4,
                    "s": 2,
                    "lmTS": self.now - 3 * 86400,
                    "players": [
                        {"profile_id": p1, "alias": f"BR_{i}_A", "country": "br"},
                        {"profile_id": p2, "alias": f"BR_{i}_B", "country": "br"},
                    ],
                }
            )
        # Dupla 100% Veterana ativa mas com ELO menor que o #32 (1600 - 31*10 = 1290) -> deve ficar DE FORA
        raw_teams.append(
            {
                "id": "ax_vet_low",
                "type": "2v2",
                "side": "axis",
                "elo": 1100.0,
                "w": 20,
                "l": 15,
                "s": 1,
                "lmTS": self.now - 1 * 86400,
                "players": [
                    {"profile_id": 26910, "alias": "nigo_BR ᵀᴹ", "country": "br"},
                    {"profile_id": 110078, "alias": "Bineto", "country": "br"},
                ],
            }
        )

        unified = compute_unified_2v2_arranged_teams(
            raw_teams,
            custom_players=self.custom_players,
            inactive_days=30,
            now_ts=self.now,
            min_total_games=10,
        )
        self.assertEqual(len(unified), 32)
        self.assertEqual([t["br_rank"] for t in unified], list(range(1, 33)))
        self.assertEqual(unified[0]["avg_elo"], 1600.0)
        self.assertEqual(unified[-1]["avg_elo"], 1290.0)
        # Nenhuma dupla após a 32ª posição (mesmo Veterana ativa) entra
        self.assertNotIn("26910_110078", {t["pair_key"] for t in unified})

    def test_sqlite_arranged_cache_and_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "cache.db"
            with RankingCacheDB(db_path) as db:
                old_row = {
                    "match_id": 1, "side": "axis", "pair_key": "1_2",
                    "p1_id": 1, "p1_alias": "A", "p1_country": "br", "p1_steam_id": "",
                    "p2_id": 2, "p2_alias": "B", "p2_country": "br", "p2_steam_id": "",
                    "won": 1, "enemy_elo": 1200.0, "completion_ts": 100000,
                }
                recent_ts = 10_000_000_000  # futuro distante para garantir retenção
                new_row = dict(old_row, match_id=2, completion_ts=recent_ts)
                db.save_arranged_2v2_matches([old_row, new_row])
                db.save_arranged_2v2_matches([old_row, new_row])  # idempotência
                self.assertEqual(len(db.load_all_arranged_2v2_matches()), 2)

                db.mark_arranged_day_processed(100000, matches_total=10, br_2v2_matches=1)
                db.mark_arranged_day_processed(recent_ts, matches_total=10, br_2v2_matches=1)
                pruned = db.prune_old_arranged_matches(max_age_days=365)
                self.assertEqual(pruned, 1)
                self.assertEqual(len(db.load_all_arranged_2v2_matches()), 1)
                self.assertEqual(db.get_processed_arranged_days(), {recent_ts})

    def test_sync_stops_at_archive_floor_after_consecutive_historical_404s(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def fetch_day_brazilian_2v2_matches(self, day_ts: int, vet_pids: set[int]):
                self.calls += 1
                # Simula 404 para todos os dias antigos (> 30 dias)
                return ("not_found", 0, [])

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "cache.db"
            with RankingCacheDB(db_path) as db:
                client = FakeClient()
                sync_brazilian_2v2_arranged_matches(
                    client=client,
                    db=db,
                    bootstrap_days=200,
                    max_scan_seconds=60.0,
                    now_ts=self.now,
                )
                # Deve parar após os 30 dias iniciais + MAX_CONSECUTIVE_HISTORICAL_404_DAYS (14) dias (arredondado para lote de 4)
                self.assertLessEqual(client.calls, 30 + MAX_CONSECUTIVE_HISTORICAL_404_DAYS + 4)
                floor_val = int(db.get_state("arranged_history_floor_ts") or 0)
                self.assertGreater(floor_val, 0)

                # Segunda chamada não deve tentar baixar dias mais antigos que o floor salvo
                prev_calls = client.calls
                sync_brazilian_2v2_arranged_matches(
                    client=client,
                    db=db,
                    bootstrap_days=200,
                    max_scan_seconds=60.0,
                    now_ts=self.now,
                )
                # Apenas os 2 dias mais recentes (offset 1 e 2, < 48h) são rechecados
                self.assertEqual(client.calls - prev_calls, 2)

    def test_smurf_accounts_stay_separate_with_owner_metadata_and_nick_change_asterisk(self) -> None:
        # 1. Chacineiro (79327) + Golden Cobra (16867) e Charles Bronson (Chacineiro) (1333418) + Golden Cobra (16867)
        #    ficam SEPARADOS (sem merge!) e a Smurf carrega is_smurf=True, smurf_of="Chacineiro", has_smurf=True.
        # 2. geladeira velha (arc) (1423426, Veterano Smurf) + Alekel (Smurf: Shadow) (1297853, outro BR Smurf)
        #    ambos carregam smurf_of corretamente ("arc" e "Alekel").
        # 3. Outro BR não-Veterano (9999991) muda de nick (AntigoNick -> NovoNick) com Golden Cobra -> preserva '*' sem is_smurf.
        rows = [
            # Chacineiro (79327) + Golden Cobra (16867) -> 3 jogos
            {
                "match_id": 10, "side": "axis", "pair_key": "16867_79327",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 79327, "p2_alias": "Chacineiro", "p2_country": "br", "p2_steam_id": "76561197963172584",
                "won": 1, "enemy_elo": 1250.0, "completion_ts": self.now - 5000,
            },
            {
                "match_id": 11, "side": "axis", "pair_key": "16867_79327",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 79327, "p2_alias": "Chacineiro", "p2_country": "br", "p2_steam_id": "76561197963172584",
                "won": 1, "enemy_elo": 1250.0, "completion_ts": self.now - 4000,
            },
            {
                "match_id": 12, "side": "axis", "pair_key": "16867_79327",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 79327, "p2_alias": "Chacineiro", "p2_country": "br", "p2_steam_id": "76561197963172584",
                "won": 0, "enemy_elo": 1250.0, "completion_ts": self.now - 3000,
            },
            # Smurf Charles Bronson (Chacineiro) (1333418) + Golden Cobra (16867) -> 3 jogos SEPARADOS
            {
                "match_id": 20, "side": "axis", "pair_key": "16867_1333418",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 1333418, "p2_alias": "TacticalForceConcentration", "p2_country": "br", "p2_steam_id": "76561198698969876",
                "won": 1, "enemy_elo": 1250.0, "completion_ts": self.now - 5000,
            },
            {
                "match_id": 21, "side": "axis", "pair_key": "16867_1333418",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 1333418, "p2_alias": "Charles Bronson", "p2_country": "br", "p2_steam_id": "76561198698969876",
                "won": 1, "enemy_elo": 1250.0, "completion_ts": self.now - 4000,
            },
            {
                "match_id": 22, "side": "axis", "pair_key": "16867_1333418",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 1333418, "p2_alias": "Charles Bronson", "p2_country": "br", "p2_steam_id": "76561198698969876",
                "won": 1, "enemy_elo": 1250.0, "completion_ts": self.now - 1000,
            },
            # Smurf Veterano geladeira velha (arc) (1423426) + Smurf Outro BR Alekel (Smurf: Shadow) (1297853) -> 3 jogos
            {
                "match_id": 25, "side": "allies", "pair_key": "1297853_1423426",
                "p1_id": 1297853, "p1_alias": "Shadow", "p1_country": "br", "p1_steam_id": "76561199869580388",
                "p2_id": 1423426, "p2_alias": "geladeira velha", "p2_country": "br", "p2_steam_id": "76561199482388323",
                "won": 1, "enemy_elo": 1350.0, "completion_ts": self.now - 4000,
            },
            {
                "match_id": 26, "side": "allies", "pair_key": "1297853_1423426",
                "p1_id": 1297853, "p1_alias": "Shadow", "p1_country": "br", "p1_steam_id": "76561199869580388",
                "p2_id": 1423426, "p2_alias": "geladeira velha", "p2_country": "br", "p2_steam_id": "76561199482388323",
                "won": 1, "enemy_elo": 1350.0, "completion_ts": self.now - 3000,
            },
            {
                "match_id": 27, "side": "allies", "pair_key": "1297853_1423426",
                "p1_id": 1297853, "p1_alias": "Shadow", "p1_country": "br", "p1_steam_id": "76561199869580388",
                "p2_id": 1423426, "p2_alias": "geladeira velha", "p2_country": "br", "p2_steam_id": "76561199482388323",
                "won": 1, "enemy_elo": 1350.0, "completion_ts": self.now - 2000,
            },
            # Golden Cobra (16867) + outro BR não-Veterano (9999991) que muda de nick durante o histórico
            {
                "match_id": 30, "side": "axis", "pair_key": "16867_9999991",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 9999991, "p2_alias": "AntigoNickBR", "p2_country": "br", "p2_steam_id": "",
                "won": 1, "enemy_elo": 1200.0, "completion_ts": self.now - 5000,
            },
            {
                "match_id": 31, "side": "axis", "pair_key": "16867_9999991",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 9999991, "p2_alias": "AntigoNickBR", "p2_country": "br", "p2_steam_id": "",
                "won": 1, "enemy_elo": 1200.0, "completion_ts": self.now - 4000,
            },
            {
                "match_id": 32, "side": "axis", "pair_key": "16867_9999991",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 9999991, "p2_alias": "NovoNickBR", "p2_country": "br", "p2_steam_id": "",
                "won": 0, "enemy_elo": 1200.0, "completion_ts": self.now - 1000,
            },
        ]

        built = build_side_teams_from_matches(rows, min_games=3)
        # Com o padrão de 10 jogos, duplas de apenas 3 jogos são filtradas
        self.assertEqual(compute_unified_2v2_arranged_teams(built, inactive_days=30, now_ts=self.now), [])

        # Com min_total_games=3 validamos separação de Smurfs (sem merge!), detecção de smurf_of e '*' em troca de nick
        unified = compute_unified_2v2_arranged_teams(built, inactive_days=30, now_ts=self.now, min_total_games=3)
        self.assertEqual(len(unified), 4)

        by_key = {t["pair_key"]: t for t in unified}

        # Conta principal Chacineiro + Golden Cobra separada
        self.assertIn("16867_79327", by_key)
        chac_cobra = by_key["16867_79327"]
        self.assertTrue(chac_cobra["has_veterano"])
        self.assertFalse(chac_cobra["has_smurf"])
        self.assertEqual(chac_cobra["total"], 3)
        self.assertEqual([p["alias"] for p in chac_cobra["players"]], ["Chacineiro", "Golden Cobra"])
        self.assertFalse(chac_cobra["players"][0]["is_smurf"])
        self.assertEqual(chac_cobra["players"][0]["smurf_of"], "")

        # Smurf Charles Bronson (Smurf: Chacineiro) + Golden Cobra SEPARADA com metadados de dono original
        self.assertIn("16867_1333418", by_key)
        smurf_cobra = by_key["16867_1333418"]
        self.assertTrue(smurf_cobra["has_veterano"])
        self.assertTrue(smurf_cobra["has_smurf"])
        self.assertEqual(smurf_cobra["total"], 3)
        smurf_p = next(p for p in smurf_cobra["players"] if p["profile_id"] == 1333418)
        self.assertEqual(smurf_p["alias"], "Charles Bronson (Smurf: Chacineiro)")
        self.assertTrue(smurf_p["is_smurf"])
        self.assertEqual(smurf_p["smurf_of"], "Chacineiro")

        # Duas contas Smurf (1 Veterano + 1 Outro BR) identificam ambos os donos originais
        self.assertIn("1297853_1423426", by_key)
        dual_smurf = by_key["1297853_1423426"]
        self.assertTrue(dual_smurf["has_veterano"])
        self.assertTrue(dual_smurf["has_smurf"])
        self.assertEqual(dual_smurf["veteranos_count"], 1)
        by_pid_ds = {p["profile_id"]: p for p in dual_smurf["players"]}
        self.assertEqual(by_pid_ds[1423426]["alias"], "geladeira velha (Smurf: arc)")
        self.assertTrue(by_pid_ds[1423426]["is_smurf"])
        self.assertEqual(by_pid_ds[1423426]["smurf_of"], "arc")
        self.assertEqual(by_pid_ds[1297853]["alias"], "Shadow (Smurf: Alekel)")
        self.assertTrue(by_pid_ds[1297853]["is_smurf"])
        self.assertEqual(by_pid_ds[1297853]["smurf_of"], "Alekel")

        # Outro BR não-Veterano que muda de nick preserva nick anterior com '*' sem falso positivo de Smurf
        self.assertIn("16867_9999991", by_key)
        cobra_team = by_key["16867_9999991"]
        self.assertFalse(cobra_team["has_veterano"])
        self.assertFalse(cobra_team["has_smurf"])
        by_pid = {p["profile_id"]: p for p in cobra_team["players"]}
        self.assertEqual(by_pid[16867]["alias"], "Golden Cobra")
        self.assertFalse(by_pid[16867]["is_veterano"])
        self.assertEqual(by_pid[9999991]["alias"], "AntigoNickBR* (NovoNickBR)")
        self.assertFalse(by_pid[9999991]["is_smurf"])
        self.assertEqual(by_pid[9999991]["smurf_of"], "")

    def test_extract_supports_top_level_matchhistorymember_and_profiles_fallback(self) -> None:
        legacy_match = {
            "id": 9991,
            "matchtype_id": 2,
            "completiontime": self.now - 86400 * 500,
            "matchhistoryreportresults": [
                {"profile_id": 26910, "teamid": 0, "race_id": 137123, "resulttype": 2},
                {"profile_id": 110078, "teamid": 0, "race_id": 198437, "resulttype": 1},
                {"profile_id": 8888881, "teamid": 1, "race_id": 129494, "resulttype": 0},
                {"profile_id": 8888882, "teamid": 1, "race_id": 203852, "resulttype": 0},
            ],
            "matchhistorymember": [
                {"profile_id": 26910, "outcome": 1, "oldrating": 1320},
                {"profile_id": 110078, "outcome": 1, "oldrating": 1280},
                {"profile_id": 8888881, "outcome": 0, "oldrating": 1410},
                {"profile_id": 8888882, "outcome": 0, "oldrating": 1390},
            ],
            "profiles": [
                {"profile_id": 26910, "alias": "nigo_BR ᵀᴹ", "country": "br", "name": "/steam/76561197963741792"},
                {"profile_id": 110078, "alias": "Bineto", "country": "br", "name": "/steam/76561198024839791"},
                {"profile_id": 8888881, "alias": "Opp1", "country": "de", "name": "/steam/76561198000000091"},
                {"profile_id": 8888882, "alias": "Opp2", "country": "de", "name": "/steam/76561198000000092"},
            ],
        }
        ext = extract_brazilian_2v2_matches_from_dump([legacy_match], self.vet_pids)
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]["pair_key"], "26910_110078")
        self.assertEqual(ext[0]["won"], 1)
        self.assertEqual(ext[0]["enemy_elo"], 1400.0)

    def test_relic_live_sync_captures_night_brt_matches_for_veteranos_and_other_br_idempotently(self) -> None:
        import urllib.parse
        from relic_ranking import LEADERBOARD_ID_TO_MODE_FACTION

        lb_2v2 = next(k for k, v in LEADERBOARD_ID_TO_MODE_FACTION.items() if v[0] == "2v2")
        # Open Data já cobriu até 22/11 00:00 UTC (22/11 21:00 BRT), mas faltam jogos noturnos BRT (23/11 01:00-02:30 UTC)
        open_data_day = 1795305600  # 2026-11-22 00:00 UTC
        now_ts = open_data_day + 86400 + 58000  # 2026-11-23 ~16:06 UTC
        night_match_vet_ts = open_data_day + 86400 + 8900   # 2026-11-22 23:28 BRT (2026-11-23 02:28 UTC)
        night_match_br_ts = open_data_day + 86400 + 5400    # 2026-11-22 22:30 BRT (2026-11-23 01:30 UTC)
        old_match_ts = open_data_day - 7 * 86400            # 2026-11-15

        class FakeRelicClient:
            def __init__(self) -> None:
                self.personal_calls = 0
                self.history_pids: list[int] = []

            def _get_json(self, url: str, max_bytes: int = 5 * 1024 * 1024) -> dict:
                if "getPersonalStat" in url:
                    self.personal_calls += 1
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                    pids = json.loads(qs["profile_ids"][0])
                    sgs, stats = [], []
                    # 26910 (nigo) e 9999992 (outro BR) jogaram 2v2 na noite de 22/11 BRT; 110078 (Bineto) só jogou dia 15/11
                    lmd_map = {
                        26910: night_match_vet_ts,
                        9999992: night_match_br_ts,
                        110078: old_match_ts,
                    }
                    for idx, pid in enumerate(pids, start=1):
                        sg_id = pid * 10 + idx
                        sgs.append({"id": sg_id, "type": 1, "members": [{"profile_id": pid, "alias": f"P{pid}"}]})
                        if pid in lmd_map:
                            stats.append({"statgroup_id": sg_id, "leaderboard_id": lb_2v2, "lastmatchdate": lmd_map[pid]})
                    return {"statGroups": sgs, "leaderboardStats": stats}

                if "getRecentMatchHistoryByProfileId" in url:
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                    pid = int(qs["profile_id"][0])
                    self.history_pids.append(pid)
                    if pid == 26910:
                        return {
                            "profiles": [
                                {"profile_id": 26910, "alias": "nigo_BR ᵀᴹ", "country": "br", "name": "/steam/76561197963741792"},
                                {"profile_id": 55693, "alias": "BinhoBiceps", "country": "br", "name": "/steam/76561197996817129"},
                                {"profile_id": 8888881, "alias": "Opp1", "country": "de", "name": "/steam/76561198000000091"},
                                {"profile_id": 8888882, "alias": "Opp2", "country": "de", "name": "/steam/76561198000000092"},
                            ],
                            "matchHistoryStats": [
                                {
                                    "id": 92618851,
                                    "matchtype_id": 21,
                                    "completiontime": night_match_vet_ts,
                                    "matchhistoryreportresults": [
                                        {"profile_id": 26910, "teamid": 0, "race_id": 137123, "resulttype": 0},
                                        {"profile_id": 55693, "teamid": 0, "race_id": 198437, "resulttype": 0},
                                        {"profile_id": 8888881, "teamid": 1, "race_id": 129494, "resulttype": 1},
                                        {"profile_id": 8888882, "teamid": 1, "race_id": 203852, "resulttype": 1},
                                    ],
                                    "matchhistorymember": [
                                        {"profile_id": 26910, "outcome": 0, "oldrating": 1162},
                                        {"profile_id": 55693, "outcome": 0, "oldrating": 1321},
                                        {"profile_id": 8888881, "outcome": 1, "oldrating": 1280},
                                        {"profile_id": 8888882, "outcome": 1, "oldrating": 1275},
                                    ],
                                }
                            ],
                        }
                    if pid == 9999992:
                        return {
                            "profiles": [
                                {"profile_id": 9999992, "alias": "OutroBR1", "country": "br", "name": "/steam/76561198000000012"},
                                {"profile_id": 9999991, "alias": "OutroBR2", "country": "br", "name": "/steam/76561198000000011"},
                                {"profile_id": 8888881, "alias": "Opp1", "country": "de", "name": "/steam/76561198000000091"},
                                {"profile_id": 8888882, "alias": "Opp2", "country": "de", "name": "/steam/76561198000000092"},
                            ],
                            "matchHistoryStats": [
                                {
                                    "id": 92617000,
                                    "matchtype_id": 21,
                                    "completiontime": night_match_br_ts,
                                    "matchhistoryreportresults": [
                                        {"profile_id": 9999992, "teamid": 0, "race_id": 129494, "resulttype": 1},
                                        {"profile_id": 9999991, "teamid": 0, "race_id": 203852, "resulttype": 1},
                                        {"profile_id": 8888881, "teamid": 1, "race_id": 137123, "resulttype": 0},
                                        {"profile_id": 8888882, "teamid": 1, "race_id": 198437, "resulttype": 0},
                                    ],
                                    "matchhistorymember": [
                                        {"profile_id": 9999992, "outcome": 1, "oldrating": 1250},
                                        {"profile_id": 9999991, "outcome": 1, "oldrating": 1230},
                                        {"profile_id": 8888881, "outcome": 0, "oldrating": 1310},
                                        {"profile_id": 8888882, "outcome": 0, "oldrating": 1290},
                                    ],
                                }
                            ],
                        }
                    return {"profiles": [], "matchHistoryStats": []}
                raise AssertionError(f"URL inesperada: {url}")

        with tempfile.TemporaryDirectory() as td:
            with RankingCacheDB(Path(td) / "cache.db") as db:
                db.mark_arranged_day_processed(open_data_day, matches_total=23000, br_2v2_matches=25)
                # Cache prévio com partida antiga de outro BR (9999992 + 9999991) para entrar na lista de candidatos
                db.save_arranged_2v2_matches(
                    [
                        {
                            "match_id": 91000001, "side": "allies", "pair_key": "9999991_9999992",
                            "p1_id": 9999991, "p1_alias": "OutroBR2", "p1_country": "br", "p1_steam_id": "",
                            "p2_id": 9999992, "p2_alias": "OutroBR1", "p2_country": "br", "p2_steam_id": "",
                            "won": 1, "enemy_elo": 1200.0, "completion_ts": old_match_ts,
                        }
                    ]
                )
                fake_relic = FakeRelicClient()
                inserted = sync_recent_brazilian_2v2_matches_from_relic(
                    relic_client=fake_relic,
                    db=db,
                    custom_players=self.custom_players,
                    inactive_days=30,
                    now_ts=now_ts,
                )
                # Deve capturar ambas as partidas noturnas (Veteranos 26910+55693 e outros BR 9999992+9999991)
                self.assertEqual(inserted, 2)
                self.assertEqual(sorted(fake_relic.history_pids), [26910, 9999992])
                self.assertNotIn(110078, fake_relic.history_pids)  # Bineto só jogou 15/11 -> pré-filtrado!

                all_rows = db.load_all_arranged_2v2_matches()
                by_mid = {r["match_id"]: r for r in all_rows}
                self.assertIn(92618851, by_mid)
                self.assertEqual(by_mid[92618851]["pair_key"], "26910_55693")
                self.assertEqual(by_mid[92618851]["enemy_elo"], 1277.5)
                self.assertIn(92617000, by_mid)
                self.assertEqual(by_mid[92617000]["pair_key"], "9999991_9999992")

                # Segunda execução imediata deve ser 100% idempotente (0 chamadas de histórico!)
                fake_relic.history_pids.clear()
                inserted_2 = sync_recent_brazilian_2v2_matches_from_relic(
                    relic_client=fake_relic,
                    db=db,
                    custom_players=self.custom_players,
                    inactive_days=30,
                    now_ts=now_ts,
                )
                self.assertEqual(inserted_2, 0)
                self.assertEqual(fake_relic.history_pids, [])


if __name__ == "__main__":
    unittest.main()
