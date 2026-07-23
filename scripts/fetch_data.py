"""
Busca o leaderboard de blitz do Chess.com e o histórico recente de partidas
dos jogadores no topo, e gera docs/data.json para a página estática consumir.

Roda server-side (via GitHub Actions ou localmente) - aqui não existe CORS,
então as chamadas à API do Chess.com funcionam de forma direta e confiável.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# --- Configuração -----------------------------------------------------------
PLAYER_COUNT = 25          # quantos jogadores do topo do ranking analisar (máx. 50)
MONTHS_BACK = 1            # quantos meses de arquivo de partidas buscar por jogador
MAX_WORKERS = 6            # requisições em paralelo
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "data.json"

# A API do Chess.com pede um User-Agent identificável nas requisições.
# Troque o e-mail abaixo pelo seu.
HEADERS = {
    "User-Agent": "blitz-patterns-dashboard/1.0 (contato: seu-email@exemplo.com)"
}

# Aproximação de fuso horário por país (offset UTC, sem horário de verão).
# Países ausentes caem em 0 (UTC). É uma estimativa, não um valor exato.
COUNTRY_OFFSET = {
    "US": -5, "CA": -5, "MX": -6, "BR": -3, "AR": -3, "CO": -5, "PE": -5, "CL": -4, "VE": -4,
    "EC": -5, "CU": -5, "DO": -4, "PR": -4,
    "GB": 0, "IE": 0, "PT": 0, "GH": 0,
    "DE": 1, "FR": 1, "ES": 1, "IT": 1, "NL": 1, "BE": 1, "CH": 1, "AT": 1, "DK": 1, "NO": 1,
    "SE": 1, "PL": 1, "RS": 1, "HU": 1, "CZ": 1, "SK": 1, "SI": 1, "HR": 1, "MA": 1, "DZ": 1, "TN": 1, "NG": 1,
    "UA": 2, "RO": 2, "GR": 2, "EG": 2, "ZA": 2, "FI": 2, "BG": 2, "LT": 2, "LV": 2, "EE": 2, "MD": 2, "IL": 2,
    "TR": 3, "RU": 3, "IR": 3.5, "SA": 3, "KE": 3, "BY": 3,
    "AE": 4, "AZ": 4, "AM": 4, "GE": 4,
    "IN": 5.5, "PK": 5, "KZ": 5, "UZ": 5,
    "BD": 6,
    "TH": 7, "VN": 7, "ID": 7,
    "CN": 8, "MY": 8, "SG": 8, "TW": 8, "HK": 8, "PH": 8,
    "JP": 9, "KR": 9,
    "AU": 10,
    "NZ": 12,
}


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
            if g.get("time_class") == "blitz" and g.get("end_time"):
                collected.append(g["end_time"])
    return collected


def local_hour(end_time, country_code):
    utc_hour = datetime.fromtimestamp(end_time, tz=timezone.utc).hour
    offset = COUNTRY_OFFSET.get(country_code, 0)
    return int((utc_hour + offset) % 24)


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

    hourly_utc = [0] * 24
    hourly_local = [0] * 24
    day_hour_utc = [[0] * 24 for _ in range(7)]
    day_hour_local = [[0] * 24 for _ in range(7)]
    total_games = 0
    skipped = 0

    print(f"Buscando partidas de {len(players)} jogadores ({MONTHS_BACK} mes(es))...")
    per_player_hours = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {
            pool.submit(fetch_player_games, p["username"], MONTHS_BACK): p
            for p in players
        }
        for i, future in enumerate(as_completed(future_map), 1):
            p = future_map[future]
            try:
                end_times = future.result()
            except Exception as exc:
                print(f"  Aviso: falha em {p['username']}: {exc}")
                end_times = []
            if not end_times:
                skipped += 1

            hours_utc = [0] * 24
            hours_local = [0] * 24
            for et in end_times:
                dt = datetime.fromtimestamp(et, tz=timezone.utc)
                h_utc = dt.hour
                h_local = local_hour(et, p["countryCode"])
                # weekday(): 0=segunda -> convertendo para 0=domingo
                day = (dt.weekday() + 1) % 7

                hourly_utc[h_utc] += 1
                hourly_local[h_local] += 1
                day_hour_utc[day][h_utc] += 1
                day_hour_local[day][h_local] += 1
                hours_utc[h_utc] += 1
                hours_local[h_local] += 1
                total_games += 1

            per_player_hours[p["username"]] = (hours_utc, hours_local)
            print(f"  [{i}/{len(players)}] {p['username']}: {len(end_times)} partidas de blitz")

    player_rows = []
    for p in players:
        hours_utc, hours_local = per_player_hours.get(p["username"], ([0] * 24, [0] * 24))
        total = sum(hours_utc)
        peak_utc = max(range(24), key=lambda h: hours_utc[h]) if total else None
        peak_local = max(range(24), key=lambda h: hours_local[h]) if total else None
        player_rows.append({
            **p,
            "total": total,
            "peakHourUtc": peak_utc,
            "peakHourLocal": peak_local,
        })

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "playerCount": PLAYER_COUNT,
        "monthsBack": MONTHS_BACK,
        "totalGames": total_games,
        "skipped": skipped,
        "players": player_rows,
        "hourlyUtc": hourly_utc,
        "hourlyLocal": hourly_local,
        "dayHourUtc": day_hour_utc,
        "dayHourLocal": day_hour_local,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Dados salvos em {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
