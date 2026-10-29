# Omini Web

全站与后台共用浅色 / 深色模式，浅色为原配色，深色为墨绿与金色。设置支持 100% / 110% / 125% 缩放，电脑默认 125%、手机默认 100%，按设备保存。聊天正文取消气泡和身份标签，支持真实流式输出、停止生成、复制回复及云端文字历史。左侧加号、拖拽或粘贴图片可添加附件，提供图片预览和发送前移除；支持 PNG/JPEG/WebP、PDF、TXT/Markdown/CSV/JSON/日志、代码与 DOCX。每条最多 6 个附件、每个最多 5 MB、整段对话附件合计最多 12 MB。文本与 DOCX 提取正文，图片和 PDF 需要所选上游模型支持视觉输入。附件只在发送时随请求交给上游，不单独写入服务器磁盘或数据库。

文字聊天与附件元数据按账号加密保存，可从「历史对话」续聊、重命名或删除，每账号最多 100 个会话、50 MiB；附件原文件只保留于当前页面内存，刷新或退出后需重新上传。错误或停止保留已生成文字，不自动重发。界面通过服务端 API 调用上游，不使用 ChatGPT 网页会话。本轮预留 Agent 工具扩展结构，暂未启用工具执行。

「导入课表 → AI 识别」支持图片、PDF、网格型 XLSX 及原有规范表格，先预览编辑再确认导入。后台「课表 AI 线路」独立配置识别地址、Key、协议、模型和默认线路；不同于聊天模型配置。缺失的周次或节次需要补齐，识别和取消不改变正式课表。

侧栏「热搜大全」提供抖音、百度、微博、B 站四张榜单卡片，支持游客浏览，服务端共享缓存与页面自动刷新；单源失败展示带真实时间的缓存，其余榜单继续显示。

2026-10-29 已发布到正式服务器，运行镜像为 `omini-toolbox:1.2.7-workspace-v3-20261029`，本次按要求未生成新备份。接口、迁移、测试和发布/回滚步骤见 [工作台交付说明](../docs/WEB_WORKSPACE_V3.md)。

管理员登录后，在「设置 → 管理控制台 → 打开后台」或 `/admin` 使用总览表盘、用户管理与聊天模型配置。支持新增用户、按邮箱/角色/状态搜索、编辑邮箱、重置密码、启停账号和永久删除用户。删除用户会同时清理其会话、备忘录、图片及同步数据；管理员账号保留。学校账号的登录标识保持固定。

后台「模型配置」按模型显示独立卡片，支持名称、API 基础地址、API Key、协议、模型 ID 和可选显示 ID，每张卡片均可编辑、启停、删除。支持 OpenAI Chat Completions / 兼容接口、OpenAI Responses、Anthropic Messages；获取模型后点击对应 ID 选择，也可手填。旧版一个配置下的多个模型会分别展示卡片，编辑或停用其中一个时自动拆分并保留其余模型。保存后，用户点击聊天页右上角的刷新图标即可选择。Key 随配置加密存入数据库，沿用 `account-sync.key`；编辑时 Key 留空保留原值，前端列表不返回 Key。数据库备份应一并保留该密钥。

表盘展示账号总数、有效会话对应的已登录账号数、近七天新增、停用账号、有效备忘录、可选模型、北京时间今日请求数以及总请求/成功数。请求统计从本功能启用后记录，不保存聊天正文。每账号最多同时生成一条回复，每分钟最多 10 次请求；单次最多 80 条消息。

