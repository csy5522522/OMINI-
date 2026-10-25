# 网页静态缓存与按需加载 · 2026-10-25

## 问题与改动

原 `headers_common()` 将 `Cache-Control: no-store` 用于全部响应，账号接口与 JS/CSS 共用该规则；首页又同时引入备忘录编辑器、备忘录控制器、账号同步控制器和三份样式。公共文件重复下载，课表首屏也需下载其他功能的资源。

现已按响应内容区分缓存：

- 公共 JS/CSS/图标：内容哈希版本 URL，`public, max-age=31536000, immutable`。入口与按需加载器共享服务端生成的版本表。
- HTML 及无版本或版本不匹配的公共资源：`public, max-age=0, must-revalidate`，使用 ETag / 304 复用文件。更新源码自动改变版本引用与 HTML 的 ETag。
- 会话、个人笔记、图片、同步与其他 API：继续 `no-store`。没有缓存账号信息或私有响应。
- 服务端在内存中复用公共文件字节与摘要，文件元数据变化时重新读取。该缓存只保存静态源码。

首屏只加载课表所需的基础 `app.js`、`style.css`。进入备忘录才加载编辑器、控制器、样式和加密草稿工具；登录后按需加载账号同步，进入助手或打开设置才加载相应样式。公用加密工具拆为 `draft-store.js`，沿用原 IndexedDB 数据库、键名和加密方式。加载请求去重，失败提供重试，等待期间可继续切换页面与打开登录。

Nginx 模板增加 HTML / CSS / JavaScript 的 gzip。原有持久卷、账号会话、数据库结构与用户资料保持原状。本次没有制作安装包，也没有操作线上服务和真实客户端数据。

## 验证

- 首屏 JS/CSS 原始体积由 **318,083 字节降为 64,620 字节，减少 79.7%**；测量基准是上一提交 `54ee795` 的首页引入文件。该数字是源码体积，并非线上耗时承诺。
- Chromium 真实网络验收：初次课表只请求两份基础 JS/CSS；后续导航这两份资源的 `transferSize` 为 0；进入功能后再请求对应文件；重复路由不会重复加载共享脚本。
- 模拟编辑器下载延迟时，导航和登录仍可操作；离开备忘录后，下载完成不会重新打开旧页面。模拟脚本或样式下载失败后，页内重试成功，无浏览器运行时错误。
- 公共缓存回归覆盖入口 ETag、304 空响应、长期版本缓存、旧版本回退校验，以及个人 API 仍返回禁止缓存。
- 文件修改回归确认：更新编辑器源码后，HTML ETag 与动态版本 URL 同时变化；旧版本 URL 不被当作当前版本长期缓存。
- 既有游客验收、双 Qt 客户端与 Chromium 同步、备忘录图片编辑/历史恢复/移动布局均通过。全部使用临时目录和测试账号。
- 最终结果：31 项相关 Python 测试、4 组 JavaScript 回归、6 项新增性能验收和 32 项既有真实跨端验收通过。后端日志为 `verification/web-performance-backend.log`；性能报告为 `verification/web-performance/report.json`。

测试入口：`tests/test_web_assets.py`、`tests/test_web_server.py`、`tests/web_performance_check.py`、`tests/web_guest_check.py`、`tests/account_sync_check.py`、`tests/memo_workspace_check.py` 和四组 `.cjs` 回归。日志与报告位于忽略目录 `verification/`。

本机未运行 Nginx，模板语法仍需在部署主机通过 `nginx -t` 校验。GitHub 源码更新后仍需重建 Web 容器，线上站点才会使用这些改动。
