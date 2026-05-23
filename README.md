# CDK 兑换系统

纯 HTML 前端 + Python 后端。生产架构固定为 MySQL + Redis:

- MySQL: 商品、库存、卡密、兑换记录、后台账号等持久化数据。
- Redis: 秒抢库存队列、同一卡密并发锁、登录限流。

## 安全特性

- ✅ CSRF 保护（HMAC 签名，所有管理后台修改操作）
- ✅ 会话管理（2小时活动超时，7天绝对过期）
- ✅ 登录保护（Redis 存储，10次失败锁定30分钟，多 worker 统一）
- ✅ IP 限流（可配置兑换频率限制）
- ✅ 请求体大小限制（默认10MB）
- ✅ 安全响应头（X-Frame-Options, X-Content-Type-Options 等）
- ✅ 路径遍历防护
- ✅ SQL 注入防护（参数化查询 + 字段白名单）
- ✅ Redis 密码认证
- ✅ Docker 非特权用户运行
- ✅ 健康检查接口（仅本地访问）
- ✅ 可信代理验证（防止 IP 伪造）

## 重要安全配置

### 1. 可信代理设置

如果使用 Nginx/Caddy 等反向代理，**必须**配置 `TRUSTED_PROXIES` 环境变量：

```bash
# 单个代理
TRUSTED_PROXIES="127.0.0.1"

# 多个代理（支持 IP 和 CIDR，逗号分隔）
TRUSTED_PROXIES="127.0.0.1,::1,172.16.0.0/12"
```

**重要**:
- 如果不配置，系统将**忽略** `X-Forwarded-For` 和 `X-Real-IP` 头，直接使用连接IP
- 这可以防止攻击者伪造IP绕过登录限制和兑换限流
- 生产环境建议只允许反向代理访问后端端口（防火墙规则）

### 2. CSRF Secret

首次部署时建议设置固定的 CSRF secret，确保多 worker 和重启后后台操作稳定：

```bash
# 生成随机 secret
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 设置环境变量
CSRF_SECRET="your_generated_secret_here"
```

### 3. Nginx 反向代理配置示例

```nginx
upstream duihuan_backend {
    server 127.0.0.1:8877;
}

server {
    listen 80;
    server_name your-domain.com;

    # 强制 HTTPS
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # 安全响应头
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;

    location / {
        proxy_pass http://duihuan_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**配置后端可信代理**：
```bash
# docker-compose.yml 中添加
TRUSTED_PROXIES: "127.0.0.1,::1,172.16.0.0/12"
```

## 后台账号

- 登录地址: `/login.html`
- 默认用户名: `Fyanxv`
- 默认密码: `Fyb2530+`

**重要**: 默认账号只在数据库第一次初始化、后台还没有管理员时写入。首次登录后请立即修改密码。

## 服务器直跑部署

安装依赖:

```bash
cd /opt/duihuan
python3 -m pip install -r requirements.txt
```

创建 MySQL 数据库和账号:

```sql
CREATE DATABASE duihuan CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'duihuan'@'127.0.0.1' IDENTIFIED BY 'change_duihuan_password';
GRANT ALL PRIVILEGES ON duihuan.* TO 'duihuan'@'127.0.0.1';
FLUSH PRIVILEGES;
```

配置 Redis 密码（推荐）:

```bash
# 编辑 redis.conf
requirepass your_redis_password
```

启动:

```bash
cd /opt/duihuan
pkill -9 -f "python3 app.py"
MYSQL_HOST=127.0.0.1 \
MYSQL_PORT=3306 \
MYSQL_USER=duihuan \
MYSQL_PASSWORD='change_duihuan_password' \
MYSQL_DATABASE=duihuan \
REDIS_URL=redis://:your_redis_password@127.0.0.1:6379/0 \
HOST=0.0.0.0 \
PORT=8877 \
nohup python3 app.py > app.log 2>&1 &
```

查看日志:

```bash
tail -f /opt/duihuan/app.log
```

健康检查:

```bash
curl http://localhost:8877/api/health
```

## Docker Compose

**重要**: 修改 `docker-compose.yml` 里的所有密码:

```bash
nano docker-compose.yml
# 修改以下密码:
# - MYSQL_ROOT_PASSWORD
# - MYSQL_PASSWORD (两处)
# - Redis requirepass (command 参数)
# - REDIS_URL (包含 Redis 密码)
# - CSRF_SECRET
# - TURNSTILE_SECRET
# - ADMIN_PASSWORD (可选，建议首次登录后在后台修改)
```

启动:

```bash
docker compose up -d --build
```

查看状态和日志:

```bash
docker compose ps
docker compose logs -f duihuan
```

健康检查:

```bash
docker compose exec duihuan curl http://localhost:8877/api/health
```

默认容器配置:

- `WEB_WORKERS=4`
- `WEB_THREADS=8`
- `MYSQL_POOL_SIZE=12`
- `WEB_TIMEOUT=120`
- 资源限制: CPU 2核, 内存 1GB

总 MySQL 连接上限约等于 `WEB_WORKERS * MYSQL_POOL_SIZE`。如果继续提高 worker 或连接池,也要同步确认 MySQL `max-connections` 足够。

## 高并发配置

使用 `docker-compose.high-concurrency.yml` 获得更高性能:

```bash
docker compose -f docker-compose.high-concurrency.yml up -d --build
```

高并发配置资源限制:
- MySQL: CPU 4核, 内存 2GB
- Redis: CPU 2核, 内存 1GB
- 应用: CPU 4核, 内存 2GB

## 秒抢说明

兑换时后端会先给卡密加 Redis 短锁，再从 Redis 商品库存队列 `LPOP` 一个库存 ID，最后进入 MySQL 事务二次确认卡密和库存状态后落库。

后台导入、删除库存会同步刷新 Redis 队列。如果你手工改过 MySQL，可以登录后台后调用 `/admin/cache/rebuild` 重建全部商品库存队列。

后台「站点设置」可以开启 IP 兑换限制。开启后同一个 IP 在设定秒数内只能发起一次**成功**兑换请求，默认提示词为 `当前IP兑换频繁`。

## 监控和维护

### 健康检查

```bash
# 检查服务状态
curl http://localhost:8877/api/health

