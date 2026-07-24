"""
Busca o leaderboard de blitz do Chess.com, o histórico recente de partidas
dos jogadores no topo, e também do jogador-alvo (TARGET_USERNAME) — e gera
docs/data.json para a página estática consumir.

Além dos padrões de horário e aberturas, calcula um "score de sobreposição"
entre a rotina do jogador-alvo e a de cada um dos top jogadores: o quanto os
horários em que cada um costuma jogar coincidem com os horários em que o
jogador-alvo costuma jogar.

Roda server-side (via GitHub Actions ou localmente) - aqui não existe CORS,
então as chamadas à API do Chess.com funcionam de forma direta e confiável.

Horários são convertidos direto para Horário de Brasília (UTC-3, fixo - o
Brasil não observa mais horário de verão desde 2019, então essa conversão
é exata, sem aproximação).
"""

import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# --- Configuração -----------------------------------------------------------
PLAYER_COUNT = 50            # quantos jogadores do topo do ranking analisar (máx. 50)
MONTHS_BACK = 1               # quantos meses de arquivo de partidas buscar por jogador
TARGET_USERNAME = "LPSupi"    # jogador para o qual calculamos a sobreposição de rotina
TARGET_MONTHS_BACK = 3         # meses de histórico do jogador-alvo (mais meses = rotina mais confiável)
MAX_WORKERS = 6                # requisições em paralelo
TOP_OPENINGS = 8               # quantas aberturas guardar em cada ranking
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "data.json"

# A API do Chess.com pede um User-Agent identificável nas requisições.
# Troque o e-mail abaixo pelo seu.
HEADERS = {
    "User-Agent": "blitz-patterns-dashboard/1.0 (contato: seu-email@exemplo.com)"
}

# Time control alvo: "180" = 3 minutos sem incremento (3+0).
# Troque para None se quiser voltar a pegar todo tipo de blitz (3 a 10 min, com ou sem incremento).
TIME_CONTROL_FILTER = "180"

BRT_OFFSET = -3  # Horário de Brasília, fixo

RECENT_FORM_SIZE = 5  # quantas partidas recentes mostrar como "forma" (W/D/L)

# Resultados do Chess.com que contam como empate; qualquer coisa que não seja
# "win" nem esteja nessa lista conta como derrota para aquele lado.
DRAW_RESULTS = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}


def get_json(url, attempts=3):
    last_err = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.json()
            last_err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"Falha ao buscar {url}: {last_err}")


def country_code_from_url(url):
    if not url:
        return None
    return url.rstrip("/").split("/")[-1]


def opening_name_from_eco(eco_url):
    """Deriva um nome de abertura legível a partir da URL 'eco' do Chess.com.
    Ex.: '.../Sicilian-Defense-Najdorf-Variation-6.Be2' -> 'Sicilian Defense Najdorf Variation'
    É uma heurística (corta no primeiro token com '.', que costuma marcar um lance)."""
    if not eco_url:
        return None
    slug = eco_url.rstrip("/").split("/")[-1]
    tokens = slug.split("-")
    name_tokens = []
    for tok in tokens:
        if "." in tok:
            break
        name_tokens.append(tok)
    name = " ".join(name_tokens) if name_tokens else slug.replace("-", " ")
    return name.strip() or None


def outcome_letter(result_raw):
    if result_raw == "win":
        return "W"
    if result_raw in DRAW_RESULTS:
        return "D"
    return "L"


def fetch_player_games(username, months_back):
    now = datetime.now(timezone.utc)
    collected = []
    for i in range(months_back):
        month = now.month - i
        year = now.year
        while month <= 0:
            month += 12
            year -= 1
        url = f"https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}"
        try:
            data = get_json(url, attempts=2)
        except RuntimeError:
            continue
        for g in data.get("games", []):
            if (
                g.get("time_class") == "blitz"
                and g.get("end_time")
                and (TIME_CONTROL_FILTER is None or g.get("time_control") == TIME_CONTROL_FILTER)
            ):
                collected.append(g)
    return collected


def brt_hour(utc_hour):
    return int((utc_hour + BRT_OFFSET) % 24)


def top_counter(counter, n):
    return [{"name": name, "count": count} for name, count in counter.most_common(n)]


def empty_day_hour():
    return [[0] * 24 for _ in range(7)]


def best_windows(day_hour_matrix, total, n=3):
    """Retorna os N melhores blocos dia+hora (maior % do total de partidas do jogador)."""
    cells = []
    for day in range(7):
        for hour in range(24):
            count = day_hour_matrix[day][hour]
            if count > 0:
                cells.append((day, hour, count))
    cells.sort(key=lambda c: c[2], reverse=True)
    out = []
    for day, hour, count in cells[:n]:
        out.append({
            "day": day,
            "hour": hour,
            "count": count,
            "pct": round(100 * count / total, 1) if total else 0,
        })
    return out


