# app.py — Aplikasi Flask tunggal untuk Vercel (entrypoint otomatis terdeteksi)
#
# Route:
#   GET /                                  -> halaman dashboard (web/index.html)
#   GET /api/scrape?secret=CRON_SECRET     -> scrape + simpan ke Postgres (juga dipanggil cron)
#   GET /api/data?aksi=dates               -> daftar tanggal tersedia (desc)
#   GET /api/data?aksi=harga&tanggal=...   -> snapshot lengkap satu tanggal
#   GET /api/data?aksi=history             -> deret harian untuk grafik (asc)
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, request, jsonify, Response
from bs4 import BeautifulSoup

app = Flask(__name__)

URL_HARGA = "https://www.logammulia.com/id/harga-emas-hari-ini"
URL_BUYBACK = "https://www.logammulia.com/id/sell/gold"
URL_GALERI24 = "https://galeri24.co.id/harga-emas"
WIB = timezone(timedelta(hours=7))
HTML_PATH = Path(__file__).resolve().parent / "web" / "index.html"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    "Referer": "https://www.logammulia.com/",
}


# =========================================================================
# FETCH — beberapa strategi berurutan untuk lolos antibot Cloudflare
# =========================================================================
def fetch(url: str) -> str:
    errors = []
    try:  # 1) curl_cffi: tiru TLS fingerprint Chrome (paling ampuh)
        from curl_cffi import requests as curl_requests
        r = curl_requests.get(url, impersonate="chrome", timeout=20)
        if r.status_code == 200:
            return r.text
        errors.append(f"curl_cffi={r.status_code}")
    except Exception as e:
        errors.append(f"curl_cffi: {e}")
    try:  # 2) cloudscraper
        import cloudscraper
        r = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        ).get(url, timeout=20)
        if r.status_code == 200:
            return r.text
        errors.append(f"cloudscraper={r.status_code}")
    except Exception as e:
        errors.append(f"cloudscraper: {e}")
    try:  # 3) requests biasa
        import requests
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.text
        errors.append(f"requests={r.status_code}")
    except Exception as e:
        errors.append(f"requests: {e}")
    raise RuntimeError(f"Gagal fetch {url}: {'; '.join(errors)}")