# 返回示例
{
  "code": 0,
  "msg": "ok",
  "data": {
    "status": "healthy",
    "mysql": "ok",
    "redis": "ok",
    "timestamp": "2026-05-23 19:30:00"
  }
}
```

### 日志查看

```bash
# Docker 环境
docker compose logs -f duihuan

# 直接运行
tail -f /opt/duihuan/app.log
```

### 会话清理

系统会自动清理过期会话和登录尝试记录。如需手动清理:

```sql
-- 清理过期会话
DELETE FROM admin_session WHERE expires_at < NOW();

-- 清理过期兑换日志（可选，保留最近30天）
DELETE FROM redeem_log WHERE created_at < DATE_SUB(NOW(), INTERVAL 30 DAY);
```

## 安全建议

1. **修改默认密码**: 首次登录后立即在后台修改管理员密码
2. **使用 HTTPS**: 在生产环境使用 Nginx/Caddy 反向代理并启用 HTTPS
3. **配置防火墙**: 仅开放必要端口（80/443）
4. **Redis 密码**: 务必为 Redis 设置强密码
5. **定期备份**: 定期备份 MySQL 数据库
6. **监控日志**: 关注异常登录和兑换行为
7. **Cloudflare**: 建议使用 Cloudflare 提供额外的 DDoS 防护和 WAF

## 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `HOST` | 127.0.0.1 | 监听地址 |
| `PORT` | 8765 | 监听端口 |
| `MYSQL_HOST` | 127.0.0.1 | MySQL 地址 |
| `MYSQL_PORT` | 3306 | MySQL 端口 |
| `MYSQL_USER` | duihuan | MySQL 用户名 |
| `MYSQL_PASSWORD` | - | MySQL 密码 |
| `MYSQL_DATABASE` | duihuan | MySQL 数据库名 |
| `MYSQL_POOL_SIZE` | 12 | 连接池大小 |
| `REDIS_URL` | - | Redis 连接 URL（格式: redis://:password@host:port/db） |
| `REDIS_PREFIX` | duihuan | Redis 键前缀 |
| `MAX_REQUEST_BODY_SIZE` | 10485760 | 请求体最大大小（字节） |
| `ADMIN_USERNAME` | Fyanxv | 默认管理员用户名 |
| `ADMIN_PASSWORD` | Fyb2530+ | 默认管理员密码 |
| `CSRF_SECRET` | 自动生成 | CSRF token 签名密钥（建议固定） |
| `TRUSTED_PROXIES` | 127.0.0.1,::1 | 可信代理IP列表（逗号分隔） |

## 故障排查

### Redis 连接失败

检查 Redis URL 格式是否正确，密码是否匹配:

```bash
# 测试 Redis 连接
redis-cli -h 127.0.0.1 -p 6379 -a your_password ping
```

### MySQL 连接失败

检查 MySQL 用户权限和网络连接:

```bash
# 测试 MySQL 连接
mysql -h 127.0.0.1 -u duihuan -p duihuan
```

### 登录被锁定

Redis 存储，等待30分钟或手动清理：

```bash
# 方法1: 使用 redis-cli 清理特定 IP
redis-cli -a your_redis_password
> DEL duihuan:login_attempt:192.168.1.100

# 方法2: 清理所有登录尝试记录
redis-cli -a your_redis_password --scan --pattern "duihuan:login_attempt:*" | xargs redis-cli -a your_redis_password DEL
```

### 库存不一致

重建 Redis 库存队列:

```bash
# 登录后台，访问（需要管理员权限）
curl -X POST http://localhost:8877/admin/cache/rebuild \
  -H "Cookie: admin_session=YOUR_SESSION_TOKEN" \
  -H "X-CSRF-Token: YOUR_CSRF_TOKEN"
```