def analyze_games(username, games):
    """Processa a lista de partidas de um jogador e retorna suas métricas
    (distribuição por hora/dia, aberturas, janelas de pico, forma recente)."""
    uname_lower = username.lower()
    hours_brt = [0] * 24
    day_hour_brt = empty_day_hour()
    openings = Counter()
    results_log = []  # [(end_time, "W"/"D"/"L"), ...] só das partidas do próprio jogador

    for g in games:
        dt = datetime.fromtimestamp(g["end_time"], tz=timezone.utc)
        h_brt = brt_hour(dt.hour)
        day = (dt.weekday() + 1) % 7  # 0 = domingo

        hours_brt[h_brt] += 1
        day_hour_brt[day][h_brt] += 1

        white = g.get("white", {})
        black = g.get("black", {})
        white_user = (white.get("username") or "").lower()
        black_user = (black.get("username") or "").lower()

        if uname_lower == white_user:
            result_raw = white.get("result")
        elif uname_lower == black_user:
            result_raw = black.get("result")
        else:
            result_raw = None

        if result_raw is not None:
            opening = opening_name_from_eco(g.get("eco"))
            if opening:
                openings[opening] += 1
            results_log.append((g["end_time"], outcome_letter(result_raw)))

    total = sum(hours_brt)
    peak_brt = max(range(24), key=lambda h: hours_brt[h]) if total else None

    results_log.sort(key=lambda r: r[0])
    recent_form = [outcome for _, outcome in results_log[-RECENT_FORM_SIZE:]]

    return {
        "hoursBrt": hours_brt,
        "dayHourBrt": day_hour_brt,
        "openings": openings,
        "total": total,
        "peakHourBrt": peak_brt,
        "bestWindows": best_windows(day_hour_brt, total, n=3),
        "recentForm": recent_form,
    }


def overlap_score(target_day_hour, target_total, player_day_hour, player_total):
    """% das partidas do jogador que aconteceram em algum bloco dia+hora em que
    o jogador-alvo já registrou pelo menos uma partida no período analisado.
    Ex.: 40% significa que 40% das partidas desse jogador caem em horários
    onde o alvo historicamente também costuma estar jogando."""
    if not target_total or not player_total:
        return 0.0
    active_cells = sum(
        player_day_hour[day][hour]
        for day in range(7)
        for hour in range(24)
        if target_day_hour[day][hour] > 0
    )
    return 100 * active_cells / player_total


def main():
    print("Buscando leaderboard de blitz...")
    lb = get_json("https://api.chess.com/pub/leaderboards")
    entries = lb.get("live_blitz", [])[:PLAYER_COUNT]

    players = []
    for e in entries:
        players.append({
            "username": e["username"],
            "rank": e["rank"],
            "rating": e["score"],
            "countryCode": country_code_from_url(e.get("country")),
        })

    hourly_brt = [0] * 24
    day_hour_brt = empty_day_hour()
    openings_overall = Counter()
    total_games = 0
    skipped = 0

    print(f"Buscando rotina do jogador-alvo ({TARGET_USERNAME})...")
    target_games = fetch_player_games(TARGET_USERNAME, TARGET_MONTHS_BACK)
    target_analysis = analyze_games(TARGET_USERNAME, target_games)
    print(f"  {TARGET_USERNAME}: {target_analysis['total']} partidas de blitz 3+0 encontradas")

    print(f"Buscando partidas de {len(players)} jogadores ({MONTHS_BACK} mes(es))...")
    per_player_data = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {
            pool.submit(fetch_player_games, p["username"], MONTHS_BACK): p
            for p in players
        }
        for i, future in enumerate(as_completed(future_map), 1):
            p = future_map[future]
            try:
                games = future.result()
            except Exception as exc:
                print(f"  Aviso: falha em {p['username']}: {exc}")
                games = []
            if not games:
                skipped += 1

            analysis = analyze_games(p["username"], games)
            per_player_data[p["username"]] = analysis

            for hour in range(24):
                hourly_brt[hour] += analysis["hoursBrt"][hour]
            for day in range(7):
                for hour in range(24):
                    day_hour_brt[day][hour] += analysis["dayHourBrt"][day][hour]
            for name, count in analysis["openings"].items():
                openings_overall[name] += count
            total_games += analysis["total"]

            print(f"  [{i}/{len(players)}] {p['username']}: {len(games)} partidas de blitz")

    player_rows = []
    for p in players:
        d = per_player_data.get(p["username"])
        if not d or not d["total"]:
            player_rows.append({
                **p, "total": 0, "peakHourBrt": None,
                "dayHourBrt": empty_day_hour(), "openings": [], "bestWindows": [],
                "overlapScore": 0.0, "recentForm": [],
            })
            continue

        score = overlap_score(
            target_analysis["dayHourBrt"], target_analysis["total"],
            d["dayHourBrt"], d["total"],
        )

        player_rows.append({
            **p,
            "total": d["total"],
            "peakHourBrt": d["peakHourBrt"],
            "dayHourBrt": d["dayHourBrt"],
            "openings": top_counter(d["openings"], TOP_OPENINGS),
            "bestWindows": d["bestWindows"],
            "overlapScore": round(score, 2),  # % das partidas do jogador em janelas ativas do alvo
            "recentForm": d["recentForm"],
        })

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "playerCount": PLAYER_COUNT,
        "monthsBack": MONTHS_BACK,
        "totalGames": total_games,
        "skipped": skipped,
        "players": player_rows,
        "hourlyBrt": hourly_brt,
        "dayHourBrt": day_hour_brt,
        "openingsOverall": top_counter(openings_overall, TOP_OPENINGS * 2),
        "targetPlayer": {
            "username": TARGET_USERNAME,
            "monthsBack": TARGET_MONTHS_BACK,
            "total": target_analysis["total"],
            "hoursBrt": target_analysis["hoursBrt"],
            "dayHourBrt": target_analysis["dayHourBrt"],
            "peakHourBrt": target_analysis["peakHourBrt"],
            "bestWindows": target_analysis["bestWindows"],
            "openings": top_counter(target_analysis["openings"], TOP_OPENINGS),
            "recentForm": target_analysis["recentForm"],
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Dados salvos em {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
