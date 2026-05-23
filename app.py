#!/usr/bin/env python3
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))
from http import cookies
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
MYSQL_HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "duihuan")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "duihuan")
MYSQL_POOL_SIZE = int(os.environ.get("MYSQL_POOL_SIZE", "12"))
MYSQL_POOL_WAIT_SECONDS = int(os.environ.get("MYSQL_POOL_WAIT_SECONDS", "5"))
MAX_REQUEST_BODY_SIZE = int(os.environ.get("MAX_REQUEST_BODY_SIZE", str(10 * 1024 * 1024)))  # 10MB
CDK_CHARS = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CDK_RE = re.compile(r"^[A-Z]{3,5}(-[A-Z2-9]{4}){4}$")
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
REDIS_PREFIX = os.environ.get("REDIS_PREFIX", "duihuan")
REDIS_CLIENT = None
MYSQL_POOL = queue.LifoQueue(maxsize=MYSQL_POOL_SIZE)
MYSQL_POOL_CREATED = 0
MYSQL_POOL_LOCK = threading.Lock()
STARTUP_DONE = False
STARTUP_LOCK = threading.Lock()
# CSRF secret key - must be stable across Gunicorn workers.
CSRF_SECRET = (os.environ.get("CSRF_SECRET") or "").strip()
if not CSRF_SECRET:
    fallback_secret = "|".join([
        MYSQL_HOST,
        MYSQL_USER,
        MYSQL_DATABASE,
        os.environ.get("ADMIN_USERNAME", "Fyanxv"),
        os.environ.get("ADMIN_PASSWORD", "Fyb2530+"),
    ])
    CSRF_SECRET = hashlib.sha256(fallback_secret.encode("utf-8")).hexdigest()

# Trusted reverse proxy IPs/CIDRs used for X-Forwarded-For.
# Cloudflare IP ranges (https://www.cloudflare.com/ips/)
CLOUDFLARE_IPS = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32",
    "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
]
TRUSTED_PROXIES = [
    item.strip()
    for item in os.environ.get("TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
    if item.strip()
] + CLOUDFLARE_IPS

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)


def now_str():
    return beijing_now().strftime("%Y-%m-%d %H:%M:%S")


