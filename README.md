# CDK 兑换系统

纯 HTML 前端 + Python 后端。生产架构固定为 MySQL + Redis:

- MySQL: 商品、库存、卡密、兑换记录、后台账号等持久化数据。
- Redis: 秒抢库存队列、同一卡密并发锁。

## 后台账号

- 登录地址: `/login.html`
- 默认用户名: `Fyanxv`
- 默认密码: `Fyb2530+`

默认账号只在数据库第一次初始化、后台还没有管理员时写入。后续可在后台修改密码。

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

启动:

```bash
cd /opt/duihuan
pkill -9 -f "python3 app.py"
MYSQL_HOST=127.0.0.1 \
MYSQL_PORT=3306 \
MYSQL_USER=duihuan \
MYSQL_PASSWORD='change_duihuan_password' \
MYSQL_DATABASE=duihuan \
REDIS_URL=redis://127.0.0.1:6379/0 \
HOST=0.0.0.0 \
PORT=8877 \
nohup python3 app.py > app.log 2>&1 &
```

查看日志:

```bash
tail -n 80 /opt/duihuan/app.log
```

## Docker Compose

先修改 `docker-compose.yml` 里的两个 MySQL 密码:

```bash
nano docker-compose.yml
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

## 秒抢说明

兑换时后端会先给卡密加 Redis 短锁，再从 Redis 商品库存队列 `LPOP` 一个库存 ID，最后进入 MySQL 事务二次确认卡密和库存状态后落库。

后台导入、删除库存会同步刷新 Redis 队列。如果你手工改过 MySQL，可以登录后台后调用 `/admin/cache/rebuild` 重建全部商品库存队列。
