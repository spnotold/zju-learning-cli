# zju-learning-cli

> 这是 [8eoyw/zju-learning-cli](https://github.com/8eoyw/zju-learning-cli) 的**简体中文版** fork：提示、报错、帮助文字和输出文件夹名都改成了简体中文（大陆用语），另外合入了还在上游评审的 `rollcall` 签到查询命令。繁体中文版请用上游仓库。

学在浙大（courses.zju.edu.cn）与智云课堂（classroom.zju.edu.cn）的单文件命令行工具：同步课件、把智云课堂的 PPT 截图合并成 PDF（可去除重复截图）、导出课堂语音转录、查待办。

它是 [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)（ZLA）的 CLI 移植版。ZLA 是很好用的桌面 GUI，但没办法写进脚本、定时任务或让 AI agent 调用；这个项目把它的 API 逻辑改写成一个 Python 文件，补上增量同步、并行下载、PPT 去重，也修了几个上游的边界情况。

## 功能

| 命令 | 作用 |
| --- | --- |
| `zju login` | 设置学号，密码存进系统凭证库（macOS Keychain、Windows 凭据管理器；其他系统走 [keyring](https://pypi.org/project/keyring/)） |
| `zju courses [--all]` | 课程列表（默认只列最新学年） |
| `zju sync [课程...]` | 增量同步课件到 `<输出目录>/<课程>/` |
| `zju todo` | 待办事项，按截止时间排序（本地时区） |
| `zju notify [课程…] [--json]` | 测验、作业新增／开放／时间变更／临近截止提醒，本地去重；包含以限时作业形式发布的小测 |
| `zju activities [课程…] [--type forum homework …]` | 列出所有活动（课件、视频、作业、讨论、网页、链接、测验），附状态与截止时间 |
| `zju show <活动id> [--read]` | 活动详情：说明、附件、完成条件；作业显示自己的提交状态，讨论显示帖数；`--read` 下载附件并打印文字（docx/pptx/pdf/doc，贴图标成 [图]） |
| `zju forum list <讨论id> [--mine] [--full]` / `forum read <topic>` | 列出讨论帖／读帖与回复 |
| `zju forum post <讨论id> --title … --body …` / `forum reply <topic> --body …` | 发帖／回帖，可 `--body-file`、`--attach` 附件 |
| `zju upload 文件…` | 上传文件到学在浙大，输出 upload id |
| `zju submit <作业id> --file … [--body …] [--draft] [-y]` | 交作业；默认提交前确认，已截止会拦下 |
| `zju classroom search 关键词` | 在智云课堂找课，取得 `course_id` |
| `zju classroom subs <course_id>` | 列出该课每一堂的 `sub_id` |
| `zju classroom day [日期] [--days N]` | 某天（或最近 N 天）自己的课，含追踪中的课 |
| `zju classroom add\|rm <course_id>...` | 追踪课表外的课（听课申请已获批准的）。智云的课表 API 只返回自己选的班，追踪后 `classroom day`、`ppt --days`、`transcript --days` 都会带上，输出文件夹为“课名 老师”避免和自己那班撞名 |
| `zju classroom tracked` | 列出追踪中的课 |
| `zju rollcall [--in-class] [--json]` | 列出进行中的签到（只查看、不代签）；`--in-class` 只在当天课表的上课时段查询（默认前后各放宽 10 分钟，可用 `--margin` 调整），给定时提醒用 |
| `zju ppt --course <id> \| --days N [--dedup]` | 智云 PPT 截图 → `<课程>/智云PPT/<堂>.pdf` |
| `zju transcript --course <id> \| --days N` | 语音转录 → `<课程>/转录/<堂>.txt\|srt\|md` |

## 安装

需要 [uv](https://docs.astral.sh/uv/)。依赖写在文件头（PEP 723），第一次运行时 uv 会自动安装。

```bash
git clone https://github.com/spnotold/zju-learning-cli-simplified.git
ln -s "$PWD/zju-learning-cli-simplified/zju.py" ~/.local/bin/zju   # 或直接 ./zju.py
zju login
```

没有 uv 的话：`pip install requests img2pdf pillow keyring numpy`，再用 `python zju.py ...` 运行。

也可以不开终端，直接双击登录脚本（每次都会清掉上一次的学号、密码和 cookie，重新登录）：

- macOS：`zju_login.command`，在访达里双击会打开一个终端窗口。用 git clone 下载的可以直接双击；下载 zip 的会被 Gatekeeper 拦下，先运行一次 `chmod +x zju_login.command && xattr -c zju_login.command`。
- Windows：`zju_login.bat`，需要 Python 启动器 `py -3.11`，并先用 pip 装好上面的依赖。

## 使用

```bash
zju sync --dry-run                 # 先看会下载什么、总共多大
zju sync                           # 最新学年全部课程
zju sync 微积分 123456              # 指定课程（名称片段或 id，id 用 zju courses 查）
zju sync --videos --max-size 0     # 连音视频和大文件一起下载
zju sync -j 8                      # 并行数（默认 4）

zju classroom day --days 7
zju ppt --days 1 --dedup           # 今天所有课的 PPT，重复截图只留最完整的一张
zju transcript --days 1 --format md
```

输出目录的优先级：`--out` > 环境变量 `ZJU_OUT` > `~/.config/zju-learning/config.json` 的 `"out"` > `~/ZJU-Courses`。

定时任务示例（cron，每天 22:00）：

```cron
0 22 * * * ~/.local/bin/zju sync && ~/.local/bin/zju ppt --days 1 && ~/.local/bin/zju transcript --days 1
```

退出码：`0` 成功；`1` 设置、登录或 API 错误；`2` 部分文件或堂次失败（其余照常完成）。

### 小测与作业提醒

`activities --type exam homework` 可以查看现有测验和作业；`notify` 会保存上次查询的记录，只输出新的提醒。它同时读取正式测验列表和完整的分页作业列表，不依赖标题里是否有“小测”二字，因此以作业形式发布的随堂练习也能覆盖。

```bash
zju notify                         # 检查最新学年的课程；首次列出现有未完成、未截止的项目
zju notify --init                  # 可选：静默建立记录，跳过现有项目的首次提醒
zju notify 微积分 123456 --json      # 指定课程；输出事件数组，没有新提醒时为 []
zju notify --type homework         # 只查作业；--type exam 只查正式测验
zju notify --due-within 60          # 截止前 60 分钟提醒一次，默认 30；0 关闭截止提醒
zju notify --in-class --json       # 只在课表上课时段（前后各放宽 --margin 分钟）查正在上的课，其余时间不联网
```

提醒包括新发现的项目（`new`）、定时发布后开放或重新开放（`opened`）、开放／截止时间调整（`schedule_changed`）、临近截止（`due`）。同一项目同一轮的多个原因会合成一条提醒；同一个截止时间只提醒一次。已提交、已关闭、已截止或明确标为未发布的项目不会提醒。时间显示为本地时区，API 中没有时区的时间按北京时间解析。

每次执行只检查一轮，与 `rollcall` 一样输出到终端或供外部定时任务读取，不直接发送桌面或消息推送。课上小测只在上课时发布，可以上课时段每分钟查当前课程、其余时间低频全查，例如：

```cron
* * * * *   ~/.local/bin/zju notify --in-class --json
0 */2 * * * ~/.local/bin/zju notify --json
```

两条同时触发时后启动的一条会因进程锁报错退出，不影响记录；外部脚本可以把两种检查合成一个任务，避免重叠。课表查询失败时 `--in-class` 报错退出，不会当成“今天没课”跳过。

推送程序建议加 `--outbox FILE`：事件先追加到这个 JSON 数组，再保存去重记录，推送程序发送成功后再从数组中删除，这样推送失败或进程中途退出都不会丢提醒。

JSON 中每条事件包含 `id`、`type`、`title`、`course_id`、`course_name`、`status`、`start_time`、`end_time`、`reasons`；时间为 UTC ISO 8601 或 `null`。普通文本没有新事件时不输出内容，JSON 则输出 `[]`。输出成功后即记录为已提醒；外部推送程序需要自行处理发送失败与重试。

记录保存在 `~/.config/zju-learning/notifications-<用户ID>.json`（支持 `ZJU_STATE_DIR`），按账号、课程、活动类型和 ID 区分；切换课程或类型筛选不会清除其他记录。查询失败时返回退出码 `1`，本轮不输出事件、不更新记录，下次可重试。损坏的记录会明确报错；进程锁避免定时任务重叠导致重复提醒。

该命令覆盖学在浙大的 `exam` 和 `homework`，不包含“课堂互动”里的即答题等其他活动。短于轮询间隔的小测可能在两次查询之间开始并结束，需要按实际时限设置定时任务；作业可能在课外发布，查询不受当天上课时段限制。`--init` 仅抑制初始化当时的提醒，之后开放、改期以及尚未到提醒窗口的截止事件仍会输出。

## 跟 ZLA 的差异

- **增量同步**：以 `.zju_manifest.json` 记录 upload id，而不是比对文件名和大小；老师换了新版（新 id）才会重新下载。
- **下载完整性**：先写 `.part-*`，核对 `Content-Length` 后才 rename；空文件、截断、服务器返回的 HTML 错误页都不会被记成已下载。
- **并行下载**：课件默认 4 个文件同时，PPT 截图 8 张同时；每条线程有自己的 session，共用 cookie jar。遇到 429/503 会按 `Retry-After` 退避。
- **PPT 去重**（`--dedup`）：智云是对投影画面定时截图，同一页会因动画逐步出现、老师边讲边写、翻回前面而被截很多次。一页的笔画若全都还在后面那页（或之前留下的某页）里就删掉，所以动画只留跑完的那张、手写只留写完的那张，批注不会丢。用局部对比找笔画，白底、黑底、底图纹理、教学视频都适用；实测三堂课 73→68、81→48、176→99 页，逐页核对无误删。`--keep-images` 仍保留全部原图。
- **默认直连**，连不上才改走系统 proxy；连接 timeout 6 秒，定时运行时不会卡死。只有幂等请求会自动重试，登录 POST 不会被重发。
- **学年判断**：学校常常不把旧课程标成已结束（`is_closed`），因此改用 `academic_year_id` 找最新学年。
- **同名课程**（不同教学班）分开存放；同一课程里的同名文件一律加上 id，命名不受 API 返回顺序影响。
- 默认跳过音视频文件和 200MB 以上的文件（通常是软件安装包、项目压缩包），`--dry-run` 会列出总大小。

修掉的上游边界情况：

- 智云 `search-ppt` 不遵守 `per_page`：常常第 1 页就返回全部，下一页再重复一次。ZLA 假设每页最多 100 张，超过 100 页的课会一直重试然后失败；这里改成按顺序去重。
- 转录 API 对“还没有语音数据”返回 `code=10002`，现在当成“无转录”处理，不再中止整批。
- **明文发送登录凭证**：智云 PPT 截图网址是 `http://`，而 `.zju.edu.cn` 的 SSO cookie（包括 `iPlanetDirectoryPro`）没有设 `Secure`，照常下载就会把登录凭证用明文发出去。这里把学校主机的网址升级成 HTTPS，而且所有 `http://` 请求都不带 Cookie 和 Authorization。
- `courses.zju.edu.cn` 与 `identity.zju.edu.cn` 只支持 1024-bit DHE／静态 RSA，OpenSSL 3 默认拒绝连接（`DH_KEY_TOO_SMALL`）。`SECLEVEL=1` 只用在这两台，其他主机维持默认的 TLS 设置。
- 智云的 `_token` cookie 设在 `.zju.edu.cn` 父域名，而不是 `classroom.zju.edu.cn`。

## 附件下载来源

`sync` 对每个附件按优先级依次尝试以下来源，用第一个能拿到文件的：

1. `/api/uploads/reference/{rid}/blob`：常规下载
2. `/api/uploads/{id}/blob`：原始文件
3. `/api/uploads/{id}/blob?refer_id={活动id}&refer_type=learning_activity`：reference 参数与官方网页前端下载按钮发送的相同（`classroom` 活动用 `classroom_activity`、考试不带）；部分活动的附件只有此来源提供，来源标记 `未开放原文件`
4. `/api/uploads/reference/document/{rid}/url?preview=true`：预览器转出的 PDF（[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader) 采用的方式）

**定时发布、尚未开放的活动**（`is_started=false`）通常由来源 3 照常下载；若所有来源都返回 403，该文件标记 `[未开放]（开放时间）`，不算失败，开放后下次 `sync` 会自动下载。

## 安全性

- 密码只存在系统凭证库。macOS 由系统 `security`、Windows 由 Python `getpass` 在终端提示输入，不会出现在命令行参数或 shell 历史记录。也可以改用环境变量 `ZJU_USER` / `ZJU_PASS`。
- 登录时密码先用 CAS 提供的公钥加密再发送，跟网页登录的做法相同。
- Session cookie 以 JSON（不是 pickle）缓存在 `~/.config/zju-learning/cookies.json`；在 macOS／Linux 上文件权限是 `0600`，目录是 `0700`。缓存位置可用环境变量 `ZJU_STATE_DIR` 覆盖。
- Cookie 只会通过 HTTPS 发往 `*.zju.edu.cn`，明文 `http://` 请求一律不带。没有任何遥测。
- TLS 验证失败会直接报错，不会自动重试或改走 proxy，避免把中间人攻击误当成网络不稳。

## 免责声明

在 macOS 和 Windows 上实测过；Linux 理论上可以使用，但没有测试过。

仅供个人学习使用。课件的版权属于授课教师与学校，请勿传播下载的内容；使用时请遵守学校的相关规定，不要高频或大量抓取。学校的 API 没有公开文档，随时可能改版。

## 致谢

- [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)（MIT）：登录流程、学在浙大与智云课堂的 API 调用都移植自它的 `src-tauri/src/zju_assist.rs`。本项目沿用 MIT 许可证并保留其版权声明，见 [LICENSE](LICENSE)。
- [eWloYW8/ZJU-course-material-download](https://github.com/eWloYW8/ZJU-course-material-download)（MIT）、[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader)、[xzzd-pro/xzzd-pro](https://github.com/xzzd-pro/xzzd-pro)：参考了关闭下载时的端点做法（没有拷贝代码）。

## 许可证

[MIT](LICENSE)
