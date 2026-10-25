# Docker / MySQL 部署

正式服务为 https://my.omini.cn，管理入口为 https://my.omini.cn/admin。桌面设置默认连接此地址，也支持「连接官方云端」切回。

服务包括独立 Web 容器和 MySQL 8.4 容器。MySQL 无宿主机映射端口；Web 只映射 `127.0.0.1:18765`，由宿主机 Nginx 提供 HTTPS。`mysql_data` 保存账号、加盐密码哈希、会话、备忘录、图片、版本及个人偏好；`web_data` 保存服务设置和两份存储加密密钥。备忘录、图片、课表和功能选择均按账号隔离。Web 下载中心及任务/文件接口在 1.2.2 已删除，原下载卷保留为旧数据，更新不再挂载。

首次部署，在项目内创建 `deploy/secrets/`，分别写入 `mysql_password`、`mysql_root_password` 和 `admin_password`。数据库密码使用随机强密码；管理员初始密码由部署者写入文件。秘密目录加入 `.gitignore`，请勿提交。容器中的秘密文件需允许 UID 10001 读取，宿主机秘密目录权限设为 0700。

```sh
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail 50 web
```

Nginx 配置模板在 `nginx.conf`。先使用 HTTP 的 ACME 路径签发 `my.omini.cn` 证书，再启用模板中的 HTTPS 服务，并通过 `nginx -t` 检查后重载。部署主机使用 Certbot webroot `/var/www/omini-acme` 自动续期，续期后重载 Nginx。

网页公共资源使用内容哈希版本号与长期缓存，入口 HTML 使用 ETag / 304 校验后引用当前版本；私有 API 仍禁止缓存。保留应用返回的缓存头，避免在代理或 CDN 上把 `/api/` 纳入公共缓存规则。Nginx 模板为 HTML、CSS、JavaScript 开启 gzip。更新时重建 Web 容器；如同步更新了 Nginx 模板，还需在部署主机执行 `nginx -t` 后重载。仅推送 GitHub 源码不会更新已运行的站点。

管理员用户名为 `admin`，首次初始化读取 `admin_password`；数据库已存在该账号时，重启不会覆盖后来设置的密码。后台可以查看账号、停用/启用账号和重置密码。密码变更会撤销该账号全部会话。管理员自身密码可通过「修改管理员密码」更新。

备忘录每次保存追加完整快照；删除采用回收站标记。回滚创建新版本，旧历史继续保留。桌面未同步动作逐个上传，云端并发修改保留为独立冲突副本。图片最多 10 MB，支持 PNG/JPEG/GIF/WebP，每账号图片总额 200 MB；每篇正文最多 2 MB，每账号最多 2000 篇。

桌面未登录的旧 TXT 与账号缓存分开保存；如需上传旧笔记，登录后使用「导入本机备忘录」，会复制文本与关联图片，保留原文件。

手动数据库备份：在部署目录运行 `sh deploy/backup.sh`，使用事务一致性导出并以 gzip 保存到私有备份目录，包含全部图片和历史。请定期复制到服务器以外的备份存储。更新只需重建 Web 容器并保留卷；不要运行 `docker compose down -v`，该命令会移除持久数据卷。

本机开发仍可运行 `python web_server.py --open`，未配置 MySQL 时使用独立 SQLite 数据目录。桌面本机登录状态由 Windows DPAPI 加密，密码不落盘。邮箱是账号标识，本版本未接入邮件验证码。

账号工作区通过 `/api/sync` 和 `/api/sync/{preferences,schedule,assistants}` 同步，持久化到同一 MySQL 中；旧账号偏好与课表在首次访问时迁移。Codex 凭据只来自工具箱账号池中导入或登录的成员，版本与冲突备份同样加密。同步不会启动服务器上的账号池进程，也不会改变电脑上的全局 Codex 登录。

服务首次启动自动在持久卷生成 `/data/account-sync.key`（权限 0600），用于 Fernet 加密；可用 `OMINI_SYNC_KEY_FILE` 指向预先配置的密钥文件。更新容器须保留 `web_data` 与 `mysql_data`。`deploy/backup.sh` 同时保存数据库与对应的 `.account-sync.key` 文件；恢复时先恢复原密钥，再导入数据库。已有同步文档而密钥缺失时，服务会停止启动，保留原数据以便恢复。请将密钥备份与数据库备份一起存入私有备份存储。

更新后验证 `python -m unittest tests.test_account_sync tests.test_components`，并运行 `python -m tests.account_sync_check` 检查两个独立 Qt 客户端与 Chromium 的同账号同步、加密离线草稿、成员增删、冲突恢复、账号切换和重启。测试使用临时服务与虚拟凭据，截图及报告位于 `verification/account-sync/`。

## 1.2.2 备忘录存储加密

服务器首次启动生成持久卷 `/data/memo-storage.key`（权限 0600），Fernet 加密备忘录标题、正文、图片名称/内容、历史版本及旧工作区数据。密文包含归属/记录/版本上下文校验，错配数据会停止读取。版本、时间、记录 ID、图片 MIME/原始字节数仍为数据库索引或配额元数据。服务运行时可解密，不属于端到端加密。

升级前先停写并用旧版的备份脚本或 mysqldump 保存数据库及旧代码。新服务会先写入加密恢复副本 `.memo-migration-*.enc`，再在事务内更新正文、图片和历史；已有密文但缺失原密钥时停止启动。迁移后运行新版 `deploy/backup.sh`，同时保存 `.sql.gz`、`.account-sync.key` 与 `.memo-storage.key`。恢复数据库必须配套原密钥。旧明文备份、MySQL 旧日志及用户主动导出仍保持原格式，按私有备份保管。

桌面缓存首次打开会将旧库、TXT 和回收站迁入加密存储，并保留加密恢复副本。`.ominote` 文件名为带密钥摘要，不暴露笔记标题；Windows `.sqlite3.key` 由原用户 DPAPI 保护，备份需包含隐藏数据库、密钥和恢复副本。浏览器使用 AES-GCM 与不可导出的 CryptoKey 保存 IndexedDB 草稿；登录状态下的页面仍可访问明文。主动导出 TXT 为普通文本。
