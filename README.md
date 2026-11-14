<div align="center">

# fnmusic-ext · 飞牛音乐扩展（二次开发版）

**熟悉的飞牛音乐，不止于本地曲库。**

本仓库是 [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) 的二次开发版——在上游「在线听歌」的基础上，强化**曲库管理、收藏下载、小爱音箱、歌单导入**等本地化能力。衷心感谢原作者 [@javycoder](https://github.com/javycoder) 的优秀开源工作，本仓库的全部基础能力（Socket 接管、三音源、WebUI、收藏/歌单/历史体系）均来自上游项目。

[![Release](https://img.shields.io/github/v/release/superxyline/fnos_music_ext?label=%E6%AD%A3%E5%BC%8F%E7%89%88)](https://github.com/superxyline/fnos_music_ext/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/superxyline/fnos_music_ext?style=flat&label=Stars)](https://github.com/superxyline/fnos_music_ext/stargazers)

[**下载安装**](https://github.com/superxyline/fnos_music_ext/releases) · [快速开始](#快速开始) · [二次开发功能](#二次开发功能) · [配置参考](#配置参考) · [更新日志](CHANGELOG.md) · [上游仓库](https://github.com/javycoder/fnos_music_ext) · [Gitee 镜像](https://gitee.com/superxyline/fnos_music_ext)

</div>

> 面向 fnOS 官方音乐应用 `trim.music` 的社区扩展，非飞牛官方产品。保留官方客户端，通过可逆的代理接管增加能力；**不修改官方二进制或 nginx 配置，不直接写入官方数据库结构**。停止扩展即可恢复官方直连。

![飞牛桌面里的 fnMusic 扩展管理](preview_image/preview.png)

## 一眼了解

当前版本：**v3.7.7**（变更详情见 [CHANGELOG.md](CHANGELOG.md)，版本增量摘要见各节标注的版本号）。

| 能力 | 说明 |
| :--- | :--- |
| **原生入口** | 继续使用官方飞牛音乐 Web / App / 车机，无需换播放器；搜索严格本地优先，在线结果带来源标记（`[kuwo]` / `[migu]` / `[bilibili]` / `[music box]` 等） |
| **三音源单选** | 网易云 musicbox（扫码登录）/ musicdl 聚合（57 平台）/ 洛雪自定义源，WebUI 秒级切换 |
| **七档音质** | 标准(128k) / 较高 / 极高(320k) / 无损 / Hi-Res / 超清母带 / 沉浸声，缺档逐级下降不回升；播放与 App 下载分别设置 |
| **红心自动下载** | 在线歌曲点红心即后台整轨下载落盘，写标签/歌词、内嵌封面，自动转官方收藏并持续对账——收藏全程不丢 |
| **曲库管理** | 升级音质 / 换源修信息 / 物理删除三动作共用一曲库列表；「内嵌信息不全」筛选；只改 `audio_file` 一行，收藏刮削不受影响 |
| **曲库目录整理** | 按「歌手/专辑/歌名」两层归位，规划预览→确认→执行；移动后同步官方库路径，**无需重扫、收藏/刮削保留**，官方 watcher 软删自动对账 |
| **歌单与推荐** | 网易/QQ 歌单导入、最近常听、公共歌单（网易榜单+酷狗 TOP500）、网易账号歌单全音源常驻、大模型推荐（双协议+用量账本） |
| **歌曲匹配** | 多平台（网易/QQ/酷狗/musicdl）搜索把歌词与封面写回官方数据；重复清理（简繁折叠、收藏/歌单引用迁移） |
| **小爱音箱** | 口令播歌/点歌/停止/睡眠定时、正在播放显示、native 口令引擎；三步向导配置 |
| **管理 WebUI** | 音源/音质/存储/推荐/LLM 全部图形化配置、热生效；深色主题、检查更新、音源测试 |
| **直链输出** | 配置令牌后，车机/电视等外部播放器 302 直连源站 CDN |

**先了解三点**：主音源三选一，并非同时聚合全部提供者；洛雪源脚本由使用者自行提供；安装扩展不等于获得第三方平台的会员或付费内容权限。

## 二次开发功能

以下为本仓库在上游 v2.3.0 基础上新增/修改的能力（v2.3.1 起，实现细节见 [CHANGELOG.md](CHANGELOG.md)）。

### 曲库管理

| 功能 | 说明 |
| :--- | :--- |
| **升级音质**（v3.7.3） | 列出曲库真实音质（格式/码率/采样率/位深），到酷我/咪咕找更高音质同名版本替换，支持 mp3 → flac；原文件备份 7 天可回滚，时长差 >3s 拒绝 |
| **换源修信息**（v3.7.5） | 内嵌信息（标题/歌手/专辑）有错时换源替换，按信息完整度（infoScore 0~6）+音质排序，替换后写回候选标签；「内嵌信息不全」筛选（mutagen 实测，逐行「缺xx」徽标） |
| **物理删除**（v3.7.5） | 勾选多首连文件带歌词/库记录/全部引用一并移除（不进回收站）；路径被重复收录共享时自动拒绝 |
| **曲库目录整理**（v3.7.7） | 按「歌手/专辑/歌名」两层归位：规划预览（汇总+逐首路径对照+冲突/缺失清单）确认后执行；移动后**立即同步官方库路径**（guid/track 不动，无需重扫）；元数据兜底链（官方库→文件标签→文件名解析→未知歌手）；官方 watcher 批量软删自动对账 |
| **重复清理**（v2.3.11） | 按歌名归组扫描（简繁折叠），组内保留音质最优，其余进回收站可找回，收藏/歌单/播放历史引用自动迁移 |

### 收藏与下载

| 功能 | 说明 |
| :--- | :--- |
| **红心自动下载**（v2.3.1） | 对在线歌曲点红心即后台整轨下载落盘（定名「歌手 - 歌名」、写标签、歌词随迁），不依赖完整试听触发；WebUI 开关热生效 |
| **自动转官方收藏**（v2.3.2） | 落盘入库后自动建官方红心并撤在线条目；官方未及时索引时持续对账自愈，收藏全程不丢 |
| **下载音质门槛**（v3.6.0） | `FNMUSIC_DOWNLOAD_MIN_QUALITY`（any/320/lossless）：落库前按内容实测分档，不达标先换源、仍不达标拒绝入库 |
| **下载元数据兜底**（v2.3.2 / v2.3.5） | 收藏快照补元数据、ID3 抢救定名、unknown 文件按快照自动改名；曲库/滚动缓存分治——支持「关边听边存、只开收藏下载」的纯净用法 |

### 在线播放与音质

| 功能 | 说明 |
| :--- | :--- |
| **七档播放音质**（v3.7.6，移植上游 v2.12.0） | 标准/较高/极高/无损/Hi-Res/超清母带/沉浸声，缺档逐级下降不回升，旧配置自动迁移；播放与 App 下载音质分别设置 |
| **搜索音质优先**（v3.6.0） | 在线结果按音质分档排序（无损 > 高码率 > 低码率，同档按源配置顺序），快源抢跑不截断能出无损的慢源 |
| **直链输出**（v3.6.0） | 管理台配置令牌后 `GET /direct?id=<源>:<id>&token=…` 302 到源站 CDN，供车机/电视等外部播放器 |
| **B 站音源**（v3.1.0） | musicdl 新增 BilibiliMusicClient：免费歌曲 MV 抓 DASH 音轨（192k AAC 免登录），视频标题自动清洗为正经歌名+歌手 |
| **搜索修复与兼容**（v2.4.1 / v3.7.6） | App 1.0.2 复数端点拦截增强；飞牛音乐 1.1.19 搜索默认窗口 50→100 |

### 歌单与推荐

| 功能 | 说明 |
| :--- | :--- |
| **歌单导入**（v3.6.0） | 粘贴网易云/QQ 歌单链接或 ID，逐首在线匹配整轨下载（试听探针+音质门槛）、写标签内嵌封面，官方收录后自动加入目标歌单 |
| **最近常听**（v3.6.0） | 播放历史（次数+新鲜度）驱动的只读虚拟歌单注入音乐页，数据全在本地 |
| **公共歌单 + 酷狗榜单**（v3.2.0 / v3.7.3） | 网易官方榜单/推荐/分类/精品歌单勾选注入（匿名可用），另含酷狗 TOP500 等 20 个官方榜单 |
| **网易账号歌单全音源可用**（v2.4.2） | 任意音源下都可扫码登录网易云，账号歌单（自建+收藏）注入音乐页；开启即常驻网易组件，搜索仍走所选音源 |
| **大模型推荐升级**（v3.7.6，移植上游 v2.11.0） | Chat Completions / Responses 双协议、每日 token 限额、单次输出上限、用量账本（今日/累计/近 7 天趋势） |
| **小爱睡眠定时**（v3.6.0） | 「半小时后停止播放」「定时30分钟」等到点自动停止，「取消定时」撤销 |

### 小爱音箱（v3.3.0，可选）

把独立应用 [fnmusic-xiaomusic](https://github.com/superxyline/fnmusic-xiaomusic) 移植为容器内桥接服务（端口 8775），管理台「小爱音箱」页三步向导配置。

| 功能 | 说明 |
| :--- | :--- |
| **口令播放** | 「播放收藏」随机连播、「顺序播放本地歌曲」（按收藏先后整张循环）、「播放周杰伦的本地收藏歌曲」（按歌手打乱）；「播放晴天」点歌（收藏→本地→在线源，都不写收藏）；30+ 口语化停止变体 |
| **正在播放**（v3.7.0） | 管理台实时显示歌名 + 本地/在线来源徽标，5 秒刷新 |
| **native 口令引擎**（v3.4.0） | 自研编排 + 上游登录/设备组件复用；带屏设备后台预解析 audioID 落盘缓存（实测命中 96.7%），屏幕不再随机错歌 |
| **数据与运维** | 数据独立落 `sources-data/`，升级不丢；`FNMUSIC_XIAOMUSIC_ENABLED` 开关，保存即起停 |

### 界面与其他

| 功能 | 说明 |
| :--- | :--- |
| **管理 WebUI**（8774） | 音源切换/平台勾选与音源测试/网易扫码/洛雪源配置（URL/上传/NAS 选择）/音质/存储/推荐/LLM，全部热生效；「关于」页检查更新（GitHub 优先 Gitee 兜底，升级弹窗双下载源） |
| **深色主题**（v3.5.0） | 跟随系统/浅色/深色三档；嵌入飞牛时随宿主主题切换，深色不闪白底 |
| **歌曲匹配多音源**（v2.3.17） | QQ（QRC 逐字歌词）/酷狗（KRC）搜索内核取自 [FnMusicEnhance](https://github.com/kuilei0926/FnMusicEnhance)，冷门歌与翻译歌词命中率显著提升 |
| **上游移植持续跟进** | 封面直读/直出、来源标记、卸载数据保留、首播预热、流式禁缓冲、洛雪 UA 一致化、命名模板等 A 级项均已吸收，详见 CHANGELOG |
| **ARM64 支持**（v3.7.2） | x86_64 与 arm64 共用一个包；依赖全预编译 wheel、镜像架构自动跟随宿主、构建失败自动装编译工具重试 |
| **moo 商店** | `moo.json` 协议源索引，第三方应用商店可直接安装与升级 |

<details>
<summary><strong>推荐配置</strong></summary>

WebUI「边听边存」页：**关闭边听边存、打开收藏自动下载**——只有点红心的歌会下载进曲库，文件名恒为「歌手 - 歌名」，下载完成打开收藏页即自动加入收藏列表。

</details>

## 快速开始

### 前置条件

1. fnOS 已在「应用中心」安装并启动官方**飞牛音乐**应用；
2. fnOS 已安装 **Docker**（v2.0.0 起仅支持 Docker 部署音源，未安装 Docker 会直接报错退出）。

### 安装（推荐：应用中心 fpk 包）

从 [GitHub Releases](https://github.com/superxyline/fnos_music_ext/releases)（国内可用 [Gitee Releases](https://gitee.com/superxyline/fnos_music_ext/releases)）下载最新 `fnmusic-ext-<版本>.fpk`，在 fnOS「应用中心 → 手动安装」选择该文件，按向导选择**初始音源**即可自动完成安装并启用。

- 桌面会出现「fnMusic 扩展管理」图标，点击即在飞牛桌面窗口内打开管理页；
- 选洛雪音源时向导不索要任何源信息：装好后打开管理页，在「音乐源 → 洛雪自定义源」里粘贴脚本 URL、上传电脑 `.js` 文件或从 NAS 选择，测试可用后保存即激活；
- 在应用中心可随时「停止」（秒级还原官方直连）与「启动」（恢复扩展）；
- 卸载前会自动把配置与数据（.env、网易云登录、收藏、播放历史）备份为存储卷根目录的 `fnmusic-ext-backup-<时间戳>.tar.gz`；
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

向导依次引导：**音源三选一**（1 网易云 musicbox → 扫码登录；2 musicdl → 平台多选；3 洛雪 → 装后在管理页配置源脚本）→ **是否安装管理 WebUI**（默认否）→ 可选 LLM 推荐配置 → 自动执行 `./extend.sh` 接管验收。

非交互示例：

```bash
# 网易云 + WebUI
./install.sh --non-interactive --sources musicbox --webui --extend

# musicdl（酷我+咪咕精选）
./install.sh --non-interactive --sources musicdl --extend

# 洛雪自定义源（URL 或本机 .js；不给则无源安装，装后在管理页配置）
./install.sh --non-interactive --sources lxmusic \
  --lx-source-url 'https://example.com/your-source.js' --extend
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

核心代理必须在宿主机以 systemd 运行（接管 Socket）；三个音源 + WebUI 合并为一个 Docker 容器，启动时读取挂载的 `.env` 只拉起所选进程——常驻内存约 100-200MB。

<details>
<summary><strong>支持的 CPU 架构（x86_64 / arm64 通用一个包）</strong></summary>

- fpk `manifest` 里 `platform = all`，镜像在目标机上 `docker compose build` 本地构建，基础镜像 `python:3.13-slim` 官方提供多架构变体，架构自动跟随宿主；
- 全部 Python 依赖在 arm64 上都有预编译 wheel（含 `curl_cffi` / `pillow` / `aiohttp` / `pycryptodome`），构建不需要编译器；`nodejs` / `ffmpeg` / `supervisor` 走 Debian apt，arm64 有官方包；
- 万一某个依赖只发源码包，Dockerfile 会自动临时安装编译工具、装完立即卸掉（只走失败路径，正常运行零开销）；选定基础镜像前会校验 `.Architecture` 与宿主一致——部分国内镜像代理只缓存 amd64，直接构建会以 `exec format error` 收场、极难定位；
- 已知差异（只影响首次安装体感）：ARM 设备首次构建约 **5–15 分钟**（建议内存 ≥2GB），安装脚本会先打印提示。飞牛 ARM 版要求系统 ≥ 1.2.0。

> 以上为静态验证结论。本项目目前**没有 ARM 真机实测记录**，如遇问题请带上 `docker compose build` 的日志反馈。

</details>

## 配置参考

配置集中在项目根目录 `.env`（安装向导生成维护，权限 600），完整键项见 [.env.example](.env.example)。常用项：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_MUSICDL_ENABLED` / `FNMUSIC_NETEASE_ENABLED` / `FNMUSIC_LX_ENABLED` | 单选 | 三音源互斥开关，只能一个为 `true`（热重载） |
| `FNMUSIC_AUTO_COVER` | `true` | 下载歌曲落库后把源站封面内嵌进音频（飞牛只认内嵌图；热重载） |
| `FNMUSIC_LYRIC_AUTO_DL` | `false` | 下载歌曲落库后自动下载同名 .lrc 到同目录（热重载） |
| `FNMUSIC_PLAY_QUALITY` | 跟随旧键自动迁移 | 播放音质七档：`standard` / `higher` / `exhigh` / `lossless` / `hires` / `jymaster` / `sky`（热重载） |
| `FNMUSIC_DOWNLOAD_QUALITY` | `app` | APP 下载音质：`app`=跟随 APP，或七档之一（热重载） |
| `FNMUSIC_DOWNLOAD_MIN_QUALITY` | `any` | 收藏下载音质门槛：`any` / `320` / `lossless` |
| `FNMUSIC_DOWNLOAD_LAYOUT` | `flat` | 新下载落盘布局：`flat` / `artist` / `album` / `artist_album`（热重载） |
| `FNMUSIC_TEE_SAVE_ENABLED` | `true` | 边听边存开关；`FNMUSIC_TEE_SAVE_DIR` 留空自动探测飞牛共享曲库 |
| `FNMUSIC_FAVORITE_DOWNLOAD_ENABLED` | `true` | 收藏自动下载开关（热重载） |
| `FNMUSIC_LIBRARY_NAME_PATTERN` | `{artist} - {title}` | 曲库文件命名模板，占位符 `{artist}` / `{title}` / `{source}` |
| `FNMUSIC_NETEASE_MY_PLAYLISTS` | `false` | 网易账号歌单只读注入音乐页（需扫码；热重载） |
| `FNMUSIC_PUBLIC_PLAYLISTS_ENABLED` | `false` | 公共歌单开关（网易榜单/推荐/分类 + 酷狗榜单；热重载） |
| `FNMUSIC_QQ_ENABLED` / `FNMUSIC_KUGOU_ENABLED` | `true` | 歌曲匹配页 QQ/酷狗音源开关（无需登录态，热重载） |
| `FNMUSIC_LLM_PROTOCOL` | `chat` | 大模型接口协议：`chat` / `responses`（GPT-5 / o 系列推理模型） |
| `FNMUSIC_LLM_DAILY_TOKEN_LIMIT` | `1000000` | 大模型每日 token 上限，`0`=不限 |
| `FNMUSIC_WEBUI_ENABLED` | `false` | 管理 WebUI 开关（端口 8774，无鉴权，仅限可信内网） |
| `FNMUSIC_XIAOMUSIC_ENABLED` | `false` | 小爱音箱接管开关（保存即生效，可与任意音源叠加） |
| `LX_SOURCE_URL` | *(空)* | 洛雪自定义源脚本地址（管理页上传/NAS 选择生成） |
| `FNMUSIC_ONLINE_SOURCES` / `MUSICDL_SOURCES` | 酷我+咪咕 | musicdl 平台白名单（短名/全名均可） |
| `FNMUSIC_ENV_WATCH` | `true` | .env 热重载总开关 |

## 常见问题

- **WebUI 打不开 / 提醒**：WebUI 无鉴权，仅限可信内网使用；确认安装时选择了 WebUI，或设置 `FNMUSIC_WEBUI_ENABLED=true` 后运行 `./extend.sh`。
- **洛雪源播放失败**：源脚本由第三方提供，在容器内 Node 沙箱中运行——请在 WebUI 中用「测试」按钮验证源可用性，失败时更换源 URL 或重新上传脚本文件。
- **musicdl 某平台搜索为空**：上游接口变化所致，不影响其他平台；可升级 musicdl（`>=2.13.11`）后重建镜像。
- **改了 `.env` 不生效**：热重载仅覆盖白名单键（音源开关/音质/推荐/边听边存/LLM 等）；路径、端口、平台白名单类改动需执行 `./extend.sh` 重启容器。

<details>
<summary><strong>小爱音箱常见问题</strong></summary>

- **口令喊了没反应**：先看管理台「小爱音箱」页状态行——「口令轮询未开启」＝桥接的 `enable_pull_ask` 被关了；「小米账号登录已失效」＝passToken 被吊销/顶号，点「重新配置」重新扫码即可；某台音箱单独不灵多为设备标识没对上，「重新配置」重新勾选。
- **「音箱访问本机的地址」必须填 NAS 局域网 IP**（如 `192.168.x.x`，与音箱同网段），不能填容器网关（`172.x.x.x`）——填错时口令收得到但拉不到音频流，向导已拦截此值。
- **点歌时小爱会先抢答一句**：口令须经小爱云端路由，1~2 秒内被接管切换为飞牛曲库版本。想减少抢答可用「播放**本地**晴天」句式，或在米家 App 关闭该音箱的音乐技能。
- **重启 NAS 后桥接不可达**：3.6.1 之前的镜像缺桥接依赖文件，升级或重装最新 fpk 即可。
- **常用口令**：`播放收藏` / `播放本地收藏歌曲` / `播放周杰伦的本地收藏歌曲` / `顺序播放本地歌曲`、`播放晴天`（点歌：收藏→本地→在线源，都不写收藏）、`下一首` / `上一首` / `暂停` / `停止播放`（`停下来吧` 等口语说法同样生效）、`刷新列表`。

</details>

## 开发与测试

```bash
python3 -m pytest        # 全量测试（无需 Docker/飞牛环境）
```

仓库结构：`proxy/`（核心代理）、`musicdl-service/`、`musicbox-service/`、`lxmusic-service/`、`xiaomusic-service/`（小爱音箱桥接，`native/`＝native 口令引擎）、`webui-service/`（管理界面）、`container/`（单容器构建与编排）、`packaging/fpk/`（应用中心 fpk 打包）。

<details>
<summary><strong>fpk 打包与发布（维护者）</strong></summary>

```bash
./packaging/fpk/build.sh   # 本地打包：组装 + fnpack 校验 → dist/fnmusic-ext-<版本>.fpk
```

- 版本号唯一来源为根目录 `VERSION`，打包时注入 manifest（本仓库无 CI，发布由本地脚本完成）；
- 发布流程：`./packaging/fpk/build.sh` → `git tag -a v<版本>` → 双端 push → GitHub/Gitee Release 挂 fpk 附件 → `python3 tools/update_moo_index.py` 刷新 `moo.json`；
- 打包结构由 `packaging/tests/test_fpk_pack.py` 离线校验；Windows 打包注意 `cmd/*` 可执行位；
- 实机安装/卸载自动测试（需在飞牛设备上以 root 运行）：`sudo python3 tests/integration/fpk_lifecycle.py --auto-restore`。

</details>

## 免责与版权声明

- 本项目基于 **MIT 许可证** 开源（见 [LICENSE](LICENSE)），严格限定于**个人技术研究与非商业用途**；
- 本项目是协议中继与数据适配层，不托管、不分发任何受版权保护的音频与元数据；音频及元数据版权归属各原始版权方，请支持正版；
- 洛雪自定义源脚本等第三方代码由使用者自行提供并在隔离子进程中运行，请仅使用可信来源的脚本、仅访问您有权收听的内容；
- 使用者应遵守所在国家/地区法律法规与第三方平台用户协议；因滥用导致的任何责任由使用者自行承担。

## 鸣谢

- **[javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) 原项目及作者 [@javycoder](https://github.com/javycoder)**：本仓库是其二开版本，Socket 接管架构、三音源体系、管理 WebUI、在线收藏/歌单/播放历史等所有基础能力均出自原项目，没有原作者的工作就没有这个二开版，特此致谢！
- **[kuilei0926/FnMusicEnhance](https://github.com/kuilei0926/FnMusicEnhance)**：歌曲匹配页 QQ/酷狗音源的搜索/解密内核（`proxy/enhance_search/`）取自该项目，特此致谢！
- 上游致谢：[CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl)、[darknessomi/musicbox](https://github.com/darknessomi/musicbox)、洛雪音乐（LX Music）社区及其自定义源规范。