协议参考：[OpenAI Docs：Responses 与 Chat Completions](https://developers.openai.com/api/docs/guides/migrate-to-responses)。验证：`python -m unittest tests.test_web_chat -v`、`python -m tests.web_chat_check`。

网页入口 https://omini.xin，管理员入口 /admin。Web 包含学校课表、超级备忘录和公告，AI 编程助手与配置管理保留在桌面端。下载中心及其任务、日志、文件取回接口已删除；下载保留在桌面版。

首次访问支持游客浏览，右上角「邮箱登录 / 注册」按需登录，登录窗口可以关闭。游客可预览示例课表、切换主题及保存当前浏览器的功能显示偏好；个人课表和备忘录仍需登录，公告向所有访客展示。已有有效网页会话继续自动恢复，网页退出仅结束当前网页会话。

顶部快捷导航提供「ChatGPT 重置」占位入口（仅展示即将上线提示）和「API 中转站」（新窗口打开 https://api.omini.cn）。

管理员在 `/admin` 的「公告管理」填写标题和正文发布公告，也可编辑和删除。标题最多 120 字，正文最多 20000 字，支持换行；同时编辑时通过版本检查保留当前输入，刷新后再提交。公告出现在顶部提示及顶部导航栏「公告」页面。管理员登录与公告写入在服务端校验权限。

网页账号同步只请求课表与设置，旧助手草稿不会自动上传。桌面自动同步课表、备忘录和设置；AI 助手的成员、配置和操作记录仅在点击「手动同步全部数据（含 AI 助手）」时传输。现有云端助手数据保留，旧客户端需安装更新包后使用新的同步规则。

网页入口加载基础应用、共享主题/缩放、聊天、热搜及课表 AI 模块；进入备忘录时按需加载编辑器、备忘录控制器及样式；登录后才加载共用的账号同步与加密草稿模块，同步备份样式在打开设置时加载。加载失败提供重试，加载期间导航和登录仍可操作。管理员脚本仅在管理员页面加载。

公共 JS、CSS 与图标使用按内容生成的版本 URL，允许浏览器长期缓存；入口 HTML 和未带版本的资源允许存储，并使用 ETag / 304 校验。文件内容修改后，入口会自动引用新版本，动态加载也使用相同的版本表。账号会话、笔记、图片及同步 API 继续返回 `no-store`，公共静态缓存不包含账号数据。Nginx 模板启用 HTML、CSS、JavaScript 的 gzip 压缩。

技术栈：原生 JavaScript / HTML / CSS，备忘录编辑器使用 ProseMirror；后端为 Python + PySide6 HTTP 服务。仓库的 Docker 部署配置使用 MySQL 8.4，本机默认使用 SQLite，通过 `OMINI_MYSQL_HOST` 选择 MySQL。当前未接入 Redis，也没有 Redis 缓存或 Redis 消息队列；同步采用本机持久化待处理记录、后台线程与定时重试。此处描述仓库配置，实际线上部署需以服务器运行配置为准。

课表支持 HAR/JSON 导入、JSON 导出、学期日期和周次选择。所选周次（0 表示全学期）自动保存，浏览器刷新后恢复，并与同账号客户端双向同步。示例课表预览不保存个人周次。

备忘录支持文字图片统一编辑、自动保存、历史回滚、回收站和 TXT 导出。服务器标题、正文、图片与历史采用 Fernet 存储加密，浏览器未保存草稿采用 AES-GCM 加密 IndexedDB。旧 localStorage 草稿成功加密迁移后移除。服务器运行时可解密，这是存储加密。原有导出与历史备份保持原状。

登录后首次打开备忘录立即显示已同步列表和第一篇正文，切回页面立即刷新，也可使用「刷新备忘录」手动读取。刷新前先保存正在编辑的内容，保存失败时保留草稿。若显示暂无已同步内容，请核对网页与客户端的服务地址、邮箱账号，并确认客户端已完成上传。

本机开发：

```powershell
python -m pip install -r requirements-web.txt
python web_server.py --open
python web_server.py --port 18766 --data-dir "D:\Omini-Web"
```

数据默认保存在 AppData/Omini工具箱/web。生产使用 Docker + MySQL，HTTPS 由 Nginx 提供，详见 [部署说明](../deploy/README.md)。更新前备份数据库及两份加密密钥，保留持久卷。

```powershell
python -m unittest tests.test_announcements tests.test_web_assets tests.test_web_server tests.test_memos tests.test_memo_encryption tests.test_account_sync -v
npm install --prefix .cache/web-test --no-audit --no-fund linkedom@0.18.13 fake-indexeddb@6.2.4
npm ci --prefix scripts/memo-editor --no-audit --no-fund
node tests/test_web_frontend.cjs
node tests/test_memo_frontend.cjs
node tests/test_sync_races.cjs
node tests/test_api_members_frontend.cjs
node tests/test_web_guest.cjs
python -m tests.account_sync_check
python -m tests.memo_workspace_check
python -m tests.web_guest_check
python -m tests.web_announcements_check
python -m tests.web_performance_check
```


课表统一从「导入课表」进入：表格导入支持 XLSX / CSV / TSV（提供 CSV 模板），教务系统登录通过学校微信扫码认证后由服务器直接抓取，另保留 HAR / JSON 文件导入。表格每行对应一条课程安排，必需列为课程名称、星期、节次、周次；可选教师、教室、校区、学分。


左侧菜单可折叠为图标，并记住本机偏好；手机端折叠菜单后保留 Logo 和展开按钮。Logo 与首项菜单的桌面间距为 60px。

新增验证：`python -m unittest tests.test_timetable_table tests.test_school_login`、`python -m tests.web_timetable_import_check`、`python -m tests.web_school_direct_check`。

学校登录无需浏览器扩展：选择学年学期后生成学校微信二维码，用已绑定学校账号的微信扫码确认，服务器接续学校 CAS / 教务票据并自动读取课表。已连接会话可直接在线更新；学校会话仅在服务器内存中保存，绑定当前工具箱账号和浏览器会话，退出账号后清除，二维码五分钟过期。服务器重启后重新扫码即可。高级 Cookie 输入仍作为可选入口。

华立同学可直接从登录窗口或「导入课表 → 教务系统登录」扫码，无需邮箱注册、HAR 或扩展。首次扫码由服务器验证学校身份后自动建立独立账号并保存课表；同一学校身份再次扫码会回到原账号。邮箱账号已登录时，扫码仍导入该邮箱账号。学校身份只保存由持久化加密密钥派生的 HMAC，学校 Cookie 留在内存中。必须保留原 account-sync.key 才能保持学校身份与工具箱账号的对应关系。

验证：`python -m unittest tests.test_school_accounts tests.test_school_login`、`python -m tests.web_school_signin_check`。
