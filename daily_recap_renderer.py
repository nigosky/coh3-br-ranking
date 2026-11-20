"""Renderizador gráfico seguro (Pillow) para o Boletim Diário dos Veteranos e Card do 'Jogo do Dia'."""

from __future__ import annotations

import hashlib
import random
from datetime import datetime
from typing import Any

from PIL import Image, ImageDraw

from daily_recap import format_duration_hms
from ranking_renderer import (
    SteamAvatarProvider,
    _encode_rgb_png,
    get_country_flag,
    get_veteranos_badge_icon,
    pick_font,
    text_width,
    truncate_to_px,
)

# Garante proteção anti-decompression-bomb herdada de ranking_renderer
Image.MAX_IMAGE_PIXELS = 4_000_000


def _format_brt_date_label(iso_date: str) -> str:
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except ValueError:
        return iso_date


def _format_compact_number(val: int) -> str:
    n = int(val)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    x0: int,
    y0: int,
    w: int,
    h: int,
    label: str,
    bg_col: tuple[int, int, int, int],
    border_col: tuple[int, int, int, int],
    text_col: tuple[int, int, int, int],
    font_size: int = 12,
    radius: int = 4,
) -> None:
    draw.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=radius, fill=bg_col, outline=border_col, width=1)
    fnt = pick_font(label, size=font_size, bold=True, condensed=True)
    bbox = draw.textbbox((0, 0), label, font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x0 + (w - tw) / 2 - bbox[0], y0 + (h - th) / 2 - bbox[1]), label, font=fnt, fill=text_col)


def _draw_bg_streaks(draw: ImageDraw.ImageDraw, width: int, height: int, seed_bytes: bytes) -> None:
    rng = random.Random(int.from_bytes(hashlib.sha256(seed_bytes).digest()[:8], "big"))
    for _ in range(10):
        x0, y0 = rng.randint(0, width), rng.randint(0, height)
        x1, y1 = x0 + rng.randint(-160, 160), y0 + rng.randint(-30, 30)
        draw.line([(x0, y0), (x1, y1)], fill=(46, 41, 33, 95), width=1)


