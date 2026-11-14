<p align="center">
  <img src="preview_image/preview.png" alt="fnMusic 扩展管理：飞牛桌面内的管理界面" width="720">
</p>

<p align="center">
  <b>熟悉的飞牛音乐，不止于本地曲库。</b><br>
  在原生 Web 与 App 里搜索、播放、收藏在线音乐，再把红心的歌、整个歌单、甚至小爱音箱，都接进你的 NAS 曲库。
</p>

<p align="center">
  <a href="https://github.com/superxyline/fnos_music_ext/releases/latest"><b>下载安装</b></a> ·
  <a href="#快速开始"><b>快速开始</b></a> ·
  <a href="#二次开发功能"><b>功能列表</b></a> ·
  <a href="#配置参考">配置参考</a> ·
  <a href="#常见问题">常见问题</a> ·
  <a href="CHANGELOG.md">更新日志</a> ·
  <a href="https://github.com/javycoder/fnos_music_ext"><b>上游仓库</b></a> ·
  <a href="https://gitee.com/superxyline/fnos_music_ext">Gitee 镜像</a>
</p>

<p align="center">
  <a href="https://github.com/superxyline/fnos_music_ext/releases/latest"><img src="https://img.shields.io/github/v/release/superxyline/fnos_music_ext?color=7F77DD&label=%E6%AD%A3%E5%BC%8F%E7%89%88" alt="Latest release"></a>
  <a href="https://github.com/superxyline/fnos_music_ext/releases"><img src="https://img.shields.io/badge/fnOS-1.2.0%2B-1D9E75" alt="fnOS 1.2.0+"></a>
  <img src="https://img.shields.io/badge/CPU-x86__64%20%7C%20ARM64-3C3489" alt="x86_64 | ARM64">
  <img src="https://img.shields.io/badge/%E9%A3%9E%E7%89%9B%E9%9F%B3%E4%B9%90-1.0.10%20%7C%201.1.19-534AB7" alt="飞牛音乐 1.0.10 | 1.1.19">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6A62C9" alt="MIT license"></a>
  <a href="https://github.com/superxyline/fnos_music_ext/stargazers"><img src="https://img.shields.io/github/stars/superxyline/fnos_music_ext?style=flat&color=EA4AAA" alt="Stars"></a>
</p>

<table>
  <tr>
    <td width="33%" valign="top">
      <b>原生入口，零侵入</b><br>
      保留官方飞牛音乐客户端，通过可逆的 Unix Socket 接管增加在线能力。
      不修改官方程序与 nginx 配置，停止扩展即恢复官方直连。
    </td>
    <td width="33%" valign="top">
      <b>红心即收藏，收藏即本地</b><br>
      对在线歌曲点红心，后台自动整轨下载进 NAS 曲库并转成官方收藏。
      音质门槛、自动换源、持续对账，收藏全程不丢。
    </td>
    <td width="33%" valign="top">
      <b>曲库管理，一次到位</b><br>
      升级音质、换源修信息、按歌手/专辑整理目录、重复清理、批量修标签。
      所有操作只动 <code>audio_file</code> 一行，收藏与刮削状态分毫不动。
    </td>
  </tr>
</table>

## 快速开始

### 前置条件

- fnOS 已在「应用中心」安装并启动官方**飞牛音乐**（适配基线 1.0.10 / 1.1.19）；
- fnOS 已安装 **Docker**（v2.0.0 起仅支持 Docker 部署音源，未安装会直接报错退出）；
- 支持 `x86_64` 与 `aarch64 / ARM64`（共用同一个安装包）。

### 安装扩展

