# docker-updater

[English](README.md) | **简体中文**

一个用于管理 Docker 容器更新的轻量级自托管 Web UI，也是 Watchtower 的一种“人工确认后更新”替代方案。

与新镜像发布后立即自动拉取并重启容器不同，docker-updater 会按计划轮询镜像仓库，展示可用更新，并由你决定何时更新某个容器，或者是否更新。更新过程内置回滚保护：旧容器会被保留，如果新容器启动失败，可以自动恢复旧版本。为了进一步降低风险，你还可以选择在更新“成功”后继续保留旧容器备份，这样即使新版本启动正常、但之后才暴露问题，也能随时回滚。执行更新前，还可以直接查看 GitHub Release 更新说明。

---

![更新页 — 待更新容器、批量选择和最近更新历史](screenshot-updates.jpeg)

![备份页 — 保留的回滚点，可一键回滚或删除](screenshot-backups.jpeg)

![设置页 — 备份保留开关和保留时长](screenshot-settings.jpeg)

![主机页 — 本地与远程 Docker 主机管理](screenshot-hosts.jpeg)

## 登录鉴权

为了兼容现有部署，面板默认保持开放访问。在公网或不受信任的网络中，请**同时**设置用户名和密码；只设置其中一项时，程序会输出警告并保持鉴权关闭。

| 变量 | 说明 |
|----------|-------------|
| `AUTH_USERNAME` | 面板登录用户名 |
| `AUTH_PASSWORD` | 面板登录密码 |
| `FLASK_SECRET_KEY` | 可选的会话签名密钥；未设置时会在 `/app/data/.secret_key` 自动生成 |

在 `docker-compose.yml` 同目录创建一个已被 Git 忽略的 `.env` 文件，然后重新创建容器：

```dotenv
AUTH_USERNAME=admin
AUTH_PASSWORD=replace-with-a-strong-password
# FLASK_SECRET_KEY=replace-with-a-long-random-value
```

登录会话有效期为 **7 天**。`/webhook/github` 无需登录即可访问，但仍依赖 `GITHUB_WEBHOOK_SECRET` 验证请求。将 WebUI 暴露到公网时，请在前面配置 HTTPS。

## 语言

WebUI 支持 **English** 和 **简体中文**：

- **自动（默认）**根据浏览器语言选择界面语言：`zh*` 使用中文，其它语言使用英文。
- 在 **设置 → 外观 → 语言** 中可以立即切换“自动”、English 或中文。
- 手动选择会保存在浏览器中，并用于决定推送通知的文案；自动模式会把当前浏览器的有效语言同步到服务端。
- Docker 日志流、镜像名称和原始 Docker 错误保持原文。

## 功能