def render_daily_recap_table_png(
    recap: dict[str, Any],
    player_rows: list[dict[str, Any]],
    avatar_provider: SteamAvatarProvider,
    subtitle_override: str | None = None,
    include_kpi_header: bool = True,
    include_awards_footer: bool = True,
) -> bytes:
    """Renderiza o Card de Resumo Diário dos Veteranos (1040px de largura)."""
    width = 1040
    banner_h = 40
    kpi_h = 74 if include_kpi_header else 0
    col_hdr_h = 34
    row_h = 38

    awards = recap.get("awards") or {}
    award_cards: list[tuple[str, str, str, tuple[int, int, int, int]]] = []
    if include_awards_footer:
        mvp = awards.get("mvp")
        if mvp:
            delta_s = f"+{mvp['elo_delta']}" if mvp["elo_delta"] > 0 else str(mvp["elo_delta"])
            award_cards.append(
                ("MVP DO DIA", mvp["alias"], f"{delta_s} ELO • {mvp['wins']}V-{mvp['losses']}D", (245, 184, 46, 255))
            )
        mar = awards.get("marathoner")
        if mar:
            award_cards.append(
                (
                    "MARATONISTA",
                    mar["alias"],
                    f"{mar['games']} jogos • {format_duration_hms(mar.get('duration_sec', 0))}",
                    (96, 165, 250, 255),
                )
            )
        dest = awards.get("top_destroyer")
        if dest:
            award_cards.append(
                (
                    "MÁQUINA DE DANO",
                    dest["alias"],
                    f"{_format_compact_number(dest['dmgdone'])} dano • {dest['vkill']} blind.",
                    (248, 113, 113, 255),
                )
            )
        duo = awards.get("hot_duo")
        if duo:
            duo_names = f"{duo['aliases'][0]} + {duo['aliases'][1]}"
            award_cards.append(
                ("DUPLA QUENTE", duo_names, f"{duo['wins']}V-{duo['losses']}D juntos", (74, 222, 128, 255))
            )
        elif awards.get("br_derby"):
            derby = awards["br_derby"]
            a_lbl = derby["allies_vets"][0] if derby.get("allies_vets") else "Aliados"
            x_lbl = derby["axis_vets"][0] if derby.get("axis_vets") else "Eixo"
            award_cards.append(
                ("CLÁSSICO BR", f"{a_lbl} vs {x_lbl}", f"{derby['mode'].upper()} • {derby['map_name']}", (192, 132, 252, 255))
            )

    awards_h = 80 if award_cards else 0
    rows_count = max(1, len(player_rows))
    total_h = banner_h + kpi_h + col_hdr_h + rows_count * row_h + awards_h + 6

    img = Image.new("RGBA", (width, total_h), (15, 17, 21, 255))
    draw = ImageDraw.Draw(img)

    # Banner superior
    draw.rectangle((0, 0, width, banner_h + kpi_h + col_hdr_h), fill=(22, 20, 17, 255))
    _draw_bg_streaks(draw, width, banner_h + kpi_h + col_hdr_h, b"boletim_diario_veteranos")

    date_lbl = _format_brt_date_label(str(recap.get("target_date_brt") or ""))
    sub_txt = subtitle_override or f"Resumo de {date_lbl} (BRT)"
    s_fnt = pick_font(sub_txt, size=14, bold=True)
    sw = text_width(draw, sub_txt, s_fnt)

    title_txt = "COH3 BRASIL  •  BOLETIM DIÁRIO DOS VETERANOS"
    t_fnt = pick_font(title_txt, size=17, bold=True, condensed=True)
    badge = get_veteranos_badge_icon(size=22)
    img.paste(badge, (16, 9), badge)
    draw.text((44, 10), title_txt, font=t_fnt, fill=(245, 184, 46, 255))
    draw.text((width - 20 - sw, 12), sub_txt, font=s_fnt, fill=(198, 192, 180, 255))
    draw.line([(0, banner_h), (width, banner_h)], fill=(48, 44, 36, 255), width=1)

    y = banner_h

    # Faixa de KPIs da Comunidade
    if include_kpi_header:
        summary = recap.get("summary") or {}
        uniq_m = int(summary.get("total_unique_matches", 0))
        act_v = int(summary.get("active_veteranos", 0))
        tw = int(summary.get("total_wins", 0))
        tl = int(summary.get("total_losses", 0))
        owr = int(summary.get("overall_win_rate", 0))
        csec = int(summary.get("total_combat_seconds", 0))
        avg_min = int(round((csec / 60.0) / uniq_m)) if uniq_m > 0 else 0
        net_elo = int(summary.get("net_community_elo", 0))
        net_str = f"+{net_elo} ELO" if net_elo > 0 else f"{net_elo} ELO"
        net_col = (74, 222, 128, 255) if net_elo > 0 else ((248, 113, 113, 255) if net_elo < 0 else (209, 213, 219, 255))

        jogos_lbl = "jogo" if uniq_m == 1 else "jogos"
        vet_lbl = "veterano em campo" if act_v == 1 else "veteranos em campo"
        vit_lbl = "vitória" if tw == 1 else "vitórias"
        der_lbl = "derrota" if tl == 1 else "derrotas"

        kpis = (
            ("PARTIDAS ONTEM", f"{uniq_m} {jogos_lbl}", f"{act_v} {vet_lbl}", (245, 184, 46, 255)),
            ("APROVEITAMENTO BR", f"{owr}%", f"{tw} {vit_lbl} • {tl} {der_lbl}", (74, 222, 128, 255) if owr >= 50 else (248, 113, 113, 255)),
            ("TEMPO EM COMBATE", format_duration_hms(csec), f"média {avg_min} min/partida", (96, 165, 250, 255)),
            ("SALDO COLETIVO ELO", net_str, "variação líquida dos veteranos", net_col),
        )
        card_gap = 12
        card_w = (width - 32 - card_gap * 3) // 4
        ky0 = y + 8
        kh = kpi_h - 16
        lbl_fnt = pick_font("PARTIDAS ONTEM", size=11, bold=True, condensed=True)
        val_fnt = pick_font("14h 25m", size=20, bold=True, condensed=True)
        sub_fnt = pick_font("média 30 min/partida", size=12, bold=False)

        for idx, (k_lbl, k_val, k_sub, accent) in enumerate(kpis):
            kx0 = 16 + idx * (card_w + card_gap)
            draw.rounded_rectangle(
                (kx0, ky0, kx0 + card_w, ky0 + kh),
                radius=6,
                fill=(26, 29, 36, 255),
                outline=(52, 56, 66, 255),
                width=1,
            )
            draw.rectangle((kx0, ky0 + 6, kx0 + 3, ky0 + kh - 6), fill=accent)
            draw.text((kx0 + 12, ky0 + 5), k_lbl, font=lbl_fnt, fill=(168, 174, 186, 255))
            draw.text((kx0 + 12, ky0 + 19), k_val, font=val_fnt, fill=accent)
            draw.text((kx0 + 12, ky0 + 42), k_sub, font=sub_fnt, fill=(176, 182, 194, 255))
        y += kpi_h

    # Cabeçalho da tabela de jogadores
    gold_col = (245, 184, 46, 255)
    ch_y = y + 7
    for lbl, cx, align in (
        ("JOGADOR", 22, "left"),
        ("JOGOS ▾", 395, "center"),
        ("V - D", 485, "center"),
        ("TAXA", 585, "right"),
        ("SALDO ELO", 680, "right"),
        ("MODOS", 712, "left"),
        ("DESTAQUE", 1022, "right"),
    ):
        hdr_fnt = pick_font(lbl, size=14, bold=True, condensed=True)
        lw = text_width(draw, lbl, hdr_fnt)
        tx = (cx - lw / 2) if align == "center" else ((cx - lw) if align == "right" else cx)
        draw.text((tx, ch_y), lbl, font=hdr_fnt, fill=gold_col)

    rule_y = y + col_hdr_h - 1
    draw.line([(10, rule_y), (width - 10, rule_y)], fill=(234, 179, 8, 255), width=2)
    y += col_hdr_h

    if not player_rows:
        msg = "Nenhuma partida PvP ranqueada registrada para os Veteranos neste dia."
        efnt = pick_font(msg, size=15, bold=False)
        ew = text_width(draw, msg, efnt)
        draw.text(((width - ew) / 2, y + 10), msg, font=efnt, fill=(156, 163, 175, 255))
        y += row_h
    else:
        num_fnt = pick_font("12", size=17, bold=True, condensed=True)
        row_badge = get_veteranos_badge_icon(size=18)
        white = (243, 244, 246, 255)
        green = (74, 222, 128, 255)
        red = (248, 113, 113, 255)
        muted = (156, 163, 175, 255)

        mode_pill_styles: dict[str, tuple[tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int]]] = {
            "1v1": ((58, 45, 18, 255), (218, 165, 32, 200), (250, 204, 21, 255)),
            "2v2": ((24, 52, 86, 255), (76, 146, 232, 200), (191, 219, 254, 255)),
            "3v3": ((46, 32, 74, 255), (147, 102, 230, 200), (221, 214, 254, 255)),
            "4v4": ((22, 62, 54, 255), (52, 186, 152, 200), (167, 243, 208, 255)),
        }

        for idx, pl in enumerate(player_rows):
            r_bg = (17, 19, 24, 255) if idx % 2 == 0 else (24, 27, 34, 255)
            draw.rectangle((0, y, width, y + row_h), fill=r_bg)
            draw.line([(10, y + row_h - 1), (width - 10, y + row_h - 1)], fill=(34, 38, 46, 255), width=1)

            pid = int(pl.get("profile_id", 0))
            sid = str(pl.get("steam_id") or "")
            alias_raw = str(pl.get("alias") or f"Jogador {pid}")
            country = str(pl.get("country") or "br").lower()

            av_im = avatar_provider.get_circular_avatar(
                size=28,
                profile_id=pid,
                steam_id=sid,
                alias=alias_raw,
                ring_color=(245, 184, 46, 245),
            )
            img.paste(av_im, (22, y + 5), av_im)

            flag_im = get_country_flag(country, w=20, h=14)
            img.paste(flag_im, (56, y + 12), flag_im)

            disp_alias, a_fnt = truncate_to_px(draw, alias_raw, max_px=212, size=16, bold=True)
            draw.text((84, y + 9), disp_alias, font=a_fnt, fill=(253, 230, 138, 255))
            bx = int(84 + text_width(draw, disp_alias, a_fnt) + 6)
            img.paste(row_badge, (bx, y + 10), row_badge)

            games = int(pl.get("games", 0))
            wins = int(pl.get("wins", 0))
            losses = int(pl.get("losses", 0))
            wr = int(pl.get("win_rate", 0))
            elo_delta = int(pl.get("elo_delta", 0))

            g_str = str(games)
            gw = text_width(draw, g_str, num_fnt)
            draw.text((395 - gw / 2, y + 9), g_str, font=num_fnt, fill=(255, 255, 255, 255))

            # V - D colorido centralizado em x=485
            w_str, sep_str, l_str = f"{wins}V", " - ", f"{losses}D"
            ww = text_width(draw, w_str, num_fnt)
            sw_sep = text_width(draw, sep_str, num_fnt)
            lw_txt = text_width(draw, l_str, num_fnt)
            tot_vd = ww + sw_sep + lw_txt
            vx = 485 - tot_vd / 2
            draw.text((vx, y + 9), w_str, font=num_fnt, fill=green if wins > 0 else muted)
            draw.text((vx + ww, y + 9), sep_str, font=num_fnt, fill=muted)
            draw.text((vx + ww + sw_sep, y + 9), l_str, font=num_fnt, fill=red if losses > 0 else muted)

            wr_str = f"{wr}%"
            wrw = text_width(draw, wr_str, num_fnt)
            draw.text((585 - wrw, y + 9), wr_str, font=num_fnt, fill=white)

            if elo_delta > 0:
                ed_str, ed_col = f"+{elo_delta}", green
            elif elo_delta < 0:
                ed_str, ed_col = f"{elo_delta}", red
            else:
                ed_str, ed_col = "0", muted
            edw = text_width(draw, ed_str, num_fnt)
            draw.text((680 - edw, y + 9), ed_str, font=num_fnt, fill=ed_col)

            # Mini pills de modos jogados
            mx = 712
            for mk in list(pl.get("modes") or [])[:4]:
                p_bg, p_bd, p_tx = mode_pill_styles.get(mk, ((36, 40, 48, 255), (82, 88, 100, 255), white))
                _draw_pill(draw, mx, y + 9, 36, 20, mk.upper(), p_bg, p_bd, p_tx, font_size=11, radius=4)
                mx += 41

            # Tag de destaque à direita
            hl = str(pl.get("highlight") or "").strip()
            if hl:
                if "VENCEU TOP" in hl:
                    h_cols = ((74, 52, 16, 255), (245, 184, 46, 235), (253, 224, 71, 255))
                elif "VS TOP" in hl:
                    h_cols = ((78, 36, 24, 255), (234, 130, 62, 225), (254, 215, 170, 255))
                elif "INVICTO" in hl:
                    h_cols = ((20, 64, 42, 255), (52, 211, 153, 220), (167, 243, 208, 255))
                else:
                    h_cols = ((24, 52, 86, 255), (96, 165, 250, 215), (191, 219, 254, 255))
                hl_fnt = pick_font(hl, size=11, bold=True, condensed=True)
                pw = min(142, max(78, int(text_width(draw, hl, hl_fnt)) + 16))
                _draw_pill(draw, 1022 - pw, y + 9, pw, 20, hl, *h_cols, font_size=11, radius=4)

            y += row_h

    # Rodapé de Prêmios do Dia
    if award_cards:
        draw.rectangle((0, y, width, y + awards_h), fill=(20, 22, 28, 255))
        draw.line([(10, y), (width - 10, y)], fill=(52, 56, 66, 255), width=1)
        n_cards = len(award_cards)
        gap = 12
        aw_w = (width - 32 - gap * (n_cards - 1)) // n_cards
        ay0 = y + 10
        ah = awards_h - 18
        albl_fnt = pick_font("MÁQUINA DE DANO", size=11, bold=True, condensed=True)
        asub_fnt = pick_font("148k dano • 9 blind.", size=12, bold=False)

        for idx, (a_title, a_name, a_sub, a_col) in enumerate(award_cards):
            ax0 = 16 + idx * (aw_w + gap)
            draw.rounded_rectangle(
                (ax0, ay0, ax0 + aw_w, ay0 + ah),
                radius=6,
                fill=(28, 31, 39, 255),
                outline=(58, 62, 74, 255),
                width=1,
            )
            draw.rectangle((ax0, ay0 + 6, ax0 + 3, ay0 + ah - 6), fill=a_col)
            draw.text((ax0 + 12, ay0 + 5), a_title, font=albl_fnt, fill=a_col)
            nm_trunc, nm_fnt = truncate_to_px(draw, a_name, max_px=aw_w - 22, size=15, bold=True)
            draw.text((ax0 + 12, ay0 + 20), nm_trunc, font=nm_fnt, fill=(243, 244, 246, 255))
            sub_trunc, _ = truncate_to_px(draw, a_sub, max_px=aw_w - 22, size=12, bold=False)
            draw.text((ax0 + 12, ay0 + 41), sub_trunc, font=asub_fnt, fill=(176, 182, 194, 255))

    return _encode_rgb_png(img)


