"""
Busca o leaderboard de blitz do Chess.com e o histórico recente de partidas
dos jogadores no topo, e gera docs/data.json para a página estática consumir.

Roda server-side (via GitHub Actions ou localmente) - aqui não existe CORS,
então as chamadas à API do Chess.com funcionam de forma direta e confiável.
"""

import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# --- Configuração -----------------------------------------------------------
PLAYER_COUNT = 50          # quantos jogadores do topo do ranking analisar (máx. 50)
MONTHS_BACK = 1            # quantos meses de arquivo de partidas buscar por jogador
MAX_WORKERS = 6            # requisições em paralelo
TOP_OPENINGS = 8           # quantas aberturas guardar em cada ranking
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "data.json"

# A API do Chess.com pede um User-Agent identificável nas requisições.
# Troque o e-mail abaixo pelo seu.
HEADERS = {
    "User-Agent": "blitz-patterns-dashboard/1.0 (contato: lorenz.bruno@gmail.com)"
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

BRT_OFFSET = -3  # Horário de Brasília, fixo (Brasil não observa horário de verão desde 2019)


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
                collected.append(g)
    return collected


def local_hour(utc_hour, offset):
    return int((utc_hour + offset) % 24)


def top_counter(counter, n):
    return [{"name": name, "count": count} for name, count in counter.most_common(n)]


def empty_day_hour():
    return [[0] * 24 for _ in range(7)]


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
    hourly_brt = [0] * 24
    day_hour_utc = empty_day_hour()
    day_hour_local = empty_day_hour()
    day_hour_brt = empty_day_hour()
    openings_overall = Counter()
    total_games = 0
    skipped = 0

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

            uname_lower = p["username"].lower()
            hours_utc = [0] * 24
            hours_local = [0] * 24
            hours_brt = [0] * 24
            p_day_hour_utc = empty_day_hour()
            p_day_hour_local = empty_day_hour()
            p_day_hour_brt = empty_day_hour()
            openings = Counter()

            for g in games:
                dt = datetime.fromtimestamp(g["end_time"], tz=timezone.utc)
                h_utc = dt.hour
                h_local = local_hour(h_utc, COUNTRY_OFFSET.get(p["countryCode"], 0))
                h_brt = local_hour(h_utc, BRT_OFFSET)
                day = (dt.weekday() + 1) % 7  # 0 = domingo

                hourly_utc[h_utc] += 1
                hourly_local[h_local] += 1
                hourly_brt[h_brt] += 1
                day_hour_utc[day][h_utc] += 1
                day_hour_local[day][h_local] += 1
                day_hour_brt[day][h_brt] += 1

                hours_utc[h_utc] += 1
                hours_local[h_local] += 1
                hours_brt[h_brt] += 1
                p_day_hour_utc[day][h_utc] += 1
                p_day_hour_local[day][h_local] += 1
                p_day_hour_brt[day][h_brt] += 1
                total_games += 1

                white_user = (g.get("white", {}).get("username") or "").lower()
                black_user = (g.get("black", {}).get("username") or "").lower()
                if uname_lower in (white_user, black_user):
                    opening = opening_name_from_eco(g.get("eco"))
                    if opening:
                        openings_overall[opening] += 1
                        openings[opening] += 1

            per_player_data[p["username"]] = {
                "hoursUtc": hours_utc,
                "hoursLocal": hours_local,
                "hoursBrt": hours_brt,
                "dayHourUtc": p_day_hour_utc,
                "dayHourLocal": p_day_hour_local,
                "dayHourBrt": p_day_hour_brt,
                "openings": openings,
            }

            print(f"  [{i}/{len(players)}] {p['username']}: {len(games)} partidas de blitz")

    player_rows = []
    for p in players:
        d = per_player_data.get(p["username"])
        if not d:
            player_rows.append({
                **p, "total": 0, "peakHourUtc": None, "peakHourLocal": None, "peakHourBrt": None,
                "dayHourUtc": empty_day_hour(), "dayHourLocal": empty_day_hour(), "dayHourBrt": empty_day_hour(),
                "openings": [],
            })
            continue

        total = sum(d["hoursUtc"])
        peak_utc = max(range(24), key=lambda h: d["hoursUtc"][h]) if total else None
        peak_local = max(range(24), key=lambda h: d["hoursLocal"][h]) if total else None
        peak_brt = max(range(24), key=lambda h: d["hoursBrt"][h]) if total else None

        player_rows.append({
            **p,
            "total": total,
            "peakHourUtc": peak_utc,
            "peakHourLocal": peak_local,
            "peakHourBrt": peak_brt,
            "dayHourUtc": d["dayHourUtc"],
            "dayHourLocal": d["dayHourLocal"],
            "dayHourBrt": d["dayHourBrt"],
            "openings": top_counter(d["openings"], TOP_OPENINGS),
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
        "hourlyBrt": hourly_brt,
        "dayHourUtc": day_hour_utc,
        "dayHourLocal": day_hour_local,
        "dayHourBrt": day_hour_brt,
        "openingsOverall": top_counter(openings_overall, TOP_OPENINGS * 2),
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Dados salvos em {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
