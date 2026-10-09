# 上游 v2.7.0 移植清单（2026-10-09 评估稿，待用户拍板）

> 分叉点：我方 HEAD `804e46a`（v2.4.2）⇄ 上游 `ec90c42`（v2.6.3，上次合并基线）
> 上游新提交：`a0bb44f..f036e1f` 共 13 个 → `origin/main` = v2.7.0（`f036e1f`）
> 冲突面：14 个文件双方都改过（求交集得出）；我方独有 30+ 文件（enhance_search / nmplaylists /
> match_* / takeover / musicdl-service 等）上游完全没碰，**不会冲突**。

## 一、上游 v2.7.0 内容全景（按主题拆 7 块）

| # | 主题 | 上游提交 | 规模 | 涉及文件 |
|---|---|---|---|---|
| T1 | **lxserver 洛雪底层重写**：官方生态服务端（XCQ0607/lxserver，内嵌洛雪桌面 musicSdk）替换自研 Python 抓取器+Node 沙箱；覆盖 kw/kg/tx/wy/mg 五平台免登录搜索/歌词/元数据 + 自定义源 vm2 沙箱；`lx:<src>:<id>` 契约不变；预编译产物打包内嵌、离线安装；旧数据自动迁移 | `62c0149` + `6dbe4f0` + `56d7046` + `e9dddae`(lx部分) | **巨**（lxmusic-service -6800 行重写 + Dockerfile/supervisord/entrypoint/build.sh） | lxmusic-service/*、container/*、packaging/fpk/build.sh、scripts/update_lxserver.sh |
| T2 | **洛雪多源管理**：WebUI 维护多个洛雪源脚本（URL/上传/备注名），单源互斥激活即切即生效 | `a0bb44f`(webui+lx+proxy) | 中 | webui-service/*、lxmusic-service/*、proxy/app.py |
| T3 | **搜索深分页**：本地优先布局不变，在线段翻页越过首屏池时向音源增量取页；`FNMUSIC_SEARCH_DEEP_MAX_PAGES`（默认10）；e9dddae 再修游标隔离（独立游标+源维度查重，防跨源重歌误判取尽/死锁） | `a0bb44f`(proxy+docs) + `e9dddae`(proxy) | 中大（search_track 核心 +200 行） | proxy/app.py、docs/SEARCH_PAGINATION.md |
| T4 | **搜索来源标记**：在线条目歌名前加 `[music box]` / `[dl]` / 备注名或 `[lx]`（仅展示，播放/收藏/入库标题保持纯净）；含 `strip_source_tag`（event_report 标题清洗） | `28bf13a` + `e9dddae`(strip) | 小（+76 行 + 独立测试 265 行） | proxy/app.py、proxy/tests/test_source_tag.py(新) |
| T5 | **本地歌曲封面直读**：探测官方封面根目录按魔数嗅探返回，支持 `size` 缩略图，根治接管后本地歌封面回退占位图 | `429ae39` | 小（+90 行 + 独立测试 174 行） | proxy/app.py、proxy/tests/test_local_cover.py(新) |
| T6 | **卸载保留音乐源数据**：uninstall 默认保留数据到持久卷，重装无缝恢复 | `a0bb44f`(packaging+install) | 小 | packaging/fpk/cmd/uninstall_init、install.sh |
| T7 | **WebUI 健壮性**：预览启动失败逆序回滚 + 健康检查；`.env` 无 diff 时仍可重试 lx 激活 | `e9dddae`(webui) | 小（+27/+55 测试） | webui-service/* |

## 二、分级建议

### A 级——建议移植（低成本高价值，与我方改动弱冲突）

| 项 | 理由 | 预估冲突 |
|---|---|---|
| **T5 封面直读** | 独立修复 + 独立新测试文件；解决用户日常可见问题（本地歌封面占位图）；diff 只落在 proxy/app.py 与我方改动区域不重叠 | 低（proxy/app.py 追加块） |
| **T4 来源标记** | 纯展示层 + 独立测试；我方搜索改动（复数端点/开关）与它不同区域 | 低（CONF/_ENV_WATCH_KEYS 小冲突） |
| **T6 卸载保留数据** | packaging 与 install.sh；install.sh 我方动过（sources-data 权限），逐 hunk 对照即可 | 低 |
| **T7 WebUI 健壮性** | 预览回滚/重试与我方 musicbox_pinned 同函数不同块；合并时两块并存 | 中低（webui api_config_put 一区域） |

### B 级——建议缓一缓（大块、依赖链长、当前收益低）

| 项 | 理由 |
|---|---|
| **T1 lxserver 重写** | 上游核心大件，改动近 7000 行；**我方未动 lxmusic-service → 结构上可整取**，但 Dockerfile/supervisord/entrypoint 与我方「musicbox 常驻块」在同一区域（entrypoint 手工并存可行）。价值评估：用户当前音源=musicdl 且已拍板只开 kuwo+migu，洛雪是备用音源——升级能提升备用音源质量（五平台免登录），但非当前刚需；测试面大（容器重建+装机验证） |
| **T2 洛雪多源管理** | 依赖 T1，捆绑走 |
| **T3 搜索深分页** | 上游注明**只作用于网易/洛雪**（musicdl 不支持深分页）——对用户主力搜索源（kuwo/migu）零收益；而改动落在 search_track 核心（我方 2.4.1 改过复数端点拦截与 query 日志），冲突不小。等 T1 落地后连带评估 |

### C 级——明确排除（不再问）

- ❌ `.github/workflows/ci.yml`：我方仓库已删（GitHub 令牌无 workflow scope，推不了含 CI 文件的 ref）——**合并时保持删除**，上游对该文件的修改一并丢弃
- ❌ `VERSION` / `CHANGELOG.md` / `README.md` 上游版本行：版本号以我方为准（合并后 bump 我方版本），CHANGELOG 取我方为基准补移植条目，README 手工并集
- ❌ 上游 `tests/integration/` CI 契约改动：随 T1 决定，默认不带（我方不跑该套）

## 三、冲突文件处置预案（14 个交集文件）

| 文件 | 上游改 | 我方改 | 处置 |
|---|---|---|---|
| `proxy/app.py` | 深分页/封面直读/来源标记/strip | 复数端点+搜索开关+nm_on+_source_enabled+query日志 | **以我方为基准**，按主题逐块移植（同 2.4.0 手法） |
| `webui-service/app.py` | 多源管理+保存重试 | SCHEMA 开关+musicbox_pinned+音乐源常驻 | 我方为基准补上游块 |
| `webui-service/static/{app.js,index.html,style.css}` | lx 面板重做 | 扫码常显+新开关 | 手工并集（lx 区块与我方区域不同） |
| `webui-service/test_webui.py` | 多源+重试测试 | 开关断言更新 | 手工并集 |
| `proxy/tests/test_merge.py` | 深分页+标记测试(+138) | 复数端点测试(+70) | 追加并集 |
| `proxy/tests/test_proxy_reliability.py` | 小改 | 小改 | 看 hunk |
| `container/entrypoint.sh` | lxserver init+启动 | musicbox 常驻块 | 手工并存（两块各自独立） |
| `.env.example` / `README` / `CHANGELOG` / `VERSION` | 常规 | 常规 | 我方为基准 |
| `.github/workflows/ci.yml` | 修改 | **已删除** | 保持删除（C 级） |

## 四、执行方法（若 A 级拍板）

1. `git merge --no-commit origin/main` 起步（同 2.4.0），或先独立 cherry-pick `429ae39`（封面直读）、`28bf13a`（来源标记）——两者 parent 在 `a0bb44f` 之后但文件独立性好，**cherry-pick 优先于整体 merge**，冲突更可控；
2. 每主题跑分套件（proxy/webui）确认无新增失败（Windows 既有基线：proxy 33 / webui 1）；
3. 热补丁部署宿主侧 + 容器重建，实测封面/标记/卸载逻辑；
4. bump 版本（建议 2.5.0，跨主合并）→ README/CHANGELOG 同批 → 双端推送 → NAS 打 fpk → 双端 release。

> B 级（T1+T2+T3）若后续拍板：单独一轮，以整取 lxmusic-service + 冲突文件手工并存为主，
> 需要容器重建装机验证洛雪全链（搜索/播放/歌词/自定义源）。
