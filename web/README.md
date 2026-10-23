# Omini Web 1.2.2

网页入口 https://my.omini.cn，管理员入口 /admin。Web 包含学校课表、超级备忘录和 AI 编程助手账号管理。下载中心及其任务、日志、文件取回接口已删除；下载保留在桌面版。

课表支持 HAR/JSON 导入、JSON 导出、学期日期和周次选择。所选周次（0 表示全学期）自动保存，浏览器刷新后恢复，并与同账号客户端双向同步。示例课表预览不保存个人周次。

备忘录支持文字图片统一编辑、自动保存、历史回滚、回收站和 TXT 导出。服务器标题、正文、图片与历史采用 Fernet 存储加密，浏览器未保存草稿采用 AES-GCM 加密 IndexedDB。旧 localStorage 草稿成功加密迁移后移除。服务器运行时可解密，这是存储加密。原有导出与历史备份保持原状。

本机开发：

```powershell
python -m pip install -r requirements-web.txt
python web_server.py --open
python web_server.py --port 18766 --data-dir "D:\Omini-Web"
```

数据默认保存在 AppData/Omini工具箱/web。生产使用 Docker + MySQL，HTTPS 由 Nginx 提供，详见 [部署说明](../deploy/README.md)。更新前备份数据库及两份加密密钥，保留持久卷。

```powershell
python -m unittest tests.test_web_server tests.test_memos tests.test_memo_encryption tests.test_account_sync -v
npm install --prefix .cache/web-test --no-audit --no-fund linkedom@0.18.13 fake-indexeddb@6.2.4
node tests/test_web_frontend.cjs
node tests/test_memo_frontend.cjs
python -m tests.account_sync_check
python -m tests.memo_workspace_check
```
