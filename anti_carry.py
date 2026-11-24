"""Sistema Anti-Carry compartilhado entre o Ranking Unificado BR (Veteranos) e os Campeões BR por Battlegroup.

Objetivo:
  Evitar que jogadores com ELO ou aproveitamento inflados por parceiros/smurfs muito mais fortes
  em filas de equipe (2v2, 3v3, 4v4) sejam promovidos acima de peso-pesados reais, preservando
  integralmente:
    1) Desempenho solo em 1v1 (nunca sofre penalização anti-carry);
    2) Carregadores comprovados em equipe (aliados mais fracos e liderança de dano);
    3) Paridade solo no 1v1 (quem prova o mesmo nível sozinho no 1v1 sem dependência 2v2).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from roster import CORE_BR_ROSTER, MANUAL_OTHER_BR_ALIASES, extract_smurf_owner
from security import strip_unsafe_chars

ANTI_CARRY_VERSION = "v4_shared_anticarry"

# Offset equivalente de ELO por modo (1v1 = referência; 2v2 leve desconto; 3v3/4v4 mais inflacionados)
MODE_ELO_OFFSET: dict[str, float] = {
    "1v1": 0.0,
    "2v2": -20.0,
    "3v3": -165.0,
    "4v4": -285.0,
}

# Pesos de combate por modo (1v1/2v2 pesam mais do que 3v3/4v4)
MODE_COMBAT_WEIGHT: dict[str, float] = {
    "1v1": 1.00,
    "2v2": 0.90,
    "3v3": 0.55,
    "4v4": 0.35,
}

# Regras compartilhadas para detecção de upsets reais e partidas carregadas
UPSET_MIN_ELO_GAP = 15.0
UPSET_MAX_TEAM_CARRY_GAP = 25.0
UPSET_MIN_ENEMY_ADJ_ELO = 1350.0
UPSET_MAX_ALLY_CARRY_GAP = 70.0

# Contas Smurf conhecidas da comunidade BR (usadas para resolver o ELO real do dono ao detectar carry)
KNOWN_SMURF_PIDS: dict[int, str] = {
    1333418: "Chacineiro",      # Charles Bronson
    1390350: "Chacineiro",      # TearsInTheRain
    1423426: "arc",             # geladeira velha
    1487738: "arc",             # Državljan H
    1482961: "arc",             # Vog
    557890: "Gabe",             # gabe44arg
    1318162: "General Winter",  # Smurf 1
    1297821: "General Winter",  # Smurf 2
    1297853: "Alekel",          # Shadow
    1297804: "『V』SKOBADARK",  # Leite Moço
    1076221: "Colossus",        # Lucielle
    720147: "icemaker",         # アリス
}


def extract_player_best_adj_elo(player: dict[str, Any] | None) -> float:
    """Extrai o melhor ELO ajustado por modo de um registro (compatível com Battlegroups e Ranking Unificado)."""
    if not isinstance(player, dict):
        return 0.0
    direct = float(player.get("global_best_adj_elo") or 0.0)
    if direct > 0.0:
        return direct

    best = 0.0
    # Formato Ranking Unificado (modes + inactive_modes)
    for bucket_key in ("modes", "inactive_modes"):
        bucket = player.get(bucket_key)
        if not isinstance(bucket, dict):
            continue
        for st in bucket.values():
            if not isinstance(st, dict):
                continue
            mode = str(st.get("mode") or "").lower()
            elo = float(st.get("elo") or st.get("rating") or 0.0)
            games = int(st.get("wins", 0) or 0) + int(st.get("losses", 0) or 0)
            if elo >= 500.0 and games >= 8 and mode in MODE_ELO_OFFSET:
                adj = elo + MODE_ELO_OFFSET[mode]
                if adj > best:
                    best = adj

    # Formato já computado (mode_max_elos)
    mode_max = player.get("mode_max_elos")
    if isinstance(mode_max, dict):
        for mode, elo_v in mode_max.items():
            m = str(mode).lower()
            elo = float(elo_v or 0.0)
            if elo >= 500.0 and m in MODE_ELO_OFFSET:
                adj = elo + MODE_ELO_OFFSET[m]
                if adj > best:
                    best = adj

    # Formato Battlegroups (queues)
    queues = player.get("queues")
    if isinstance(queues, dict):
        for kq, q in queues.items():
            if not isinstance(q, dict) or ":" not in str(kq):
                continue
            m = str(kq).split(":")[1].lower()
            elo = float(q.get("rating") or 0.0)
            games = int(q.get("games") or 0)
            if elo >= 500.0 and games >= 8 and m in MODE_ELO_OFFSET:
                adj = elo + MODE_ELO_OFFSET[m]
                if adj > best:
                    best = adj

    return best


def extract_player_best_1v1(player: dict[str, Any] | None, min_games: int = 8) -> int:
    """Extrai o maior ELO 1v1 calibrado de um jogador (compatível com Battlegroups e Ranking Unificado)."""
    if not isinstance(player, dict):
        return 0
    best = 0

    mode_max = player.get("mode_max_elos")
    if isinstance(mode_max, dict) and int(mode_max.get("1v1") or 0) > 0:
        best = max(best, int(mode_max["1v1"]))

    for bucket_key in ("modes", "inactive_modes"):
        bucket = player.get(bucket_key)
        if not isinstance(bucket, dict):
            continue
        for st in bucket.values():
            if not isinstance(st, dict):
                continue
            if str(st.get("mode") or "").lower() != "1v1":
                continue
            elo = int(round(float(st.get("elo") or st.get("rating") or 0.0)))
            games = int(st.get("wins", 0) or 0) + int(st.get("losses", 0) or 0)
            if elo >= 500 and games >= min_games and elo > best:
                best = elo

    queues = player.get("queues")
    if isinstance(queues, dict):
        for kq, q in queues.items():
            if not isinstance(q, dict) or not str(kq).endswith(":1v1"):
                continue
            elo = int(round(float(q.get("rating") or 0.0)))
            games = int(q.get("games") or 0)
            if elo >= 500 and games >= min_games and elo > best:
                best = elo

    return best


def resolve_smurf_owner_name(pid: int, rec: dict[str, Any]) -> str:
    if pid in KNOWN_SMURF_PIDS:
        return KNOWN_SMURF_PIDS[pid]
    explicit = str(rec.get("smurf_of") or "").strip()
    if explicit:
        return strip_unsafe_chars(explicit)[:40]
    alias = str(rec.get("original_alias") or rec.get("roster_alias") or rec.get("alias") or "")
    return extract_smurf_owner(alias, pin_alias=bool(rec.get("is_smurf") or rec.get("pin_alias")))


def build_smurf_owner_maps(
    players_by_pid: dict[int, dict[str, Any]],
    peer_adj_elos: dict[int, float] | None = None,
) -> tuple[dict[int, int], dict[int, float]]:
    """Mapeia `smurf_pid -> owner_pid` e `smurf_pid -> ELO real do dono` para detecção anti-carry."""
    peer_elos = peer_adj_elos or {}
    name_to_pid: dict[str, int] = {}

    # 1) Mapeamento base via roster oficial (cobre donos fora do subconjunto de Veteranos, ex.: General Winter)
    for row in (*CORE_BR_ROSTER, *MANUAL_OTHER_BR_ALIASES):
        rpid = int(row.get("profile_id") or 0)
        if rpid <= 0 or rpid in KNOWN_SMURF_PIDS:
            continue
        if resolve_smurf_owner_name(rpid, row):
            continue
        for raw_name in (
            str(row.get("original_alias") or ""),
            *[str(k) for k in (row.get("known_aliases") or [])],
        ):
            clean = strip_unsafe_chars(raw_name).replace("(Principal)", "").strip()
            if clean:
                name_to_pid.setdefault(clean.casefold(), rpid)

    # 2) Sobrescreve/enriquece com os jogadores ativos no pool atual
    for pid, p in players_by_pid.items():
        if resolve_smurf_owner_name(int(pid), p):
            continue
        for raw_name in (
            str(p.get("original_alias") or ""),
            str(p.get("roster_alias") or ""),
            str(p.get("alias") or ""),
            str(p.get("live_alias") or ""),
            *[str(k) for k in (p.get("known_aliases") or [])],
        ):
            clean = strip_unsafe_chars(raw_name).replace("(Principal)", "").strip()
            if clean:
                name_to_pid[clean.casefold()] = int(pid)

    smurf_to_owner: dict[int, int] = {}
    smurf_to_elo: dict[int, float] = {}

    def _register_smurf(spid: int, owner_name: str) -> None:
        clean_owner = strip_unsafe_chars(owner_name).replace("(Principal)", "").strip().casefold()
        if not clean_owner:
            return
        opid = name_to_pid.get(clean_owner)
        if not opid or opid == spid:
            return
        smurf_to_owner[spid] = opid
        owner_elo = max(
            extract_player_best_adj_elo(players_by_pid.get(opid)),
            float(peer_elos.get(opid) or 0.0),
        )
        if owner_elo >= 500.0:
            # Converte ELO ajustado de volta para referência 2v2 (+20) para comparação justa por partida
            smurf_to_elo[spid] = round(owner_elo + 20.0, 1)

    for spid, owner_name in KNOWN_SMURF_PIDS.items():
        _register_smurf(int(spid), owner_name)
    for pid, p in players_by_pid.items():
        owner_name = resolve_smurf_owner_name(int(pid), p)
        if owner_name:
            _register_smurf(int(pid), owner_name)

    return smurf_to_owner, smurf_to_elo


def evaluate_match_ally_carry(
    *,
    mode: str,
    my_elo: float,
    my_dmg: float,
    my_kills: float,
    other_ally_elos: list[float],
    other_ally_dmgs: list[float],
    other_ally_kills: list[float],
) -> tuple[float, float, float, int]:
    """Avalia gap de ELO dos aliados (já smurf-aware) e contribuição relativa em uma partida de equipe."""
    ally_elo_gap = 0.0
    dmg_vs_ally = 1.0
    kills_vs_ally = 1.0
    carried_match = 0
    if mode != "1v1":
        if other_ally_elos and my_elo >= 500.0:
            ally_elo_gap = max(-500.0, min(500.0, (sum(other_ally_elos) / len(other_ally_elos)) - my_elo))
        if other_ally_dmgs and my_dmg > 0.0:
            dmg_vs_ally = max(0.25, min(3.0, my_dmg / (sum(other_ally_dmgs) / len(other_ally_dmgs))))
        if other_ally_kills and my_kills > 0.0:
            kills_vs_ally = max(0.25, min(3.0, my_kills / (sum(other_ally_kills) / len(other_ally_kills))))
        if (
            ally_elo_gap >= 85.0
            or (ally_elo_gap >= 40.0 and dmg_vs_ally < 0.95)
            or (dmg_vs_ally < 0.78 and kills_vs_ally < 0.85)
        ):
            carried_match = 1
    return round(ally_elo_gap, 1), round(dmg_vs_ally, 3), round(kills_vs_ally, 3), carried_match


def blend_team_ally_elo_gap(team_items: list[dict[str, Any]]) -> float:
    """Combina média ponderada por modo (50%) + mediana (50%) do ally_elo_gap para neutralizar outliers isolados."""
    if not team_items:
        return 0.0
    num = 0.0
    den = 0.0
    vals: list[float] = []
    for x in team_items:
        v = float(x.get("ally_elo_gap") or 0.0)
        w = MODE_COMBAT_WEIGHT.get(str(x.get("mode") or ""), 0.5)
        num += w * v
        den += w
        vals.append(v)
    wavg = (num / den) if den > 0.0 else 0.0
    med = statistics.median(vals) if vals else 0.0
    return 0.50 * wavg + 0.50 * med


def summarize_team_carry_from_rows(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Agrega telemetria anti-carry por jogador a partir das linhas Automatch em `bg_combat_matches`."""
    by_pid: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if str(r.get("mode") or "") != "1v1":
            by_pid[int(r["profile_id"])].append(r)

    def _wavg(items: list[dict[str, Any]], key: str, default: float) -> float:
        num = 0.0
        den = 0.0
        for x in items:
            w = MODE_COMBAT_WEIGHT.get(str(x.get("mode") or ""), 0.5)
            num += w * float(x.get(key, default) or 0.0)
            den += w
        return (num / den) if den > 0.0 else default

    out: dict[int, dict[str, Any]] = {}
    for pid, team_items in by_pid.items():
        if not team_items:
            continue
        out[pid] = {
            "team_n": len(team_items),
            "avg_ally_elo_gap": round(blend_team_ally_elo_gap(team_items), 1),
            "avg_dmg_vs_ally": round(_wavg(team_items, "dmg_vs_ally", 1.0), 3),
            "avg_kills_vs_ally": round(_wavg(team_items, "kills_vs_ally", 1.0), 3),
            "carried_match_rate": round(_wavg(team_items, "carried_match", 0.0), 3),
        }
    return out


