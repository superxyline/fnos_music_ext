# fnmusic-ext 飞牛音乐扩展代理（二次开发版）

> **本仓库是 [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) 的二次开发版**，在上游 v2.3.0 基础上新增「红心收藏自动下载」等能力。**衷心感谢原作者 [@javycoder](https://github.com/javycoder) 的优秀开源工作**——本仓库的全部基础能力（Socket 接管、三音源、WebUI、收藏/歌单/历史体系）均来自上游项目，二次开发增量见 [二次开发功能](#二次开发功能) 一节。
>
> - 本仓库 Gitee：https://gitee.com/superxyline/fnos_music_ext
> - 本仓库 GitHub：https://github.com/superxyline/fnos_music_ext
> - 上游仓库：https://github.com/javycoder/fnos_music_ext （Gitee 同名：https://gitee.com/javycoder/fnos_music_ext ）

`fnmusic-ext` 是专为 fnOS（飞牛私有云）自带音乐应用（`trim.music`）打造的**无侵入增强扩展**。它通过接管官方后端的 Unix Socket 通信入口，在完全不修改官方程序、nginx 配置与数据库的前提下，让原生飞牛音乐获得在线音乐能力；可随时一条命令还原官方直连。

> 当前版本：**v3.3.0**（小爱音箱接管：xiaomusic 桥接 + 管理台三步配置 + 收藏点歌；另含 musicbox `playback.py` 漏 COPY、网易扫码不可达等修复；详见 [CHANGELOG.md](CHANGELOG.md)）

![飞牛桌面里的 fnMusic 扩展管理](preview_image/preview.png)

## 功能特性

- **在线聚合搜播**：在官方搜索框输入歌名，聚合三大音源之一的曲库（见下），在线歌曲即点即播，自动补齐滚动歌词与高清封面。搜索结果严格**本地优先**：本地曲库条目始终排在前面，在线音源结果（网易 > musicdl > 洛雪）紧随其后；
- **三音源单选**（v2.0.0 起互斥，可在 WebUI 秒级切换）：
  - [musicbox](https://github.com/darknessomi/musicbox)：网易云高品质解析，支持扫码登录 VIP/无损曲库与原生每日推荐；
  - [musicdl](https://github.com/CharlesPikachu/musicdl)：酷我/咪咕等 57 个平台聚合，可按平台粒度勾选（编号见 [musicdl-service/PLATFORMS.md](musicdl-service/PLATFORMS.md)）。部分音乐源歌曲少，或返回的音乐不可播放，请自行测试并使用可靠音乐源；
  - **lxmusic**：洛雪音乐自定义源运行时——搜索/歌词/榜单走内置平台接口，播放解析由你提供的洛雪自定义源脚本（Node 沙箱隔离运行）完成。源脚本支持三种配置方式：**粘贴 URL**、**上传电脑上的 `.js` 文件**、**从 NAS 选择 `.js`**（飞牛桌面内），搜索结果以及能否播放视源脚本而定，请自行测试并使用可靠来源；
- **小爱音箱接管**（v3.3.0，可选，端口 8775）：内置 [fnmusic-xiaomusic](https://github.com/superxyline/fnmusic-xiaomusic) 桥接（xiaomusic 内嵌 + 收藏同步 + 口令播放），管理台「小爱音箱」页三步配置（小米账号登录 → 勾选音箱 → 飞牛账号），对小爱说「播放收藏」随机播放收藏、「播放晴天」直接点歌（在线源自动下载、不进收藏）。完成页实时显示**小米登录态**与**口令轮询状态**，失效时红字引导重新登录；「重新配置」为完全重置（退出登录 + 清配置 + 回第一步）。独立开关 `FNMUSIC_XIAOMUSIC_ENABLED`，与音源三选一互斥无关；
- **管理 WebUI**（可选，端口 8774）：浏览器里完成音源切换、musicdl 平台勾选与音源测试搜索（输入歌名逐平台验证是否出歌）、网易扫码、洛雪源配置（URL/上传/NAS 选择）与测试保存、音质偏好、边听边存、推荐开关与 LLM 配置，全部热生效；v3.2.0 起还有「关于」页（版本/部署形态/仓库链接）与**检查更新**（有新版时导航点红点，点开看更新说明）；
- **音质偏好**：`高音质`（从高到低）/ `平衡`（取中间档）/ `流畅`（优先最低）三种模式，覆盖全部音源；
- **智能边听边存**：在线听歌时后台自动缓存，再次播放本地秒开；可选完整试听后保存进本地曲库；
- **推荐体系**：「热门推荐」与「每日推荐 MM-DD」两个独立歌单、独立开关；默认采信音源原生推荐，未启用网易时可配 OpenAI 兼容大模型兜底；歌单封面取列表里第一首有封面的曲目；
- **多用户隔离收藏**：家庭多成员的红心收藏彼此独立，与本地曲库融合。

## 二次开发功能

以下为本仓库在上游 v2.3.0 基础上新增/修改的能力（v2.3.1 起）：

- **小爱音箱接管（fnmusic-xiaomusic 移植）**（v3.3.0）：把独立应用 [fnmusic-xiaomusic](https://github.com/superxyline/fnmusic-xiaomusic) 的桥接服务移植为 fnmusic-sources 容器内第 5 个 supervisor 进程（`xiaomusic-service/`，容器内 8005 → 宿主 8775）：
  - xiaomusic 内嵌运行（pip 依赖并入统一镜像），小米账号登录/口令轮询/播放控制全链路保留；
  - 管理台左侧菜单新增「小爱音箱」页：三步向导（小米账号密码/扫码登录 → 勾选小爱音箱 → 飞牛音乐地址自动扫描 + 账号验证）+ 状态页（收藏数/歌单数/推送状态/最近错误、口令表、立即同步、重新配置）；
  - 口令能力与独立版一致：14+ 种「播放收藏」变体、点歌兜底（收藏 → 本地曲库 → 在线源 ffmpeg 下载，不进收藏）、播完自动接歌单、5 分钟定时同步、自愈式口令推送；
  - 数据独立落 `/data/xiaomusic/`（sources-data 卷内），升级不丢；开关 `FNMUSIC_XIAOMUSIC_ENABLED`（保存时自动起/停进程，默认关）；
  - 音箱经 `http://<NAS_IP>:8775` 拉流（bridge `/proxy/music` → xiaomusic 代理 → 带认证取流飞牛音乐，经本扩展代理的增强搜索/在线取流）；
  - 桥接自带页面保留作旁路（`http://<NAS_IP>:8775/`），主入口为管理台配置页；
- **红心收藏自动下载**（v2.3.1）：对在线歌曲点红心（我喜爱）后，代理立即在后台把该曲目的**完整原文件**整轨下载落盘进本地曲库（定名"歌手 - 歌名"、写音频标签、歌词随迁），不再依赖完整试听触发；
- **下载元数据兜底**（v2.3.2）：后台整轨下载拿不到搜索会话缓存时，用收藏快照里的歌名/歌手/专辑/歌词补齐元数据，保证落盘文件名与标签始终正确；
- **自动转官方收藏 + 持续对账**（v2.3.2 / v2.3.4）：下载文件进入官方曲库索引后，代理自动用当前用户身份为对应本地曲目创建**官方红心**，并撤掉在线收藏条目（避免重复）；官方未及时索引时条目进入待关联集合，每次打开收藏页都会自愈重试，直到转换成功——收藏全程不丢；
- **WebUI 收藏自动下载开关**（v2.3.3）：管理 WebUI「边听边存」页新增「收藏自动下载」开关（默认开，热生效），配置键 `FNMUSIC_FAVORITE_DOWNLOAD_ENABLED`；
- **unknown 文件名三层修复**（v2.3.5）：musicdl 取流补 `/info` 元数据兜底、落盘前从音频自带 ID3 标签抢救定名、红心命中已落盘的 unknown 文件时按收藏快照自动改名（含 .lrc 随迁 / 路径记录 / 标签 / 触发重扫）；
- **曲库/滚动缓存分治**（v2.3.6）：支持「关掉边听边存、只开收藏自动下载」的纯净用法——播放只留临时滚动缓存不进曲库；红心时区分三种情况：曲库已有（unknown 修复）、只在滚动缓存（定名挪进曲库，不重复下载）、都没有（后台下载）；
- **歌曲匹配（歌词/封面批量写回）**（v2.3.8）：管理 WebUI 新增「歌曲匹配」页面——从当前音源**多平台**搜索候选，把**歌词**与**封面**写回官方数据（music.db 的 lyric 表 + `lyric-sqlite` 分片库 + 封面文件），标题/歌手/专辑等信息字段不动；支持勾选批量、按「缺歌词/缺封面/缺任一」筛选后一键匹配、进度实时展示。写入与官方同构，飞牛官方页面立即可见。参考了 [kuilei0926/FnMusicEnhance](https://github.com/kuilei0926/FnMusicEnhance) 的匹配/写回思路，鸣谢！
- **重复清理**（v2.3.11，v2.3.18 起支持简繁折叠）：WebUI「歌曲匹配」页可按**歌名**归组扫描曲库重复（忽略文件名差异；简繁标题折叠后「山水之间/山水之間」式孪生条目也能归组），组内保留音质最好的（无损 > 码率 > 大小），其余移入回收站（可找回），收藏/歌单/播放历史引用自动迁移到保留副本——重复文件不再占用空间，红心和歌单完好；
- **上游 2.6.3 合并**（v2.4.0）：官方 9.25 兼容四件套（分页重复/trackGUIDs 删除/中文下载 500/端点透传）、自动封面内嵌（`FNMUSIC_AUTO_COVER`）、洛雪沙箱 CPU 治理、网易账号歌单（`FNMUSIC_NETEASE_MY_PLAYLISTS`）、每日推荐按账户隔离、在线专辑多源、切歌断点续传（`FNMUSIC_TEE_HANDOFF_MAX`）、取消收藏翻译（bind_registry）、musicdl 2.14.0——详见 CHANGELOG；
- **App 1.0.2 搜索修复**（v2.4.1）：新版官方 App 搜索改调 `/search/tracks` 复数端点，官方后端无此路由、返回网页导致"搜索永远无结果"；代理现拦截该端点并按单数同款逻辑增强（本地优先 + 在线聚合），转发路径自动改写回官方支持的单数路由。可在 WebUI「搜索」页开关（`FNMUSIC_APP_V2_SEARCH` 默认开），并新增带关键词的搜索日志（`FNMUSIC_SEARCH_LOG_QUERY` 默认开）便于远程定位问题；
- **公共歌单 + 关于页**（v3.2.0）：管理页「播放与推荐 → 公共歌单」可浏览网易官方的**排行榜 / 推荐歌单 / 分类歌单 / 精品歌单 / 账号推荐**，勾选后以只读虚拟歌单注入音乐页（最多 50 份，按选择顺序展示，`FNMUSIC_PUBLIC_PLAYLISTS_ENABLED`）。匿名可用、不要求扫码，musicbox 会按需常驻；歌单只读，加歌/移歌/删除都不回写网易。同版加入：
  - **关于页与检查更新**：GitHub Release 优先、Gitee 兜底（成功缓存 6 小时），有新版时导航点红点并在弹窗展示更新说明；
  - **`moo.json` 商店协议源索引**：fnOS 应用商店/第三方商店可直接据此安装与检查升级；
  - **首播预热与共享取链（issue #56）**：metadata 请求时并行解析网易直链，同曲并发只出网一次、整条音质阶梯共用 30 秒预算，缓解"首播跳歌"；
  - **流式禁缓冲（issue #49）**：所有媒体响应带 `X-Accel-Buffering: no`，并输出 Range 形态与首块/总耗时投递日志，根治反代下"起播等整段、拖进度条卡住"；
  - **洛雪 UA 一致化（issue #54）**：播放与探活统一 Chrome 128 PC UA，失效直链 60 秒冷却快速失败；
  - **可配置曲库命名模板（issue #47）**：`FNMUSIC_LIBRARY_NAME_PATTERN`，占位符 `{artist}` / `{title}` / `{source}`；
  - **升级 musicdl 2.14.1**：平台表退役 50/54/56，新增 gequke / leedodo / heishan；
- **B 站音源**（v3.1.0）：musicdl 新增自研 BilibiliMusicClient 平台——B 站免费歌曲 MV 抓 DASH 音轨（192k AAC，无需登录），实测搜索 0.4 秒级；**视频标题自动清洗为正经歌名+歌手**（飞牛刮削友好），WebUI 勾选即用；
- **上游 2.8.x 精选移植**（v3.0.0）：在线封面同源直出（App 网易曲目整屏无封面根治，磁盘缓存+直链映射）、网易 VIP 封面缺失源头修复、边听边存同曲跨 guid 复用（不覆盖已有文件）、WebUI 静态资源 no-cache（WebView 升级缓存根治）；
- **上游 2.7.0 A 级移植**（v2.5.0）：本地歌曲封面直读官方封面目录（`FNMUSIC_COVER_DIR`，根治占位图问题且加载更快）、搜索结果在线来源标记 `[music box]`/`[dl]`/`[lx]`（仅展示，入库标题纯净）、卸载默认保留音乐源数据（重装自动恢复）、WebUI 预览启动健康检查回滚与保存重试激活；
- **网易账号歌单（全音源可用）**（v2.4.2）：任意音源（musicdl / 洛雪 / 网易盒子）下都可扫码登录网易云账号，账号歌单显示在音乐页「热门推荐」下方——自建歌单（含「我喜欢的音乐」）与收藏/关注的他人歌单（`FNMUSIC_NETEASE_SUBSCRIBED` 默认开）一起注入，点开即听（网易曲目走网易盒子取流）。开启开关即自动常驻拉起网易组件，搜索仍走所选音源；歌单只读，不回写网易。WebUI「音乐源」页扫码入口常显；
- **匹配页 QQ / 酷狗音源**（v2.3.17）：「歌曲匹配」页在 musicdl/netease/洛雪之外新增 QQ（QRC 逐字歌词 + 3DES 解密）与酷狗（KRC XOR 解密）两路音源——冷门歌与翻译歌词的命中率和准确度明显提升，封面取 QQ/酷狗官方高清图库；默认开启（`FNMUSIC_QQ_ENABLED` / `FNMUSIC_KUGOU_ENABLED` 可关）、无需登录态、失败自动降级其余源，结果列显示命中音源。搜索内核取自 [kuilei0926/FnMusicEnhance](https://github.com/kuilei0926/FnMusicEnhance)（纯标准库），鸣谢！
- **对账搜索根因修复**（v2.3.7）：对账改用官方搜索真正接受的 `q` 参数（此前 `keyword` 被 100002 拒绝导致转官方收藏从未成功过），并放行红心链路诊断日志（takeover 白名单）——**下载完成 → 打开收藏页 → 自动出现在收藏列表**的完整闭环由此打通。

### 推荐配置

WebUI「边听边存」页：**关闭边听边存、打开收藏自动下载**——只有点红心的歌会下载进曲库，文件名恒为「歌手 - 歌名」，下载完成打开收藏页即自动加入收藏列表。

## 架构

```text
[飞牛音乐客户端 Web / App / 车载]
            │
            ▼
      [飞牛 Nginx]（Unix Socket）
            │
┌─────────────────────────────────────────────────────────┐
│ fnmusic-ext 代理（宿主机 systemd，零侵入接管 Socket）     │
│   ├─ 本地接口透传 ──► 官方后端 (upstream socket)          │
│   ├─ 在线搜索/播放/歌词/封面/收藏                         │
│   ├─ 边播边存 Tee 落盘                                   │
│   └─ .env 热重载（2s 检测，白名单键免重启生效）           │
└───────────────┬─────────────────────────────────────────┘
                │ 127.0.0.1（仅 musicbox/WebUI 暴露局域网）
┌───────────────▼─────────────────────────────────────────┐
│ Docker 单容器 fnmusic-sources（supervisor 按需加载）      │
│   ├─ musicdl  127.0.0.1:8768 → 容器 8001                 │
│   ├─ musicbox 0.0.0.0:8770  → 容器 8002（扫码）          │
│   ├─ lxmusic  127.0.0.1:8772 → 容器 8003                 │
│   └─ WebUI    0.0.0.0:8774  → 容器 8004                  │
│   只启动当前所选音源进程（+可选 WebUI），其余不驻留内存；  │
│   切换音源 = supervisorctl 秒级 stop/start                │
└─────────────────────────────────────────────────────────┘
```

核心代理必须在宿主机以 systemd 运行（接管 Socket）；三个音源 + WebUI 合并为一个 Docker 容器，镜像内由 supervisor 管理四个程序，启动时读取挂载的 `.env` 只拉起所选进程——常驻内存约 100-200MB。

## 快速开始

### 前置条件

1. fnOS 已在「应用中心」安装并启动官方**飞牛音乐**应用；
2. fnOS 已安装 **Docker**（v2.0.0 起仅支持 Docker 部署音源，未安装 Docker 会直接报错退出）。

### 安装（推荐：应用中心 fpk 包）

从 [GitHub Releases](https://github.com/superxyline/fnos_music_ext/releases)（国内可用 [Gitee Releases](https://gitee.com/superxyline/fnos_music_ext/releases)）下载最新 `fnmusic-ext-<版本>.fpk`，在 fnOS「应用中心 → 手动安装」选择该文件，按向导选择**初始音源**即可自动完成安装并启用。

- 桌面会出现「fnMusic 扩展管理」图标，点击即在飞牛桌面窗口内打开管理页（音源切换/扫码登录/平台选择/洛雪源配置）；
- 选洛雪音源时向导不索要任何源信息：装好后打开管理页，在「音乐源 → 洛雪自定义源」里粘贴脚本 URL、上传电脑 `.js` 文件或从 NAS 选择，测试可用后保存即激活；
- 在应用中心可随时「停止」（秒级还原官方直连）与「启动」（恢复扩展）；
- 卸载前会自动把配置与数据（.env、网易云登录、收藏、播放历史）备份为存储卷根目录的 `fnmusic-ext-backup-<时间戳>.tar.gz`，需要彻底清理时手动删除该文件即可；
- 也可用命令行安装：`sudo appcenter-cli install-fpk fnmusic-ext-<版本>.fpk`。

> 升级：应用中心内直接安装新版本 fpk（升级前自动备份用户数据，升级后恢复）。命令行 `install-fpk` 在已安装时不会升级，请在应用中心操作。

### 安装（进阶：git clone 脚本安装）

适合需要修改代码或精细控制参数的用户：

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv git
git clone https://github.com/superxyline/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext
chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh
./install.sh
```

向导依次引导：**音源三选一**（1 网易云 musicbox → 扫码登录；2 musicdl → 平台多选；3 洛雪 → 直接安装，源脚本装后在管理页配置）→ **是否安装管理 WebUI**（默认否）→ 可选 LLM 推荐配置 → 自动执行 `./extend.sh` 接管验收。

非交互示例：

```bash
# 网易云 + WebUI
./install.sh --non-interactive --sources musicbox --webui --extend

# musicdl（酷我+咪咕精选）
./install.sh --non-interactive --sources musicdl --extend

# 洛雪自定义源（可选直接给源：http(s) URL 或本机 .js 文件路径；
# 不给则无源安装，装好在管理页 WebUI 里配置 URL / 上传 .js / NAS 选择）
./install.sh --non-interactive --sources lxmusic \
  --lx-source-url 'https://example.com/your-source.js' --extend
./install.sh --non-interactive --sources lxmusic \
  --lx-source-url "$HOME/scripts/my-source.js" --extend   # 本机路径自动复制进数据卷
./install.sh --non-interactive --sources lxmusic --webui --extend  # 无源安装
```

### 验证

```bash
# 组件健康状态
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz

# WebUI（若安装）：浏览器打开 http://<NAS_IP>:8774
# 小爱音箱桥接（若开启）：http://<NAS_IP>:8775/health
```

打开飞牛音乐 Web 端或 App，搜索「晴天」等关键词即可试听在线歌曲。

### 日常运维

```bash
./extend.sh          # 重新启用/自检（改 .env 后重启容器并验收）
./restore.sh         # 秒级还原官方直连（保留 .env 与全部数据）
./restore.sh --full  # 彻底清理（连配置/登录态/缓存/收藏一并删除）
```

网易云扫码（musicbox 源）：终端 `./install.sh --qr`，或浏览器访问 `http://<NAS_IP>:8770/api/v1/auth/login/qr.png`。

> 部署形态细节、单机多副本约束（`--adopt`）、洛雪源配置与故障排查见 [docs/INSTALL.md](docs/INSTALL.md)。

## 配置参考

配置集中在项目根目录 `.env`（安装向导生成维护，权限 600），完整键项见 [.env.example](.env.example)。常用项：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_MUSICDL_ENABLED` / `FNMUSIC_NETEASE_ENABLED` / `FNMUSIC_LX_ENABLED` | 单选 | 三音源互斥开关，只能一个为 `true`（热重载） |
| `FNMUSIC_AUTO_COVER` | `true` | 下载歌曲落库后把源站封面内嵌进音频（飞牛只认内嵌图；热重载） |
| `FNMUSIC_LYRIC_AUTO_DL` | `false` | 下载歌曲落库后自动下载同名 .lrc 到同目录（热重载） |
| `FNMUSIC_TEE_HANDOFF_MAX` | `3` | 切歌续传后台并发上限（0=关，热重载） |
| `FNMUSIC_NETEASE_MY_PLAYLISTS` | `false` | 网易账号自建歌单只读注入音乐页（需网易源扫码；热重载） |
| `FNMUSIC_NETEASE_PLAYLIST_RESIDENT` | *(未配置)* | 网易云歌单常驻：切到其它音源时也保活 musicbox 供数（未配置＝有需求即常驻） |
| `FNMUSIC_PUBLIC_PLAYLISTS_ENABLED` | `false` | 公共歌单开关：管理页勾选网易榜单/推荐/分类歌单后注入音乐页（匿名可用；热重载） |
| `FNMUSIC_PUBLIC_PLAYLISTS_SELECTION` | *(空)* | 已选公共歌单（JSON 数组，管理页自动维护；空＝默认四大榜，`[]`＝不注入） |
| `FNMUSIC_LIBRARY_NAME_PATTERN` | `{artist} - {title}` | 曲库文件命名模板，占位符 `{artist}` / `{title}` / `{source}`（对新落盘歌曲生效） |
| `FNMUSIC_QQ_ENABLED` / `FNMUSIC_KUGOU_ENABLED` | `true` | 歌曲匹配页 QQ/酷狗音源开关（与三音源开关独立，热重载；QQ 用 QRC 逐字歌词、酷狗用 KRC，均无需登录态） |
| `FNMUSIC_WEBUI_ENABLED` | `false` | 管理 WebUI 开关（端口 8774，无鉴权） |
| `FNMUSIC_XIAOMUSIC_ENABLED` | `false` | 小爱音箱接管开关（v3.3.0；桥接进程随容器按需起停，保存即生效；与三音源开关互斥无关，可任意叠加） |
| `LX_SOURCE_URL` | *(空)* | 洛雪自定义源脚本地址：`http(s)://` URL 或 `file:///data/lxmusic/uploads/<名字>.js`（管理页上传/NAS 选择生成）；建议在 WebUI 里「测试并保存」 |
| `LX_SOURCES` | `kg,wy,mg,kw` | lxmusic 启用的平台（kg/wy/mg/kw/tx） |
| `FNMUSIC_ONLINE_SOURCES` / `MUSICDL_SOURCES` | 酷我+咪咕 | musicdl 平台白名单（短名/全名均可） |
| `FNMUSIC_QUALITY_MODE` | `high` | 音质偏好：`high` / `balanced` / `smooth`（热重载） |
| `FNMUSIC_TEE_SAVE_ENABLED` | `true` | 边听边存开关；`FNMUSIC_TEE_SAVE_DIR` 留空自动探测飞牛共享曲库 |
| `FNMUSIC_TEE_CACHE_MAX` | `2` | 关闭边听边存时滚动保留的试听缓存条数（仅关闭时生效） |
| `FNMUSIC_RECOMMEND_HOT` / `FNMUSIC_RECOMMEND_DAILY` | `true` | 「热门推荐」/「每日推荐」两个独立歌单的开关（热重载） |
| `FNMUSIC_COVER_ENRICH` | `true` | 缺失封面用网易曲库补全（热重载） |
| `FNMUSIC_LLM_BASE_URL` 等 | *(空)* | 大模型每日推荐兜底（OpenAI 兼容，热重载） |
| `FNMUSIC_ENV_WATCH` | `true` | .env 热重载总开关 |

## 从 v1.x 升级

v2.0.0 是**架构级重构**：部署形态（三容器→单容器）、数据目录（`musicbox-data/`→`sources-data/`）、配置键（`LX_THIRD_PARTY` 移除）均有变化。**推荐先还原再安装**，让升级从干净状态开始（`.env` 与全部数据保留，不会丢配置）：

```bash
cd /path/to/fnmusic_ext
git pull
./restore.sh      # 先还原官方直连并清理旧部署（v2 的 restore 兼容清理 v1.x 旧容器/宿主机服务）
./install.sh      # 全新安装 v2.0.0，按向导三选一
```

直接原地升级（`git pull && ./install.sh`）同样支持——安装器会自动迁移数据目录、清理旧三容器，遇到多源并存的旧 `.env` 会要求重新三选一。但机器状态复杂时（曾混用 host/Docker 模式、历经多次版本升级），先 `./restore.sh` 再安装更稳妥省心。

另注意两点行为变化：

1. **三音源互斥**：旧版多音源并存的 `.env` 会被要求重新单选（想换源时在 WebUI 里秒切）；
2. **`LX_THIRD_PARTY` 移除**：lxmusic 不再内置第三方聚合解析链，播放解析完全由你的洛雪自定义源脚本提供（安装或 WebUI 中配置）。

平台编号变化：`53`（zhuolin）、`50`（LivePOO）、`54`（LiziYY）、`56`（ITingWa）已随上游 musicdl 下线退役，编号永久空缺不再复用；新增 `64`（yinyueku）、`65`（gequke）、`66`（leedodo）、`67`（heishan）。

## 常见问题

- **WebUI 打不开 / 提醒**：WebUI 无鉴权，仅限可信内网使用；确认安装时选择了 WebUI，或设置 `FNMUSIC_WEBUI_ENABLED=true` 后运行 `./extend.sh`。
- **洛雪源播放失败**：源脚本由第三方提供，在容器内 Node 沙箱中运行——请在 WebUI 中用「测试」按钮验证源可用性，失败时更换源 URL 或重新上传脚本文件。
- **musicdl 某平台搜索为空**：上游接口变化所致，不影响其他平台；可升级 musicdl（`>=2.13.11`）后重建镜像。
- **切源后内存没有变化**：切换在容器内完成，`docker stats fnmusic-sources` 稍等片刻后查看；未启用音源进程会被停止而非休眠。
- **改了 `.env` 不生效**：热重载仅覆盖白名单键（音源开关/音质/推荐/边听边存/LLM 等）；路径、端口、平台白名单类改动需执行 `./extend.sh` 重启容器。

### 小爱音箱（v3.3.0）

- **口令喊了没反应、音箱自己放歌**：先看管理台「小爱音箱」页完成区的状态行。①「口令轮询未开启」＝桥接的 `enable_pull_ask` 被关了（正常应是开启）；②「小米账号登录已失效」＝小米的 passToken 被吊销/顶号，点「重新配置」重新扫码登录即可（新凭证会写盘，重启后不会再连续失败）；③ 若某台音箱单独不灵，多半是设备标识没对上——点「重新配置」重新勾选，设备列表里灰色不可勾选的那台点「刷新设备」重试。
- **「音箱访问本机的地址」必须填 NAS 局域网 IP**（如 `192.168.x.x`，与音箱同网段），不能填容器网关（`172.x.x.x`）——填错时口令收得到但拉不到音频流。向导已拦截此值并给出提示。
- **点歌时小爱会先抢答一句**：口令必须经小爱云端路由，说「播放周杰伦的晴天」时小爱自带音乐技能可能先回「网易云暂无合适版本，播放 QQ 音乐试听版」，随后 1~2 秒内被接管、切换为飞牛曲库里的版本（播放器日志可见 `group_force_stop_xiaoai` → `【周杰伦 - 晴天】已经开始播放了`）。想减少抢答可用「播放**本地**晴天」这类句式，或在米家 App 关闭该音箱的音乐技能。
- **想换小米账号 / 重选设备**：完成页点「重新配置」＝完全重置（退出小米登录、清飞牛配置、桥接重启回第一步），确认后需重新扫码并重新勾选设备。
- **常用口令**：`播放收藏` / `播放本地收藏歌曲` / `放几首本地歌`（随机打乱播放收藏）、`播放晴天` / `播放周杰伦的晴天`（点歌：收藏优先，没有则在线源下载）、`下一首` / `上一首` / `暂停` / `停止播放`、`随机播放` / `全部循环` / `单曲循环`、`刷新列表`。

## 开发与测试

```bash
python3 -m pytest        # 全量测试（无需 Docker/飞牛环境）
```

仓库结构：`proxy/`（核心代理）、`musicdl-service/`、`musicbox-service/`、`lxmusic-service/`、`xiaomusic-service/`（小爱音箱桥接，v3.3.0）、`webui-service/`（管理界面）、`container/`（单容器构建与编排）、`packaging/fpk/`（应用中心 fpk 打包）。

### fpk 打包与发布

```bash
./packaging/fpk/build.sh   # 本地打包：组装 + fnpack 校验 → dist/fnmusic-ext-<版本>.fpk
```

- 版本号唯一来源为根目录 `VERSION`，打包时注入 manifest（本仓库无 CI，发布由本地脚本完成）；
- 发布流程（维护者）：
  ```bash
  ./packaging/fpk/build.sh                     # 产物 dist/fnmusic-ext-<版本>.fpk + .sha256
  git tag -a v<版本> -m "v<版本>" && git push github main --tags && git push gitee main --tags
  gh release create v<版本> dist/fnmusic-ext-<版本>.fpk dist/*.sha256 --title v<版本> --notes-file <说明>
  python3 tools/update_moo_index.py            # 刷新 moo.json（只收正式版，版本降序）
  ```
  Gitee 侧在仓库「发行版」页面用同名 tag 手工上传同一个 `.fpk`；
- 打包结构由 `packaging/tests/test_fpk_pack.py` 离线校验（含 fnpack 实测校验规则）；
- Windows 上打出的 fpk 可能丢失 `cmd/*` 的可执行位，安装前用 `zipfile` 检查一遍（`0o755`），必要时重打包；
- 实机安装/卸载自动测试（需在飞牛设备上以 root 运行）：

```bash
sudo python3 tests/integration/fpk_lifecycle.py --auto-restore
```

## 免责与版权声明

- 本项目基于 **MIT 许可证** 开源（见 [LICENSE](LICENSE)），严格限定于**个人技术研究与非商业用途**；
- 本项目是协议中继与数据适配层，不托管、不分发任何受版权保护的音频与元数据；音频及元数据版权归属各原始版权方，请支持正版；
- 洛雪自定义源脚本等第三方代码由使用者自行提供并在隔离子进程中运行，请仅使用可信来源的脚本、仅访问您有权收听的内容；
- 使用者应遵守所在国家/地区法律法规与第三方平台用户协议；因滥用导致的任何责任由使用者自行承担。

## 鸣谢

- **[javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) 原项目及作者 [@javycoder](https://github.com/javycoder)**：本仓库是其二开版本，Socket 接管架构、三音源体系、管理 WebUI、在线收藏/歌单/播放历史等所有基础能力均出自原项目，没有原作者的工作就没有这个二开版，特此致谢！
- **[kuilei0926/FnMusicEnhance](https://github.com/kuilei0926/FnMusicEnhance)**：歌曲匹配页 QQ/酷狗音源的搜索/解密内核（`proxy/enhance_search/`）取自该项目，特此致谢！
- 上游致谢：[CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl)、[darknessomi/musicbox](https://github.com/darknessomi/musicbox)、洛雪音乐（LX Music）社区及其自定义源规范。

## 免责与版权声明
