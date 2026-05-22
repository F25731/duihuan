# CDK 兑换系统

纯 HTML 前端 + Python 标准库后端 + SQLite 数据库。

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