def render_match_of_the_day_png(
    match: dict[str, Any],
    target_date_brt: str,
    avatar_provider: SteamAvatarProvider,
) -> bytes:
    """Renderiza o Card Cartaz de Destaque do 'Jogo do Dia' (1040px de largura)."""
    width = 1040
    allies = list(match.get("allies_team") or [])
    axis = list(match.get("axis_team") or [])
    team_rows = max(1, len(allies), len(axis))

    banner_h = 44
    badges_h = 34
    team_hdr_h = 32
    player_row_h = 52
    arena_h = team_hdr_h + team_rows * player_row_h + 14

    has_vp = match.get("vp_allies") is not None and match.get("vp_axis") is not None and (
        int(match.get("vp_allies") or 0) > 0 or int(match.get("vp_axis") or 0) > 0
    )
    bars_count = 3
    stats_h = 34 + bars_count * 26 + 12
    total_h = banner_h + badges_h + arena_h + stats_h + 6

    img = Image.new("RGBA", (width, total_h), (15, 17, 21, 255))
    draw = ImageDraw.Draw(img)

    # Banner Hero
    draw.rectangle((0, 0, width, banner_h + badges_h), fill=(24, 21, 16, 255))
    _draw_bg_streaks(draw, width, banner_h + badges_h, f"motd_{match.get('match_id', 0)}".encode("utf-8"))
    draw.rectangle((0, 0, width, 3), fill=(245, 184, 46, 255))

    badge_im = get_veteranos_badge_icon(size=24)
    img.paste(badge_im, (16, 11), badge_im)

    mode_lbl = str(match.get("mode") or "1v1").upper()
    map_lbl = str(match.get("map_name") or "Mapa")
    dur_lbl = format_duration_hms(int(match.get("duration_sec", 0) or 0))
    date_lbl = _format_brt_date_label(target_date_brt)
    hero_txt = f"JOGO DO DIA  •  {mode_lbl}  •  {map_lbl.upper()}  •  {dur_lbl}"

    score_val = int(match.get("epicness_score", 0))
    score_lbl = f"ÍNDICE ÉPICO {score_val}/100"
    sc_fnt = pick_font(score_lbl, size=13, bold=True, condensed=True)
    sc_w = int(text_width(draw, score_lbl, sc_fnt)) + 20

    hero_trunc, h_fnt = truncate_to_px(draw, hero_txt, max_px=width - 66 - sc_w - 20, size=17, bold=True)
    draw.text((48, 12), hero_trunc, font=h_fnt, fill=(245, 184, 46, 255))
    _draw_pill(
        draw,
        width - 18 - sc_w,
        10,
        sc_w,
        24,
        score_lbl,
        (68, 50, 16, 255),
        (245, 184, 46, 240),
        (253, 224, 71, 255),
        font_size=13,
        radius=5,
    )
    draw.line([(0, banner_h), (width, banner_h)], fill=(54, 48, 36, 255), width=1)

    # Faixa de Badges explicando os critérios do Jogo do Dia
    bx = 16
    by = banner_h + 6
    all_badges = list(match.get("badges") or [])
    avg_elo = int(match.get("avg_match_elo", 0) or 0)
    if avg_elo > 0:
        all_badges.append(f"ELO MÉDIO {avg_elo}")
    all_badges.append(date_lbl)

    for tag in all_badges[:6]:
        u_tag = str(tag).upper()
        if "TOP #" in u_tag or "TOP 5" in u_tag or "#1 UNIFICADO" in u_tag:
            cols = ((74, 52, 16, 255), (245, 184, 46, 235), (253, 224, 71, 255))
        elif "SUFOCO" in u_tag or "VP" in u_tag or "EQUILÍBRIO" in u_tag or "PAREADA" in u_tag:
            cols = ((78, 28, 34, 255), (220, 82, 92, 225), (254, 202, 202, 255))
        elif "UPSET" in u_tag:
            cols = ((20, 66, 44, 255), (52, 211, 153, 225), (167, 243, 208, 255))
        elif "CLÁSSICO" in u_tag or "DUELO BR" in u_tag or "TIME FECHADO" in u_tag or "SQUAD" in u_tag:
            cols = ((52, 32, 82, 255), (167, 118, 245, 220), (233, 213, 255, 255))
        else:
            cols = ((28, 36, 48, 255), (78, 92, 114, 210), (209, 217, 230, 255))

        tfnt = pick_font(u_tag, size=12, bold=True, condensed=True)
        tw = int(text_width(draw, u_tag, tfnt)) + 18
        if bx + tw > width - 16:
            break
        _draw_pill(draw, bx, by, tw, 22, u_tag, *cols, font_size=12, radius=4)
        bx += tw + 8

    rule_y = banner_h + badges_h - 1
    draw.line([(10, rule_y), (width - 10, rule_y)], fill=(234, 179, 8, 255), width=2)

    # Arena 2 Colunas: ALIADOS (esq) vs EIXO (dir)
    arena_y = banner_h + badges_h + 8
    col_w = 482
    left_x = 16
    right_x = width - 16 - col_w
    winner_side = str(match.get("winner_side") or "")

    def _draw_team_box(
        x0: int,
        side_key: str,
        side_label: str,
        players: list[dict[str, Any]],
        hdr_bg: tuple[int, int, int, int],
        border_col: tuple[int, int, int, int],
        accent_txt: tuple[int, int, int, int],
        vp_val: int | None,
    ) -> None:
        is_win = winner_side == side_key
        box_h = team_hdr_h + team_rows * player_row_h
        draw.rounded_rectangle(
            (x0, arena_y, x0 + col_w, arena_y + box_h),
            radius=7,
            fill=(20, 23, 30, 255),
            outline=border_col if is_win else (46, 52, 64, 255),
            width=2 if is_win else 1,
        )
        draw.rounded_rectangle((x0 + 1, arena_y + 1, x0 + col_w - 1, arena_y + team_hdr_h), radius=6, fill=hdr_bg)
        sh_fnt = pick_font(side_label, size=15, bold=True, condensed=True)
        draw.text((x0 + 12, arena_y + 7), side_label, font=sh_fnt, fill=accent_txt)

        rx = x0 + col_w - 10
        if is_win:
            _draw_pill(draw, rx - 66, arena_y + 6, 66, 20, "VITÓRIA", (74, 54, 16, 255), (245, 184, 46, 240), (253, 224, 71, 255), font_size=11)
            rx -= 72
        if vp_val is not None and has_vp:
            vp_s = f"{vp_val} VP"
            vp_w = 54
            _draw_pill(draw, rx - vp_w, arena_y + 6, vp_w, 20, vp_s, (18, 22, 30, 255), (110, 120, 140, 210), (243, 244, 246, 255), font_size=11)

        vet_badge_sm = get_veteranos_badge_icon(size=17)
        meta_fnt = pick_font("ELO 1614 (+21) • Dano 54.2k • 4 blind.", size=12, bold=False)

        for idx, pl in enumerate(players):
            py = arena_y + team_hdr_h + idx * player_row_h
            is_vet = bool(pl.get("is_veterano"))
            if is_vet:
                draw.rectangle((x0 + 2, py + 1, x0 + col_w - 2, py + player_row_h - 1), fill=(36, 30, 19, 255))
                draw.rectangle((x0 + 2, py + 1, x0 + 5, py + player_row_h - 1), fill=(245, 184, 46, 245))
            if idx < len(players) - 1:
                draw.line([(x0 + 10, py + player_row_h - 1), (x0 + col_w - 10, py + player_row_h - 1)], fill=(38, 43, 54, 255), width=1)

            pid = int(pl.get("profile_id", 0))
            sid = str(pl.get("steam_id") or "")
            alias_raw = str(pl.get("alias") or f"Jogador {pid}")
            country = str(pl.get("country") or "un").lower()
            fac_short = str(pl.get("faction_short") or "???")

            ring = (245, 184, 46, 245) if is_vet else ((76, 146, 232, 220) if side_key == "allies" else (210, 82, 92, 220))
            av_im = avatar_provider.get_circular_avatar(size=34, profile_id=pid, steam_id=sid, alias=alias_raw, ring_color=ring)
            img.paste(av_im, (x0 + 12, py + 9), av_im)

            flag_im = get_country_flag(country, w=18, h=13)
            img.paste(flag_im, (x0 + 52, py + 10), flag_im)

            # Selo da facção
            f_bg = (24, 52, 86, 255) if side_key == "allies" else (82, 28, 32, 255)
            f_bd = (76, 146, 232, 210) if side_key == "allies" else (196, 76, 84, 210)
            f_tx = (191, 219, 254, 255) if side_key == "allies" else (254, 202, 202, 255)
            _draw_pill(draw, x0 + 75, py + 7, 42, 18, fac_short, f_bg, f_bd, f_tx, font_size=11, radius=3)

            # Tag Top Global (Top 20 Relic & >1800 ELO) se aplicável
            g_rank = pl.get("global_rank")
            is_top_g = pl.get("is_top_global")
            show_top_pill = (
                bool(is_top_g) and g_rank is not None
                if is_top_g is not None
                else (g_rank is not None and int(g_rank) <= 20)
            )
            top_tag_w = 0
            if show_top_pill:
                t_lbl = f"TOP #{int(g_rank)}"
                top_tag_w = 58
                _draw_pill(
                    draw,
                    x0 + col_w - 12 - top_tag_w,
                    py + 7,
                    top_tag_w,
                    18,
                    t_lbl,
                    (74, 52, 16, 255),
                    (245, 184, 46, 240),
                    (253, 224, 71, 255),
                    font_size=11,
                    radius=3,
                )

            name_x = x0 + 123
            avail_name_px = (x0 + col_w - 16 - (top_tag_w + 8 if top_tag_w else 0) - (24 if is_vet else 0)) - name_x
            disp_nm, nm_fnt = truncate_to_px(draw, alias_raw, max_px=max(90, avail_name_px), size=15, bold=True)
            draw.text((name_x, py + 7), disp_nm, font=nm_fnt, fill=(253, 230, 138, 255) if is_vet else (243, 244, 246, 255))
            if is_vet:
                nbx = int(name_x + text_width(draw, disp_nm, nm_fnt) + 6)
                img.paste(vet_badge_sm, (nbx, py + 8), vet_badge_sm)

            # Linha 2: ELO pré-jogo + delta + dano/blindados
            old_elo = int(pl.get("old_elo", 0) or 0)
            delta = int(pl.get("elo_delta", 0) or 0)
            elo_part = f"ELO {old_elo}" if old_elo > 0 else "ELO N/D"
            if delta != 0:
                elo_part += f" ({delta:+d})"
            dmg = int(pl.get("dmgdone", 0) or 0)
            vk = int(pl.get("vkill", 0) or 0)
            sqk = int(pl.get("sqkill", 0) or 0)
            stat_parts = [elo_part]
            if dmg > 0:
                stat_parts.append(f"Dano {_format_compact_number(dmg)}")
            if vk > 0 or sqk > 0:
                stat_parts.append(f"{vk} blind. • {sqk} esq.")
            meta_str = "  •  ".join(stat_parts)
            meta_trunc, _ = truncate_to_px(draw, meta_str, max_px=col_w - 68, size=12, bold=False)
            draw.text((x0 + 52, py + 29), meta_trunc, font=meta_fnt, fill=(176, 184, 198, 255))

    _draw_team_box(
        left_x,
        "allies",
        "ALIADOS",
        allies,
        (22, 46, 78, 255),
        (76, 146, 232, 230),
        (191, 219, 254, 255),
        match.get("vp_allies"),
    )
    _draw_team_box(
        right_x,
        "axis",
        "EIXO",
        axis,
        (76, 24, 28, 255),
        (210, 78, 86, 230),
        (254, 202, 202, 255),
        match.get("vp_axis"),
    )

    # Medalhão central VS
    vs_cy = arena_y + team_hdr_h + (team_rows * player_row_h) // 2
    draw.ellipse((width // 2 - 16, vs_cy - 16, width // 2 + 16, vs_cy + 16), fill=(26, 24, 20, 255), outline=(245, 184, 46, 235), width=2)
    vs_fnt = pick_font("VS", size=13, bold=True, condensed=True)
    vbb = draw.textbbox((0, 0), "VS", font=vs_fnt)
    draw.text((width // 2 - (vbb[2] - vbb[0]) / 2 - vbb[0], vs_cy - (vbb[3] - vbb[1]) / 2 - vbb[1]), "VS", font=vs_fnt, fill=(245, 184, 46, 255))

    # Seção inferior: Barras comparativas de combate (Aliados vs Eixo)
    stats_y = arena_y + team_hdr_h + team_rows * player_row_h + 12
    draw.rounded_rectangle((16, stats_y, width - 16, total_h - 8), radius=7, fill=(20, 23, 30, 255), outline=(46, 52, 64, 255), width=1)
    sh2_fnt = pick_font("ESTATÍSTICAS DE COMBATE  •  ALIADOS vs EIXO", size=13, bold=True, condensed=True)
    draw.text((28, stats_y + 8), "ESTATÍSTICAS DE COMBATE  •  ALIADOS vs EIXO", font=sh2_fnt, fill=(245, 184, 46, 255))

    t_stats = match.get("team_stats") or {}
    a_st = t_stats.get("allies") or {}
    x_st = t_stats.get("axis") or {}

    bar_rows: list[tuple[str, int, int, str, str]] = [
        ("DANO TOTAL", int(a_st.get("dmgdone", 0)), int(x_st.get("dmgdone", 0)), _format_compact_number(a_st.get("dmgdone", 0)), _format_compact_number(x_st.get("dmgdone", 0))),
        ("BLINDADOS ABATIDOS", int(a_st.get("vkill", 0)), int(x_st.get("vkill", 0)), str(int(a_st.get("vkill", 0))), str(int(x_st.get("vkill", 0)))),
        ("ESQUADRÕES ABATIDOS", int(a_st.get("sqkill", 0)), int(x_st.get("sqkill", 0)), str(int(a_st.get("sqkill", 0))), str(int(x_st.get("sqkill", 0)))),
    ]

    bar_lbl_fnt = pick_font("ESQUADRÕES ABATIDOS", size=12, bold=True, condensed=True)
    bar_val_fnt = pick_font("142.8k", size=13, bold=True, condensed=True)

    by_cur = stats_y + 32
    bar_x0 = 260
    bar_x1 = width - 110
    bar_w = bar_x1 - bar_x0

    for b_lbl, v_allies, v_axis, txt_a, txt_x in bar_rows:
        draw.text((28, by_cur + 2), b_lbl, font=bar_lbl_fnt, fill=(186, 192, 204, 255))

        wa = text_width(draw, txt_a, bar_val_fnt)
        draw.text((bar_x0 - 12 - wa, by_cur + 1), txt_a, font=bar_val_fnt, fill=(147, 197, 253, 255))
        draw.text((bar_x1 + 12, by_cur + 1), txt_x, font=bar_val_fnt, fill=(252, 165, 165, 255))

        tot = v_allies + v_axis
        ratio_a = (v_allies / tot) if tot > 0 else 0.5
        split_x = bar_x0 + int(round(bar_w * ratio_a))

        draw.rounded_rectangle((bar_x0, by_cur + 3, bar_x1, by_cur + 17), radius=4, fill=(34, 38, 48, 255))
        if split_x > bar_x0 + 2:
            draw.rounded_rectangle((bar_x0, by_cur + 3, min(bar_x1, split_x), by_cur + 17), radius=4, fill=(52, 118, 206, 255))
        if split_x < bar_x1 - 2:
            draw.rounded_rectangle((max(bar_x0, split_x), by_cur + 3, bar_x1, by_cur + 17), radius=4, fill=(188, 58, 66, 255))
        # Marcador central de 50%
        mid_x = bar_x0 + bar_w // 2
        draw.line([(mid_x, by_cur + 1), (mid_x, by_cur + 19)], fill=(230, 232, 238, 150), width=1)
        by_cur += 26

    return _encode_rgb_png(img)
