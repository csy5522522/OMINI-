# Web 工作台开发与发布交付

2026-10-29 已按用户后续指令发布到 `8.134.171.94`，运行镜像为 `omini-toolbox:1.2.7-workspace-v3-20261029`，两个域名均启用 Nginx 无缓冲流式转发。本次按要求未生成新备份，更新完成后清理临时上传与构建文件。桌面应用版本号未提升。

## 已实现

- 所有 Web 页面与后台共用主题变量：浅色沿用原配色，深色为墨绿与金色。设置中可选 100% / 110% / 125% 缩放，电脑默认 125%、手机默认 100%；本机偏好和同源后台共享。
- 聊天使用正文排版，支持真实流式输出、停止生成、历史列表、新建、重命名、删除、继续对话。文字与附件元数据加密保存到账号数据库，附件原文件保留于当前页面内存；刷新后提示重新上传。每账号 100 个会话、50 MiB，单次 80 条消息；不会自动删除旧历史。
- 三协议适配器、异步运行器、加密会话存储和空工具注册表分离。工具尚未开放，模型请求不携带工具定义。架构参考 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 和 [Pydantic AI](https://github.com/pydantic/pydantic-ai)，未复制其源码。
- 独立的课表 AI 线路配置，支持三协议、模型列表、默认线路、启停、图片/PDF 能力。AI 识别图片/PDF及网格 XLSX，规则表格直接解析；均先进入可编辑预览。缺失周次/节次需要补充，重叠课程提示但保留；确认导入时继续使用账号版本冲突机制。
- 热搜大全展示抖音、百度、微博及 B 站的站内热搜，Python 后端共享缓存、独立失败降级。缓存 5 分钟，强制刷新最短 60 秒，抓取最多两并发；当前页面 60 秒读缓存，后台标签页暂停。公共快照最多 256 KiB，只保留最新数据，过期一天不继续展示。

## 接口与数据

| 接口 | 用途 |
|---|---|
| `POST /api/chat/stream` | 登录用户发起 SSE，包含 `model/messages/conversation_id/version/request_id` |
| `POST /api/chat/runs/{id}/cancel` | 取消本人运行并关闭上游连接 |
| `GET /api/chat/conversations` | 当前账号的会话摘要、已用字节和额度 |
| `GET /api/chat/conversations/{id}` | 读取本人会话正文与附件元数据 |
| `POST /api/chat/conversations/{id}` | `{action:"rename",title,version}` 或 `{action:"delete",version}` |
| `GET /api/timetable/ai/routes` | 启用的识别线路，`models` 为兼容别名 |
| `POST /api/timetable/ai/inspect` | 附件检查及 Excel 工作表信息 |
| `POST /api/timetable/ai/recognize` | 生成预览；不写正式课表 |
| `POST /api/timetable/ai/validate` | 复核修改后的课程，返回 issues、warnings、ready、schedule |
| `GET/POST /api/admin/timetable-ai/providers` | 管理员独立识别线路列表及增删改 |
| `POST /api/admin/timetable-ai/models` | 获取上游模型 ID |
| `GET /api/trends?refresh=1` | 游客可读的公共缓存，refresh 可选 |

SSE 发送 `start/delta/done/error` 四类事件及 10 秒注释心跳。HTTP 校验在开始流之前完成；版本冲突 409、并发超限 429。同 request_id 的重复请求重放已保存结果，不重新付费；主动重新生成使用新 request_id。服务端以已存会话作为唯一历史来源，前端历史只用于按 SHA-256 校验并补回内存里的原附件。

每账号一条生成，全局两条生成，总时间 300 秒、上游空闲 60 秒。中断保留部分回复；生成文本每秒检查点写入，异常重启恢复为取消状态。日志、请求统计和数据库不保存附件原始数据。历史使用原 `account-sync.key` 加密，备份数据库时必须保留该密钥。

新增表见 `deploy/migrations/20261029_workspace_v3.sql`。启动时自动幂等建表，无旧表破坏性迁移。删除账号清理聊天会话和去重记录；课表 AI 线路属于全局管理员配置。

## 验证

```powershell
python -m unittest tests.test_workspace_features tests.test_web_chat tests.test_chat_attachments tests.test_web_assets tests.test_accounts tests.test_announcements tests.test_web_server tests.test_timetable_table -q
python -m tests.web_workspace_check
python -m tests.mysql_workspace_check
node tests/test_web_frontend.cjs
node tests/test_web_guest.cjs
node tests/test_memo_frontend.cjs
node tests/test_sync_races.cjs
```

MySQL 检查创建临时、仅绑定回环地址的独立实例，使用本机 mysqld 程序，完成后关闭并清理临时目录；不会连接生产数据库。浏览器验证截图和 MySQL 报告位于 `artifacts/verification/workspace-v3/`。自动测试使用本地模拟模型及固定热搜数据，不调用付费模型；四平台另做只读联网检查。正式模型的识别准确率仍依赖上游视觉能力，预览核对属于导入步骤。

## 后续发布与回滚

1. 先解决生产磁盘余量，再做发布前数据库和两份加密密钥备份，保留原镜像、源码、Compose 和 Nginx 配置。
2. 新镜像安装 `httpx[socks]`，沿用持久卷及账号数据库。不要直接套用历史 `.cache` 发布脚本：旧脚本未包含本轮新增模块及资源。
3. 在隔离候选实例验证增量表、三协议 SSE、课表预览与历史隔离；预览及模型调用使用测试账号和本地模拟上游。
4. 两个域名均应用本仓库 Nginx 中 `/api/chat/stream` 的独立 location，关闭缓冲、缓存和 gzip，读取超时 310 秒；先 `nginx -t` 再重载。
5. 切换后验证真实流式首片段、停止、文字历史、账号同步、四平台缓存和数据库/容器健康；失败恢复原镜像、源码、Compose 和 Nginx。新增表可保留以便后续恢复，不必为代码回滚恢复整库。

公开热搜接口可能改字段或返回 403，页面按平台保留旧结果并显示真实时间，不影响其他平台。当前缓存抓取基于页面请求触发；页面保持打开时自动刷新，全部页面关闭后不持续轮询源站。
