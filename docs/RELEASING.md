# 版本管理与发布

`main` 保存开发源码，正式发布使用 `v主版本.次版本.修订版本` 标签。旧标签和已发布附件保持不变，每次发布递增版本；当前系列的小幅功能接入和修正递增末段，大规模功能迭代递增次版本，兼容性变化递增主版本。

程序版本唯一来源为根目录 `app_version.py`。安装脚本和目录打包脚本读取同一版本，防止程序版本、安装包名称与 Git 标签不一致。每次发布同步更新 README、CHANGELOG、安装说明及 `releases/v版本.md`。

## 检查并构建

在 Windows Python 3.11 环境准备 `requirements.txt` 与 `tools/` 的下载及 AI 账号池引擎后执行：

```powershell
$version = py -3.11 app_version.py
py -3.11 -m unittest tests.test_queue tests.test_icloud_notes tests.test_updater -v
py -3.11 -m unittest tests.test_ai_assistants tests.test_codex_quota tests.test_pool_scheduling tests.test_text_notes tests.test_local_ffmpeg tests.test_timetable tests.test_class_timetable -v
py -3.11 -m unittest tests.test_accounts tests.test_desktop_settings tests.test_web_server -v
node tests/test_web_frontend.cjs
py -3.11 -m unittest tests.test_account_sync tests.test_components -v
py -3.11 -m tests.account_sync_check
./scripts/build_release.ps1
& "./artifacts/releases/$version/Omini工具箱/Omini工具箱.exe" --self-test-ai-assistants
& "./artifacts/releases/$version/Omini工具箱/Omini工具箱.exe" --self-test-settings
py -3.11 scripts/verify_compact_release.py "artifacts/releases/$version/Omini工具箱" --report artifacts/verification/compact-release.json
./scripts/build_installer.ps1
```

安装包生成到 `artifacts/installers/版本/`，附带 SHA-256 文件。可用 `scripts/verify_installer.py` 在临时目录验证安装、启动、覆盖升级和卸载后的数据保留。已有正式安装时使用 `py -3.11 scripts/verify_installer.py "artifacts/installers/版本/Omini工具箱-版本-Setup.exe" --isolated-registration`：同一安装包通过 `/OMINIVERIFY=1` 使用独立测试 AppId，测试结束核对原安装注册项保持一致。默认模式在已有安装时停止执行。

1.1.8 的 AI 助手检查包含：停服显示本地成员、额度与操作记录重启恢复、恢复上次运行的账号池，以及手动停止后保持停止。安装器检查还会逐字节验证升级和卸载保留成员凭据、加密配置、状态与操作记录。所有检查使用隔离数据目录。

完整构建只使用 PySide6-Essentials 的原生 Widgets，显式排除 Qt WebEngine、QML 及 Chromium 资源。FFmpeg 由本机安装提供，所有打包入口共用 `release_packaging.copy_release_tools`，排除 FFmpeg、ffprobe、ffplay 及其随包元数据。媒体验证脚本支持 `--ffmpeg` 指定本机现有程序，运行环境也可通过 PATH 提供。安装器使用 `lzma2/ultra64`，压缩耗时比旧快速配置更长，分发文件更小。

1.1.9 构建包含 `web/` 静态资源和 `web_server` 入口。构建后验证 EXE 的 `--web` 模式：在隔离 `--data-dir` 下注册邮箱、登录、退出、重启恢复会话，以及功能开关。个人账号数据库和桌面加密会话仅保存在 AppData，发布包只分发代码及静态资源。

## 提交与发布

先检查 `git diff`，只提交源码、文档、资源和测试。个人账号、凭据、HAR、运行日志与打包目录由 `.gitignore` 排除。首次从新电脑获取源码：

```powershell
git clone https://github.com/csy5522522/OMINI-Tool.git
```

正式发布示例：

```powershell
git add -A
git commit -m "Release 1.2.6: keep memo editing active and remember responsive AI layout"
git tag -a v1.2.6 -m "Omini工具箱 1.2.6"
git push --atomic origin main v1.2.6
py -3.11 scripts/publish_release.py --publish
```

发布脚本从 Git Credential Manager 读取本机已有的 GitHub 凭据，仅在内存中用于请求。它核对工作区、源码提交、远端标签、安装包校验和，再创建草稿、上传安装包与校验文件，最后发布为最新正式版本。已有公开版本与内容不同的同名附件会保留，修复请使用新版本号。

用户可在 GitHub Release 下载已发布的指定版本；1.1.2 及后续桌面版也可通过「设置 → 软件更新」升级。回看源码使用 `git show v1.2.4`，复现旧构建请在独立目录检出对应标签。

1.2.0 发布前增加 `tests.test_memos` 及桌面账号切换验收，验证图片归属、离线动作逐个同步、冲突副本与回滚历史。生产镜像与 MySQL 使用独立 Docker Compose 项目，确认 `https://my.omini.cn` 的 HTTPS、登录和备忘录同步后再推送版本。部署说明和秘密文件约定见 `deploy/README.md`；数据库备份、会话、SSH 凭据和验收账号数据均排除出仓库。

1.2.2 发布增加 `tests.test_memo_encryption`、`tests.test_dpapi_memory`、浏览器加密草稿及双向周次同步验收；安装包检查使用 `installed_desktop_smoke`，并确认退役模块不在 PYZ 中。部署前保存私有旧数据库备份；迁移后备份数据库与两份加密密钥。旧明文备份不自动改写。

1.2.3 发布增加 `tests.test_memo_import` 和凭据过期后保留工作区、重新登录续传的桌面验收。冻结程序验证本机笔记首次关联账号时自动上传；安装器验证覆盖升级与卸载保留加密登录身份及备忘录密钥。生产验收检查桌面记住登录 30 天、有效旧凭据续期，以及过期、撤销和停用仍生效。

1.2.4 发布增加 `tests.test_api_members` 和 `node tests/test_api_members_frontend.cjs`。完整窗口自测覆盖 Codex 登录成员与 API Key 线路混合管理、真实引擎模型注册、配置应用和重启恢复；同步测试验证线路凭据原子合并和删除冲突。线路测试仅使用本地模拟上游，不读取个人 Key。自建同步服务须同步更新，以接收新增 API 线路字段。

1.2.5 安装包验证覆盖精简后的固定桌面导航、独立登录和注册表单、设置保存、账号和备忘录同步，以及旧版 Syncthing 清理。覆盖升级与卸载逐字节核对个人配置、备忘录密钥、登录凭据、成员配置和操作记录保留；测试使用独立注册项及临时安装目录。

1.2.6 增加慢速同步期间输入、中文候选、光标和撤销记录保留，以及云端下载与新本地动作的并发验收。安装器使用 `artifacts/releases/版本/`，结果放入 `artifacts/installers/版本/`；验收报告放入 `artifacts/verification/`。本次只提交源码与文档，不执行发布脚本上传安装包。