def build_peer_elo_hints_from_combat_rows(
    combat_rows: list[dict[str, Any]],
) -> tuple[dict[int, float], dict[int, int]]:
    """Extrai hints de ELO ajustado e 1v1 dos jogadores BR presentes em `bg_combat_matches`."""
    adj_elos: dict[int, float] = {}
    elo_1v1: dict[int, int] = {}
    for r in combat_rows:
        pid = int(r.get("profile_id") or 0)
        my_elo = float(r.get("my_elo") or 0.0)
        mode = str(r.get("mode") or "").lower()
        if pid <= 0 or my_elo < 500.0 or mode not in MODE_ELO_OFFSET:
            continue
        adj = my_elo + MODE_ELO_OFFSET[mode]
        if adj > adj_elos.get(pid, 0.0):
            adj_elos[pid] = adj
        if mode == "1v1" and int(round(my_elo)) > elo_1v1.get(pid, 0):
            elo_1v1[pid] = int(round(my_elo))
    return adj_elos, elo_1v1


def compute_2v2_carry_profiles(
    arranged_pairs: list[tuple[int, int, int]],
    players_by_pid: dict[int, dict[str, Any]],
    smurf_owner_map: dict[int, int] | None = None,
    peer_adj_elos: dict[int, float] | None = None,
    peer_1v1_elos: dict[int, int] | None = None,
) -> dict[int, dict[str, Any]]:
    """Mede dependência histórica em 2v2 arranjado (parceiros +90 ELO / smurfs de peso-pesados e ganho de WR)."""
    if not arranged_pairs:
        return {}
    smurf_map = smurf_owner_map or {}
    p_adj = peer_adj_elos or {}
    p_1v1 = peer_1v1_elos or {}

    def _true_adj(pid: int) -> float:
        opid = smurf_map.get(pid, pid)
        return max(
            extract_player_best_adj_elo(players_by_pid.get(opid)),
            float(p_adj.get(opid) or 0.0),
        )

    def _best_1v1(pid: int) -> int:
        opid = smurf_map.get(pid, pid)
        return max(
            extract_player_best_1v1(players_by_pid.get(opid)),
            int(p_1v1.get(opid) or 0),
        )

    acc: dict[int, dict[str, Any]] = {}
    for p1, p2, won in arranged_pairs:
        w = 1 if int(won or 0) == 1 else 0
        for me, mate in ((int(p1), int(p2)), (int(p2), int(p1))):
            if me not in players_by_pid:
                continue
            if resolve_smurf_owner_name(me, players_by_pid[me]):
                continue
            s = acc.setdefault(
                me,
                {"tot": 0, "wins": 0, "carry_g": 0, "carry_w": 0, "smurf_g": 0, "gaps": []},
            )
            s["tot"] += 1
            s["wins"] += w
            my_adj = _true_adj(me)
            mate_adj = _true_adj(mate)
            my_1v1 = _best_1v1(me)
            mate_1v1 = _best_1v1(mate)
            gap_adj = (mate_adj - my_adj) if (mate_adj > 0.0 and my_adj > 0.0) else 0.0
            gap_1v1 = (mate_1v1 - my_1v1) if (mate_1v1 > 0 and my_1v1 > 0) else 0.0
            if mate_adj > 0.0 and my_adj > 0.0:
                s["gaps"].append(gap_adj)
            is_smurf_carry = (mate in smurf_map) and (gap_adj >= 60.0 or gap_1v1 >= 120.0)
            is_heavy_carry = (gap_adj >= 90.0) or (gap_1v1 >= 150.0) or is_smurf_carry
            if is_heavy_carry:
                s["carry_g"] += 1
                s["carry_w"] += w
            if is_smurf_carry:
                s["smurf_g"] += 1

    out: dict[int, dict[str, Any]] = {}
    for pid, s in acc.items():
        tot = int(s["tot"])
        if tot < 12:
            continue
        cg = int(s["carry_g"])
        sg = tot - cg
        wr_c = (s["carry_w"] / cg) if cg > 0 else 0.0
        wr_s = ((s["wins"] - s["carry_w"]) / sg) if sg > 0 else 0.0
        out[pid] = {
            "tot_2v2": tot,
            "carry_2v2_share": round(cg / tot, 4),
            "smurf_carry_games": int(s["smurf_g"]),
            "wr_with_carry": round(wr_c, 4),
            "wr_without_carry": round(wr_s, 4),
            "carry_wr_lift": round(wr_c - wr_s, 4) if (cg >= 5 and sg >= 5) else 0.0,
            "avg_2v2_mate_gap": round(sum(s["gaps"]) / len(s["gaps"]), 1) if s["gaps"] else 0.0,
        }
    return out


