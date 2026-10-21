# Docker / MySQL 部署

正式服务为 https://my.omini.cn，管理入口为 https://my.omini.cn/admin。桌面设置默认连接此地址，也支持「连接官方云端」切回。

服务包括独立 Web 容器和 MySQL 8.4 容器。MySQL 无宿主机映射端口；Web 只映射 `127.0.0.1:18765`，由宿主机 Nginx 提供 HTTPS。`mysql_data` 保存账号、加盐密码哈希、会话、备忘录、图片、版本及个人偏好；`web_data` 保存下载任务记录，`downloads` 保存下载文件。备忘录、图片、下载记录、课表和功能选择均按账号隔离。

首次部署，在项目内创建 `deploy/secrets/`，分别写入 `mysql_password`、`mysql_root_password` 和 `admin_password`。数据库密码使用随机强密码；管理员初始密码由部署者写入文件。秘密目录加入 `.gitignore`，请勿提交。容器中的秘密文件需允许 UID 10001 读取，宿主机秘密目录权限设为 0700。

```sh
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail 50 web
```

Nginx 配置模板在 `nginx.conf`。先使用 HTTP 的 ACME 路径签发 `my.omini.cn` 证书，再启用模板中的 HTTPS 服务，并通过 `nginx -t` 检查后重载。部署主机使用 Certbot webroot `/var/www/omini-acme` 自动续期，续期后重载 Nginx。

管理员用户名为 `admin`，首次初始化读取 `admin_password`；数据库已存在该账号时，重启不会覆盖后来设置的密码。后台可以查看账号、停用/启用账号和重置密码。密码变更会撤销该账号全部会话。管理员自身密码可通过「修改管理员密码」更新。

备忘录每次保存追加完整快照；删除采用回收站标记。回滚创建新版本，旧历史继续保留。桌面未同步动作逐个上传，云端并发修改保留为独立冲突副本。图片最多 10 MB，支持 PNG/JPEG/GIF/WebP，每账号图片总额 200 MB；每篇正文最多 2 MB，每账号最多 2000 篇。

桌面未登录的旧 TXT 与账号缓存分开保存；如需上传旧笔记，登录后使用「导入本机备忘录」，会复制文本与关联图片，保留原文件。

手动数据库备份：在部署目录运行 `sh deploy/backup.sh`，使用事务一致性导出并以 gzip 保存到私有备份目录，包含全部图片和历史。请定期复制到服务器以外的备份存储。更新只需重建 Web 容器并保留卷；不要运行 `docker compose down -v`，该命令会移除持久数据卷。

本机开发仍可运行 `python web_server.py --open`，未配置 MySQL 时使用独立 SQLite 数据目录。桌面本机登录状态由 Windows DPAPI 加密，密码不落盘。邮箱是账号标识，本版本未接入邮件验证码。