def beijing_now():
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def json_default(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class CursorAdapter:
    def __init__(self, cursor):
        self.cursor = cursor

    def __iter__(self):
        return iter(self.cursor.fetchall())

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()


class MySQLAdapter:
    def __init__(self, conn, pooled=False):
        self.conn = conn
        self.pooled = pooled

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def sql(self, statement):
        statement = statement.strip()
        if statement.upper() == "BEGIN IMMEDIATE":
            return "BEGIN"
        return statement.replace("?", "%s")

    def execute(self, statement, params=()):
        upper = statement.strip().upper()
        if upper == "COMMIT":
            self.conn.commit()
            return CursorAdapter(None)
        if upper == "ROLLBACK":
            self.conn.rollback()
            return CursorAdapter(None)
        cur = self.conn.cursor()
        cur.execute(self.sql(statement), params)
        return CursorAdapter(cur)

    def executemany(self, statement, seq):
        cur = self.conn.cursor()
        cur.executemany(self.sql(statement), seq)
        return CursorAdapter(cur)

    def executescript(self, script):
        statements = script if isinstance(script, list) else [s.strip() for s in script.split(";") if s.strip()]
        for statement in statements:
            try:
                self.execute(statement)
            except Exception:
                if str(statement).strip().upper().startswith("CREATE INDEX"):
                    continue
                raise

    def close(self):
        if not self.pooled:
            self.conn.close()
            return
        try:
            MYSQL_POOL.put_nowait(self.conn)
        except queue.Full:
            self.conn.close()


def create_mysql_connection():
    try:
        import pymysql
        import pymysql.cursors
    except ImportError as exc:
        raise RuntimeError("缺少 MySQL 驱动,请先执行: python3 -m pip install -r requirements.txt") from exc
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def db():
    global MYSQL_POOL_CREATED
    try:
        conn = MYSQL_POOL.get_nowait()
    except queue.Empty:
        with MYSQL_POOL_LOCK:
            if MYSQL_POOL_CREATED < MYSQL_POOL_SIZE:
                conn = create_mysql_connection()
                MYSQL_POOL_CREATED += 1
            else:
                conn = MYSQL_POOL.get(timeout=MYSQL_POOL_WAIT_SECONDS)
    try:
        conn.ping(reconnect=True)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        with MYSQL_POOL_LOCK:
            conn = create_mysql_connection()
    return MySQLAdapter(conn, pooled=True)


def wait_for_dependencies():
    deadline = time.time() + int(os.environ.get("STARTUP_WAIT_SECONDS", "120"))
    last_error = None
    while time.time() < deadline:
        try:
            with db() as conn:
                conn.execute("SELECT 1")
            if REDIS_URL:
                client = redis_client()
                if not client:
                    raise RuntimeError("Redis 驱动未安装或不可用")
                # 验证 Redis 连接
                client.ping()
            return
        except Exception as exc:
            last_error = exc
            logging.warning(f"等待 MySQL/Redis 就绪: {exc}")
            time.sleep(2)
    raise RuntimeError(f"MySQL/Redis 启动超时: {last_error}")


def schema_statements():
    pk = "BIGINT PRIMARY KEY AUTO_INCREMENT"
    idx_prefix = "CREATE INDEX"
    return [
        f"""CREATE TABLE IF NOT EXISTS admin (
            id {pk},
            username VARCHAR(64) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            last_login_at TIMESTAMP NULL,
            last_login_ip VARCHAR(45),
            created_at TIMESTAMP NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS admin_session (
            token VARCHAR(128) PRIMARY KEY,
            admin_id BIGINT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL,
            last_activity_at TIMESTAMP NOT NULL
        )""",
        f"{idx_prefix} idx_admin_session_expires ON admin_session(expires_at)",
        f"""CREATE TABLE IF NOT EXISTS product (
            id {pk},
            name VARCHAR(128) NOT NULL,
            intro TEXT NOT NULL,
            usage_text TEXT NOT NULL,
            cdk_prefix VARCHAR(8) NOT NULL,
            full_threshold INT NOT NULL DEFAULT 100,
            status INT NOT NULL DEFAULT 1,
            sort INT NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS inventory (
            id {pk},
            product_id BIGINT NOT NULL,
            content LONGTEXT NOT NULL,
            status INT NOT NULL DEFAULT 0,
            delivered_cdk_id BIGINT NULL,
            expire_at TIMESTAMP NULL,
            batch_no VARCHAR(64),
            created_at TIMESTAMP NOT NULL,
            delivered_at TIMESTAMP NULL
        )""",
        f"{idx_prefix} idx_inventory_product_status ON inventory(product_id,status)",
        f"{idx_prefix} idx_inventory_batch ON inventory(batch_no)",
        f"""CREATE TABLE IF NOT EXISTS cdk (
            id {pk},
            code VARCHAR(32) NOT NULL UNIQUE,
            product_id BIGINT NOT NULL,
            status INT NOT NULL DEFAULT 0,
            valid_days INT NOT NULL DEFAULT 0,
            generated_at TIMESTAMP NOT NULL,
            expire_at TIMESTAMP NULL,
            used_at TIMESTAMP NULL,
            used_ip VARCHAR(45),
            inventory_id BIGINT NULL,
            remark VARCHAR(255),
            batch_no VARCHAR(64)
        )""",
        f"{idx_prefix} idx_cdk_product_status ON cdk(product_id,status)",
        f"{idx_prefix} idx_cdk_status_expire ON cdk(status,expire_at)",
        f"""CREATE TABLE IF NOT EXISTS redeem_log (
            id {pk},
            type INT NOT NULL,
            cdk_code VARCHAR(32),
            product_id BIGINT NULL,
            ip VARCHAR(45),
            ua VARCHAR(255),
            result INT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )""",
        f"{idx_prefix} idx_redeem_cdk ON redeem_log(cdk_code)",
        f"{idx_prefix} idx_redeem_created ON redeem_log(created_at)",
        f"""CREATE TABLE IF NOT EXISTS admin_log (
            id {pk},
            admin_id BIGINT NULL,
            action VARCHAR(64) NOT NULL,
            target VARCHAR(255),
            ip VARCHAR(45),
            created_at TIMESTAMP NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS setting (
            skey VARCHAR(64) PRIMARY KEY,
            value TEXT
        )""",
    ]


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


def is_integrity_error(exc):
    name = exc.__class__.__name__.lower()
    return "integrity" in name or "unique" in str(exc).lower() or "duplicate" in str(exc).lower()


def redis_client():
    global REDIS_CLIENT
    if not REDIS_URL:
        return None
    if REDIS_CLIENT is not None:
        return REDIS_CLIENT
    try:
        import redis
    except ImportError:
        return None
    REDIS_CLIENT = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    REDIS_CLIENT.ping()
    return REDIS_CLIENT


def redis_key(*parts):
    return ":".join([REDIS_PREFIX, *[str(p) for p in parts]])


def rebuild_inventory_queue(conn, product_id):
    r = redis_client()
    if not r:
        return 0
    key = redis_key("inventory", product_id)
    rows = conn.execute(
        "SELECT id FROM inventory WHERE product_id=? AND status=0 ORDER BY id",
        (product_id,),
    ).fetchall()
    pipe = r.pipeline()
    pipe.delete(key)
    ids = [str(row["id"]) for row in rows]
    if ids:
        pipe.rpush(key, *ids)
        # 设置过期时间为30天，防止内存泄漏
        pipe.expire(key, 30 * 24 * 3600)
    pipe.execute()
    return len(ids)


def rebuild_all_inventory_queues(conn):
    r = redis_client()
    if not r:
        return 0
    total = 0
    for row in conn.execute("SELECT id FROM product").fetchall():
        total += rebuild_inventory_queue(conn, row["id"])
    return total


def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM setting WHERE skey=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO setting(skey,value) VALUES(?,?) ON DUPLICATE KEY UPDATE value=VALUES(value)",
        (key, str(value)),
    )