# =========================================================================
# PARSER
# =========================================================================
def _to_int(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def parse_harga(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result = {"tanggal_halaman": None, "kategori": {}}
    m = re.search(r"Harga Emas Hari Ini[,]?\s*([\d]{1,2}\s+\w+\s+\d{4})",
                  soup.get_text(" "))
    if m:
        result["tanggal_halaman"] = m.group(1)

    current = None
    for table in soup.find_all("table"):
        if "Emas Batangan" not in table.get_text(" "):
            continue
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if not cells:
                continue
            first = cells[0]
            if first.lower().startswith("berat"):
                continue
            if len(cells) == 1 or _to_int(cells[-1]) is None:
                current = first
                result["kategori"].setdefault(current, [])
                continue
            if current and len(cells) >= 3:
                berat = re.sub(r"\s*gr\.?$", "", first, flags=re.I).strip()
                try:
                    berat_f = float(berat.replace(",", "."))
                except ValueError:
                    continue
                hd, hp = _to_int(cells[1]), _to_int(cells[2])
                if hd and hp:
                    result["kategori"][current].append(
                        {"berat_gr": berat_f, "harga_dasar": hd, "harga_pajak": hp})
        break
    return result


def parse_buyback(html: str) -> dict:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    out = {"buyback_per_gram": None, "perubahan": None, "update_terakhir": None}
    m = re.search(r"Harga Buyback:?\s*Rp\.?\s*([\d.,]+)", text)
    if m:
        out["buyback_per_gram"] = _to_int(m.group(1))
    m = re.search(r"Perubahan:?\s*Rp\.?\s*(-?\s*[\d.,]+)", text)
    if m:
        raw = m.group(1).replace(" ", "")
        val = _to_int(raw)
        if val is not None and raw.startswith("-"):
            val = -val
        out["perubahan"] = val
    m = re.search(r"Perubahan Terakhir:?\s*([\d]{1,2}\s+\w+\s+\d{4}\s+[\d:]+)", text)
    if m:
        out["update_terakhir"] = m.group(1)
    return out


def parse_galeri24(html: str) -> dict:
    """Parser Galeri24: banyak kategori merek, tiap tabel kolom
    Berat | Harga Jual | Harga Buyback. Berbasis teks baris agar tahan
    terhadap apakah halaman memakai <table> atau <div> grid."""
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return _galeri24_from_text(text)


def _galeri24_from_text(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    result = {"tanggal_halaman": None, "kategori": {}}
    num_re = re.compile(r"^\d+(?:\.\d+)?$")
    last_harga = None
    i, n = 0, len(lines)

    while i < n:
        ln = lines[i]

        m = re.match(r"Diperbarui\s+(.+)", ln)
        if m and not result["tanggal_halaman"]:
            result["tanggal_halaman"] = m.group(1).strip()

        if ln.startswith("Harga "):
            last_harga = ln[len("Harga "):].strip()

        # Awal tabel: baris "Berat" diikuti header "...Jual" lalu "...Buyback"
        if (ln == "Berat" and i + 2 < n
                and "Jual" in lines[i + 1] and "Buyback" in lines[i + 2]):
            cat = (last_harga or "Tidak diketahui").split(" - ")[0].strip()
            rows = []
            j = i + 3
            while j + 2 < n:  # butuh 3 baris: berat, jual, buyback
                berat_s, jual_s, buyback_s = lines[j], lines[j + 1], lines[j + 2]
                if not num_re.match(berat_s):
                    break
                if not (jual_s.startswith("Rp") and buyback_s.startswith("Rp")):
                    break
                rows.append({
                    "berat_gr": float(berat_s),
                    "harga_jual": _to_int(jual_s),
                    "harga_buyback": _to_int(buyback_s),
                })
                j += 3
            if rows:
                result["kategori"][cat] = rows
            i = j
            continue
        i += 1

    return result


# =========================================================================
# DATABASE (Postgres)
# =========================================================================
def db():
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


DDL = """
CREATE TABLE IF NOT EXISTS harga_emas (
    tanggal          DATE PRIMARY KEY,
    harga_dasar_1gr  BIGINT,
    harga_pajak_1gr  BIGINT,
    buyback_per_gram BIGINT,
    spread_1gr       BIGINT,
    snapshot         JSONB NOT NULL,
    diambil_pada     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


# =========================================================================
# ROUTES
# =========================================================================
@app.route("/")
def home():
    try:
        html = HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = "<h1>index.html tidak ditemukan</h1>"
    return Response(html, mimetype="text/html")


@app.route("/api/scrape")
def scrape():
    # Auth: header Bearer dari Vercel Cron ATAU ?secret= untuk pemicuan manual
    secret = os.environ.get("CRON_SECRET", "")
    auth = request.headers.get("Authorization", "")
    if not (secret and (auth == f"Bearer {secret}" or
                        request.args.get("secret", "") == secret)):
        return jsonify({"ok": False, "error": "secret salah / CRON_SECRET belum diset"}), 401

    try:
        snap = {
            "diambil_pada": datetime.now(WIB).isoformat(timespec="seconds"),
            "sumber": {"harga": URL_HARGA, "buyback": URL_BUYBACK,
                       "galeri24": URL_GALERI24},
            "harga": parse_harga(fetch(URL_HARGA)),
            "buyback": parse_buyback(fetch(URL_BUYBACK)),
        }
        batangan = snap["harga"]["kategori"].get("Emas Batangan", [])
        if not batangan:
            return jsonify({"ok": False, "error": "Tabel Emas Batangan tidak terbaca "
                            "(diblokir antibot / struktur berubah). Data lama aman."}), 502

        # Galeri24 — non-fatal: kalau gagal, data logammulia tetap disimpan
        galeri24_status = "ok"
        try:
            g24 = parse_galeri24(fetch(URL_GALERI24))
            if g24["kategori"]:
                snap["galeri24"] = g24
            else:
                galeri24_status = "kosong (kemungkinan diblokir / struktur berubah)"
        except Exception as ge:
            galeri24_status = f"gagal: {ge}"

        satu = next((x for x in batangan if x["berat_gr"] == 1.0), None)
        bb = snap["buyback"]["buyback_per_gram"]
        tanggal = datetime.now(WIB).strftime("%Y-%m-%d")

        conn = db()
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("""
                INSERT INTO harga_emas
                  (tanggal, harga_dasar_1gr, harga_pajak_1gr,
                   buyback_per_gram, spread_1gr, snapshot, diambil_pada)
                VALUES (%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (tanggal) DO UPDATE SET
                  harga_dasar_1gr  = EXCLUDED.harga_dasar_1gr,
                  harga_pajak_1gr  = EXCLUDED.harga_pajak_1gr,
                  buyback_per_gram = EXCLUDED.buyback_per_gram,
                  spread_1gr       = EXCLUDED.spread_1gr,
                  snapshot         = EXCLUDED.snapshot,
                  diambil_pada     = now();
            """, (
                tanggal,
                satu["harga_dasar"] if satu else None,
                satu["harga_pajak"] if satu else None,
                bb,
                (satu["harga_dasar"] - bb) if (satu and bb) else None,
                json.dumps(snap, ensure_ascii=False),
            ))
        conn.close()
        return jsonify({"ok": True, "tanggal": tanggal,
                        "harga_dasar_1gr": satu["harga_dasar"] if satu else None,
                        "buyback_per_gram": bb,
                        "galeri24_kategori": len(snap.get("galeri24", {}).get("kategori", {})),
                        "galeri24_status": galeri24_status})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/data")
def data():
    aksi = request.args.get("aksi", "")
    conn = None
    try:
        conn = db()
        cur = conn.cursor()

        if aksi == "dates":
            cur.execute("SELECT tanggal FROM harga_emas ORDER BY tanggal DESC;")
            payload = {"tanggal_tersedia": [str(r[0]) for r in cur.fetchall()]}

        elif aksi == "harga":
            cur.execute("SELECT snapshot FROM harga_emas WHERE tanggal=%s;",
                        (request.args.get("tanggal", ""),))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "data tanggal itu tidak ada"}), 404
            snap = row[0]
            # JSONB bisa dikembalikan sebagai dict (umum) atau string (driver tertentu)
            body = snap if isinstance(snap, str) else json.dumps(snap, ensure_ascii=False)
            return Response(body, mimetype="application/json")

        elif aksi == "history":
            cur.execute("""SELECT tanggal, harga_dasar_1gr, harga_pajak_1gr,
                                  buyback_per_gram, spread_1gr
                           FROM harga_emas ORDER BY tanggal ASC;""")
            payload = [{"tanggal": str(r[0]), "harga_dasar_1gr": r[1],
                        "harga_pajak_1gr": r[2], "buyback_per_gram": r[3],
                        "spread_1gr": r[4]} for r in cur.fetchall()]
        else:
            return jsonify({"error": "aksi tidak dikenal (dates|harga|history)"}), 400

        resp = Response(json.dumps(payload, ensure_ascii=False, default=str),
                        mimetype="application/json")
        resp.headers["Cache-Control"] = "s-maxage=300, stale-while-revalidate=600"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


# untuk pengujian lokal: python app.py
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)