def compute_carry_adjustment(
    *,
    mode: str,
    eff_elo: float,
    best_1v1_elo: int,
    team_n: int,
    avg_ally_elo_gap: float,
    avg_dmg_vs_ally: float,
    avg_kills_vs_ally: float,
    carried_match_rate: float,
    arr_profile: dict[str, Any] | None,
) -> tuple[float, float, float]:
    """Calcula `(carry_mult, elo_deflation, carry_risk)` compartilhado por Ranking Unificado e Battlegroups."""
    # 1) Fila 1v1 pura nunca sofre penalização de carry
    if str(mode or "").lower() == "1v1":
        return 1.0, 0.0, 0.0

    # 2) Escudo para carregadores comprovados em equipe (aliados bem mais fracos + liderança de dano)
    if team_n >= 4 and avg_ally_elo_gap <= -40.0 and avg_dmg_vs_ally >= 1.12:
        return 1.0, 0.0, 0.0

    # Evidência histórica de carry em 2v2 arranjado (parceiros +90 ELO / smurfs de peso-pesados + ganho de WR)
    arr_sig = 0.0
    if arr_profile and int(arr_profile.get("tot_2v2") or 0) >= 12:
        c_share = float(arr_profile.get("carry_2v2_share") or 0.0)
        smurf_g = int(arr_profile.get("smurf_carry_games") or 0)
        wr_lift = max(0.0, float(arr_profile.get("carry_wr_lift") or 0.0))
        if c_share >= 0.25 or smurf_g >= 12:
            share_f = max(0.0, min(1.0, (c_share - 0.22) / 0.45))
            smurf_f = min(1.0, smurf_g / 25.0)
            lift_f = min(1.0, wr_lift / 0.12)
            arr_sig = min(1.0, 0.55 * share_f + 0.25 * lift_f + 0.20 * smurf_f)
            if float(arr_profile.get("carry_wr_lift") or 0.0) < -0.02 and smurf_g == 0 and avg_ally_elo_gap <= 0.0:
                arr_sig *= 0.45

    # Evidência recente por partida em jogos de equipe (exige aliados de fato mais fortes na média: >= +15 ELO)
    match_sig = 0.0
    if team_n >= 4 and avg_ally_elo_gap >= 15.0 and (avg_ally_elo_gap >= 25.0 or carried_match_rate >= 0.40):
        gap_f = max(0.0, min(1.0, (avg_ally_elo_gap - 20.0) / 140.0))
        rate_f = max(0.0, min(1.0, (carried_match_rate - 0.25) / 0.50))
        contrib_def = max(0.0, min(1.0, (1.02 - 0.55 * avg_dmg_vs_ally - 0.45 * avg_kills_vs_ally) / 0.22))
        match_sig = min(1.0, 0.45 * gap_f + 0.35 * rate_f + 0.20 * contrib_def)

    # 3) Escudo de paridade solo 1v1: quem prova o mesmo nível no 1v1 (<=80 ELO) e sem dependência 2v2 não é carregado
    if best_1v1_elo > 0 and (eff_elo - float(best_1v1_elo)) <= 80.0 and arr_sig == 0.0:
        return 1.0, 0.0, 0.0

    carry_risk = max(arr_sig, match_sig, min(1.0, 0.65 * arr_sig + 0.55 * match_sig))
    if carry_risk < 0.15:
        return 1.0, 0.0, round(carry_risk, 3)

    # Deflação do ELO inflado em equipe em direção à âncora real de 1v1 (quando há divergência comprovada)
    elo_deflation = 0.0
    if best_1v1_elo > 0 and eff_elo > float(best_1v1_elo) + 70.0:
        excess = eff_elo - float(best_1v1_elo)
        elo_deflation = excess * min(0.55, 0.55 * carry_risk)
    elif best_1v1_elo == 0 and carry_risk >= 0.30:
        elo_deflation = 85.0 * carry_risk

    carry_mult = max(0.76, 1.0 - 0.22 * carry_risk)
    return round(carry_mult, 4), round(elo_deflation, 1), round(carry_risk, 3)


