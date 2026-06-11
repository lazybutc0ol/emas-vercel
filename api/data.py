# api/data.py — API baca database untuk website
#   /api/data?aksi=dates                     -> daftar tanggal tersedia (desc)
#   /api/data?aksi=harga&tanggal=YYYY-MM-DD  -> snapshot lengkap tanggal tsb
#   /api/data?aksi=history                   -> deret harian utk grafik (asc)
import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


def db():
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


class handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "s-maxage=300, stale-while-revalidate=600")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        aksi = qs.get("aksi", [""])[0]
        try:
            conn = db()
            with conn, conn.cursor() as cur:
                if aksi == "dates":
                    cur.execute("SELECT tanggal FROM harga_emas ORDER BY tanggal DESC;")
                    return self._json(200, {"tanggal_tersedia":
                                            [str(r[0]) for r in cur.fetchall()]})

                if aksi == "harga":
                    tanggal = qs.get("tanggal", [""])[0]
                    cur.execute("SELECT snapshot FROM harga_emas WHERE tanggal=%s;",
                                (tanggal,))
                    row = cur.fetchone()
                    if not row:
                        return self._json(404, {"error": f"data {tanggal} tidak ada"})
                    return self._json(200, row[0])

                if aksi == "history":
                    cur.execute("""SELECT tanggal, harga_dasar_1gr, harga_pajak_1gr,
                                          buyback_per_gram, spread_1gr
                                   FROM harga_emas ORDER BY tanggal ASC;""")
                    return self._json(200, [
                        {"tanggal": str(r[0]), "harga_dasar_1gr": r[1],
                         "harga_pajak_1gr": r[2], "buyback_per_gram": r[3],
                         "spread_1gr": r[4]} for r in cur.fetchall()])

                return self._json(400, {"error": "aksi tidak dikenal "
                                                 "(dates|harga|history)"})
        except Exception as e:
            return self._json(500, {"error": str(e)})
        finally:
            try:
                conn.close()
            except Exception:
                pass
