"""Elenco dos Veteranos brasileiros monitorados no Ranking Unificado CoH3 Brasil (base única compartilhada)."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from security import (
    build_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    strip_unsafe_chars,
    validate_https_endpoint,
)

logger = logging.getLogger("coh3_ranking.roster")

STEAM_ID64_VALID_RE = re.compile(r"^7656119\d{10}$")
STEAM_URL_EXTRACT_RE = re.compile(r"steamcommunity\.com/profiles/(7656119\d{10})", re.IGNORECASE)
COH3STATS_PLAYER_URL_RE = re.compile(
    r"coh3stats\.com/(?:[a-z]{2}(?:-[a-z]{2,4})?/)?players/(\d{1,12})(?:\D|$)",
    re.IGNORECASE,
)
_COUNTRY_ISO2_RE = re.compile(r"^[a-z]{2}$")
_SMURF_COLON_RE = re.compile(r"^(.+?)\s*\(\s*smurf\s*:\s*(.+?)\s*\)$", re.IGNORECASE)
_SMURF_NUM_RE = re.compile(r"^(.+?)\s*\(\s*smurf(?:\s+\d+)?\s*\)$", re.IGNORECASE)
_SMURF_PAREN_OWNER_RE = re.compile(r"^(.+?)\s*\(\s*([^()]+?)\s*\)$")
_NON_SMURF_PAREN_LABELS: frozenset[str] = frozenset({"principal", "main"})

# Base única compartilhada de jogadores (Radar + Scheduler + Tracker + Ranking)
SHARED_ROSTER_HOSTS: frozenset[str] = frozenset({"raw.githubusercontent.com"})
SHARED_ROSTER_PATH_RE = re.compile(r"^/nigosky/coh3-br-lobby-radar/main/players_roster\.json$")
SHARED_ROSTER_URL = "https://raw.githubusercontent.com/nigosky/coh3-br-lobby-radar/main/players_roster.json"
MAX_SHARED_ROSTER_BYTES = 256 * 1024  # 256 KB teto rígido

# Lista fixa dos Veteranos brasileiros monitorados (contas Smurf contabilizadas separadamente, sem merge)
# Usada como fallback seguro caso o download de players_roster.json falhe temporariamente.
CORE_BR_ROSTER: list[dict[str, Any]] = [
    {"profile_id": 79327, "steam_id": "76561197963172584", "original_alias": "Chacineiro"},
    {
        "profile_id": 1333418,
        "steam_id": "76561198698969876",
        "original_alias": "Charles Bronson (Chacineiro)",
        "pin_alias": True,
        "known_aliases": [
            "Charles Bronson",
            "Lord Charles Bronson",
            "Charles Broson",
            "TacticalForceConcentration",
            "Charles Bronson (Chacineiro)",
        ],
    },
    {"profile_id": 991764, "steam_id": "76561198115923932", "original_alias": "SaNgar [BR]"},
    {"profile_id": 1088455, "steam_id": "76561198108568598", "original_alias": "luiz_fmh"},
    {"profile_id": 735940, "steam_id": "76561198034510845", "original_alias": "Jimmy Duelista"},
    {
        "profile_id": 559962,
        "steam_id": "76561198062871244",
        "original_alias": "Gabe",
        "known_aliases": ["É o Gabe", "Gabe"],
    },
    {"profile_id": 648514, "steam_id": "76561198372757032", "original_alias": "Halokoo"},
    {"profile_id": 35952, "steam_id": "76561198121484554", "original_alias": "Sauronzinho"},
    {"profile_id": 900358, "steam_id": "76561198070545853", "original_alias": "Margheriti"},
    {"profile_id": 691, "steam_id": "76561198001604015", "original_alias": "Vítor"},
    {"profile_id": 5768, "steam_id": "76561198205052385", "original_alias": "The Dog"},
    {"profile_id": 13450, "steam_id": "76561198284234077", "original_alias": "7 DIVISAO PHANTOM"},
    {"profile_id": 944488, "steam_id": "76561198063124280", "original_alias": "gbytes™"},
    {"profile_id": 21206, "steam_id": "76561198061434984", "original_alias": "hjax"},
    {"profile_id": 1114404, "steam_id": "76561198024118058", "original_alias": "Vikitor"},
    {"profile_id": 366826, "steam_id": "76561198014985380", "original_alias": "Lem22", "country": "us"},
    {"profile_id": 1282592, "steam_id": "76561198028644178", "original_alias": "SapoBoi_BR"},
    {"profile_id": 639157, "steam_id": "76561197979883434", "original_alias": "Itaperuna"},
    {"profile_id": 110078, "steam_id": "76561198024839791", "original_alias": "Bineto"},
    {"profile_id": 1143262, "steam_id": "76561198105073246", "original_alias": "Mazzaroppee"},
    {
        "profile_id": 55693,
        "steam_id": "76561197996817129",
        "original_alias": "Alface aka BinhoBiceps",
        "known_aliases": ["BinhoBiceps", "Alface", "Alface aka BinhoBiceps"],
    },
    {"profile_id": 337051, "steam_id": "76561197981615184", "original_alias": "Maiti"},
    {"profile_id": 1022333, "steam_id": "76561198264285149", "original_alias": "gaules"},
    {"profile_id": 32922, "steam_id": "76561197986271242", "original_alias": "Showtaro"},
    {"profile_id": 153969, "steam_id": "76561198005412401", "original_alias": "Cunha"},
    {
        "profile_id": 557890,
        "steam_id": "76561199538846112",
        "original_alias": "gabe44arg (Gabe)",
        "pin_alias": True,
        "known_aliases": ["gabe44arg", "gabe444arg", "gabe44arg (Gabe)"],
    },
    {"profile_id": 26910, "steam_id": "76561197963741792", "original_alias": "nigo_BR ᵀᴹ"},
    {"profile_id": 6557, "steam_id": "76561198135142137", "original_alias": "Dragoness"},
    {"profile_id": 1226910, "steam_id": "76561198140782132", "original_alias": "Lupus Alba"},
    {
        "profile_id": 1005730,
        "steam_id": "76561199791811246",
        "original_alias": "arc",
        "pin_alias": True,
    },
    {
        "profile_id": 1423426,
        "steam_id": "76561199482388323",
        "original_alias": "geladeira velha (arc)",
        "pin_alias": True,
        "known_aliases": ["geladeira velha", "dibas", "uncle hans", "geladeira velha (arc)"],
    },
    {"profile_id": 31145, "steam_id": "76561197985268629", "original_alias": "vonMises"},
    {"profile_id": 58737, "steam_id": "76561197993922195", "original_alias": "INFIEL"},
    {"profile_id": 431802, "steam_id": "76561198000099422", "original_alias": "Conflict.BR"},
    {"profile_id": 59864, "steam_id": "76561198123446420", "original_alias": "Lion Heart"},
    {"profile_id": 84821, "steam_id": "76561198076576055", "original_alias": "TJBR"},
    {"profile_id": 99722, "steam_id": "76561198021071716", "original_alias": "MonteiroBR"},
    {"profile_id": 31061, "steam_id": "76561198009056438", "original_alias": "ACB"},
    {"profile_id": 737235, "steam_id": "76561198026394732", "original_alias": "meuqsaco"},
    {
        "profile_id": 1097608,
        "steam_id": "76561199498421192",
        "original_alias": "Winter Animal",
        "known_aliases": ["Superior Culture", "Winter Animal"],
    },
    {"profile_id": 975688, "steam_id": "76561197994245218", "original_alias": "Kefbr"},
    {
        "profile_id": 1390350,
        "steam_id": "76561198089278659",
        "original_alias": "TearsInTheRain (Chacineiro)",
        "pin_alias": True,
        "known_aliases": ["TearsInTheRain", "TearsInTheRain (Chacineiro)"],
    },
]

FALLBACK_CORE_BR_ROSTER: list[dict[str, Any]] = [dict(row) for row in CORE_BR_ROSTER]

# Apelidos/bandeiras de referência para brasileiros fora da lista de Veteranos (`manual_other_br`),
# usados APENAS para exibição em Times Arranjados 2v2 e Boletim Diário.
# NUNCA entram em CORE_BR_ROSTER nem no Ranking Unificado BR!
MANUAL_OTHER_BR_ALIASES: list[dict[str, Any]] = [
    {
        "profile_id": 33487,
        "steam_id": "76561197995712470",
        "original_alias": "Generick",
        "country": "us",
    },
    {
        "profile_id": 16867,
        "steam_id": "76561198874381986",
        "original_alias": "Golden Cobra",
        "known_aliases": ["COHBRAMACHINE", "Golden Cobra"],
    },
    {"profile_id": 19747, "steam_id": "76561198058548575", "original_alias": "twitch.tv/luck22ai"},
    {
        "profile_id": 23250,
        "steam_id": "76561198028378060",
        "original_alias": "Nargoth",
        "known_aliases": ["Nargoth de Bebbanburg", "Nargoth"],
    },
    {"profile_id": 28937, "steam_id": "76561198089948995", "original_alias": "Mjr Bruno"},
    {"profile_id": 81751, "steam_id": "76561198080023560", "original_alias": "Razi"},
    {"profile_id": 3895, "steam_id": "76561198054318813", "original_alias": "Liquid"},
    {
        "profile_id": 462597,
        "steam_id": "76561198133760614",
        "original_alias": "General Winter (Principal)",
        "pin_alias": True,
        "known_aliases": ["General Winter", "General Winter (Principal)"],
    },
    {
        "profile_id": 1297821,
        "steam_id": "76561198722284920",
        "original_alias": "General Winter (Smurf 1)",
        "pin_alias": True,
        "known_aliases": ["General Winter", "Dick of the Galaxies", "General Winter (Smurf 1)"],
    },
    {
        "profile_id": 1318162,
        "steam_id": "76561198708550887",
        "original_alias": "General Winter (Smurf 2)",
        "pin_alias": True,
        "known_aliases": ["General Winter", ".", "General Winter (Smurf 2)"],
    },
    {
        "profile_id": 509690,
        "steam_id": "76561198053226112",
        "original_alias": "Alekel",
        "pin_alias": True,
        "known_aliases": ["Minhoca de ferro!", "Minhoca de ferro", "Alekel"],
    },
    {
        "profile_id": 1297853,
        "steam_id": "76561198721980631",
        "original_alias": "Alekel (Smurf: Shadow)",
        "pin_alias": True,
        "known_aliases": ["Shadow", "Zé da manga", "Alekel (Smurf: Shadow)"],
    },
    {
        "profile_id": 844225,
        "steam_id": "76561198009000654",
        "original_alias": "『V』SKOBADARK",
        "pin_alias": True,
        "known_aliases": ["『V』SKOBADARK", "SKOBADARK"],
    },
    {
        "profile_id": 1297804,
        "steam_id": "76561198723667226",
        "original_alias": "『V』SKOBADARK (Smurf: Leite Moço)",
        "pin_alias": True,
        "known_aliases": ["Leite Moço", "『V』SKOBADARK (Smurf: Leite Moço)"],
    },
    {
        "profile_id": 164152,
        "steam_id": "76561198029749621",
        "original_alias": "seu pai (Sgt.Yoda)",
        "pin_alias": True,
        "known_aliases": ["seu pai", "Sgt.Yoda", "seu pai (Sgt.Yoda)"],
    },
    {
        "profile_id": 1076221,
        "steam_id": "76561198815935562",
        "original_alias": "Lucielle (Colossus)",
        "pin_alias": True,
        "known_aliases": ["Lucielle", "Lucifer", "Lucielle (Colossus)"],
    },
]

FALLBACK_MANUAL_OTHER_BR_ALIASES: list[dict[str, Any]] = [dict(row) for row in MANUAL_OTHER_BR_ALIASES]


def _normalize_country_code(raw: Any) -> str:
    raw_ctry = str(raw or "").strip().lower()
    if raw_ctry in ("usa", "eua"):
        raw_ctry = "us"
    elif raw_ctry == "bra":
        raw_ctry = "br"
    return raw_ctry if _COUNTRY_ISO2_RE.fullmatch(raw_ctry) else ""


def extract_smurf_owner(
    alias: str,
    *,
    pin_alias: bool = False,
    explicit_owner: str = "",
) -> str:
    """
    Identifica de qual conta original pertence uma conta Smurf segundo a convenção
    de `players_roster.json` (sem merge):
      - `<NomeOriginal> (Smurf: <NomeDaSmurf>)` -> `<NomeOriginal>`
      - `<NomeOriginal> (Smurf 1)` / `(Smurf 2)` -> `<NomeOriginal>`
      - `<NomeDaSmurf> (<NomeOriginal>)` (quando `pin_alias=True`, exceto `(Principal)`) -> `<NomeOriginal>`
    Retorna string vazia `""` se não for uma conta Smurf.
    """
    exp = strip_unsafe_chars(explicit_owner)[:40]
    if exp:
        return exp
    txt = strip_unsafe_chars(alias).strip()
    if not txt:
        return ""
    m_colon = _SMURF_COLON_RE.fullmatch(txt)
    if m_colon:
        return m_colon.group(1).strip()[:40]
    m_num = _SMURF_NUM_RE.fullmatch(txt)
    if m_num:
        return m_num.group(1).strip()[:40]
    if pin_alias:
        m_paren = _SMURF_PAREN_OWNER_RE.fullmatch(txt)
        if m_paren:
            left = m_paren.group(1).strip()
            owner = m_paren.group(2).strip()
            if left and not left.endswith("*") and owner and owner.casefold() not in _NON_SMURF_PAREN_LABELS:
                return owner[:40]
    return ""


# Contas explicitamente reclassificadas como "Outros Brasileiros" (fora dos Veteranos),
# garantindo exclusão imediata do Ranking Unificado mesmo enquanto o `players_roster.json`
# remoto em `coh3-br-lobby-radar` ainda não refletir a movimentação para `manual_other_br`.
EXCLUDED_VETERANO_PIDS: frozenset[int] = frozenset({
    1076221,  # Lucielle (Colossus) — pertence a Outros Brasileiros, nunca foi Veterano
})


def _parse_shared_roster_entry(
    row: dict[str, Any],
    *,
    pid: int,
    sid: str,
    fallback_alias: str = "",
) -> dict[str, Any] | None:
    alias = strip_unsafe_chars(row.get("original_alias") or row.get("alias") or "")[:40] or fallback_alias
    if not alias:
        return None
    entry: dict[str, Any] = {
        "profile_id": pid,
        "steam_id": sid,
        "original_alias": alias,
    }
    raw_ctry = _normalize_country_code(row.get("country") or row.get("flag_country"))
    if raw_ctry and raw_ctry != "br":
        entry["country"] = raw_ctry
    known = [strip_unsafe_chars(k)[:40] for k in (row.get("known_aliases") or []) if strip_unsafe_chars(k)]
    if known:
        entry["known_aliases"] = known
    is_pinned = row.get("pin_alias") is True
    if is_pinned:
        entry["pin_alias"] = True
    smurf_owner = extract_smurf_owner(
        alias,
        pin_alias=is_pinned,
        explicit_owner=str(row.get("smurf_of") or ""),
    )
    if smurf_owner:
        entry["smurf_of"] = smurf_owner
    return entry


def parse_shared_ranking_roster(data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Valida e extrai da base única compartilhada (`players_roster.json`) EXCLUSIVAMENTE os
    Veteranos elegíveis para o Ranking (`veteranos` com `in_ranking != false`), sem merge de Smurfs.
    Ignora propositalmente `manual_other_br` e quaisquer outros brasileiros fora dos Veteranos.
    """
    if not isinstance(data, dict) or int(data.get("schema_version", 0) or 0) < 1:
        raise ValueError("Formato inválido de players_roster.json.")
    raw_vets = data.get("veteranos")
    if not isinstance(raw_vets, list) or not raw_vets:
        raise ValueError("Lista 'veteranos' ausente ou vazia em players_roster.json.")

    parsed_rows: list[dict[str, Any]] = []
    seen_pids: set[int] = set()
    seen_sids: set[str] = set()
    for row in raw_vets:
        if not isinstance(row, dict) or row.get("in_ranking", True) is False:
            continue
        try:
            pid = int(row.get("profile_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if pid in EXCLUDED_VETERANO_PIDS:
            continue
        sid = extract_steam_id(row.get("steam_id"))
        if pid <= 0 or not sid or pid in seen_pids or sid in seen_sids:
            continue
        seen_pids.add(pid)
        seen_sids.add(sid)

        entry = _parse_shared_roster_entry(row, pid=pid, sid=sid, fallback_alias=f"Player {pid}")
        if entry is not None:
            parsed_rows.append(entry)

    if not parsed_rows:
        raise ValueError("Nenhum Veterano elegível encontrado em players_roster.json.")
    return parsed_rows


def parse_shared_manual_other_br_aliases(data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extrai de `players_roster.json` a seção `manual_other_br` exclusivamente para correção
    de apelido/bandeira e aviso de Smurf em Times Arranjados 2v2 e Boletim Diário (NUNCA entra no Ranking Unificado).
    """
    if not isinstance(data, dict) or int(data.get("schema_version", 0) or 0) < 1:
        return []
    raw_manual = data.get("manual_other_br")
    if not isinstance(raw_manual, list):
        raw_manual = []

    vet_pids: set[int] = set()
    vet_sids: set[str] = set()
    reclassified_from_vets: list[dict[str, Any]] = []
    for row in data.get("veteranos") or []:
        if not isinstance(row, dict):
            continue
        try:
            vpid = int(row.get("profile_id", 0) or 0)
        except (TypeError, ValueError):
            vpid = 0
        if vpid in EXCLUDED_VETERANO_PIDS:
            reclassified_from_vets.append(row)
            continue
        vsid = extract_steam_id(row.get("steam_id"))
        if vpid > 0:
            vet_pids.add(vpid)
        if vsid:
            vet_sids.add(vsid)

    parsed_rows: list[dict[str, Any]] = []
    seen_pids: set[int] = set()
    seen_sids: set[str] = set()
    for row in [*raw_manual, *reclassified_from_vets]:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("profile_id", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        sid = extract_steam_id(row.get("steam_id"))
        if pid <= 0 and not sid:
            continue
        if (pid > 0 and (pid in vet_pids or pid in seen_pids)) or (sid and (sid in vet_sids or sid in seen_sids)):
            continue
        if pid > 0:
            seen_pids.add(pid)
        if sid:
            seen_sids.add(sid)

        entry = _parse_shared_roster_entry(row, pid=pid, sid=sid)
        if entry is not None:
            parsed_rows.append(entry)
    return parsed_rows


def _fetch_shared_roster_document(
    url: str = SHARED_ROSTER_URL,
    local_path: Path | None = None,
) -> dict[str, Any] | None:
    """Baixa (ou lê localmente) o JSON bruto validado de `players_roster.json`."""
    if local_path is not None:
        if not local_path.is_file() or local_path.is_symlink() or local_path.stat().st_size > MAX_SHARED_ROSTER_BYTES:
            return None
        try:
            data = json.loads(local_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("Falha ao ler players_roster local (%s): %s", local_path.name, redact_secrets(exc))
            return None

    validate_https_endpoint(
        url,
        SHARED_ROSTER_HOSTS,
        path_prefix="/nigosky/coh3-br-lobby-radar/main/",
        path_regex=SHARED_ROSTER_PATH_RE,
    )
    headers = {
        "User-Agent": "CoH3-BR-Ranking/3.2",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Cache-Control": "no-cache",
    }
    opener = build_no_redirect_https_opener()
    for attempt in range(2):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=8) as resp:
                raw = read_bounded_http_body(resp, max_bytes=MAX_SHARED_ROSTER_BYTES)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as exc:
            if attempt == 0:
                time.sleep(0.5)
            else:
                logger.warning(
                    "Falha transitória ao baixar players_roster.json compartilhado (mantendo fallback): %s",
                    redact_secrets(exc),
                )
    return None


def fetch_shared_ranking_roster(
    url: str = SHARED_ROSTER_URL,
    local_path: Path | None = None,
) -> list[dict[str, Any]] | None:
    """
    Baixa a base compartilhada `players_roster.json` (`nigosky/coh3-br-lobby-radar`) via HTTPS endurecido
    e retorna somente os Veteranos listados para o Ranking. Retorna `None` em falha transitória.
    """
    doc = _fetch_shared_roster_document(url=url, local_path=local_path)
    if doc is None:
        return None
    try:
        return parse_shared_ranking_roster(doc)
    except Exception as exc:
        logger.warning("Falha ao validar players_roster.json compartilhado: %s", redact_secrets(exc))
        return None


def refresh_core_br_roster(
    url: str = SHARED_ROSTER_URL,
    local_path: Path | None = None,
) -> bool:
    """
    Atualiza `CORE_BR_ROSTER` in-place exclusivamente com os Veteranos elegíveis de `players_roster.json`
    (sem merge de Smurfs) e sincroniza `MANUAL_OTHER_BR_ALIASES`.
    """
    doc = _fetch_shared_roster_document(url=url, local_path=local_path)
    if doc is None:
        return False
    try:
        fetched = parse_shared_ranking_roster(doc)
    except Exception as exc:
        logger.warning("Falha ao validar players_roster.json compartilhado: %s", redact_secrets(exc))
        return False

    CORE_BR_ROSTER[:] = fetched
    MANUAL_OTHER_BR_ALIASES[:] = parse_shared_manual_other_br_aliases(doc)
    smurf_cnt = count_smurf_roster_players(fetched)
    logger.info(
        "[ROSTER COMPARTILHADO] Ranking sincronizado (somente Veteranos, sem merge): %d contas (%d Smurfs identificados).",
        len(fetched),
        smurf_cnt,
    )
    return True


def count_unique_roster_players(roster: list[dict[str, Any]] | None = None) -> int:
    src = roster if roster is not None else CORE_BR_ROSTER
    return len(src)


def count_smurf_roster_players(roster: list[dict[str, Any]] | None = None) -> int:
    src = roster if roster is not None else CORE_BR_ROSTER
    return sum(
        1
        for r in src
        if extract_smurf_owner(
            str(r.get("original_alias") or r.get("alias") or ""),
            pin_alias=bool(r.get("pin_alias")),
            explicit_owner=str(r.get("smurf_of") or ""),
        )
    )


def extract_steam_id(raw_name: Any) -> str:
    s = str(raw_name or "").strip()
    if s.startswith("/steam/"):
        s = s[7:].strip()
    return s if STEAM_ID64_VALID_RE.fullmatch(s) else ""


def parse_profile_id_input(raw_input: str) -> int | None:
    """Extrai Relic profile_id numérico (1 a 12 dígitos) puro ou de URL coh3stats.com."""
    txt = (raw_input or "").strip()
    if not txt or STEAM_ID64_VALID_RE.fullmatch(txt) or STEAM_URL_EXTRACT_RE.search(txt):
        return None
    if txt.isdigit() and len(txt) <= 12:
        val = int(txt)
        return val if val > 0 else None
    m = COH3STATS_PLAYER_URL_RE.search(txt)
    if m:
        val = int(m.group(1))
        return val if val > 0 else None
    return None


def parse_steam_id_input(raw_input: str) -> str | None:
    """Extrai SteamID64 de 17 dígitos puro ou de URL https://steamcommunity.com/profiles/7656119..."""
    txt = (raw_input or "").strip().replace("/steam/", "")
    if STEAM_ID64_VALID_RE.fullmatch(txt):
        return txt
    m = STEAM_URL_EXTRACT_RE.search(txt)
    return m.group(1) if m else None


def format_bound_display_alias(
    current_alias: str,
    original_alias: str = "",
    known_aliases: list[str] | tuple[str, ...] | None = None,
    *,
    pin_alias: bool = False,
) -> str:
    """
    Exibe o nome preferencial vinculado à conta Steam ou 'NickAnterior* (NovoNick)' se inédito.
    Quando `pin_alias=True`, mantém sempre `original_alias` fixo sem asterisco (ex.: 'arc').
    """
    cur = (current_alias or "").strip()
    orig = (original_alias or "").strip()
    if not orig:
        return cur or "Jogador"
    if pin_alias:
        return orig
    if not cur:
        return orig

    cur_cf = cur.casefold()
    orig_cf = orig.casefold()
    if cur_cf == orig_cf:
        return orig

    known_cf = {str(k).strip().casefold() for k in (known_aliases or []) if str(k).strip()}
    if cur_cf in known_cf:
        return orig
    for k_cf in known_cf:
        if (
            cur_cf.startswith(k_cf + " (")
            or cur_cf.startswith(k_cf + "* (")
            or cur_cf.endswith(f"({k_cf})")
        ):
            return orig

    if cur_cf.startswith(orig_cf + "* ("):
        return cur
    if cur_cf.startswith(orig_cf + " ("):
        return f"{orig}*{cur[len(orig):]}"
    return f"{orig}* ({cur})"


def _merge_roster_fields(rec: dict[str, Any], prev: dict[str, Any]) -> None:
    for field in (
        "original_alias",
        "steam_id",
        "known_aliases",
        "country",
        "pin_alias",
        "smurf_of",
    ):
        if not rec.get(field) and prev.get(field):
            rec[field] = prev[field]


def _build_lookup_maps_from_sources(
    sources: tuple[list[dict[str, Any]], ...],
    *,
    merge_existing: bool,
) -> tuple[dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    steam_map: dict[str, dict[str, Any]] = {}
    pid_map: dict[int, dict[str, Any]] = {}
    for src in sources:
        for item in src:
            if not isinstance(item, dict):
                continue
            pid = int(item.get("profile_id", 0) or 0)
            sid = extract_steam_id(item.get("steam_id"))
            orig_alias = str(item.get("original_alias") or item.get("alias") or "").strip()
            is_pin = bool(item.get("pin_alias"))
            smurf_owner = extract_smurf_owner(
                orig_alias,
                pin_alias=is_pin,
                explicit_owner=str(item.get("smurf_of") or ""),
            )
            rec: dict[str, Any] = {
                "profile_id": pid,
                "steam_id": sid,
                "original_alias": orig_alias,
                "known_aliases": list(item.get("known_aliases") or []),
                "country": str(item.get("country") or "").strip().lower(),
                "pin_alias": is_pin,
                "smurf_of": smurf_owner,
            }
            if sid:
                if merge_existing and sid in steam_map:
                    _merge_roster_fields(rec, steam_map[sid])
                steam_map[sid] = rec
            if pid > 0:
                if merge_existing and pid in pid_map:
                    _merge_roster_fields(rec, pid_map[pid])
                pid_map[pid] = rec
    return steam_map, pid_map


def build_roster_lookup_maps(
    custom_players: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Cria mapas rápidos por steam_id (primário) e profile_id (secundário) unindo o elenco fixo + extras (sem merge)."""
    return _build_lookup_maps_from_sources((custom_players or [], CORE_BR_ROSTER), merge_existing=True)


def build_manual_other_br_lookup_maps(
    entries: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Cria mapas rápidos para apelidos/bandeiras/Smurf de `manual_other_br` (fora dos Veteranos)."""
    src = entries if entries is not None else MANUAL_OTHER_BR_ALIASES
    return _build_lookup_maps_from_sources((src,), merge_existing=False)


def ensure_core_roster_in_config(
    cfg: dict[str, Any],
    *,
    prune_removed_core: bool = True,
) -> bool:
    """
    Sincroniza `cfg['custom_players']` com os Veteranos de `CORE_BR_ROSTER` (base compartilhada):
    adiciona novos Veteranos, atualiza flags/aliases/smurf_of, remove legados `merged_into_*`
    e remove Veteranos excluídos da base.
    """
    core_pids = {int(c["profile_id"]) for c in CORE_BR_ROSTER if int(c.get("profile_id", 0) or 0) > 0}
    core_sids = {str(c.get("steam_id") or "").strip() for c in CORE_BR_ROSTER if str(c.get("steam_id") or "").strip()}

    raw_custom: list[dict[str, Any]] = list(cfg.get("custom_players") or [])
    custom_players: list[dict[str, Any]] = []
    by_sid: dict[str, dict[str, Any]] = {}
    by_pid: dict[int, dict[str, Any]] = {}
    changed = False

    for cp in raw_custom:
        if not isinstance(cp, dict):
            continue
        sid = extract_steam_id(cp.get("steam_id"))
        pid = int(cp.get("profile_id", 0) or 0)
        if pid in EXCLUDED_VETERANO_PIDS or (
            prune_removed_core
            and bool(cp.get("is_core_roster"))
            and pid not in core_pids
            and (not sid or sid not in core_sids)
        ):
            changed = True
            continue
        entry_cp = dict(cp)
        for legacy_k in ("merged_into_profile_id", "merged_into_steam_id"):
            if legacy_k in entry_cp:
                entry_cp.pop(legacy_k, None)
                changed = True
        custom_players.append(entry_cp)
        if sid:
            by_sid[sid] = entry_cp
        if pid > 0:
            by_pid[pid] = entry_cp

    for core in CORE_BR_ROSTER:
        c_pid = int(core["profile_id"])
        c_sid = str(core["steam_id"])
        c_orig = str(core["original_alias"])
        c_known = list(core.get("known_aliases") or [])
        c_country = str(core.get("country") or "").strip().lower()
        c_pin = bool(core.get("pin_alias"))
        c_smurf = extract_smurf_owner(
            c_orig,
            pin_alias=c_pin,
            explicit_owner=str(core.get("smurf_of") or ""),
        )

        target = by_sid.get(c_sid) or by_pid.get(c_pid)
        if target is None:
            entry: dict[str, Any] = {
                "profile_id": c_pid,
                "steam_id": c_sid,
                "original_alias": c_orig,
                "alias": c_orig,
                "is_core_roster": True,
            }
            if c_country and c_country != "br":
                entry["country"] = c_country
            if c_known:
                entry["known_aliases"] = c_known
            if c_pin:
                entry["pin_alias"] = True
            if c_smurf:
                entry["smurf_of"] = c_smurf
            custom_players.append(entry)
            by_sid[c_sid] = entry
            by_pid[c_pid] = entry
            changed = True
            continue

        if target.get("profile_id") != c_pid:
            target["profile_id"] = c_pid
            changed = True
        if target.get("steam_id") != c_sid:
            target["steam_id"] = c_sid
            changed = True
        if target.get("original_alias") != c_orig:
            target["original_alias"] = c_orig
            changed = True
        desired_alias = format_bound_display_alias(
            str(target.get("alias") or c_orig),
            c_orig,
            c_known,
            pin_alias=c_pin,
        )
        if target.get("alias") != desired_alias:
            target["alias"] = desired_alias
            changed = True
        if not target.get("is_core_roster"):
            target["is_core_roster"] = True
            changed = True
        if c_country and c_country != "br":
            if str(target.get("country") or "").strip().lower() != c_country:
                target["country"] = c_country
                changed = True
        elif "country" in target and str(target.get("country") or "").strip().lower() != "br":
            target.pop("country", None)
            changed = True
        if c_known:
            if list(target.get("known_aliases") or []) != c_known:
                target["known_aliases"] = c_known
                changed = True
        elif "known_aliases" in target:
            target.pop("known_aliases", None)
            changed = True
        if c_pin:
            if target.get("pin_alias") is not True:
                target["pin_alias"] = True
                changed = True
        elif "pin_alias" in target:
            target.pop("pin_alias", None)
            changed = True
        if c_smurf:
            if str(target.get("smurf_of") or "") != c_smurf:
                target["smurf_of"] = c_smurf
                changed = True
        elif "smurf_of" in target:
            target.pop("smurf_of", None)
            changed = True

    if changed:
        cfg["custom_players"] = custom_players
    return changed