- **镜像仓库轮询** — 通过 Docker Registry v2 Manifest API（`HEAD` + `Docker-Content-Digest`）比较本地与远程镜像摘要，无需先拉取镜像
- **多镜像仓库支持** — 支持 Docker Hub、GHCR（`ghcr.io`）、LinuxServer（`lscr.io`）以及任何实现 Bearer Token 质询的镜像仓库
- **多主机支持** — 在一个面板中通过 SSH 或 TCP 管理多台 Docker 主机；首次测试 SSH 连接时自动接受并持久化主机密钥（TOFU），每台主机都有连接健康状态，容器卡片会显示所属主机
- **单容器控制** — 可以单独更新、暂缓 7/14/30/90 天、无限期暂缓，或随时取消暂缓
- **批量更新** — 选择多个容器并一次发起更新
- **更新说明查看器** — 获取并内嵌显示最近 5 个 GitHub Release；带有 `org.opencontainers.image.source` 标签或托管在 GHCR 的镜像可自动识别，其它镜像可通过“设置更新说明来源…”指定 GitHub 仓库，并根据镜像名预填建议地址
- **实时更新日志** — 日志弹窗实时显示拉取进度和容器重建状态；更新过程中刷新页面后会自动重新连接
- **持久化更新日志** — 每次更新和回滚的完整日志都会保存到磁盘，即使容器重启，也可以通过历史记录中的“日志”按钮随时查看
- **容器日志查看器** — 每张容器卡片都有“日志”按钮，可在弹窗中查看最近 200 行 `docker logs`，同时显示运行/停止状态并支持刷新，便于排查更新后的容器问题
- **智能历史状态图标** — 最近更新使用 ✅（成功或仍在运行且已是最新）、⚠️（出现错误但容器仍在运行）或 ❌（出现错误且容器已停止），每行还会显示 `● 运行中 / ● 已停止`；悬停图标可直接查看错误信息
- **推送通知** — 首次运行自动生成私有 ntfy 主题，也可以使用自定义 Apprise URL（ntfy、Pushover、Discord、Slack 等）
- **GitHub 通知** — 可选的 Webhook 接口可以接收任意仓库的 Issue、PR、Star、Push 和 Release 事件并转发为推送通知
- **计划检查** — 可在“设置”中选择每 6/12 小时、每天、每周、每月或自定义 cron；修改立即生效并显示下次运行时间。计划保存在数据卷中，可跨重启、重建和镜像更新保留。只有计划任务会发送更新通知，启动扫描和手动检查不会发送通知
- **Compose 栈标识** — 由 Docker Compose 启动的容器会在卡片上显示栈名称，该名称来自 `com.docker.compose.project` 标签；独立容器不受影响
- **更新后重启 Compose 栈** — 可选设置，默认关闭。更新 Compose 管理的容器后，会重启而不是重建同一栈的其它成员，使其重新获取新容器的 IP/DNS，无需手动执行 `docker compose restart`。批量更新同一栈时只会触发一轮重启，并排除刚更新的容器、`_old` 备份、docker-updater 自身以及正在更新的成员
- **自更新** — docker-updater 可以更新自己的容器：拉取新镜像后，由基于新镜像创建的短生命周期辅助容器在旧进程退出后完成停止和重建，无需人工干预
- **安全重建** — 使用 Python Docker SDK 按 Watchtower 模式重建容器，保留卷、端口、环境变量、网络、静态 IP、重启策略、Capabilities 等原始配置
- **备份与回滚** — 更新成功后可选择在指定时间内保留旧容器；如果新版本之后出现问题，可一键回滚，也可以提前删除备份以释放空间
- **镜像清理** — 可选地在更新成功后删除被替代的镜像；若备份仍引用该镜像则不会删除。设置页还提供“显示可回收镜像”，可查看所有悬空镜像的仓库名、大小和创建时间，选择部分或全部删除。删除在后台执行，避免大量镜像因代理超时而中断；仍被容器引用的镜像会显示具体阻止删除的容器
- **安全备份** — docker-updater 创建的备份容器（`{name}_old`）会自动把重启策略设为 `no`，避免主机重启后正式容器和备份同时启动；回滚时会恢复原始策略
- **崩溃恢复** — 如果 docker-updater 在更新或回滚过程中重启，会在启动时协调遗留备份，并恢复任何被中断而处于停止状态的服务
- **隐藏 `_old` 容器** — docker-updater 创建的备份容器不会出现在更新列表和镜像仓库检查中
- **多架构镜像** — 发布 `linux/amd64` 和 `linux/arm64` 镜像，可运行在 x86 服务器和树莓派等 ARM 设备上
- **跳过本地构建镜像** — 没有 `RepoDigests` 的本地 Dockerfile 构建镜像会被自动忽略，因为无法与镜像仓库比较
- **持久化状态** — 更新历史、暂缓决定和上次检查时间都会跨容器重启保留
- **可切换主题的 UI** — 面板包含“待更新 / 已暂缓 / 备份 / 已最新 / 未检查 / 全部 / 主机 / 设置”标签页，并提供 GitHub Dark、Midnight、Nord、Dracula、Carbon 和 Light 六种主题；主题立即生效，并可跨重启和更新保留

---

## 运行要求

- Docker，并允许访问 `/var/run/docker.sock`
- 可运行在 Synology DSM、Unraid、Proxmox 或任何运行 Docker 的 Linux 主机上
- 提供多架构镜像（`linux/amd64` + `linux/arm64`），支持 x86 主机和树莓派等 ARM 设备

---

## 快速开始

```bash
mkdir docker-updater && cd docker-updater
mkdir -p data

docker run -d \
  --name docker-updater \
  --restart unless-stopped \
  -p 9292:9090 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/data:/app/data \
  -e CHECK_TIME=03:00 \
  -e TIMEZONE=Australia/Melbourne \
  ghcr.io/liquidguru/docker-updater:latest
```