def ip_redeem_limit_config(conn):
    if str(get_setting(conn, "ip_redeem_limit_enabled", "1")) == "0":
        return None
    try:
        delay = max(1, int(get_setting(conn, "ip_redeem_limit_seconds", "60") or 60))
    except ValueError:
        delay = 60
    return {
        "delay": delay,
        "message": get_setting(conn, "ip_redeem_limit_text", "当前IP兑换频繁"),
    }


def ip_redeem_limit_key(ip):
    return redis_key("limit", "redeem_ip", ip)


def login_attempt_key(ip):
    """登录失败计数的 Redis key"""
    return redis_key("login_attempt", ip)


def check_login_attempts(ip):
    """检查登录失败次数（使用 Redis），返回 (是否允许, 剩余锁定秒数)"""
    r = redis_client()
    if not r:
        # Redis 不可用时降级为允许登录
        return True, 0

    key = login_attempt_key(ip)
    attempts = r.get(key)

    if attempts and int(attempts) >= 10:
        ttl = r.ttl(key)
        return False, max(0, ttl)

    return True, 0


def record_login_attempt(ip, success):
    """记录登录尝试（使用 Redis）"""
    r = redis_client()
    if not r:
        return

    key = login_attempt_key(ip)

    if success:
        # 登录成功，清除记录
        r.delete(key)
    else:
        # 登录失败，增加计数
        attempts = r.incr(key)
        if attempts == 1:
            # 首次失败，设置5分钟过期
            r.expire(key, 300)
        elif attempts >= 10:
            # 失败10次，锁定30分钟
            r.expire(key, 1800)


def is_trusted_proxy(ip):
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for rule in TRUSTED_PROXIES:
        try:
            if "/" in rule:
                if ip_obj in ipaddress.ip_network(rule, strict=False):
                    return True
            elif ip_obj == ipaddress.ip_address(rule):
                return True
        except ValueError:
            continue
    return False


def clean_header_ip(value):
    value = (value or "").strip()
    if not value:
        return ""
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        return ""


def get_real_ip(headers, client_address):
    """安全地获取真实IP地址，仅在来自可信代理时才使用转发头"""
    direct_ip = client_address[0]

    # Cloudflare 模式：优先使用 CF-Connecting-IP
    cf_ip = clean_header_ip(headers.get("CF-Connecting-IP", ""))
    if cf_ip and is_trusted_proxy(direct_ip):
        return cf_ip

    # 如果直连IP不在可信代理列表中，直接返回
    if not is_trusted_proxy(direct_ip):
        return direct_ip

    # 来自可信代理，解析转发头
    real_ip = clean_header_ip(headers.get("X-Real-IP", ""))
    if real_ip:
        return real_ip

    # 解析 X-Forwarded-For，取第一个IP（客户端IP）
    forwarded = headers.get("X-Forwarded-For", "")
    if forwarded:
        for ip in forwarded.split(","):
            candidate = clean_header_ip(ip)
            if candidate:
                return candidate

    # 回退到直连IP
    return direct_ip


