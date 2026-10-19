# Omini Web

新增的浏览器版本，只保留下载中心和学校课表。桌面版 1.1.2 继续独立使用。

## 启动

Windows 下双击根目录的 `Start-Omini-Web.cmd`，浏览器会自动打开 `http://127.0.0.1:18765`。保持服务窗口运行；按 Ctrl+C 停止服务时会暂停队列。关闭浏览器页不影响后台下载。

开发环境需 Python 3.11 和根目录 `requirements.txt` 中的依赖。首次准备可运行：

```powershell
python -m pip install -r requirements.txt
python web_server.py --open
```

源码目录 `tools/` 中需包含对应下载引擎：yt-dlp、aria2c、ffmpeg、gallery-dl、spotdl。可从已有完整 Windows 安装目录复制 `tools/`；也支持在 PATH 中查找。界面会显示引擎是否就绪。

```powershell
python web_server.py --port 18766 --download-dir "D:\Omini下载" --open
```

默认下载到运行服务电脑的 `~/Downloads/Omini-Web/`，每个任务使用独立子目录，完成后可从「文件」保存到访问页面的设备。队列、日志和课表默认存放在 `%APPDATA%/Omini工具箱/web/`；与桌面版配置分开，不自动读取个人桌面课表。可通过 `--data-dir` 指定其他位置。

## 通过 Tailscale 访问

在 PowerShell 中执行；把示例 IP 换成本机 Tailscale IPv4，密码设置为自己选择的至少 12 位密码：

```powershell
$env:OMINI_WEB_PASSWORD = Read-Host "设置 Web 访问密码"
python web_server.py --host 100.101.102.103 --port 18765
```

其他已组网设备访问 `http://100.101.102.103:18765` 并输入密码。请使用启动时指定的 IP 地址访问；本版不使用 MagicDNS 别名作为 Web 访问地址。Tailscale 网络策略与防火墙需允许此端口。本服务仅面向本人电脑和受信任的 Tailscale 网络，属于同一工作空间；同密码的设备共享队列和课表。服务本身使用 HTTP，远程链路由 Tailscale 加密，不要把端口映射到公网。

默认只监听本机回环地址；绑定远程地址时要求访问密码，不接受 `0.0.0.0`。浏览器登录使用 HttpOnly / SameSite=Strict 的会话 Cookie，12 小时到期，服务重启后需重新登录。密码通过环境变量传入，不写入配置。Web API 校验 Host、Origin 和客户端请求标识。

## 下载中心

- 视频、音频、普通文件、图库、M3U8、磁力链接、字幕、Spotify 音频。
- 任务进度、日志、搜索和状态筛选；暂停、继续、取消和重试；并发数可配置为 1～5。
- Web 服务重启后保留队列，未完成任务变为暂停，点击继续恢复。
- Web 版接收公开链接。交互式登录、浏览器 Cookie 提取和抖音图文的内置浏览器回退仍由桌面版提供；需要这些能力时使用桌面版。
- 文件操作只在指定 Web 下载根目录和任务子目录内进行，不提供任意本地路径读取和命令执行。

## 学校课表

- 从广州华立学院教务系统导出的 HAR（含响应）导入，或导入 / 导出 Omini 课表 JSON。
- 周次选择、全学期视图、第一周周一设置、本周定位、今日课程、课程详情。
- 保留不连续节次、单双周和同一时段的多门课程；冲突课程并排显示。
- 「学校登录」在独立标签页打开学校入口；「在线刷新」可填写当前教务 Cookie 读取指定学期，会话仅用于本次请求，不保存。
- 浏览器页面不自动读取其他学校网站的 Cookie；Web 版没有桌面版内置登录窗口的自动捕获功能。手机可导入电脑导出的 HAR / 课表 JSON。
- 「班级课表查询」在学校页面打开，由学校决定可访问的班级和资料。
- 「预览示例课表」仅在当前页面显示明确标注的示例，不保存、不覆盖个人课表。
- HAR 上传后只保存课程展示字段，不保留原 HAR、学号、学生姓名或登录请求头；导入失败保留原课表。服务重启可恢复课表。

## 界面与验证

苔绿与米白配色、侧栏导航、下载卡片和课程周视图。支持深浅主题和响应式布局。设计参考 [shadcn/ui](https://github.com/shadcn-ui/ui) 与 [shadcn-admin](https://github.com/satnaing/shadcn-admin)，本版 HTML / CSS / JavaScript 为项目内实现，没有复制上游组件源码，也不依赖外部 CDN。

```powershell
python -m unittest tests.test_web_server -v
node --check web/app.js
npm install --prefix .cache/web-test --no-audit --no-fund linkedom@0.18.13
node tests/test_web_frontend.cjs
```

Web 接口测试使用隔离目录和本地文件服务，验证真实下载与文件取回、暂停 / 继续 / 取消、队列并发、HAR 导入、缓存隐私、输入与访问边界、服务重启恢复。DOM 测试验证按钮、主题、筛选、课程重叠与不连续节次、导入 / 导出、失败后数据保留和 HTML 转义；linkedom 仅作为测试依赖，运行 Web 服务不需要 Node.js。
