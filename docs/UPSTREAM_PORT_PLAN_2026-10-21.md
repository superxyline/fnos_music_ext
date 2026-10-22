# 上游移植清单（javycoder/fnos_music_ext v2.9.0 + v2.10.0a–e）

> 生成时间：2026-10-21。我方 HEAD `43f0b98`（v3.4.2）⇄ 上游分叉点 `ec90c42`（v2.6.3）。
> 上游累计新提交：`ec90c42..origin/main` **93 个**（正式版 v2.9.0，2026-10-15）
> + `origin/main..v2.10.0e` **74 个**（2.10.0a–e 迭代预发布序列，v2.10.0e 于 10-21 当天发布）。
> 上次大批移植：`0f22b6b`（v3.0.0，上游 v2.8.x 精选：封面直出/VIP 封面/同曲复用/no-cache）
> 与 `2568760`（v3.2.0，上游 v2.9.0 三块：公共歌单/关于页检查更新/首播预热共享取链）。
> 另：xiaomusic 上游已停更（v0.6.1 为最终 release，我方 native 引擎正好在用），本清单只覆盖 javycoder 仓库。

## 一、已落地核对（不要重复搬）

逐项 grep/测试文件实证，以下上游能力我方树里已有：

| 上游项 | 上游版本 | 我方证据 |
|---|---|---|
| 公共推荐歌单 #55 | 2.9.0 | `proxy/publicplaylists.py` + `test_public_playlist_service.py`（2568760） |
| 关于页检查更新（双源回退） | 2.9.0 | `webui-service/app.py:761 /api/update-check` + `test_about_page.py`（2568760） |
| 首播准备/共享取链 #56 | 2.9.0 | `test_netease_shared_resolve.py`（2568760） |
| 曲库命名模板 `FNMUSIC_LIBRARY_NAME_PATTERN` | 2.9.0 | `test_library_name_pattern.py`（2568760） |
| 网易云歌单常驻 #38 | 2.9.0 | `webui-service/app.py:126 FNMUSIC_NETEASE_PLAYLIST_RESIDENT` + `musicbox_pinned()` |
| 在线封面同源直出 #48 | 2.8.1 | `171c99c` |
| 网易 VIP 封面缺失（部分） | 2.8.0 | `536927c` |
| 边听边存同曲跨 guid 复用 #52 | 2.8.1 | `9726b3d` + `usable_artist` 兜底 |
| 新版本弹窗 `hidden` 兜底 | 2.9.0 | `style.css:263 .modal-mask[hidden]` |
| 探活/播放 UA 一致 #54 | 2.8.3 | `test_lx_ua.py`（2568760） |
| 流式禁缓冲头 #49 | 2.9.0 | `test_stream_no_buffer.py`（2568760） |
| musicdl 2.14.1 平台表 | 2.9.0 | `musicdl-service/requirements.txt >=2.14.1` |
| 统一日志 fnlog | 2.9.0 | `proxy/fnlog.py` |
| moo 商店索引 | 2.9.0 | `tools/update_moo_index.py` + `moo.json`（我方发版流程在用） |
| 搜索来源标记 / 本地封面直读 | 2.7.0 | `689a8cb` / `faa0992` |
| 第三方音源搜索超时修复 + 逐曲探活 beta | 2.6.4c | `62a9182` 已并入 |

## 二、未移植内容全景

### 2.9.0 剩余（6 项）

| # | 主题 | 上游提交 | 主要文件 | 用户价值 | 冲突面 |
|---|---|---|---|---|---|
| A1 | **VIP 曲目行级标记 #57** | `2bc1281` | musicbox-service/app.py、netease_ext、proxy/app.py、recommend.py | 中低（纯展示；无 VIP 账号则无感） | 中 |
| A2 | **出声提速 + 热门日轮换 #55** | `947ce2d` | proxy/recommend.py、app.py、lxmusic-service/app.py | 中（坏链降档提速；推荐每日换血） | 中（recommend.py 我方演过） |
| A3 | **搜索空结果有界重试 #18** | `8cf2ba8`/`8e65db5` | proxy/app.py | 中低（音源全挂时首屏体验） | 中高（search 我方大改：复数端点/开关/探活） |
| A4 | **洛雪失效源自动取消激活 #59** | `4521024` | webui-service/app.py、static/app.js | 低-中（lx 是备用音源） | 低 |
| A5 | **洛雪 bangid 动态解析 #47** | `521ddc9` | lxmusic-service/lxserver_client.py、app.py | 低（备用源免登录榜单） | 低（我方几乎没动 lxmusic） |
| A6 | **songInfo 磁盘持久化 #49** | `e34f254` | lxmusic-service/*、proxy/app.py | 低-中（重启后 lx 推荐歌单可播） | 低-中 |

### 2.10.0 全系（13 项，均为预发布，未经上游完整人工验证）