def admin_log(conn, admin_id, action, target, ip):
    conn.execute(
        "INSERT INTO admin_log(admin_id, action, target, ip, created_at) VALUES(?,?,?,?,?)",
        (admin_id, action, target, ip, now_str()),
    )


def init_db():
    with db() as conn:
        conn.executescript(schema_statements())

        # 添加 last_activity_at 列（如果不存在）
        try:
            conn.execute("ALTER TABLE admin_session ADD COLUMN last_activity_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")
        except Exception:
            pass

        if not conn.execute("SELECT 1 FROM admin LIMIT 1").fetchone():
            username = os.environ.get("ADMIN_USERNAME", "Fyanxv")
            password = os.environ.get("ADMIN_PASSWORD", "Fyb2530+")
            conn.execute(
                "INSERT INTO admin(username,password_hash,created_at) VALUES(?,?,?)",
                (username, hash_password(password), now_str()),
            )
            logging.info(f"已创建默认管理员账号: {username}")
        defaults = {
            "site_name": "兑换中心",
            "query_max": "50",
            "redeem_fail_text": "卡密无效或已使用",
            "ip_redeem_limit_enabled": "1",
            "ip_redeem_limit_seconds": "60",
            "ip_redeem_limit_text": "当前IP兑换频繁",
            "frontend_history_keep": "200",
            "frontend_stock_visible": "1",
            "frontend_stock_refresh_seconds": "15",
            "announcement": "",
        }
        for key, value in defaults.items():
            if not conn.execute("SELECT 1 FROM setting WHERE skey=?", (key,)).fetchone():
                conn.execute("INSERT INTO setting(skey,value) VALUES(?,?)", (key, value))
        r = redis_client()
        if r and r.set(redis_key("lock", "queue_rebuild"), "1", nx=True, ex=60):
            try:
                rebuild_all_inventory_queues(conn)
            finally:
                r.delete(redis_key("lock", "queue_rebuild"))


