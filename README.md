# 登山计数 · Garmin 手表同步后端

## 它做什么

定时从 Garmin（中国区 `garmin.cn`）拉取**越野跑**活动，存成「待确认」；手机 App 打开时自动拉取，你在手机上**确认添加 / 忽略**，添加的记录直接写进本地登山计数。

数据流：

```
Garmin 手表
   │  每 30 分钟（可配）
   ▼
后端（Railway 容器）拉取并过滤 trail_running
   │  存「待确认」桶
   ▼
手机 App 打开自动拉取 → 你逐条确认（可改山名/次数）
   │  添加 → 写入本地 records
   ▼
登山计数统计更新
```

因为 Garmin 账号没有开 MFA，后端用「账号 + 密码」直连，无需你每次验证。

## 文件

- `main.py` — FastAPI 服务（后台定时拉取线程 + 三个接口）
- `requirements.txt` — 依赖
- `Dockerfile` — 容器构建（Railway 会自动识别）
- `.env.example` — 环境变量模板

## 环境变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `GARMIN_USER` | 佳明账号邮箱 | `you@example.com` |
| `GARMIN_PASS` | 佳明密码（无 MFA） | `********` |
| `GARMIN_IS_CN` | 是否中国区，固定 `true` | `true` |
| `SYNC_TOKEN` | 手机访问后端用的令牌，**请改成一段随机长字符串** | `a1b2c3d4e5f6...` |
| `PULL_INTERVAL_MIN` | 拉取间隔（分钟） | `30` |
| `LOOKBACK_DAYS` | 每次回溯多少天的活动 | `21` |
| `TRAIL_TYPE` | 拉取的活动类型 | `trail_running` |
| `PORT` | 服务端口（Railway 自动注入，**不用手填**） | `8000` |

## 本地试运行（可选）

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
cp .env.example .env        # 填入真实值
uvicorn main:app --host 0.0.0.0 --port 8000
```

接口：

- `GET  /api/health` — 健康检查
- `GET  /api/pending` — 取待确认列表（Header：`Authorization: Bearer <SYNC_TOKEN>`）
- `POST /api/decide` — 确认一条
  ```json
  { "activity_id": 123, "decision": "add", "name": "塘朗山", "count": 1 }
  ```
  `decision` 为 `add` 时返回 `{ "ok": true, "record": { "id":..., "date":..., "name":..., "count":... } }`，
  手机端据此写进本地记录；`ignore` 则只标记已处理。

## 部署到 Railway（推荐 GitHub 方式）

1. **把 `backend/` 推到你的 GitHub 仓库**
   ```bash
   cd backend
   git init
   git add .
   git commit -m "hiking garmin sync backend"
   git remote add origin https://github.com/<你>/<仓库>.git
   git push -u origin main
   ```
   （没有 GitHub 仓库就先在 github.com 新建一个空的）

2. 打开 [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**，选这个仓库。
   Railway 会自动识别 `Dockerfile` 并完成构建。

3. 进入项目 **Variables**，添加上面表格里的环境变量
   （`GARMIN_USER` / `GARMIN_PASS` / `GARMIN_IS_CN` / `SYNC_TOKEN` / `PULL_INTERVAL_MIN` / `LOOKBACK_DAYS`）。
   **`PORT` 不用加**，Railway 会自动注入。

4. （建议）避免重启丢数据：进入项目 **Volumes** → **Add Volume**，挂载路径填 `/app/data`。
   这样容器重启后「已忽略/已添加」的状态仍保留，不会重复提示。免费额度下建 Volume 不额外收费。

5. 部署完成后，记下生成的域名，形如 `https://xxx.up.railway.app`。

> 备选方式：安装 [Railway CLI](https://docs.railway.app/develop/cli) → `railway login` → 在本目录 `railway link` → `railway up`。

## 在手机 App 上接好

1. 打开已部署的登山计数网页（CloudStudio 链接），拉到最下方「手表同步」面板。
2. **后端地址**填 Railway 域名；**同步令牌**填你设的 `SYNC_TOKEN`。
3. 点「保存配置」→ 自动拉取待确认。
4. 每条越野跑会显示日期 / 距离 / 时长；你可改山名和次数，点「添加」写入本地记录，或「忽略」丢弃。

## 备注

- 后端是轻量 Python 服务，24/7 常开每月约 $0.5，远低于 Railway 免费额度（$5/月）。
- 数据存在容器内的 `data/sync.db`；Railway 容器重启/休眠后文件系统会重置，已「忽略/添加」的记录丢失会导致这些活动重新变待确认，重新添加时会在手机端产生**重复记录**。解决办法是在 Railway 挂一个 **Volume**（挂载到 `/app/data`），状态即持久保留。
- 如果以后给 Garmin 开了两步验证，需要改为「本地先取 token 再交给后端」的方式，可再找我调整。
