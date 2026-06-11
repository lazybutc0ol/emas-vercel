# api/scrape.py — dipanggil otomatis oleh Vercel Cron tiap hari (09:05 WIB)
# atau manual: https://NAMAPROJECT.vercel.app/api/scrape?secret=CRON_SECRET
import json
import os
import re
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

URL_HARGA = "https://www.logammulia.com/id/harga-emas-hari-ini"
URL_BUYBACK = "https://www.logammulia.com/id/sell/gold"
WIB = timezone(timedelta(hours=7))

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    "Referer": "https://www.logammulia.com/",
}


# ----------------------------- FETCH (anti Cloudflare) ----------------------
def fetch(url: str) -> str:
    errors = []
    try:  # 1) curl_cffi: tiru TLS fingerprint Chrome
        from curl_cffi import requests as curl_requests
        r = curl_requests.get(url, impersonate="chrome", timeout=35)
        if r.status_code == 200:
            return r.text
        errors.append(f"curl_cffi={r.status_code}")
    except Exception as e:
        errors.append(f"curl_cffi: {e}")
    try:  # 2) cloudscraper
        import cloudscraper
        r = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        ).get(url, timeout=35)
        if r.status_code == 200:
            return r.text
        errors.append(f"cloudscraper={r.status_code}")
    except Exception as e:
        errors.append(f"cloudscraper: {e}")
    try:  # 3) requests biasa
        import requests
        r = requests.get(url, headers=HEADERS, timeout=35)
        if r.status_code == 200:
            return r.text
        errors.append(f"requests={r.status_code}")
    except Exception as e:
        errors.append(f"requests: {e}")
    raise RuntimeError(f"Gagal fetch {url}: {'; '.join(errors)}")


# ----------------------------- PARSER ---------------------------------------
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


# ----------------------------- DATABASE --------------------------------------
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


# ----------------------------- HANDLER ---------------------------------------
class handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Autentikasi: header Bearer dari Vercel Cron ATAU ?secret= untuk manual
        secret = os.environ.get("CRON_SECRET", "")
        auth = self.headers.get("Authorization", "")
        qs = parse_qs(urlparse(self.path).query)
        ok = secret and (auth == f"Bearer {secret}" or
                         qs.get("secret", [""])[0] == secret)
        if not ok:
            return self._json(401, {"ok": False, "error": "secret salah / belum diset"})

        try:
            snap = {
                "diambil_pada": datetime.now(WIB).isoformat(timespec="seconds"),
                "sumber": {"harga": URL_HARGA, "buyback": URL_BUYBACK},
                "harga": parse_harga(fetch(URL_HARGA)),
                "buyback": parse_buyback(fetch(URL_BUYBACK)),
            }
            batangan = snap["harga"]["kategori"].get("Emas Batangan", [])
            if not batangan:
                return self._json(502, {"ok": False,
                    "error": "Tabel Emas Batangan tidak terbaca (diblokir antibot "
                             "atau struktur halaman berubah). Data lama aman."})

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
            return self._json(200, {"ok": True, "tanggal": tanggal,
                                    "harga_dasar_1gr": satu["harga_dasar"] if satu else None,
                                    "buyback_per_gram": bb})
        except Exception as e:
            return self._json(500, {"ok": False, "error": str(e)})