class App(BaseHTTPRequestHandler):
    server_version = "CDKExchange/1.0"

    def log_message(self, fmt, *args):
        # 记录关键请求日志
        if self.command in ("POST", "PUT", "DELETE"):
            logging.info(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False, default=json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 添加安全响应头
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-XSS-Protection", "1; mode=block")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, msg, status=200):
        self.send_json({"code": code, "msg": msg, "data": {}}, status)

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0") or "0")
        # 限制请求体大小
        if size > MAX_REQUEST_BODY_SIZE:
            raise ValueError(f"请求体过大，最大允许 {MAX_REQUEST_BODY_SIZE} 字节")
        raw = self.rfile.read(size) if size else b"{}"
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            data = parse_qs(raw.decode("utf-8"))
            return {k: v[0] if len(v) == 1 else v for k, v in data.items()}

    def ip(self):
        return get_real_ip(self.headers, self.client_address)

    def cookie_token(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return jar.get("admin_session").value if jar.get("admin_session") else ""

    def generate_csrf_token(self, admin_id):
        """生成 CSRF token（使用 HMAC 签名）"""
        timestamp = str(int(time.time()))
        data = f"{admin_id}:{timestamp}"
        signature = hmac.new(
            CSRF_SECRET.encode(),
            data.encode(),
            hashlib.sha256
        ).hexdigest()
        token = f"{data}:{signature}"
        return base64.urlsafe_b64encode(token.encode()).decode()

    def verify_csrf_token(self, token, admin_id):
        """验证 CSRF token（24小时有效）"""
        try:
            decoded = base64.urlsafe_b64decode(token.encode()).decode()
            parts = decoded.split(":")
            if len(parts) != 3:
                return False
            token_admin_id, timestamp, signature = parts

            # 验证 admin_id
            if str(token_admin_id) != str(admin_id):
                return False

            # 验证时间戳（24小时有效）
            if time.time() - int(timestamp) > 86400:
                return False

            # 验证签名
            data = f"{token_admin_id}:{timestamp}"
            expected_signature = hmac.new(
                CSRF_SECRET.encode(),
                data.encode(),
                hashlib.sha256
            ).hexdigest()

            return hmac.compare_digest(signature, expected_signature)
        except Exception:
            return False

    def current_admin(self, conn):
        token = self.cookie_token()
        if not token:
            return None
        row = conn.execute(
            """SELECT a.*, s.last_activity_at FROM admin_session s JOIN admin a ON a.id=s.admin_id
               WHERE s.token=? AND s.expires_at>?""",
            (token, now_str()),
        ).fetchone()
        if not row:
            return None

        # 检查会话活动超时（2小时无活动则过期）
        last_activity = row.get("last_activity_at")
        if last_activity:
            if isinstance(last_activity, str):
                last_activity = datetime.strptime(last_activity, "%Y-%m-%d %H:%M:%S")
            if beijing_now() - last_activity > timedelta(hours=2):
                conn.execute("DELETE FROM admin_session WHERE token=?", (token,))
                return None

        # 更新最后活动时间
        conn.execute("UPDATE admin_session SET last_activity_at=? WHERE token=?", (now_str(), token))
        return row_dict(row)

    def require_admin(self, conn):
        admin = self.current_admin(conn)
        if not admin:
            self.fail(2001, "未登录 / Token 无效", 401)
            return None
        return admin

    def require_csrf(self, conn, admin):
        """验证 CSRF token（仅对修改操作）"""
        if self.command in ("POST", "PUT", "DELETE"):
            token = self.headers.get("X-CSRF-Token", "")
            if not token:
                self.fail(2003, "CSRF 验证失败", 403)
                return False
            if not self.verify_csrf_token(token, admin["id"]):
                self.fail(2003, "CSRF 验证失败", 403)
                return False
        return True

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
        # 严格的路径验证，防止路径遍历
        try:
            target.relative_to(ROOT)
        except ValueError:
            self.send_error(404)
            return
        if not target.exists() or target.is_dir():
            self.send_error(404)
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix in (".html", ".md", ".txt"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 添加安全响应头
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-XSS-Protection", "1; mode=block")
        self.end_headers()
        self.wfile.write(data)

    def route_api(self, method, path):
        try:
            with db() as conn:
                if path == "/api/site" and method == "GET":
                    return self.ok({r["skey"]: r["value"] for r in conn.execute("SELECT skey,value FROM setting")})
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
                    if admin:
                        csrf_token = self.generate_csrf_token(admin["id"])
                        return self.ok({"username": admin["username"], "csrf_token": csrf_token})
                    return None
                # 健康检查接口 - 仅允许本地访问
                if path == "/api/health" and method == "GET":
                    client_ip = self.client_address[0]
                    if client_ip not in ("127.0.0.1", "::1", "localhost"):
                        return self.fail(403, "禁止访问", 403)
                    return self.health_check(conn)
                admin = self.require_admin(conn)
                if not admin:
                    return
                # 验证 CSRF token
                if not self.require_csrf(conn, admin):
                    return
                return self.admin_routes(conn, admin, method, path)
        except ValueError as exc:
            logging.warning(f"请求验证失败: {exc}")
            self.fail(4000, str(exc), 400)
        except Exception as exc:
            logging.error(f"API 错误: {exc}\n{traceback.format_exc()}")
            self.fail(5000, "服务器繁忙,请稍后再试", 500)

    def health_check(self, conn):
        """健康检查接口"""
        try:
            conn.execute("SELECT 1")
            redis_ok = False
            if REDIS_URL:
                r = redis_client()
                if r:
                    r.ping()
                    redis_ok = True
            return self.ok({
                "status": "healthy",
                "mysql": "ok",
                "redis": "ok" if redis_ok else "disabled",
                "timestamp": now_str()
            })
        except Exception as exc:
            logging.error(f"健康检查失败: {exc}")
            self.fail(5000, "服务不可用", 503)

    def public_stocks(self, conn):
        rows = conn.execute(
            """SELECT p.id product_id,p.name,p.full_threshold,
                      (SELECT COUNT(*) FROM inventory i WHERE i.product_id=p.id AND i.status=0) stock
               FROM product p
               WHERE p.status=1
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
        ip = self.ip()
        r = redis_client()
        ip_limit = ip_redeem_limit_config(conn)
        if ip_limit and r and r.exists(ip_redeem_limit_key(ip)):
            return self.fail(1007, ip_limit["message"])
        if not CDK_RE.match(code):
            self.write_redeem_log(conn, code, None, 2)
            return self.fail(1001, fail_text)
        if r:
            return self.public_redeem_redis(conn, r, code, fail_text, ip, ip_limit)
        return self.fail(5001, "Redis 未启用,秒抢兑换已暂停")

    def public_redeem_redis(self, conn, r, code, fail_text, ip, ip_limit):
        lock_key = redis_key("lock", "cdk", code)
        lock_token = secrets.token_urlsafe(16)
        if not r.set(lock_key, lock_token, nx=True, ex=30):
            return self.fail(1007, "卡密正在处理中,请勿重复提交")
        inv = None
        product = None
        cdk = None
        queue_key = None
        try:
            cdk = conn.execute("SELECT * FROM cdk WHERE code=?", (code,)).fetchone()
            if not cdk:
                self.write_redeem_log(conn, code, None, 2)
                return self.fail(1001, fail_text)
            if cdk["expire_at"] and cdk["expire_at"] < now_str():
                conn.execute("UPDATE cdk SET status=3 WHERE id=?", (cdk["id"],))
                self.write_redeem_log(conn, code, cdk["product_id"], 4)
                return self.fail(1004, "卡密已过期")
            if cdk["status"] == 1:
                self.write_redeem_log(conn, code, cdk["product_id"], 3)
                return self.fail(1002, "卡密已使用")
            if cdk["status"] != 0:
                self.write_redeem_log(conn, code, cdk["product_id"], 4)
                return self.fail(1003, fail_text)
            product = conn.execute("SELECT * FROM product WHERE id=? AND status=1", (cdk["product_id"],)).fetchone()
            if not product:
                self.write_redeem_log(conn, code, cdk["product_id"], 5)
                return self.fail(1005, "商品已下架")
            queue_key = redis_key("inventory", product["id"])
            # 优化：减少重试次数，提高性能
            for attempt in range(5):
                inventory_id = r.lpop(queue_key)
                if inventory_id is None:
                    break
                inv = conn.execute(
                    "SELECT * FROM inventory WHERE id=? AND product_id=? AND status=0",
                    (int(inventory_id), product["id"]),
                ).fetchone()
                if inv:
                    break
            if not inv:
                self.write_redeem_log(conn, code, product["id"], 5)
                return self.fail(1006, "库存不足")
            try:
                conn.execute("BEGIN IMMEDIATE")
                fresh = conn.execute("SELECT status FROM cdk WHERE id=? FOR UPDATE", (cdk["id"],)).fetchone()
                fresh_inv = conn.execute("SELECT status FROM inventory WHERE id=? FOR UPDATE", (inv["id"],)).fetchone()
                if not fresh or fresh["status"] != 0 or not fresh_inv or fresh_inv["status"] != 0:
                    conn.execute("ROLLBACK")
                    # 库存未被使用，放回队列
                    if fresh_inv and fresh_inv["status"] == 0 and queue_key:
                        r.lpush(queue_key, str(inv["id"]))
                    return self.fail(1002, "卡密或库存已被处理")
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
                # 只有成功兑换后才设置IP限制
                if ip_limit:
                    r.set(ip_redeem_limit_key(ip), "1", ex=ip_limit["delay"])
                return self.ok({
                    "product": {"id": product["id"], "name": product["name"], "intro": product["intro"], "usage_text": product["usage_text"]},
                    "content": inv["content"],
                    "time": t,
                })
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                # 回滚时将库存放回队列
                if inv and queue_key:
                    r.lpush(queue_key, str(inv["id"]))
                raise
        finally:
            r.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                1,
                lock_key,
                lock_token,
            )

    def write_redeem_log(self, conn, code, product_id, result):
        conn.execute(
            "INSERT INTO redeem_log(type,cdk_code,product_id,ip,ua,result,created_at) VALUES(?,?,?,?,?,?,?)",
            (1, code, product_id, self.ip(), self.headers.get("User-Agent", "")[:255], result, now_str()),
        )

    def admin_login(self, conn):
        payload = self.read_json()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        ip = self.ip()

        # 检查登录失败次数
        allowed, lock_seconds = check_login_attempts(ip)
        if not allowed:
            logging.warning(f"登录尝试被锁定: IP={ip}, 剩余锁定时间={lock_seconds}秒")
            return self.fail(2004, f"登录失败次数过多，请 {lock_seconds} 秒后再试", 429)

        admin = conn.execute("SELECT * FROM admin WHERE username=?", (username,)).fetchone()
        if not admin or not verify_password(password, admin["password_hash"]):
            record_login_attempt(ip, False)
            logging.warning(f"登录失败: username={username}, IP={ip}")
            return self.fail(2001, "账号或密码错误", 401)

        # 登录成功，清除失败记录
        record_login_attempt(ip, True)

        # 清理该用户的旧会话
        conn.execute("DELETE FROM admin_session WHERE admin_id=?", (admin["id"],))

        token = secrets.token_urlsafe(32)
        expires = (beijing_now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        now = now_str()
        conn.execute(
            "INSERT INTO admin_session(token,admin_id,expires_at,created_at,last_activity_at) VALUES(?,?,?,?,?)",
            (token, admin["id"], expires, now, now)
        )
        conn.execute("UPDATE admin SET last_login_at=?,last_login_ip=? WHERE id=?", (now, ip, admin["id"]))
        admin_log(conn, admin["id"], "login", username, ip)

        # 设置安全的 Cookie
        cookie_flags = "Path=/; HttpOnly; SameSite=Strict"
        # 如果是 HTTPS，添加 Secure 标志
        if self.headers.get("X-Forwarded-Proto") == "https" or self.command == "https":
            cookie_flags += "; Secure"

        logging.info(f"管理员登录成功: username={username}, IP={ip}")
        self.ok({"username": username}, headers={"Set-Cookie": f"admin_session={token}; {cookie_flags}"})

    def ok(self, data=None, msg="ok", headers=None):
        self.send_json({"code": 0, "msg": msg, "data": data if data is not None else {}}, 200, headers)

    def admin_logout(self, conn):
        token = self.cookie_token()
        if token:
            conn.execute("DELETE FROM admin_session WHERE token=?", (token,))
            logging.info(f"管理员退出登录: token={token[:8]}...")
        cookie_flags = "Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
        if self.headers.get("X-Forwarded-Proto") == "https":
            cookie_flags += "; Secure"
        self.ok({}, headers={"Set-Cookie": f"admin_session=; {cookie_flags}"})

    def admin_routes(self, conn, admin, method, path):
        if path == "/admin/dashboard/summary" and method == "GET":
            today_start = beijing_now().strftime("%Y-%m-%d 00:00:00")
            day_ago = (beijing_now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            return self.ok({
                "products": conn.execute("SELECT COUNT(*) c FROM product").fetchone()["c"],
                "inventory": conn.execute("SELECT COUNT(*) c FROM inventory WHERE status=0").fetchone()["c"],
                "cdks": conn.execute("SELECT COUNT(*) c FROM cdk").fetchone()["c"],
                "unused_cdks": conn.execute("SELECT COUNT(*) c FROM cdk WHERE status=0").fetchone()["c"],
                "used_cdks": conn.execute("SELECT COUNT(*) c FROM cdk WHERE status=1").fetchone()["c"],
                "disabled_cdks": conn.execute("SELECT COUNT(*) c FROM cdk WHERE status IN (2,3)").fetchone()["c"],
                "today_redeem": conn.execute("SELECT COUNT(*) c FROM redeem_log WHERE type=1 AND result=1 AND created_at>=?", (today_start,)).fetchone()["c"],
                "query_24h": conn.execute("SELECT COUNT(*) c FROM redeem_log WHERE type=2 AND created_at>=?", (day_ago,)).fetchone()["c"],
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
            # 白名单字段，防止SQL注入
            allowed_fields = {"name", "intro", "usage_text", "cdk_prefix", "full_threshold", "status"}
            fields = [f for f in allowed_fields if f in p]
            if not fields:
                return self.ok()
            updates = [f"{f}=?" for f in fields]
            values = [str(p[f]).upper() if f == "cdk_prefix" else p[f] for f in fields]
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
            r = redis_client()
            if r:
                r.delete(redis_key("inventory", pid))
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
            batch_no = beijing_now().strftime("%Y%m%d%H%M%S")

            # 使用事务保证数据一致性
            try:
                conn.execute("BEGIN IMMEDIATE")
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
                conn.execute("COMMIT")
                rebuild_inventory_queue(conn, product_id)
                admin_log(conn, admin["id"], "import_inventory", f"product #{product_id} +{len(lines)}", self.ip())
                return self.ok({"count": len(lines), "batch_no": batch_no})
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        if path.startswith("/admin/inventory/") and method == "DELETE":
            iid = int(path.rsplit("/", 1)[-1])
            row = conn.execute("SELECT product_id FROM inventory WHERE id=?", (iid,)).fetchone()
            conn.execute("DELETE FROM inventory WHERE id=? AND status=0", (iid,))
            if row:
                r = redis_client()
                if r:
                    r.lrem(redis_key("inventory", row["product_id"]), 0, str(iid))
            admin_log(conn, admin["id"], "delete_inventory", f"#{iid}", self.ip())
            return self.ok()
        if path == "/admin/cache/rebuild" and method == "POST":
            if not redis_client():
                return self.fail(5001, "Redis 未启用", 400)
            total = rebuild_all_inventory_queues(conn)
            admin_log(conn, admin["id"], "rebuild_cache", f"inventory {total}", self.ip())
            return self.ok({"queued": total})
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
            expire_at = (beijing_now() + timedelta(days=valid_days)).strftime("%Y-%m-%d %H:%M:%S") if valid_days else None
            batch_no = beijing_now().strftime("%Y%m%d%H%M%S")
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
                except Exception as exc:
                    if is_integrity_error(exc):
                        continue
                    raise
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
            return self.ok({r["skey"]: r["value"] for r in conn.execute("SELECT skey,value FROM setting")})
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
            new_pwd = str(p.get("new_password", ""))
            if new_pwd != str(p.get("confirm_password", "")):
                return self.fail(2002, "两次输入的新密码不一致", 400)
            if len(new_pwd) < 6:
                return self.fail(2002, "新密码至少 6 位", 400)
            conn.execute("UPDATE admin SET password_hash=? WHERE id=?", (hash_password(new_pwd), admin["id"]))
            # 修改密码后清除所有会话，强制重新登录
            conn.execute("DELETE FROM admin_session WHERE admin_id=?", (admin["id"],))
            admin_log(conn, admin["id"], "change_password", admin["username"], self.ip())
            logging.info(f"管理员修改密码: username={admin['username']}")
            return self.ok()
        self.fail(404, "接口不存在", 404)

    def admin_products(self, conn):
        rows = conn.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM inventory i WHERE i.product_id=p.id AND i.status=0) stock,
                      (SELECT COUNT(*) FROM inventory i WHERE i.product_id=p.id AND i.status=1) delivered
               FROM product p
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


class WSGIHeaders:
    def __init__(self, environ):
        self.values = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                name = key[5:].replace("_", "-").title()
                self.values[name.lower()] = value
        if environ.get("CONTENT_TYPE"):
            self.values["content-type"] = environ["CONTENT_TYPE"]
        if environ.get("CONTENT_LENGTH"):
            self.values["content-length"] = environ["CONTENT_LENGTH"]

    def get(self, key, default=None):
        return self.values.get(key.lower(), default)


class WSGIRequest(App):
    def __init__(self, environ):
        self.environ = environ
        self.command = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO") or "/"
        query = environ.get("QUERY_STRING") or ""
        self.path = path + (("?" + query) if query else "")
        self.headers = WSGIHeaders(environ)
        size = int(environ.get("CONTENT_LENGTH") or 0)
        self.rfile = io.BytesIO(environ["wsgi.input"].read(size) if size else b"")
        self.wfile = io.BytesIO()
        self.client_address = (environ.get("REMOTE_ADDR") or "127.0.0.1", 0)
        self.response_status = 200
        self.response_headers = []

    def send_response(self, code, message=None):
        self.response_status = code

    def send_header(self, key, value):
        self.response_headers.append((key, str(value)))

    def end_headers(self):
        return

    def send_error(self, code, message=None):
        text = (message or HTTPStatus(code).phrase).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(text)))
        self.end_headers()
        self.wfile.write(text)

    def finish(self, start_response):
        phrase = HTTPStatus(self.response_status).phrase
        start_response(f"{self.response_status} {phrase}", self.response_headers)
        return [self.wfile.getvalue()]


def ensure_started():
    global STARTUP_DONE
    if STARTUP_DONE:
        return
    with STARTUP_LOCK:
        if STARTUP_DONE:
            return
        wait_for_dependencies()
        init_db()
        STARTUP_DONE = True


def application(environ, start_response):
    try:
        ensure_started()
        req = WSGIRequest(environ)
        path = urlparse(req.path).path
        if path.startswith("/api/") or path.startswith("/admin/"):
            req.route_api(req.command, path)
        else:
            req.serve_static(path)
        return req.finish(start_response)
    except Exception as exc:
        logging.error(f"WSGI 错误: {exc}\n{traceback.format_exc()}")
        payload = {"code": 5000, "msg": "服务器繁忙,请稍后再试", "data": {}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        start_response("503 Service Unavailable", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ])
        return [body]


if __name__ == "__main__":
    ensure_started()
    logging.info(f"CDK 兑换系统已启动: http://{HOST}:{PORT}")
    logging.info("后台登录地址: /login.html")
    logging.info("健康检查接口: /api/health")
    LocalHTTPServer((HOST, PORT), App).serve_forever()