从 [GitHub Releases](https://github.com/superxyline/fnos_music_ext/releases/latest)（国内可用 [Gitee Releases](https://gitee.com/superxyline/fnos_music_ext/releases)）下载 `fnmusic-ext-<版本>.fpk`，在 fnOS「应用中心 → 手动安装」中选择，按向导选择**初始音源**即可。

- 桌面会出现「fnMusic 扩展管理」图标，管理页从飞牛桌面打开（或 `/app/fnmusic-ext/`）。不要直接访问或向公网暴露内部 `8774` 端口（无鉴权，仅限可信内网）；
- 选洛雪音源时向导不索要源信息：装好后到「音乐源 → 洛雪自定义源」粘贴脚本 URL、上传电脑 `.js` 或从 NAS 选择，测试可用后保存激活；
- 应用中心可随时「停止」（秒级还原官方直连）与「启动」（恢复扩展）；
- 卸载前自动把配置与数据备份为存储卷根目录的 `fnmusic-ext-backup-<时间戳>.tar.gz`；
- 命令行安装：`sudo appcenter-cli install-fpk fnmusic-ext-<版本>.fpk`（注意：已安装时 `install-fpk` 不会升级，升级请在应用中心操作）。

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

```bash
# 非交互示例
./install.sh --non-interactive --sources musicbox --webui --extend
./install.sh --non-interactive --sources musicdl --extend
./install.sh --non-interactive --sources lxmusic --lx-source-url 'https://example.com/your-source.js' --extend
```

### 验证与日常运维

```bash
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz   # 组件健康

./extend.sh          # 重新启用/自检（改 .env 后重启容器并验收）
./restore.sh         # 秒级还原官方直连（保留 .env 与全部数据）
./restore.sh --full  # 彻底清理（连配置/登录态/缓存/收藏一并删除）
```

打开飞牛音乐 Web 端或 App，搜索「晴天」即可试听在线歌曲。部署细节、多副本约束（`--adopt`）见 [docs/INSTALL.md](docs/INSTALL.md)。

## 二次开发功能

本仓库是 [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) 的二次开发版，以下为在上游 v2.3.0 基础上新增的能力（v2.3.1 起，实现细节见 [CHANGELOG.md](CHANGELOG.md)）。

### 在线播放

- **三音源单选**：网易云 musicbox（扫码登录 VIP/无损曲库与原生每日推荐）/ musicdl 聚合（57 平台，含自研 **B 站音源**——MV 抓 DASH 音轨 192k AAC 免登录，视频标题自动清洗为正经歌名+歌手）/ 洛雪自定义源（URL/上传/NAS 选择三种配置方式），WebUI 秒级切换；
- **七档音质**（v3.7.6）：标准(128k) / 较高 / 极高(320k) / 无损 / Hi-Res / 超清母带 / 沉浸声，缺档**逐级下降不回升**，旧配置自动迁移；播放与 App 下载音质分别设置；
- **搜索音质优先**（v3.6.0）：在线结果按音质分档排序（无损 > 高码率 > 低码率），快源抢跑不截断能出无损的慢源；
- **来源标记**：搜索结果严格本地优先，在线结果带 `[kuwo]` / `[migu]` / `[bilibili]` / `[music box]` 前缀，一眼区分；
- **直链输出**（v3.6.0）：配置令牌后，车机/电视等外部播放器经 `GET /direct?id=<源>:<id>&token=…` 302 直连源站 CDN；
- **客户端兼容**：App 1.0.2 复数搜索端点拦截增强（v2.4.1）、飞牛音乐 1.1.19 搜索窗口 50→100（v3.7.6）。

### 收藏与下载

- **红心自动下载**（v2.3.1）：对在线歌曲点红心即后台整轨下载落盘——定名「歌手 - 歌名」、写音频标签、歌词随迁、封面内嵌，不依赖完整试听触发；
- **自动转官方收藏**（v2.3.2）：落盘入库后自动以当前用户身份建官方红心、撤在线条目；官方未及时索引时**持续对账自愈**，收藏全程不丢；
- **下载音质门槛**（v3.6.0）：落库前按内容实测分档（any/320/lossless 可选），不达标先自动换源、仍不达标拒绝入库；
- **元数据兜底**（v2.3.2~2.3.6）：收藏快照补元数据、ID3 抢救定名、unknown 文件按快照自动改名；曲库/滚动缓存分治——支持「关边听边存、只开收藏下载」的纯净用法；
- **推荐配置**：WebUI「边听边存」页**关闭边听边存、打开收藏自动下载**——只有点红心的歌会下载，下载完成打开收藏页即自动入列。

### 曲库管理

- **升级音质**（v3.7.3）：列出曲库真实音质（格式/码率/采样率/位深），到酷我/咪咕找更高音质同名版本替换，支持 mp3 → flac；原文件备份 7 天可回滚，时长差 >3s 拒绝；
- **换源修信息**（v3.7.5）：内嵌信息有错时换源替换，按信息完整度（infoScore 0~6）+音质排序，替换后写回候选标签；「内嵌信息不全」筛选（mutagen 实测文件标签，逐行「缺xx」徽标）；
- **物理删除**（v3.7.5）：勾选多首连文件带歌词/库记录/全部引用一并移除（不进回收站）；路径被重复收录共享时自动拒绝；
- **曲库目录整理**（v3.7.7）：按「歌手/专辑/歌名」两层归位——规划预览（汇总+逐首路径对照+冲突/缺失清单）确认后执行，移动后**立即同步官方库路径**（guid/track 行不动，无需重扫、收藏/刮削保留）；元数据兜底链（官方库→文件标签→文件名解析→未知歌手）；官方 watcher 批量软删**自动对账**；
- **重复清理**（v2.3.11）：按歌名归组扫描（简繁折叠），组内保留音质最优，其余进回收站可找回，收藏/歌单/播放历史引用自动迁移；
- **歌曲匹配**（v2.3.8）：多平台（网易/QQ/酷狗/musicdl）搜索把歌词与封面写回官方数据——QQ QRC 逐字歌词、酷狗 KRC 解密内核取自 [FnMusicEnhance](https://github.com/kuilei0926/FnMusicEnhance)，冷门歌与翻译歌词命中率显著提升。

### 歌单与推荐

- **歌单导入**（v3.6.0）：粘贴网易云/QQ 歌单链接或 ID，逐首在线匹配整轨下载（试听探针+音质门槛）、写标签内嵌封面，官方收录后自动加入目标歌单；
- **最近常听**（v3.6.0）：播放历史（次数+新鲜度）驱动的只读虚拟歌单注入音乐页，数据全在本地；
- **公共歌单 + 酷狗榜单**（v3.2.0）：网易官方榜单/推荐/分类/精品歌单勾选注入（匿名可用），另含酷狗 TOP500 等 20 个官方榜单；
- **网易账号歌单全音源可用**（v2.4.2）：任意音源下都可扫码登录网易云，账号歌单（自建+收藏）注入音乐页；开启即常驻网易组件，搜索仍走所选音源；
- **大模型推荐**（v3.7.6）：Chat Completions / Responses 双协议、每日 token 限额、单次输出上限、用量账本（今日/累计/近 7 天趋势）。

### 小爱音箱（v3.3.0，可选）

把独立应用 [fnmusic-xiaomusic](https://github.com/superxyline/fnmusic-xiaomusic) 移植为容器内桥接服务（端口 8775），管理台三步向导配置，数据独立落盘升级不丢。

- **口令播放**：「播放收藏」随机连播、「顺序播放本地歌曲」（按收藏先后整张循环）、「播放周杰伦的本地收藏歌曲」（按歌手打乱）、「播放晴天」点歌（收藏→本地→在线源，都不写收藏）；30+ 口语化停止变体；
- **睡眠定时**（v3.6.0）：「半小时后停止播放」「定时30分钟」到点自动停止，「取消定时」撤销；
- **正在播放**（v3.7.0）：管理台实时显示歌名 + 本地/在线来源徽标，5 秒刷新；
- **native 口令引擎**（v3.4.0）：自研编排 + 上游登录/设备组件复用；带屏设备后台预解析 audioID 落盘缓存（实测命中 96.7%），屏幕不再随机错歌。

### 界面与运维

- **管理 WebUI**（8774）：音源/音质/存储/推荐/LLM 全部图形化配置、热生效；「关于」页检查更新（GitHub 优先 Gitee 兜底，升级弹窗双下载源）；
- **深色主题**（v3.5.0）：跟随系统/浅色/深色三档；嵌入飞牛时随宿主主题切换，深色不闪白底；
- **ARM64 加固**（v3.7.2）：x86_64 与 arm64 共用一个包，依赖全预编译 wheel、镜像架构自动跟随宿主、构建失败自动装编译工具重试；
- **moo 商店**：`moo.json` 协议源索引，第三方应用商店可直接安装与升级。

## 配置参考

配置集中在项目根目录 `.env`（安装向导生成维护，权限 600），完整键项见 [.env.example](.env.example)。常用项：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_MUSICDL_ENABLED` / `FNMUSIC_NETEASE_ENABLED` / `FNMUSIC_LX_ENABLED` | 单选 | 三音源互斥开关，只能一个为 `true`（热重载） |
| `FNMUSIC_AUTO_COVER` | `true` | 下载歌曲落库后把源站封面内嵌进音频（热重载） |
| `FNMUSIC_LYRIC_AUTO_DL` | `false` | 下载歌曲落库后自动下载同名 .lrc 到同目录（热重载） |
| `FNMUSIC_PLAY_QUALITY` | 跟随旧键自动迁移 | 播放音质七档：`standard` / `higher` / `exhigh` / `lossless` / `hires` / `jymaster` / `sky` |
| `FNMUSIC_DOWNLOAD_QUALITY` | `app` | APP 下载音质：`app`=跟随 APP，或七档之一 |
| `FNMUSIC_DOWNLOAD_MIN_QUALITY` | `any` | 收藏下载音质门槛：`any` / `320` / `lossless` |
| `FNMUSIC_DOWNLOAD_LAYOUT` | `flat` | 新下载落盘布局：`flat` / `artist` / `album` / `artist_album` |
| `FNMUSIC_TEE_SAVE_ENABLED` | `true` | 边听边存开关；`FNMUSIC_TEE_SAVE_DIR` 留空自动探测飞牛共享曲库 |
| `FNMUSIC_FAVORITE_DOWNLOAD_ENABLED` | `true` | 收藏自动下载开关 |
| `FNMUSIC_LIBRARY_NAME_PATTERN` | `{artist} - {title}` | 曲库文件命名模板，占位符 `{artist}` / `{title}` / `{source}` |
| `FNMUSIC_NETEASE_MY_PLAYLISTS` | `false` | 网易账号歌单只读注入音乐页（需扫码） |
| `FNMUSIC_PUBLIC_PLAYLISTS_ENABLED` | `false` | 公共歌单开关（网易榜单/推荐/分类 + 酷狗榜单） |
| `FNMUSIC_QQ_ENABLED` / `FNMUSIC_KUGOU_ENABLED` | `true` | 歌曲匹配页 QQ/酷狗音源开关（无需登录态） |
| `FNMUSIC_LLM_PROTOCOL` | `chat` | 大模型接口协议：`chat` / `responses`（GPT-5 / o 系列推理模型） |
| `FNMUSIC_LLM_DAILY_TOKEN_LIMIT` | `1000000` | 大模型每日 token 上限，`0`=不限 |
| `FNMUSIC_WEBUI_ENABLED` | `false` | 管理 WebUI 开关（端口 8774，无鉴权，仅限可信内网） |
| `FNMUSIC_XIAOMUSIC_ENABLED` | `false` | 小爱音箱接管开关（保存即生效，可与任意音源叠加） |
| `LX_SOURCE_URL` | *(空)* | 洛雪自定义源脚本地址（管理页上传/NAS 选择生成） |
| `FNMUSIC_ONLINE_SOURCES` / `MUSICDL_SOURCES` | 酷我+咪咕 | musicdl 平台白名单（短名/全名均可） |
| `FNMUSIC_ENV_WATCH` | `true` | .env 热重载总开关 |

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

### 隐私说明

扩展部署在你自己的 NAS 上：音频流与下载文件都经本机/局域网交付，不经过任何第三方服务器。它仅会在以下情况连接外部网络——所选音源的平台接口（搜索/取流）、歌词封面源、你自行配置的大模型接口（推荐上下文会发送给该服务商）、以及检查更新时的 GitHub/Gitee Release API。除检查更新外，全部可随对应功能关闭。

## 鸣谢

- **[javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) 原项目及作者 [@javycoder](https://github.com/javycoder)**：本仓库是其二开版本，Socket 接管架构、三音源体系、管理 WebUI、在线收藏/歌单/播放历史等所有基础能力均出自原项目，没有原作者的工作就没有这个二开版，特此致谢！
- **[kuilei0926/FnMusicEnhance](https://github.com/kuilei0926/FnMusicEnhance)**：歌曲匹配页 QQ/酷狗音源的搜索/解密内核（`proxy/enhance_search/`）取自该项目，特此致谢！
- 上游致谢：[CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl)、[darknessomi/musicbox](https://github.com/darknessomi/musicbox)、洛雪音乐（LX Music）社区及其自定义源规范。

<p align="center">
  如果这个项目帮你把 NAS 曲库盘活了，一个 ⭐ 就是给维护者最好的鼓励。
</p>
