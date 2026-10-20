# 安装包体积分析：1.1.6

1.1.6 按当前需求移除了内置 Chromium / Qt WebEngine 和随包 FFmpeg，TXT 笔记使用原生控件。未使用的虚拟键盘与 PDF 图片插件、Qt Quick / QML 原生依赖也一并排除。

| 产物 | 1.1.5 | 1.1.6 | 减少 |
| --- | ---: | ---: | ---: |
| 安装包 | 223.59 MiB | 133.56 MiB | 40.27% |
| 完整程序目录 | 603.57 MiB | 311.56 MiB | 48.38% |

新版安装包为 140,043,867 字节。覆盖升级会清理安装目录内旧版附带的浏览器与 FFmpeg 文件；TXT、旧版备忘录缓存和个人设置位于用户目录并保留。FFmpeg 由系统 PATH 或设置页选定的本机程序提供。

## 1.1.6 剩余主要组件

| 模块 | 未压缩大小 MiB |
| --- | ---: |
| Codex 成员账号池 CLIProxyAPI | 69.80 |
| Spotify 下载引擎 spotdl | 58.47 |
| 文件同步 Syncthing | 26.18 |
| 软件 OpenGL 兼容库 | 19.68 |
| 图库下载引擎 gallery-dl | 19.41 |
| 视频下载引擎 yt-dlp | 17.01 |

上述程序继续提供原有下载、同步与账号池功能。spotdl 单文件已经内部压缩，后续若继续减小安装包，共享 Python 下载引擎运行时仍是主要方向。

验证数据：`verification/compact-release-1.1.6.json`、`verification/package-size-1.1.6.json`。包含实际 EXE 的 TXT 持久化、系统浏览器路由、账号池、Omini API、本机 FFmpeg 五种音频转换与真实 HLS 下载验证。

以下保留 1.1.5 的历史基线，描述该版本当时的模块组成与后续优化机会。

# 1.1.5 基线分析

当前完整目录为 603.57 MiB，安装包为 223.59 MiB。

## 大模块

下面的独立压缩测试使用 x86 BCJ + LZMA2 preset 9。安装器采用跨文件固实压缩，因此独立测试用于比较模块的可压缩性，各项合计与安装包大小有差异。

| 模块 | 文件大小 MiB | 独立压缩后 MiB | 作用 |
| --- | ---: | ---: | --- |
| Spotify 下载引擎 spotdl | 58.47 | 57.55 | Spotify 链接解析、音源匹配和下载 |
| Qt 内置浏览器核心 | 147.28 | 43.99 | 课表登录、班级课表、iCloud 等内置网页 |
| FFmpeg | 97.18 | 25.14 | 视频合并、音频转换和 M3U8 处理 |
| 图库引擎 gallery-dl | 19.41 | 18.98 | 图片与图库下载 |
| 视频引擎 yt-dlp | 17.01 | 16.66 | 视频、音频、字幕和 M3U8 下载 |
| Codex 账号池 CLIProxyAPI | 69.80 | 15.38 | 多成员账号、本地 API 和调度 |
| 文件同步 Syncthing | 26.18 | 8.74 | 多电脑文件夹同步 |
| 软件 OpenGL 兼容库 | 19.68 | 4.76 | 软件绘图、部分显卡及远程桌面环境 |

工具箱主 EXE 为 4.15 MiB。Qt 浏览器另有约 22.45 MiB 的 Chromium 资源；其余 Qt/Python 库由多个页面共用。

## 已核实的重复依赖

读取现有 PyInstaller EXE 的归档并逐文件比较 SHA-256：

- spotdl 内有 84 个二进制/资源文件，与主程序 `_internal` 中的文件完全相同；它们在 spotdl EXE 中占 11.36 MiB 压缩数据。
- gallery-dl 内有 82 个文件同样已经存在于主程序中，占 11.32 MiB 压缩数据。
- spotdl 与 gallery-dl 还包含 732 个字节完全一致的 Python 模块，gallery-dl 中这些模块的压缩数据共 5.98 MiB。

主要重复项包括 Python 3.11、cryptography、OpenSSL、SQLite、Unicode 数据及标准库。固实压缩会消化一部分重复，以上归档数据量与最终安装包的节省量有差异；共享运行时后的收益应以新包实测确定。

## 优先优化的部分

1. **共享下载引擎运行时。** 把 Spotify、图库等 Python 引擎的公共依赖合并到同一运行时中，保持任务在独立进程执行，减少单文件 EXE 内重复打包与重复压缩。
2. **精简 Spotify 引擎的可选依赖。** 当前归档带有 NumPy/OpenBLAS、Pillow 的 AVIF 支持、Redis、MongoDB、Web 服务等依赖。其中 OpenBLAS 本身占 6.13 MiB 压缩数据，Pillow AVIF 扩展占 4.12 MiB。工具箱只调用 Spotify 命令行下载，适合进一步核对可选路径后做裁剪并验证。FastAPI/Web 服务在现有入口中立即导入，清理时需要同步调整入口。
3. **提供按需下载引擎的轻量包。** 可将 FFmpeg、Spotify、图库、同步或账号池引擎移到首次使用时下载；会增加首次使用的联网步骤。完整离线包继续保留这些引擎。

Qt 浏览器核心虽然文件较大，独立压缩后约为 44 MiB。继续减少核心文件需要调整内置浏览器的实现；当前优先处理重复的 Python 运行时和可选依赖。

## 本地测量数据

- `verification/component-compression-1.1.5.json`：各组件独立压缩结果。
- `verification/engine-duplicates-1.1.5.json`：归档中重复文件与 Python 模块统计。

这些测量文件使用本机构建产物生成，位于版本控制排除的验证目录中。