| # | 主题 | 上游提交 | 主要文件 | 用户价值 | 冲突面 |
|---|---|---|---|---|---|
| B1 | **深色主题 + 设置页** | main: `82d8871`+`191277f`（PR #72）；2.10.0e 追加修复 `98a5a37`/`749b61c`/`44b830c` | webui-service/static/{app.js,index.html,style.css,theme-boot.js(新)} | **高**（日常可见；PR72 仅 4 文件 +198/-27，自包含） | **低** |
| B2 | **在线歌曲优先播本地已验证文件** | `1738893`+`f65e12b` | proxy/app.py、transcode.py、localmatch.py(新) | 中-高（省流量/弱网秒开） | 中 |
| B3 | **收藏绑定先转存曲库 #82** | `4502fde` | proxy/app.py | 高（边听边存+自动绑定用户；与我方 `e5e87fa` fav_autobind 同区域） | **高** |
| B4 | **收藏/歌单红心同步 #77** | `1a13d76`/`53b633f`/`2a6df1c` | proxy/app.py | 中 | 中 |
| B5 | **无损保留 + 解码降级 #79** | `1d61b68`/`cd3382d`/`32b9460`/`fce1e46` | proxy/app.py、takeover.py | 中 | 中 |
| B6 | **搜索超时恢复 #69** | `2588e84`/`0bdcead`/`55be58d` | musicdl-service/{app,hardening,search_core,worker}.py、proxy/app.py、container/Dockerfile | 中 | 中高 |
| B7 | **公共歌单关键词搜索/连续加载** | `0002336`/`761dba1` | webui static、lxmusic-service/*、musicbox-service/* | 中 | 低-中 |
| B8 | **「猜你喜欢」LLM 推荐** | `3e99405`/`3ae04c5`/`bab504d` | proxy/localrecommend.py(新)、recommend.py、app.py、webui | 中（新特性） | 中高（recommend.py 双方演） |
| B9 | **垃圾清理 Beta** | `7b14c74`/`245c14d`/`91e2b9d`/`a5cf2c6`/`86d2edb` | proxy/maintenance.py(新)+maintenance_socket.py(新)、webui_gateway.py、webui、packaging/fpk/build.sh、extend.sh | 中（fnlog 已落地，顺理成章；Beta 属性） | 中 |
| B10 | **洛雪掉线恢复 #78** | `32ca5d9`/`70a13d2` | lxmusic-service/*、webui-service/app.py | 低-中（备用音源） | 低-中 |
| B11 | **手机 lx 首播稳定 #75** | `4a91761` | proxy/app.py、takeover.py | 低-中 | 中 |
| B12 | **安装/运行时改进 #70/#71/#73** | `88e41df`/`5519891`/`4b39d98` | packaging/*、container/*、install.sh | 中（安装健壮性；ARM64 项 `302acd1` 对 x86 用户无关） | 中（packaging 我方改过） |
| B13 | **lxmusic 移除探活开关（breaking）** | `665bfa0` | lxmusic-service | 低 | 低 |

## 三、分级建议

### A 级——建议首批（低冲突、高收益、独立成块）

| 顺序 | 项 | 理由 | 预估冲突 |
|---|---|---|---|
| 1 | **B1 深色主题 + 设置页** | 上游 PR #72 终态仅 4 文件且主题块自包含（app.js +81 行独立区块 + theme-boot.js 新文件 15 行 + style.css 变量覆盖）；日常可见收益；**注意**：移植后必须保留我方 3.4.2「菜单无红点」决策，PR72 本身不带红点，但要防其关于页交互覆盖我方 `xmDoneHtml` 口令表/xiaomusic 页 | 低 |
| 2 | **B2 在线优先播本地** | `localmatch.py` 为新文件零冲突；app.py/transcode.py 改动集中在取流决策点，与我方 patches 不同区域 | 中 |
| 3 | **B3 #82 收藏绑定** | 用户若开着边听边存+自动绑定（`e5e87fa`）会撞上；上游修复与我方实现同区域，需人工对照合并，但价值明确 | 高（可控） |

### B 级——按需排期

| 项 | 说明 |
|---|---|
| B4 红心同步 #77 | proxy/app.py 单文件，逐块搬 |
| B5 无损保留 #79 | 与 B3 同在 tee/绑定区域，可搭车 |
| B6 搜索恢复 #69 | 涉及 musicdl-service 四文件，我方 musicdl 改过（bili 源），逐函数对照 |
| B7 公共歌单搜索 | 上游在 `0002336` 之后继续演进，取 2.10.0e 终态整块对比我方 3.2.0 移植版 |
| B2/A2 提速与日轮换 | recommend.py 双方都演，收益中等 |
| B9 垃圾清理 | fnlog 之上顺理成章，但 Beta；且引入 `maintenance_socket.py` 新进程面 |
| B8 猜你喜欢 | 大件新特性；等 B1 站稳再评估 |

### C 级——暂缓/排除（本轮不问）

- **lxmusic 系（A4/A5/A6/B10/B13）**：洛雪是备用音源（用户主力 musicdl kuwo+migu），且我方 lxmusic-service 几乎未动——等哪天决定升级 lxmusic 时整 wave 一起评估，现在单搬收益低还割裂上下游。
- **A1 VIP 标记**：用户无 VIP 权益账号则完全无感。
- **A3 #18 空结果重试**：我方搜索链路已大改（复数端点/开关/探活），上游补丁适配成本 > 收益。
- **B12 安装改进**：只取 x86 相关（#70 镜像复用/#71 去主机网络/#73 运行时探测）；ARM64 自愈（`302acd1`）跳过；packaging 我方 FPK 流程（fnpack+repack）刚稳定，不在发版周期里动。
- **`.github/workflows/*`**：老规矩排除——我方 GitHub 令牌无 workflow scope，推不了含 CI 文件的 ref；上游对该目录的全部修改丢弃。
- **VERSION / CHANGELOG.md / README.md**：以我方为准，移植条目记入 CHANGELOG `[未发布]`，README 手工并集。

## 四、冲突文件处置预案

| 文件 | 上游改（累计） | 我方改 | 处置 |
|---|---|---|---|
| `proxy/app.py` | **+2423 行**（2.9.0+2.10.0 全系） | 重度（红心/对账/收藏绑定/搜索/封面/流式/xiaomusic 无关区） | **以我方为基准逐块移植**（同 v2.4.0/3.0.0/3.2.0 手法）；每块配上游对应测试 |
| `webui-service/static/app.js` | +968 行（2.10.0e 含主题/清理/猜你喜欢/公共歌单搜索） | 重度（xiaomusic 页/匹配页/口令表/3.4.2 步骤条） | 同上；B1 主题块自包含优先 |
| `webui-service/static/style.css` | +227 行 | 3.4.2 删 nav-dot 等 | 变量覆盖层叠加，勿动我方删除区 |
| `webui-service/static/index.html` | +174 行 | xiaomusic/关于导航改动 | 逐 hunk |
| `webui-service/app.py` | +1150 行 | SCHEMA/常驻/musicbox_pinned | 逐块 |
| `proxy/recommend.py` | +97 行 | 日推荐/空结果冷却自研 | A2/B8 时重点对照 |
| `lxmusic-service/*` | lxserver 重写后大量演进 | 仅 +14 行（v2.6.4c 探活/酷狗时长） | C 级项整体缓办；真要搬时以上游为基准再贴回我方 14 行 |
| `musicdl-service/*` | search_core/hardening/worker 新模块 | bili 源自研 | B6 时逐函数对照 |
| `packaging/*`、`container/*`、`install.sh` | 安装/ARM64/镜像复用 | FPK 流程自研（fnpack+repack） | B12 只取 x86 安装项，逐 hunk |
| `proxy/takeover.py` | 白名单/诊断 | 白名单 fav_diag | 同函数手工并集 |

**上游纯新增文件可直接取用再适配**：`theme-boot.js`、`proxy/localmatch.py`、`proxy/localrecommend.py`、`proxy/maintenance.py`、`proxy/maintenance_socket.py`、`scripts/gen_alipay_qr.py`、`scripts/update_lxserver.sh`、各上游新测试文件。

## 五、注意事项

1. **红点红线**：上游 2.9.0「检查更新」设计 = 菜单红点常亮；我方 3.4.2 已按用户要求删除全部菜单红点（`xmUpdateNavDot`/`setUpdateBadge` 只剩关于页内 NEW 徽标）。**任何 webui 移植不得把红点逻辑带回来**。
2. **2.10.0e 是预发布**：上游未经完整人工验证。移植其修复时以 main v2.9.0 为稳定参照系，2.10.0 的修复挑着搬、逐个过测试。
3. **PR #72 在 main 与 2.10.0e 有分叉**（`b796631` revert 后经 `2acf02b` 重新并入并带额外修复）：B1 取 **origin/main 的 PR72 终态（`22122ad`）** + 2.10.0e 的三个小修复（`98a5a37` 对比度 / `749b61c` hover / `44b830c` 宿主主题刷新）。
4. 每块移植后必跑：`pytest xiaomusic-service/` + `pytest webui-service/`（webui 基线允许 1 个 HEAD 既有失败 `test_put_hot_keys_roundtrip_preserves_custom`）+ FPK 打包冒烟。
5. 移植提交信息沿用「（移植上游 <版本> #<issue>）」格式，便于回溯。
6. 推送/发版前按 AGENTS.md §3 完成隐私审查。

## 六、建议动作

首批只做 **B1 深色主题**：先出详细方案（theme-boot.js 引入、CSS 变量映射到现有样式、设置页落点、与我方 xiaomusic 页/移动端底栏的兼容矩阵、回归测试），确认后热部署验证，再决定 B2/B3 是否跟进。
