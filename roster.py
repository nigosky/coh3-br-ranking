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

# Registro compartilhado de possíveis/confirmados estrangeiros usando bandeira BR via VPN
SUSPECTED_FOREIGN_PATH_RE = re.compile(r"^/nigosky/coh3-br-lobby-radar/main/suspected_foreign_br\.json$")
SUSPECTED_FOREIGN_URL = "https://raw.githubusercontent.com/nigosky/coh3-br-lobby-radar/main/suspected_foreign_br.json"
MAX_SUSPECTED_FOREIGN_BYTES = 1024 * 1024  # 1 MB teto rígido

# Fallback seguro inicial sincronizado com suspected_foreign_br.json (atualizado dinamicamente via HTTPS)
FALLBACK_EXCLUDED_SUSPECTED_FOREIGN_PIDS: frozenset[int] = frozenset(
    {
        3984,     # 北方军港 (China)
        5563,     # 别和我讲战术 (China)
        5935,     # 冰镇快乐水 (China)
        21011,    # Wywtigerking (China)
        47214,    # KUKU Monkey (China)
        154301,   # Palmarx (Venezuela CoH2)
        310028,   # 小小肥仔 (China)
        416040,   # 大哥大 (Taiwan/China)
        448521,   # 白猫黑猫 (China)
        682793,   # 拉格朗日 (China)
        743575,   # 东条英鸡 (China)
        811294,   # 你说是，那便是吧 (China)
        865785,   # 白头鹰的荣耀 (China)
        944737,   # Red Hot Chilly Pepper (China/Ásia)
        1026771,  # 赛利斯鼠人 (Áustria/China)
        1027727,  # Code002 (China)
        1096783,  # Чимичанги (Rússia)
        1142621,  # Nocturne (83% horário Ásia, 41 aliados CJK/RU, 0 BR)
        1157276,  # Ферстаппен (29 aliados RU/CJK, 0/24 amigos BR)
        1159891,  # GDI (Nara, Japão)
        1187116,  # 2639747972 (China)
        1302582,  # 爱吃鱼的猫 (China)
        1304022,  # 1872508681 (China)
        1312071,  # 东亚病夫 (China)
    }
)
EXCLUDED_SUSPECTED_FOREIGN_PIDS: set[int] = set(FALLBACK_EXCLUDED_SUSPECTED_FOREIGN_PIDS)

