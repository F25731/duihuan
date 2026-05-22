# CDK 兑换系统

纯 HTML 前端 + Python 后端。默认使用 SQLite，开启 Redis 后会进入抢兑队列模式。

## 本地启动

```bash
python app.py
```

默认访问:

- 前台: http://127.0.0.1:8765/
- 后台登录: http://127.0.0.1:8765/login.html

默认后台账号:

- 用户名: `Fyanxv`
- 密码: `Fyb2530+`

首次运行会自动创建 `data/cdk_exchange.db`。

## 修改端口

```bash
PORT=8080 python app.py
```

Windows PowerShell:

```powershell
$env:PORT=8080; python app.py
```

## 修改首次默认密码

仅在数据库还没初始化时生效:

```powershell
$env:ADMIN_USERNAME="your-name"; $env:ADMIN_PASSWORD="your-password"; python app.py
```

后台登录后也可以在「站点设置」里修改管理员密码。

## 抢兑模式

如果预计几千人在几秒内同时兑换，建议开启 Redis。开启后兑换流程会变成:

1. 同一卡密先拿 Redis 短锁，避免重复提交。
2. 商品库存从 Redis 队列原子弹出，先到先得。
3. 数据库事务落库，标记卡密已使用、库存已发货。
4. 如果落库失败，库存 ID 会放回队列。

安装依赖:

```bash
python3 -m pip install -r requirements.txt
```

启动 Redis 后运行:

```bash
REDIS_URL=redis://127.0.0.1:6379/0 HOST=0.0.0.0 PORT=8877 python3 app.py
```

后台导入库存时会自动重建对应商品的 Redis 库存队列。如果你手工改过数据库，可以登录后台后调用 `/admin/cache/rebuild` 重建全部库存队列。

这个版本仍保留 SQLite 开发模式。真正持续高并发生产环境建议配合 Redis、反向代理、进程守护，并在业务量继续增长后把 SQLite 升级到 MySQL/PostgreSQL。
