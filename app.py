#!/usr/bin/env python3
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from http import cookies
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "cdk_exchange.db"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
CDK_CHARS = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CDK_RE = re.compile(r"^[A-Z]{3,5}(-[A-Z2-9]{4}){4}$")
THROTTLE = {}


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 180000)
    return "pbkdf2_sha256$180000$%s$%s" % (
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password, stored):
    try:
        algo, rounds, salt_b64, digest_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def cdk_code(prefix):
    def part():
        return "".join(secrets.choice(CDK_CHARS) for _ in range(4))
    return f"{prefix}-{part()}-{part()}-{part()}-{part()}"


def stock_filled(stock, full_threshold):
    if stock <= 0:
        return 0
    ratio = min(stock / max(full_threshold, 1), 1)
    return max(1, int(-(-ratio * 5 // 1)))


def row_dict(row):
    return dict(row) if row else None


def rows_dict(rows):
    return [dict(r) for r in rows]


def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO setting(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def admin_log(conn, admin_id, action, target, ip):
    conn.execute(
        "INSERT INTO admin_log(admin_id, action, target, ip, created_at) VALUES(?,?,?,?,?)",
        (admin_id, action, target, ip, now_str()),
    )


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              last_login_at TEXT,
              last_login_ip TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_session (
              token TEXT PRIMARY KEY,
              admin_id INTEGER NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(admin_id) REFERENCES admin(id)
            );
            CREATE TABLE IF NOT EXISTS product (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              intro TEXT NOT NULL DEFAULT '',
              usage_text TEXT NOT NULL DEFAULT '',
              cdk_prefix TEXT NOT NULL,
              full_threshold INTEGER NOT NULL DEFAULT 100,
              status INTEGER NOT NULL DEFAULT 1,
              sort INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inventory (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id INTEGER NOT NULL,
              content TEXT NOT NULL,
              status INTEGER NOT NULL DEFAULT 0,
              delivered_cdk_id INTEGER,
              expire_at TEXT,
              batch_no TEXT,
              created_at TEXT NOT NULL,
              delivered_at TEXT,
              FOREIGN KEY(product_id) REFERENCES product(id)
            );
            CREATE INDEX IF NOT EXISTS idx_inventory_product_status ON inventory(product_id,status);
            CREATE INDEX IF NOT EXISTS idx_inventory_batch ON inventory(batch_no);
            CREATE TABLE IF NOT EXISTS cdk (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              code TEXT NOT NULL UNIQUE,
              product_id INTEGER NOT NULL,
              status INTEGER NOT NULL DEFAULT 0,
              valid_days INTEGER NOT NULL DEFAULT 0,
              generated_at TEXT NOT NULL,
              expire_at TEXT,
              used_at TEXT,
              used_ip TEXT,
              inventory_id INTEGER,
              remark TEXT,
              batch_no TEXT,
              FOREIGN KEY(product_id) REFERENCES product(id)
            );
            CREATE INDEX IF NOT EXISTS idx_cdk_product_status ON cdk(product_id,status);
            CREATE INDEX IF NOT EXISTS idx_cdk_status_expire ON cdk(status,expire_at);
            CREATE TABLE IF NOT EXISTS redeem_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              type INTEGER NOT NULL,
              cdk_code TEXT,
              product_id INTEGER,
              ip TEXT,
              ua TEXT,
              result INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_redeem_cdk ON redeem_log(cdk_code);
            CREATE INDEX IF NOT EXISTS idx_redeem_created ON redeem_log(created_at);
            CREATE TABLE IF NOT EXISTS admin_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              admin_id INTEGER,
              action TEXT NOT NULL,
              target TEXT,
              ip TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS setting (
              key TEXT PRIMARY KEY,
              value TEXT
            );
            """
        )
        if not conn.execute("SELECT 1 FROM admin LIMIT 1").fetchone():
            username = os.environ.get("ADMIN_USERNAME", "Fyanxv")
            password = os.environ.get("ADMIN_PASSWORD", "Fyb2530+")
            conn.execute(
                "INSERT INTO admin(username,password_hash,created_at) VALUES(?,?,?)",
                (username, hash_password(password), now_str()),
            )
        defaults = {
            "site_name": "兑换中心",
            "query_max": "50",
            "redeem_throttle_seconds": "3",
            "redeem_fail_text": "卡密无效或已使用",
            "frontend_history_keep": "200",
            "frontend_stock_visible": "1",
            "announcement": "",
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO setting(key,value) VALUES(?,?)", (key, value))


class App(BaseHTTPRequestHandler):
    server_version = "CDKExchange/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, msg, status=200):
        self.send_json({"code": code, "msg": msg, "data": {}}, status)

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(size) if size else b"{}"
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            data = parse_qs(raw.decode("utf-8"))
            return {k: v[0] if len(v) == 1 else v for k, v in data.items()}

    def ip(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()

    def cookie_token(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return jar.get("admin_session").value if jar.get("admin_session") else ""

    def current_admin(self, conn):
        token = self.cookie_token()
        if not token:
            return None
        row = conn.execute(
            """SELECT a.* FROM admin_session s JOIN admin a ON a.id=s.admin_id
               WHERE s.token=? AND s.expires_at>?""",
            (token, now_str()),
        ).fetchone()
        return row_dict(row)

    def require_admin(self, conn):
        admin = self.current_admin(conn)
        if not admin:
            self.fail(2001, "未登录 / Token 无效", 401)
            return None
        return admin

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/") or path.startswith("/admin/"):
            return self.route_api("GET", path)
        return self.serve_static(path)

    def do_POST(self):
        return self.route_api("POST", urlparse(self.path).path)

    def do_PUT(self):
        return self.route_api("PUT", urlparse(self.path).path)

    def do_DELETE(self):
        return self.route_api("DELETE", urlparse(self.path).path)

    def serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = (ROOT / path.lstrip("/")).resolve()
        if not str(target).startswith(str(ROOT)) or not target.exists() or target.is_dir():
            self.send_error(404)
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix in (".html", ".md", ".txt"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route_api(self, method, path):
        try:
            with db() as conn:
                if path == "/api/site" and method == "GET":
                    return self.ok({r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM setting")})
                if path == "/api/stocks" and method == "GET":
                    return self.ok(self.public_stocks(conn))
                if path == "/api/query" and method == "POST":
                    return self.public_query(conn)
                if path == "/api/redeem" and method == "POST":
                    return self.public_redeem(conn)
                if path == "/admin/login" and method == "POST":
                    return self.admin_login(conn)
                if path == "/admin/logout" and method == "POST":
                    return self.admin_logout(conn)
                if path == "/admin/me" and method == "GET":
                    admin = self.require_admin(conn)
                    return self.ok({"username": admin["username"]}) if admin else None
                admin = self.require_admin(conn)
                if not admin:
                    return
                return self.admin_routes(conn, admin, method, path)
        except Exception as exc:
            self.fail(5000, f"服务器错误: {exc}", 500)

    def public_stocks(self, conn):
        rows = conn.execute(
            """SELECT p.id product_id,p.name,p.full_threshold,
                      SUM(CASE WHEN i.status=0 THEN 1 ELSE 0 END) stock
               FROM product p LEFT JOIN inventory i ON i.product_id=p.id
               WHERE p.status=1
               GROUP BY p.id
               ORDER BY p.sort,p.id"""
        ).fetchall()
        data = []
        for r in rows:
            stock = int(r["stock"] or 0)
            data.append({
                "product_id": r["product_id"],
                "name": r["name"],
                "full_threshold": r["full_threshold"],
                "stock": stock,
                "stock_filled": stock_filled(stock, r["full_threshold"]),
            })
        return data

    def public_query(self, conn):
        payload = self.read_json()
        cdks = payload.get("cdks") or []
        if isinstance(cdks, str):
            cdks = [x.strip() for x in cdks.splitlines() if x.strip()]
        max_count = int(get_setting(conn, "query_max", "50"))
        if len(cdks) > max_count:
            return self.fail(1008, f"单次最多 {max_count} 条")
        result = []
        for code in cdks:
            code = str(code).strip().upper()
            row = conn.execute("SELECT status,expire_at FROM cdk WHERE code=?", (code,)).fetchone()
            status = "invalid"
            if row:
                expired = row["expire_at"] and row["expire_at"] < now_str()
                if row["status"] == 0 and not expired:
                    status = "unused"
                elif row["status"] == 1:
                    status = "used"
                else:
                    status = "expired"
            result.append({"cdk": code, "status": status})
        conn.execute(
            "INSERT INTO redeem_log(type,cdk_code,ip,ua,result,created_at) VALUES(?,?,?,?,?,?)",
            (2, f"批量 {len(cdks)} 条", self.ip(), self.headers.get("User-Agent", "")[:255], 1, now_str()),
        )
        self.ok(result)

    def public_redeem(self, conn):
        payload = self.read_json()
        code = str(payload.get("cdk", "")).strip().upper()
        fail_text = get_setting(conn, "redeem_fail_text", "卡密无效或已使用")
        delay = int(get_setting(conn, "redeem_throttle_seconds", "3"))
        ip = self.ip()
        last = THROTTLE.get(ip, 0)
        if time.time() - last < delay:
            return self.fail(1007, "兑换太频繁,请稍后再试")
        THROTTLE[ip] = time.time()
        if not CDK_RE.match(code):
            self.write_redeem_log(conn, code, None, 2)
            return self.fail(1001, fail_text)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cdk = conn.execute("SELECT * FROM cdk WHERE code=?", (code,)).fetchone()
            if not cdk:
                conn.execute("ROLLBACK")
                self.write_redeem_log(conn, code, None, 2)
                return self.fail(1001, fail_text)
            if cdk["expire_at"] and cdk["expire_at"] < now_str():
                conn.execute("UPDATE cdk SET status=3 WHERE id=?", (cdk["id"],))
                conn.execute("COMMIT")
                self.write_redeem_log(conn, code, cdk["product_id"], 4)
                return self.fail(1004, "卡密已过期")
            if cdk["status"] == 1:
                conn.execute("ROLLBACK")
                self.write_redeem_log(conn, code, cdk["product_id"], 3)
                return self.fail(1002, "卡密已使用")
            if cdk["status"] != 0:
                conn.execute("ROLLBACK")
                self.write_redeem_log(conn, code, cdk["product_id"], 4)
                return self.fail(1003, fail_text)
            product = conn.execute("SELECT * FROM product WHERE id=? AND status=1", (cdk["product_id"],)).fetchone()
            if not product:
                conn.execute("ROLLBACK")
                self.write_redeem_log(conn, code, cdk["product_id"], 5)
                return self.fail(1005, "商品已下架")
            inv = conn.execute(
                "SELECT * FROM inventory WHERE product_id=? AND status=0 ORDER BY id LIMIT 1",
                (product["id"],),
            ).fetchone()
            if not inv:
                conn.execute("ROLLBACK")
                self.write_redeem_log(conn, code, product["id"], 5)
                return self.fail(1006, "库存不足")
            t = now_str()
            conn.execute(
                "UPDATE cdk SET status=1,used_at=?,used_ip=?,inventory_id=? WHERE id=?",
                (t, ip, inv["id"], cdk["id"]),
            )
            conn.execute(
                "UPDATE inventory SET status=1,delivered_cdk_id=?,delivered_at=? WHERE id=?",
                (cdk["id"], t, inv["id"]),
            )
            conn.execute(
                "INSERT INTO redeem_log(type,cdk_code,product_id,ip,ua,result,created_at) VALUES(?,?,?,?,?,?,?)",
                (1, code, product["id"], ip, self.headers.get("User-Agent", "")[:255], 1, t),
            )
            conn.execute("COMMIT")
            self.ok({
                "product": {"id": product["id"], "name": product["name"], "intro": product["intro"], "usage_text": product["usage_text"]},
                "content": inv["content"],
                "time": t,
            })
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def write_redeem_log(self, conn, code, product_id, result):
        conn.execute(
            "INSERT INTO redeem_log(type,cdk_code,product_id,ip,ua,result,created_at) VALUES(?,?,?,?,?,?,?)",
            (1, code, product_id, self.ip(), self.headers.get("User-Agent", "")[:255], result, now_str()),
        )

    def admin_login(self, conn):
        payload = self.read_json()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        admin = conn.execute("SELECT * FROM admin WHERE username=?", (username,)).fetchone()
        if not admin or not verify_password(password, admin["password_hash"]):
            return self.fail(2001, "账号或密码错误", 401)
        token = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO admin_session(token,admin_id,expires_at,created_at) VALUES(?,?,?,?)", (token, admin["id"], expires, now_str()))
        conn.execute("UPDATE admin SET last_login_at=?,last_login_ip=? WHERE id=?", (now_str(), self.ip(), admin["id"]))
        admin_log(conn, admin["id"], "login", username, self.ip())
        self.ok({"username": username}, headers={"Set-Cookie": f"admin_session={token}; Path=/; HttpOnly; SameSite=Lax"})

    def ok(self, data=None, msg="ok", headers=None):
        self.send_json({"code": 0, "msg": msg, "data": data if data is not None else {}}, 200, headers)

    def admin_logout(self, conn):
        token = self.cookie_token()
        if token:
            conn.execute("DELETE FROM admin_session WHERE token=?", (token,))
        self.ok({}, headers={"Set-Cookie": "admin_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})

    def admin_routes(self, conn, admin, method, path):
        if path == "/admin/dashboard/summary" and method == "GET":
            return self.ok({
                "products": conn.execute("SELECT COUNT(*) c FROM product").fetchone()["c"],
                "inventory": conn.execute("SELECT COUNT(*) c FROM inventory WHERE status=0").fetchone()["c"],
                "cdks": conn.execute("SELECT COUNT(*) c FROM cdk").fetchone()["c"],
                "unused_cdks": conn.execute("SELECT COUNT(*) c FROM cdk WHERE status=0").fetchone()["c"],
                "used_cdks": conn.execute("SELECT COUNT(*) c FROM cdk WHERE status=1").fetchone()["c"],
                "disabled_cdks": conn.execute("SELECT COUNT(*) c FROM cdk WHERE status IN (2,3)").fetchone()["c"],
                "today_redeem": conn.execute("SELECT COUNT(*) c FROM redeem_log WHERE type=1 AND result=1 AND created_at>=date('now','localtime')").fetchone()["c"],
                "query_24h": conn.execute("SELECT COUNT(*) c FROM redeem_log WHERE type=2 AND created_at>=datetime('now','localtime','-1 day')").fetchone()["c"],
            })
        if path == "/admin/products" and method == "GET":
            return self.ok(self.admin_products(conn))
        if path == "/admin/products" and method == "POST":
            p = self.read_json()
            prefix = str(p.get("cdk_prefix", "")).strip().upper()
            conn.execute(
                """INSERT INTO product(name,intro,usage_text,cdk_prefix,full_threshold,status,sort,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (p.get("name", ""), p.get("intro", ""), p.get("usage_text", ""), prefix, int(p.get("full_threshold", 100)), 1, 0, now_str(), now_str()),
            )
            admin_log(conn, admin["id"], "edit_product", str(p.get("name", "")), self.ip())
            return self.ok()
        if path.startswith("/admin/products/") and method == "PUT":
            pid = int(path.rsplit("/", 1)[-1])
            p = self.read_json()
            fields = ["name", "intro", "usage_text", "cdk_prefix", "full_threshold", "status"]
            updates = [f"{f}=?" for f in fields if f in p]
            values = [str(p[f]).upper() if f == "cdk_prefix" else p[f] for f in fields if f in p]
            if updates:
                conn.execute(f"UPDATE product SET {','.join(updates)},updated_at=? WHERE id=?", (*values, now_str(), pid))
                admin_log(conn, admin["id"], "edit_product", f"#{pid}", self.ip())
            return self.ok()
        if path.startswith("/admin/products/") and method == "DELETE":
            pid = int(path.rsplit("/", 1)[-1])
            product = conn.execute("SELECT name FROM product WHERE id=?", (pid,)).fetchone()
            if not product:
                return self.fail(1005, "商品不存在", 404)
            conn.execute("DELETE FROM inventory WHERE product_id=?", (pid,))
            conn.execute("DELETE FROM cdk WHERE product_id=?", (pid,))
            conn.execute("DELETE FROM product WHERE id=?", (pid,))
            admin_log(conn, admin["id"], "delete_product", product["name"], self.ip())
            return self.ok()
        if path == "/admin/inventory" and method == "GET":
            qs = parse_qs(urlparse(self.path).query)
            product_id = int((qs.get("product_id") or [0])[0])
            if not product_id:
                return self.ok([])
            rows = conn.execute("SELECT * FROM inventory WHERE product_id=? ORDER BY id DESC LIMIT 200", (product_id,)).fetchall()
            return self.ok(rows_dict(rows))
        if path == "/admin/inventory/import" and method == "POST":
            p = self.read_json()
            product_id = int(p.get("product_id") or 0)
            mode = p.get("mode", "append")
            raw = p.get("contents", [])
            lines = raw if isinstance(raw, list) else str(raw).splitlines()
            lines = [x.strip() for x in lines if x.strip()]
            batch_no = datetime.now().strftime("%Y%m%d%H%M%S")
            if mode == "overwrite":
                conn.execute("DELETE FROM inventory WHERE product_id=? AND status=0", (product_id,))
            if mode == "dedupe":
                lines = list(dict.fromkeys(lines))
                exists = set(r["content"] for r in conn.execute("SELECT content FROM inventory WHERE product_id=?", (product_id,)))
                lines = [x for x in lines if x not in exists]
            conn.executemany(
                "INSERT INTO inventory(product_id,content,status,expire_at,batch_no,created_at) VALUES(?,?,?,?,?,?)",
                [(product_id, line, 0, None, batch_no, now_str()) for line in lines],
            )
            admin_log(conn, admin["id"], "import_inventory", f"product #{product_id} +{len(lines)}", self.ip())
            return self.ok({"count": len(lines), "batch_no": batch_no})
        if path.startswith("/admin/inventory/") and method == "DELETE":
            iid = int(path.rsplit("/", 1)[-1])
            conn.execute("DELETE FROM inventory WHERE id=? AND status=0", (iid,))
            admin_log(conn, admin["id"], "delete_inventory", f"#{iid}", self.ip())
            return self.ok()
        if path == "/admin/cdks" and method == "GET":
            rows = conn.execute(
                """SELECT c.*,p.name product_name FROM cdk c JOIN product p ON p.id=c.product_id
                   ORDER BY c.id DESC LIMIT 200"""
            ).fetchall()
            return self.ok(rows_dict(rows))
        if path == "/admin/cdks/generate" and method == "POST":
            p = self.read_json()
            product_id = int(p.get("product_id") or 0)
            count = max(1, min(100000, int(p.get("count") or 1)))
            valid_days = max(0, int(p.get("valid_days") or 0))
            product = conn.execute("SELECT * FROM product WHERE id=?", (product_id,)).fetchone()
            if not product:
                return self.fail(1005, "商品不存在", 404)
            expire_at = (datetime.now() + timedelta(days=valid_days)).strftime("%Y-%m-%d %H:%M:%S") if valid_days else None
            batch_no = datetime.now().strftime("%Y%m%d%H%M%S")
            codes = []
            seen = set()
            while len(codes) < count:
                code = cdk_code(product["cdk_prefix"])
                if code in seen:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO cdk(code,product_id,status,valid_days,generated_at,expire_at,remark,batch_no) VALUES(?,?,?,?,?,?,?,?)",
                        (code, product_id, 0, valid_days, now_str(), expire_at, p.get("remark", ""), batch_no),
                    )
                    seen.add(code)
                    codes.append(code)
                except sqlite3.IntegrityError:
                    continue
            admin_log(conn, admin["id"], "gen_cdk", f"{product['name']} x{count}", self.ip())
            return self.ok({"codes": codes, "count": count, "batch_no": batch_no})
        if path == "/admin/cdks/disable" and method == "POST":
            p = self.read_json()
            ids = p.get("ids") or []
            conn.executemany("UPDATE cdk SET status=2 WHERE id=? AND status=0", [(int(i),) for i in ids])
            admin_log(conn, admin["id"], "disable_cdk", f"{len(ids)} 条", self.ip())
            return self.ok()
        if path == "/admin/redeem-logs" and method == "GET":
            rows = conn.execute(
                """SELECT l.*,p.name product_name FROM redeem_log l LEFT JOIN product p ON p.id=l.product_id
                   ORDER BY l.id DESC LIMIT 200"""
            ).fetchall()
            return self.ok(rows_dict(rows))
        if path == "/admin/admin-logs" and method == "GET":
            rows = conn.execute(
                """SELECT l.*,a.username FROM admin_log l LEFT JOIN admin a ON a.id=l.admin_id
                   ORDER BY l.id DESC LIMIT 200"""
            ).fetchall()
            return self.ok(rows_dict(rows))
        if path == "/admin/settings" and method == "GET":
            return self.ok({r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM setting")})
        if path == "/admin/settings" and method == "PUT":
            p = self.read_json()
            for k, v in p.items():
                set_setting(conn, k, v)
            admin_log(conn, admin["id"], "edit_settings", "站点设置", self.ip())
            return self.ok()
        if path == "/admin/password" and method == "PUT":
            p = self.read_json()
            row = conn.execute("SELECT * FROM admin WHERE id=?", (admin["id"],)).fetchone()
            if not verify_password(str(p.get("old_password", "")), row["password_hash"]):
                return self.fail(2002, "原密码错误", 400)
            if str(p.get("new_password", "")) != str(p.get("confirm_password", "")) or len(str(p.get("new_password", ""))) < 6:
                return self.fail(2002, "新密码至少 6 位,且两次输入需一致", 400)
            conn.execute("UPDATE admin SET password_hash=? WHERE id=?", (hash_password(str(p.get("new_password"))), admin["id"]))
            admin_log(conn, admin["id"], "change_password", admin["username"], self.ip())
            return self.ok()
        self.fail(404, "接口不存在", 404)

    def admin_products(self, conn):
        rows = conn.execute(
            """SELECT p.*,
                      SUM(CASE WHEN i.status=0 THEN 1 ELSE 0 END) stock,
                      SUM(CASE WHEN i.status=1 THEN 1 ELSE 0 END) delivered
               FROM product p LEFT JOIN inventory i ON i.product_id=p.id
               GROUP BY p.id
               ORDER BY p.sort,p.id"""
        ).fetchall()
        data = []
        for r in rows:
            item = dict(r)
            item["stock"] = int(item.get("stock") or 0)
            item["delivered"] = int(item.get("delivered") or 0)
            item["stock_filled"] = stock_filled(item["stock"], item["full_threshold"])
            data.append(item)
        return data


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def server_bind(self):
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


if __name__ == "__main__":
    init_db()
    print(f"CDK 兑换系统已启动: http://{HOST}:{PORT}")
    print("默认后台账号: admin / admin123")
    LocalHTTPServer((HOST, PORT), App).serve_forever()