然后打开 `http://<你的主机>:9292`。页面会显示一条绿色横幅，其中包含自动生成的 ntfy 主题；在 ntfy 应用中订阅该主题即可接收推送通知。

---

## docker-compose.yml

```yaml
services:
  docker-updater:
    image: ghcr.io/liquidguru/docker-updater:latest
    container_name: docker-updater
    restart: unless-stopped
    ports:
      - "9292:9090"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./data:/app/data
    environment:
      - CHECK_TIME=03:00
      - TIMEZONE=Australia/Melbourne
      # NOTIFY_URL 可选；省略时会自动生成私有 ntfy 主题，
      # 并在首次运行时显示在面板中。如需使用自定义通知：
      # - NOTIFY_URL=ntfy://ntfy.sh/your-private-topic
      # - NOTIFY_URL=discord://webhookid/webhooktoken
      - DOCKER_HOST=unix:///var/run/docker.sock
```

保存为 `docker-compose.yml`，在同一目录创建 `data/`，然后运行 `docker compose up -d`。无需克隆仓库。

> **端口说明：** 容器内部监听 9090 端口。主机绑定 `9292:9090` 可以避免与通常使用 9090 的 Prometheus 冲突；你可以根据环境修改主机端口。

---

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `CHECK_TIME` | `03:00` | 检查计划的**初始默认值**。可以是每天执行的 `HH:MM`，也可以是完整的 5 段 cron 表达式，例如 `0 3 * * 0` 表示每周日执行，`0 */6 * * *` 表示每 6 小时执行。在“设置”标签页保存计划后，该计划会写入数据卷并优先于此变量，详见[检查计划](#检查计划)。 |
| `TIMEZONE` | `Australia/Melbourne` | 计划检查所使用的时区，可以是任意 [tz 数据库名称](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) |
| `NOTIFY_URL` | *（自动）* | 用于推送通知的 [Apprise URL](https://github.com/caronc/apprise/wiki)。未设置时会自动生成唯一的私有 ntfy.sh 主题。 |
| `GITHUB_WEBHOOK_SECRET` | *（空）* | 验证 GitHub Webhook 签名的密钥。使用 GitHub 通知功能时必须配置。 |
| `AUTH_USERNAME` | *（空）* | 面板登录用户名；必须同时设置 `AUTH_PASSWORD` 才会启用鉴权。 |
| `AUTH_PASSWORD` | *（空）* | 面板登录密码；绝不会写入 `state.json`。 |
| `FLASK_SECRET_KEY` | *（自动生成）* | 可选的会话签名密钥；省略时持久化到 `/app/data/.secret_key`。 |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker Socket 路径 |

---

## 主题

在 **设置 → 外观** 中可以选择六种主题：**GitHub Dark**（默认）、**Midnight**、**Nord**、**Dracula**、**Carbon** 和 **Light**。选择后立即生效，并保存到数据卷中的 `state.json`，因此容器重启和镜像更新后仍会保留。主题由服务端直接渲染，页面加载时不会先闪现旧主题。

所有主题中的状态色含义保持一致：绿色始终表示正常，红色始终表示问题。每个主题会单独调整这些颜色以保证对比度，例如 Light 主题使用更深的绿色和红色，使其在浅色背景上仍清晰可读。

添加自定义主题也很简单：每个主题都是 `templates/index.html` 中一个包含 11 个 CSS 变量的 `[data-theme="..."]` 块；界面中的半透明色调均通过 `color-mix()` 从这些变量生成。添加 CSS 块后，再把主题名称加入 `app.py` 的 `THEMES` 即可。

---

## 检查计划

可以直接在**设置**标签页配置 docker-updater 轮询镜像仓库的频率，无需编辑 Compose 文件或重启容器。

预设覆盖了常见使用场景，不需要了解 cron：

- **每 6 小时** / **每 12 小时**
- **每天** — 选择具体时间
- **每周** — 选择星期和时间
- **每月** — 选择日期和时间
- **自定义（cron）** — 使用标准 5 段表达式（`分 时 日 月 星期`），例如 `0 3 * * 0` 表示每周日凌晨 3 点

修改会立即生效，同时显示下一次计划运行时间，方便确认配置已经应用。

**计划保存位置。** 在“设置”中选择的计划会保存到数据卷的 `state.json`，与更新历史和备份设置位于同一位置，因此可以跨容器重启、重建以及 docker-updater 自身镜像更新保留。`CHECK_TIME` 仅作为全新安装时的*初始默认值*；一旦从 UI 保存过计划，就会以保存的计划为准。启动日志会明确显示当前生效来源：

```
[scheduler] Check scheduled: '30 4 * * 0' (from settings) Australia/Melbourne (next run: 2026-07-20T04:30:00+10:00)
```

如果计划因某种原因无法解析，程序会回退到每天 03:00，而不会阻止容器启动。在设置中输入无效 cron 时，请求会被拒绝，当前有效计划不会受到影响。

---

## 多主机支持

docker-updater 可以通过一个面板管理多台 Docker 主机上的容器。打开**主机**标签页即可添加远程主机。

### 支持的连接类型

| URL 格式 | 说明 |
|---|---|
| `ssh://user@host` | SSH — 使用系统中的 SSH 配置和密钥 |
| `ssh://user@host:port` | 使用非标准端口的 SSH |
| `tcp://host:2376` | TCP — 需要在远程主机上启用 Docker Remote API |

### 添加主机

1. 打开**主机**标签页
2. 输入名称和 Docker URL
3. 保存前点击**测试连接**
   - 对于 SSH 主机，首次测试会自动获取并保存远程主机密钥（Trust On First Use）。界面会显示 🔑 提示进行确认。之后的所有连接都会验证已保存的密钥，无需手动执行 `ssh-keyscan`。
4. 点击**添加主机**，程序会立即执行一次检查

之后，所有主机上的容器会统一显示在“待更新 / 已暂缓 / 已最新”等标签页中，每个容器都会带有主机标识。主机标签页会显示每台主机的连接健康状态和上次检查时间。

### SSH 主机密钥

已接受的主机密钥保存在数据卷中的 `data/known_hosts`，因此容器重启和升级后仍会自动保留。

如果希望自行管理 SSH 密钥，例如固定特定主机密钥或共享宿主机上的 `known_hosts`，可以把现有的 `~/.ssh` 目录挂载到容器中：

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - ./data:/app/data
  - ~/.ssh:/root/.ssh:ro   # 可选：挂载宿主机的 SSH 配置和密钥
```

---

## 推送通知

只有计划检查发现更新时才会发送推送通知。启动扫描和手动点击“立即检查”始终保持静默，因此重启容器不会向手机发送大量通知。

### 自动配置（默认）

如果没有设置 `NOTIFY_URL`，docker-updater 会在首次运行时生成一个唯一的私有主题，例如 `ntfy.sh/du-a3f8c12b`，并保存到 `data/state.json`。面板会显示一条带有**复制**按钮的绿色横幅；把主题粘贴到 ntfy 应用中订阅即可。订阅后可以关闭该横幅。

> **为什么使用唯一主题？** ntfy.sh 主题默认公开，任何知道主题名称的人都可以读取消息。docker-updater 自动生成的是不会在其它位置发布的随机字符串，从而保护通知隐私。

### 自定义通知

将 `NOTIFY_URL` 设置为任意 [Apprise 兼容 URL](https://github.com/caronc/apprise/wiki)：

```
ntfy://ntfy.sh/my-private-topic
discord://webhookid/webhooktoken
slack://tokenA/tokenB/tokenC
```

---

## GitHub 通知（可选）

docker-updater 可以接收 GitHub Webhook 事件，并将其转发为推送通知，包括所有仓库中的新 Issue、PR、Star、Push 和 Release。

### 配置方法

1. 为容器添加 `GITHUB_WEBHOOK_SECRET`，并使用随机密钥：
   ```bash
   openssl rand -hex 32
   ```

2. 让外网能够访问 docker-updater，例如使用 Cloudflare Tunnel 或反向代理。

3. 在每个 GitHub 仓库中注册 Webhook：
   - 打开 **Settings → Webhooks → Add webhook**
   - Payload URL：`https://your-host/webhook/github`
   - Content type：`application/json`
   - Secret：步骤 1 中的密钥
   - Events：选择需要的事件（Issue、PR、Push、Star、Release）

   也可以通过 GitHub API 一次性为所有仓库注册：
   ```bash
   TOKEN="your-github-token"
   SECRET="your-webhook-secret"
   URL="https://your-host/webhook/github"
   curl -s -X POST \
     -H "Authorization: token $TOKEN" \
     -H "Content-Type: application/json" \
     -d "{\"name\":\"web\",\"active\":true,\"events\":[\"issues\",\"pull_request\",\"watch\",\"push\",\"release\",\"issue_comment\"],\"config\":{\"url\":\"$URL\",\"content_type\":\"json\",\"secret\":\"$SECRET\"}}" \
     https://api.github.com/repos/YOUR_USERNAME/REPO_NAME/hooks
   ```

### 支持的事件

| 事件 | 通知 |
|---|---|
| Issue 创建 | 🐛 新 Issue — 仓库名 |
| Issue 关闭 | ✅ Issue 已关闭 — 仓库名 |
| PR 创建 | 🔀 新 PR — 仓库名 |
| PR 合并 | ✅ PR 已合并 — 仓库名 |
| Star | ⭐ 新 Star — 仓库名 |
| 推送到 main/master | 📦 推送到 main — 仓库名 |
| Release 发布 | 🚀 新版本发布 — 仓库名 |
| Issue 评论 | 💬 新评论 — 仓库名 |

---

## 工作原理

1. 启动时会静默扫描，并在配置的 `CHECK_TIME` 到达时遍历所有正在运行的容器
2. 对每个容器，从 `RepoDigests` 中提取对应的本地镜像摘要
3. 向镜像仓库发送 `HEAD` 请求获取 Manifest，并读取响应头中的 `Docker-Content-Digest`，整个过程不会传输镜像数据
4. 对于多架构镜像，如果摘要不同，会继续检查当前平台的 Manifest/Config 摘要，避免只有镜像索引变化时产生错误更新提示
5. 只有当前平台的本地镜像确实与镜像仓库中的最新镜像不同时，容器才会被标记为存在更新
6. 点击**更新**后，程序会：
   - 拉取新镜像，并把进度实时输出到日志弹窗
   - 停止旧容器并重命名为 `{name}_old`，作为回滚目标
   - 使用 Docker SDK 底层 API 按 Watchtower 模式，以完全相同的配置创建并启动新容器
   - 通过 `NetworkConnect` 重新连接所有网络，保留静态 IP、别名和 iptables 配置
   - 等待 2 秒，并检查新容器是否仍在运行
   - **成功时**：删除 `_old` 容器；如果启用了备份保留，则在配置的时间内继续保留，以便之后回滚，详见[备份与回滚](#备份与回滚)
   - **失败时**：删除启动失败的新容器，把 `_old` 改回原名称，并重新启动旧版本

容器状态，包括更新可用性、暂缓决定、历史记录和备份，都会保存到 `data/state.json`。

---

## 备份与回滚

docker-updater 为每次更新提供两层安全保护。

### 失败时自动回滚（始终启用）

重建容器前，旧容器会被重命名为 `{name}_old`。如果新容器启动失败，docker-updater 会删除新容器，把旧容器恢复为原名称并重新启动。因此，一次损坏的更新不会让服务永久离线，旧版本会自动恢复。

### 为“启动正常但实际有问题”的更新保留备份（可选）

有些新镜像看似启动完全正常，但过一段时间后才会发现实际问题，例如功能回归、错误发布、默认值变化或端口被移除。上面的自动回滚无法发现这些问题，因为从 Docker 的角度看，容器已经成功启动。

为了进一步降低风险，可以在**设置**标签页启用**更新成功后保留备份**。启用后，更新“成功”时不会立即删除旧容器，而是将其保持停止状态并保留指定时间，默认 24 小时。如果之后发现问题，打开**备份**标签页并点击**回滚**，即可立刻恢复旧版本，即使此前的更新被判定为成功。如果提前确认不再需要备份，可以点击**删除备份**释放磁盘空间。

备份会自动维护：每条记录都会与实际的 `_old` 容器核对，过期或孤立的记录会自动清理，因此备份标签页始终反映真实状态。

备份容器创建后会立即把重启策略设为 `no`，避免主机重启时正式容器和备份同时启动，从而造成端口冲突或重复进程。原始重启策略会被保存，并在回滚时自动恢复。

### 崩溃安全（启动恢复）

更新和回滚在后台线程中运行。如果 docker-updater 在操作进行中重启，例如手动重启、主机重启，甚至 docker-updater 自身更新，该线程会被中途终止，原本可能导致容器更新不完整或处于停止状态。

为避免这种情况，docker-updater 每次启动时都会扫描遗留的 `{name}_old` 备份容器，并逐一协调：

- **新容器正在运行** → 保留新容器；如果启用了备份保留则继续保存备份，否则清理备份。
- **正式容器不存在或未运行** → 自动从备份恢复旧版本并启动。

因此，被中断的操作会在下次启动时自动修复，而不会让服务持续离线。

---

## 更新说明查看器

待更新和已暂缓的容器卡片会显示**更新说明**按钮，从 GitHub Releases API 获取最近 5 个 Release，并使用基础 Markdown 格式内嵌显示。识别分为三个层级：

1. **OCI 来源标签** — 使用 `org.opencontainers.image.source` 标签指向 GitHub 仓库的镜像可自动工作。大多数维护良好的镜像都包含此标签，例如 Home Assistant、Homarr、Vaultwarden、Calibre-Web 和所有 LinuxServer 镜像。
2. **GHCR 镜像** — 托管在 `ghcr.io` 的任何镜像，例如 `ghcr.io/owner/repo:tag`，都会自动识别，因为 GHCR 名称可以直接映射到 GitHub 仓库。
3. **手动覆盖** — 其它镜像会显示“设置更新说明来源…”链接。点击后会打开弹窗，并根据镜像名称预填推测的 GitHub URL，例如 `owner/image` → `github.com/owner/image`，官方镜像 `redis` → `github.com/redis/redis`。确认或修正后保存，“更新说明”按钮会立即替换原提示，URL 会持久化到 `data/state.json`。

---

## 从源码构建

如果希望进行开发或运行尚未提交的最新改动：

```bash
git clone https://github.com/liquidguru/docker-updater.git
cd docker-updater
mkdir -p data
docker compose up -d   # 使用仓库中的 build: . Compose 配置
```

---

## 替换 Watchtower

如果正在使用 Watchtower，请先确认 docker-updater 工作正常，然后停止并删除 Watchtower：

```bash
docker stop watchtower
docker rm watchtower
```

---

## 注意事项

- **Docker Compose 栈**：更新通过 Docker SDK 重建单个容器，不会修改容器对应的 `docker-compose.yml`。之后再次运行 `docker compose up` 时，Compose 会识别新镜像并正常工作，但 Compose 文件中的镜像标签不会被修改。可以选择在更新后**重启**同一栈的其它成员，使它们重新获取新容器的 IP（设置 → *更新后重启同一 Compose 栈的其它容器*）。
- **命名卷**：自动保留。绑定挂载（`HostConfig.Binds`）和命名卷/`--mount` 卷（`HostConfig.Mounts`，Compose 在此保存挂载）都会在重建时重新连接。
- **本地构建镜像**：镜像没有 `RepoDigests` 的容器会被跳过，因为无法与镜像仓库比较。
- **私有镜像仓库**：当前支持匿名和 Bearer Token 镜像仓库，暂不支持使用用户名/密码的 Basic Auth 镜像仓库。
- **新版本破坏性变更**：docker-updater 会保留容器原有的环境变量，但无法判断新镜像版本是否增加了新的必需环境变量。如果容器重建后因应用层错误而失败，请查看镜像 Release Notes，确认是否新增了必需环境变量。
- **Host 网络模式**：使用 `--network host` 的容器可以正确重建，此类容器会跳过网络重新连接步骤。
- **备份保留与磁盘空间**：启用备份保留后，每个备份都会保留一个已停止容器及旧镜像层，直到备份过期或在“备份”标签页中手动删除。磁盘空间有限时，请缩短保留时间，或在确认更新稳定后及时删除备份。

---

## 许可证

MIT

## 致谢

由 [liquidguru](https://github.com/liquidguru) 开发，并使用 AI 编程工具（Anthropic Claude 和 Codex）辅助。所有改动在发布前都会经过审核和测试；由 AI 辅助完成的提交会带有 `Co-Authored-By` 尾注。