# Lista fixa dos Veteranos brasileiros monitorados (contas Smurf contabilizadas separadamente, sem merge)
# Usada como fallback seguro caso o download de players_roster.json falhe temporariamente.
CORE_BR_ROSTER: list[dict[str, Any]] = [
    {"profile_id": 79327, "steam_id": "76561197963172584", "original_alias": "Chacineiro"},
    {
        "profile_id": 1333418,
        "steam_id": "76561198698969876",
        "original_alias": "Charles Bronson (Smurf: Chacineiro)",
        "smurf_of": "Chacineiro",
        "known_aliases": [
            "Charles Bronson",
            "Lord Charles Bronson",
            "Charles Broson",
            "TacticalForceConcentration",
            "Charles Bronson (Chacineiro)",
            "Charles Bronson (Smurf: Chacineiro)",
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
        "original_alias": "gabe44arg (Smurf: Gabe)",
        "smurf_of": "Gabe",
        "known_aliases": ["gabe44arg", "gabe444arg", "gabe44arg (Gabe)", "gabe44arg (Smurf: Gabe)"],
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
        "original_alias": "geladeira velha (Smurf: arc)",
        "smurf_of": "arc",
        "known_aliases": ["geladeira velha", "dibas", "uncle hans", "geladeira velha (arc)", "geladeira velha (Smurf: arc)"],
    },
    {
        "profile_id": 1487738,
        "steam_id": "76561198602236177",
        "original_alias": "Državljan H (Smurf: arc)",
        "smurf_of": "arc",
        "known_aliases": ["Državljan H", "Državljan H (arc)", "Državljan H (Smurf: arc)"],
    },
    {
        "profile_id": 1482961,
        "steam_id": "76561199886227102",
        "original_alias": "Vog (Smurf: arc)",
        "smurf_of": "arc",
        "known_aliases": ["Vog", "Vog (arc)", "Vog (Smurf: arc)"],
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
        "original_alias": "TearsInTheRain (Smurf: Chacineiro)",
        "smurf_of": "Chacineiro",
        "known_aliases": ["TearsInTheRain", "TearsInTheRain (Chacineiro)", "TearsInTheRain (Smurf: Chacineiro)"],
    },
    {"profile_id": 1119784, "steam_id": "76561198000067932", "original_alias": "Tracker"},
]

FALLBACK_CORE_BR_ROSTER: list[dict[str, Any]] = [dict(row) for row in CORE_BR_ROSTER]

# Apelidos/bandeiras de referência para brasileiros fora da lista de Veteranos (`manual_other_br`),
# usados APENAS para exibição em Times Arranjados 2v2, Battlegroups e Boletim Diário.
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
        "original_alias": "General Winter",
    },
    {
        "profile_id": 1297821,
        "steam_id": "76561198722284920",
        "original_alias": "General Winter (Smurf: General Winter)",
        "smurf_of": "General Winter",
        "known_aliases": ["General Winter", "Dick of the Galaxies", "General Winter (Smurf: General Winter)"],
    },
    {
        "profile_id": 1318162,
        "steam_id": "76561198708550887",
        "original_alias": "General Winter (Smurf: General Winter)",
        "smurf_of": "General Winter",
        "known_aliases": ["General Winter", ".", "General Winter (Smurf: General Winter)"],
    },
    {
        "profile_id": 509690,
        "steam_id": "76561198053226112",
        "original_alias": "Alekel",
        "pin_alias": True,
        "known_aliases": [
            "Minhoca de ferro!",
            "Minhoca de ferro",
            "Akael",
            "Don Comerabo!",
            "No mercy",
            "Spectro",
            "Alekel",
        ],
    },
    {
        "profile_id": 1297853,
        "steam_id": "76561198721980631",
        "original_alias": "Shadow (Smurf: Alekel)",
        "smurf_of": "Alekel",
        "known_aliases": [
            "Shadow",
            "Shadowk",
            "Zé da manga",
            "Alekelgames",
            "Alekel (Smurf: Shadow)",
            "Shadow (Smurf: Alekel)",
        ],
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
        "original_alias": "Leite Moço (Smurf: 『V』SKOBADARK)",
        "smurf_of": "『V』SKOBADARK",
        "known_aliases": ["Leite Moço", "『V』SKOBADARK (Smurf: Leite Moço)", "Leite Moço (Smurf: 『V』SKOBADARK)"],
    },
    {
        "profile_id": 164152,
        "steam_id": "76561198029749621",
        "original_alias": "Sgt.Yoda",
        "priority_watch": True,
        "known_aliases": ["Sgt.Yoda", "seu pai", "fiadaputa"],
    },
    {
        "profile_id": 941524,
        "steam_id": "76561198162353268",
        "original_alias": "Colossus",
        "known_aliases": ["Colossus", "̶C̶o̶l̶o̶s̶s̶u̶s̶", "̶H̶e̶c̶t̶o̶r̶", "Hector"],
    },
    {
        "profile_id": 1076221,
        "steam_id": "76561198815935562",
        "original_alias": "Lucielle (Smurf: Colossus)",
        "smurf_of": "Colossus",
        "known_aliases": [
            "Lucielle",
            "Lucifer",
            "Lan",
            "Leonardo Martins Dantas Gomes",
            "Lucielle (Colossus)",
            "Lucielle (Smurf: Colossus)",
        ],
    },
    {
        "profile_id": 40833,
        "steam_id": "76561198216174113",
        "original_alias": "Tokai Teio",
        "known_aliases": [
            "Tokai Teio",
            "Tokai Teio!",
            "Aston Machan",
            "GOTE",
            "GOTEツ",
            "Cabra",
            "ouh mai GOTE",
            "miGOTE",
            "T.M. Opera O",
            "Oguri Cap",
            "Assault Grenadier",
            "Seiun Sky",
            "Silence Suzuka",
            "Nice Nature",
            "Sakura Bakushin O",
        ],
    },
    {"profile_id": 991551, "steam_id": "76561198067968388", "original_alias": "Caveira-."},
    {"profile_id": 667564, "steam_id": "76561198258429491", "original_alias": "Friedhelm Winter"},
    # Brasileiros emigrados confirmados (100% verificados — bandeira do país atual + badge BR EXT)
    {"profile_id": 7448, "steam_id": "76561198002516537", "original_alias": "icewarp", "country": "za"},
    {
        "profile_id": 91827,
        "steam_id": "76561198031697638",
        "original_alias": "[★General★] Apollo",
        "country": "au",
        "known_aliases": ["General Apolo", "[★General★] Apollo"],
    },
    {"profile_id": 312670, "steam_id": "76561197978279848", "original_alias": "TNF", "country": "au"},
    {"profile_id": 263517, "steam_id": "76561197994925919", "original_alias": "Angel of Death", "country": "jp"},
    {
        "profile_id": 26237,
        "steam_id": "76561198036822207",
        "original_alias": "[EL] Queue",
        "country": "us",
        "known_aliases": ["Qpid™", "[EL] Queue"],
    },
    {"profile_id": 304013, "steam_id": "76561197984619024", "original_alias": "Napa", "country": "us"},
    {"profile_id": 76165, "steam_id": "76561197961890785", "original_alias": "1337", "country": "ee"},
    {"profile_id": 1480421, "steam_id": "76561198070841504", "original_alias": "ShadBlack", "country": "au"},
    {"profile_id": 4805, "steam_id": "76561198048811566", "original_alias": "MDCCC", "country": "us"},
    {
        "profile_id": 29737,
        "steam_id": "76561198158934331",
        "original_alias": "PABLO FLEXCOBAR",
        "country": "ar",
        "known_aliases": ["NegroS0rete", "PABLO FLEXCOBAR"],
    },
    {"profile_id": 999940, "steam_id": "76561198074249692", "original_alias": "Warmech11", "country": "py"},
    {"profile_id": 812247, "steam_id": "76561198062044957", "original_alias": "Texugo", "country": "th"},
    {"profile_id": 229162, "steam_id": "76561198089247149", "original_alias": "Sombra", "country": "us"},
    {"profile_id": 212034, "steam_id": "76561198207565088", "original_alias": "XAPA KENTE", "country": "ca"},
    {"profile_id": 110593, "steam_id": "76561198414864681", "original_alias": "Ethan Hunt", "country": "us"},
    {"profile_id": 1304336, "steam_id": "76561198043710924", "original_alias": "V.I.T.O.X", "country": "us"},
    {
        "profile_id": 1168460,
        "steam_id": "76561198012821540",
        "original_alias": "Nonô",
        "country": "ca",
        "known_aliases": ["Killer_BR", "Nonô"],
    },
    {"profile_id": 11033, "steam_id": "76561198009533220", "original_alias": "Coka", "country": "jp"},
    {"profile_id": 24963, "steam_id": "76561197987674520", "original_alias": "Eduardo A.", "country": "sa"},
    {
        "profile_id": 357310,
        "steam_id": "76561197994129168",
        "original_alias": "Sleeping Sheep",
        "country": "br",
        "known_aliases": ["Sleeping Sheep", "Pet Shop", "RLSP", "The World"],
    },
    {
        "profile_id": 17169,
        "steam_id": "76561198066101827",
        "original_alias": "Guh",
        "country": "br",
        "known_aliases": ["Guh", "[KGB] Guh", "StrawHat", "Aoi Todo"],
    },
    {"profile_id": 1178218, "steam_id": "76561199219878733", "original_alias": "joauleonardo", "country": "br"},
    {
        "profile_id": 71299,
        "steam_id": "76561198089205644",
        "original_alias": "Bruno.s",
        "country": "br",
        "known_aliases": ["Bruno.s", "Bruno", "Bruno.s (Busy)", "Bruno114a"],
    },
    {
        "profile_id": 16507,
        "steam_id": "76561198064299508",
        "original_alias": "Honest Copper Dealer",
        "country": "br",
        "known_aliases": ["Honest Copper Dealer", "Honesto Vendedor De Cobre", "Ea-Nasir", "Peixe"],
    },
    {"profile_id": 18414, "steam_id": "76561198027958034", "original_alias": "pipiku", "country": "br"},
    {"profile_id": 869772, "steam_id": "76561198874552921", "original_alias": "Tazi", "country": "br"},
    {"profile_id": 845669, "steam_id": "76561199696199477", "original_alias": "PH0BB05", "country": "br"},
    {
        "profile_id": 2583,
        "steam_id": "76561198142536814",
        "original_alias": "Manupktd",
        "country": "br",
        "known_aliases": ["Manupktd", "Manu PKTD"],
    },
    {
        "profile_id": 661598,
        "steam_id": "76561198066255117",
        "original_alias": "Lord Tywin",
        "country": "br",
        "known_aliases": ["Lord Tywin", "Barao"],
    },
    {"profile_id": 821542, "steam_id": "76561198028624206", "original_alias": "belfort", "country": "br"},
    {
        "profile_id": 674379,
        "steam_id": "76561198353482620",
        "original_alias": "Galva",
        "country": "br",
        "known_aliases": ["Galva", "Galvinha"],
    },
    {
        "profile_id": 13516,
        "steam_id": "76561198000943380",
        "original_alias": "Pax Xavier youtube",
        "country": "br",
        "known_aliases": ["Pax Xavier youtube", "Pax_Nebadon_Br", "Nébadon", "Xavier"],
    },
    {"profile_id": 38270, "steam_id": "76561197984468206", "original_alias": "[CoBrA] Shadow*", "country": "br"},
    {
        "profile_id": 970468,
        "steam_id": "76561198937445340",
        "original_alias": "Московский метро",
        "country": "br",
        "known_aliases": ["Московский метро", "LutsBloom"],
    },
    {"profile_id": 20523, "steam_id": "76561198044964084", "original_alias": "Goa Trance Mix", "country": "br"},
    {
        "profile_id": 940355,
        "steam_id": "76561198018536980",
        "original_alias": "SacanaLord",
        "country": "br",
        "known_aliases": ["SacanaLord", "Fidelis", "Faraco"],
    },
    {
        "profile_id": 1279100,
        "steam_id": "76561198117204290",
        "original_alias": "Tiagod",
        "country": "br",
        "known_aliases": ["Tiagod", "彡 Tiagod 彡"],
    },
    {"profile_id": 1453831, "steam_id": "76561199528441090", "original_alias": "CΛЯИICΣIЯӨ", "country": "br"},
    {"profile_id": 198711, "steam_id": "76561198013841424", "original_alias": "Товарищ Mérlin", "country": "br"},
    {
        "profile_id": 1010793,
        "steam_id": "76561199641123845",
        "original_alias": "拉斐爾",
        "country": "br",
        "known_aliases": ["拉斐爾", "Caldas03", "Rafle"],
    },
    {"profile_id": 28570, "steam_id": "76561198060258379", "original_alias": "Paulo220V", "country": "br"},
    {
        "profile_id": 779143,
        "steam_id": "76561198874288631",
        "original_alias": "Nicolau",
        "country": "br",
        "known_aliases": ["Nicolau", "ニコラス"],
    },
    {
        "profile_id": 952854,
        "steam_id": "76561198131209135",
        "original_alias": "乂卂丨",
        "country": "br",
        "known_aliases": ["乂卂丨", "乂卂丨☁"],
    },
    {"profile_id": 1041470, "steam_id": "76561198125923649", "original_alias": "芋サラダ", "country": "br"},
    {"profile_id": 1216477, "steam_id": "76561199105792679", "original_alias": "FinalGATE-2", "country": "br"},
    {"profile_id": 1216912, "steam_id": "76561198269605156", "original_alias": "[KoFu] Might_lloll", "country": "br"},
    {"profile_id": 1276975, "steam_id": "76561198062002353", "original_alias": "Rozin", "country": "br"},
    {
        "profile_id": 1356223,
        "steam_id": "76561198188738303",
        "original_alias": "Manu-chan",
        "country": "br",
        "known_aliases": ["Manu-chan", "Silencer..."],
    },
    {"profile_id": 1402500, "steam_id": "76561198274006807", "original_alias": "MiiNu <3", "country": "br"},
    {
        "profile_id": 197937,
        "steam_id": "76561198066944603",
        "original_alias": "Armaddin",
        "country": "br",
        "known_aliases": ["Armaddin", "Armadin"],
    },
    {
        "profile_id": 58933,
        "steam_id": "76561198209825195",
        "original_alias": 'Mister "Generic"',
        "country": "br",
        "known_aliases": [
            'Mister "Generic"',
            "General Cris.P.Bacon",
            "SS-Verwaltung von Paris [TNO]",
            "Lieutenant Moe Ron",
        ],
    },
    {
        "profile_id": 11180,
        "steam_id": "76561197982280686",
        "original_alias": "Brujoga",
        "country": "br",
        "known_aliases": ["Brujoga", "Brujoga10"],
    },
    {
        "profile_id": 207342,
        "steam_id": "76561197961608539",
        "original_alias": "AkvaBr",
        "country": "br",
        "known_aliases": ["AkvaBr", "AkvaBr_TTV", "Akva"],
    },
    {"profile_id": 703146, "steam_id": "76561198217753836", "original_alias": "viperking", "country": "br"},
    {
        "profile_id": 110123,
        "steam_id": "76561198003329628",
        "original_alias": "Iracnats",
        "country": "br",
        "known_aliases": ["Iracnats", "Iracnats de Bebbanburg"],
    },
    {
        "profile_id": 864699,
        "steam_id": "76561198037383807",
        "original_alias": "Mael Radec",
        "country": "br",
        "known_aliases": ["Mael Radec", "Radec"],
    },
    {"profile_id": 424813, "steam_id": "76561198163017219", "original_alias": "peace", "country": "br"},
    {"profile_id": 11710, "steam_id": "76561198129851471", "original_alias": "Correia", "country": "br"},
    {
        "profile_id": 4517,
        "steam_id": "76561198033317439",
        "original_alias": "The Collector",
        "country": "br",
        "known_aliases": [
            "The Collector",
            "A Mangueira do Bombeiro",
            "Boi leiteiro",
            "Catador de papelão e latinha",
        ],
    },
    {
        "profile_id": 624624,
        "steam_id": "76561198072467801",
        "original_alias": "Filho Brasil",
        "country": "br",
        "known_aliases": [
            "Filho Brasil",
            "Michelin Brasil",
            "Coronel Jesuino",
            "Redutor de Carbono",
        ],
    },
    {
        "profile_id": 59671,
        "steam_id": "76561198257105445",
        "original_alias": "Gurenn",
        "country": "br",
        "known_aliases": ["Gurenn", "ttv/gurennplays"],
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
    canônica de `players_roster.json` (sem merge):
      - `<NickAtualDaSmurf> (Smurf: <NomeOriginal>)` -> `<NomeOriginal>`
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
        return m_colon.group(2).strip()[:40]
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


def is_excluded_suspected_foreign_pid(
    pid: int,
    *,
    protected_pids: set[int] | frozenset[int] | None = None,
) -> bool:
    """Retorna True se o profile_id estiver bloqueado temporariamente por suspeita/confirmação de VPN estrangeira."""
    try:
        pid_int = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if protected_pids and pid_int in protected_pids:
        return False
    return pid_int in EXCLUDED_SUSPECTED_FOREIGN_PIDS


def parse_suspected_foreign_excluded_pids(data: dict[str, Any]) -> set[int]:
    """Extrai o conjunto de profile_ids bloqueados dos rankings a partir de `suspected_foreign_br.json`."""
    if not isinstance(data, dict):
        raise ValueError("Formato inválido de suspected_foreign_br.json.")
    excluded: set[int] = set()
    raw_list = data.get("excluded_from_rankings_pids")
    if isinstance(raw_list, list):
        for raw_pid in raw_list:
            try:
                pid = int(raw_pid or 0)
            except (TypeError, ValueError):
                continue
            if pid > 0:
                excluded.add(pid)
    players = data.get("players")
    if isinstance(players, dict):
        for raw_k, rec in players.items():
            if not isinstance(rec, dict):
                continue
            try:
                pid = int(rec.get("profile_id") or raw_k or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            status = str(rec.get("status") or "").strip().lower()
            if rec.get("exclude_from_rankings") is True or status in (
                "suspected_foreign_vpn",
                "confirmed_foreign_vpn",
            ):
                excluded.add(pid)
            elif status == "confirmed_br" and rec.get("exclude_from_rankings") is False:
                excluded.discard(pid)
    return excluded


def fetch_suspected_foreign_br_pids(
    url: str = SUSPECTED_FOREIGN_URL,
    local_path: Path | None = None,
) -> set[int] | None:
    """Baixa (ou lê localmente) `suspected_foreign_br.json` via HTTPS endurecido anti-SSRF."""
    if local_path is not None:
        if not local_path.is_file() or local_path.is_symlink() or local_path.stat().st_size > MAX_SUSPECTED_FOREIGN_BYTES:
            return None
        try:
            doc = json.loads(local_path.read_text(encoding="utf-8"))
            return parse_suspected_foreign_excluded_pids(doc)
        except Exception as exc:
            logger.warning("Falha ao ler suspected_foreign_br local (%s): %s", local_path.name, redact_secrets(exc))
            return None

    validate_https_endpoint(
        url,
        SHARED_ROSTER_HOSTS,
        path_prefix="/nigosky/coh3-br-lobby-radar/main/",
        path_regex=SUSPECTED_FOREIGN_PATH_RE,
    )
    headers = {
        "User-Agent": "CoH3-BR-Ranking/3.4",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Cache-Control": "no-cache",
    }
    opener = build_no_redirect_https_opener()
    for attempt in range(2):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=8) as resp:
                raw = read_bounded_http_body(resp, max_bytes=MAX_SUSPECTED_FOREIGN_BYTES)
            doc = json.loads(raw.decode("utf-8"))
            return parse_suspected_foreign_excluded_pids(doc)
        except Exception as exc:
            if attempt == 0:
                time.sleep(0.5)
            else:
                logger.warning(
                    "Falha transitória ao baixar suspected_foreign_br.json (mantendo fallback): %s",
                    redact_secrets(exc),
                )
    return None


def refresh_suspected_foreign_br_pids(
    url: str = SUSPECTED_FOREIGN_URL,
    local_path: Path | None = None,
) -> bool:
    """Atualiza `EXCLUDED_SUSPECTED_FOREIGN_PIDS` in-place a partir de `suspected_foreign_br.json`."""
    fetched = fetch_suspected_foreign_br_pids(url=url, local_path=local_path)
    if fetched is None:
        return False
    EXCLUDED_SUSPECTED_FOREIGN_PIDS.clear()
    EXCLUDED_SUSPECTED_FOREIGN_PIDS.update(fetched)
    logger.info(
        "[FILTRO VPN/ESTRANGEIRO] %d perfil(is) suspeito(s)/confirmado(s) bloqueado(s) nos rankings até verificação.",
        len(EXCLUDED_SUSPECTED_FOREIGN_PIDS),
    )
    return True


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
    Extrai de `players_roster.json` a seção `monitorados` (e legado `manual_other_br`) exclusivamente para
    correção de apelido/bandeira e aviso de Smurf em Times Arranjados 2v2, Battlegroups e Boletim Diário
    (NUNCA entra no Ranking Unificado de Veteranos).
    """
    if not isinstance(data, dict) or int(data.get("schema_version", 0) or 0) < 1:
        return []
    raw_monitorados = data.get("monitorados")
    raw_manual = data.get("manual_other_br")
    combined_manual: list[Any] = []
    if isinstance(raw_monitorados, list):
        combined_manual.extend(raw_monitorados)
    if isinstance(raw_manual, list):
        combined_manual.extend(raw_manual)

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
    for row in [*combined_manual, *reclassified_from_vets]:
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
    (sem merge de Smurfs), sincroniza `MANUAL_OTHER_BR_ALIASES` e atualiza a lista de exclusão
    temporária de possíveis estrangeiros com VPN (`suspected_foreign_br.json`).
    """
    if local_path is None:
        refresh_suspected_foreign_br_pids()
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
