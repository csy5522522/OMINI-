# Omini Web 1.2.3

网页入口 https://my.omini.cn，管理员入口 /admin。Web 包含学校课表、超级备忘录和 AI 编程助手账号管理。下载中心及其任务、日志、文件取回接口已删除；下载保留在桌面版。

首次访问支持游客浏览，右上角「邮箱登录 / 注册」按需登录，登录窗口可以关闭。游客可预览示例课表、切换主题及保存当前浏览器的功能显示偏好；个人课表、备忘录与助手账号仍需登录。已有有效网页会话继续自动恢复，网页退出仅结束当前网页会话。

顶部快捷导航提供「ChatGPT 重置」占位入口（仅展示即将上线提示）和「API 中转站」（新窗口打开 https://api.omini.cn）。

「AI 编程助手 → 成员账号 / API 池」可混合管理 Codex 登录成员和 API Key 线路。「添加 API / Key」支持 OpenAI Chat、Responses、Anthropic、Gemini、Interactions、Vertex API Key、xAI 和 Meta。每行填写一个模型 ID，`上游ID => 对外ID` 设置别名；可配置出口代理、附加请求头、启停和优先级 / 权重。Key 随账号数据加密保存及同步，关闭表单或退出账号时清空输入框。线路检查和模型验证在桌面客户端执行，网页修改同步后在客户端应用调度。

网页采用按需加载：首次打开课表仅请求基础 `app.js`、`style.css`；进入备忘录时加载编辑器、备忘录控制器及样式；登录后才加载共用的账号同步与加密草稿模块，助手样式在进入助手或打开设置时加载。加载失败提供重试，加载期间导航和登录仍可操作。管理员脚本仅在管理员页面加载。

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
python -m unittest tests.test_web_assets tests.test_web_server tests.test_memos tests.test_memo_encryption tests.test_account_sync -v
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
python -m tests.web_performance_check
```