def build_shared_anti_carry_context(
    players_by_pid: dict[int, dict[str, Any]],
    combat_rows: list[dict[str, Any]],
    arranged_pairs: list[tuple[int, int, int]],
    extra_peer_adj_elos: dict[int, float] | None = None,
    extra_peer_1v1_elos: dict[int, int] | None = None,
) -> dict[int, dict[str, Any]]:
    """Constrói o contexto Anti-Carry unificado por `profile_id` para o Ranking Unificado de Veteranos.

    Contas Smurf herdam o perfil real do titular (quando resolvido), evitando que uma smurf de um
    carregador real (ex.: geladeira velha -> arc) seja falsamente classificada como carregada por
    começar com ELO baixo.
    """
    combat_adj, combat_1v1 = build_peer_elo_hints_from_combat_rows(combat_rows)
    peer_adj = dict(combat_adj)
    for pid, val in (extra_peer_adj_elos or {}).items():
        if float(val) > peer_adj.get(int(pid), 0.0):
            peer_adj[int(pid)] = float(val)
    peer_1v1 = dict(combat_1v1)
    for pid, val in (extra_peer_1v1_elos or {}).items():
        if int(val) > peer_1v1.get(int(pid), 0):
            peer_1v1[int(pid)] = int(val)

    smurf_owner_map, _ = build_smurf_owner_maps(players_by_pid, peer_adj_elos=peer_adj)
    team_summary = summarize_team_carry_from_rows(combat_rows)
    arr_profiles = compute_2v2_carry_profiles(
        arranged_pairs,
        players_by_pid,
        smurf_owner_map=smurf_owner_map,
        peer_adj_elos=peer_adj,
        peer_1v1_elos=peer_1v1,
    )

    context: dict[int, dict[str, Any]] = {}
    for pid, p in players_by_pid.items():
        ipid = int(pid)
        is_smurf = bool(p.get("is_smurf") or resolve_smurf_owner_name(ipid, p))
        eff_pid = smurf_owner_map.get(ipid, ipid) if is_smurf else ipid
        ts = team_summary.get(eff_pid) or {}
        context[ipid] = {
            "effective_pid": eff_pid,
            "team_n": int(ts.get("team_n") or 0),
            "avg_ally_elo_gap": float(ts.get("avg_ally_elo_gap") or 0.0),
            "avg_dmg_vs_ally": float(ts.get("avg_dmg_vs_ally") or 1.0),
            "avg_kills_vs_ally": float(ts.get("avg_kills_vs_ally") or 1.0),
            "carried_match_rate": float(ts.get("carried_match_rate") or 0.0),
            "arr_profile": arr_profiles.get(eff_pid),
        }
    return context
