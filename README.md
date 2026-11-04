# ncm2audio · NCM 解密工具 & 终端播放器

把网易云音乐客户端缓存的 `.ncm` 文件还原成原始音频（MP3 / FLAC / M4A / WAV / OGG），
并内置一个终端播放器：左侧专辑封面 + 歌曲信息、中间竖线分隔、右侧播放列表（左右等宽），
支持在支持的终端里用 **kitty / iTerm2 / Sixel 图形协议显示高清封面**，
不支持的终端自动降级为 24 位真彩半块字符画。

```
┌──────────────────────────────────────────────────────────────────┐
│ ♫ NCM 2 AUDIO  F:\Music                    ▶ 正在播放：月代彩 - Illumi │
│ 正在播放                                    │ 文件列表 (2)          │
│   ┌──────────────┐                         │ ▶ ♪ 月代彩 - Illumi   │
│   │              │                         │   ♫ 月代彩 - Illumi   │
│   │   专辑封面    │                         │      ...              │
│   │              │                         │                      │
│   └──────────────┘                         │                      │
│  Illumi                                     │                      │
│  月代彩 · AD:PIANO Ⅳ -Monochrome-           │                      │
│  MP3 · 320 kbps · 8.76 MB                   │                      │
│  纯音乐，请欣赏                              │                      │
│  ▶ 01:24 ━━━━●── 03:43                      │                      │
│  ▂▅▇▃▆▂▅▇▃▆▂▅▇▃▆                            │                      │
└──────────────────────────────────────────────────────────────────┘
```

## 功能特性

- **一键解密**：把 `.ncm` 文件或整个文件夹拖到 `拖入即解密.bat` 上即可；文件夹递归扫描，大小写不敏感。
- **纯标准库解密核心**：AES-128-ECB 与 NCM 密钥流全部用标准库实现，转换本身不依赖任何第三方包。
- **标签与封面回填**：解密后自动把标题 / 歌手 / 专辑 / 封面写入 MP3、FLAC、M4A、OGG（需要 `mutagen`，可用 `--no-tag` 关闭）。
- **终端播放器**：扫描当前目录的 NCM 与音频文件，Enter 播放（NCM 会先自动解密），空格暂停，方向键跳转。
- **高清封面**：自动识别终端图形协议（kitty、iTerm2、Sixel），无法识别时回退到真彩半块字符画（`▀`）。
- **歌词与频谱**：自动读取同名 `.lrc` 并随播放进度同步显示；播放时底部有律动频谱。
- **零配置文件**：所有快捷键都显示在界面底栏。

## 目录结构

```
ncm2audio/
├─ ncm2audio.py          # 入口：无参数启动播放器，带路径则批量解密
├─ ncm_core.py           # NCM 解析 / AES / 密钥流 / 转换 / 标签（纯标准库）
├─ ncm_tui.py            # 终端界面：布局、图形协议、播放后端、截图
├─ 拖入即解密.bat         # 把文件/文件夹拖到这里 → 批量解密
├─ 启动播放器.bat         # 双击打开播放器
├─ 安装依赖.bat           # 创建 venv 并安装 pillow / pygame-ce / mutagen
├─ requirements.txt
├─ venv/                 # 虚拟环境（由安装脚本创建）
└─ tests/                # 单元测试（30 项）
```

## 快速开始

1. 安装 Python 3.9+（安装时勾选 **Add python.exe to PATH**）。
2. 双击 `安装依赖.bat`：它会创建 `venv` 虚拟环境并安装依赖（默认走清华镜像）。
3. 使用：
   - **解密**：把 `.ncm` 文件或文件夹拖到 `拖入即解密.bat` 上；
   - **播放器**：双击 `启动播放器.bat`。

## 命令行用法

```bat
python ncm2audio.py                          :: 打开播放器，扫描当前目录
python ncm2audio.py 歌曲.ncm                  :: 解密单个文件（输出到同目录）
python ncm2audio.py D:\Music                  :: 解密整个文件夹（递归）
python ncm2audio.py D:\Music -o D:\Out        :: 输出到指定目录
python ncm2audio.py *.ncm --overwrite         :: 覆盖已存在的输出
python ncm2audio.py 歌曲.ncm --cover          :: 额外导出封面图片
python ncm2audio.py --dir D:\Music            :: 播放器扫描指定目录
python ncm2audio.py --self-test               :: 运行核心算法自测
```

| 选项 | 说明 |
| --- | --- |
| `-o, --out 目录` | 输出目录（默认与源文件同目录） |
| `--overwrite` | 覆盖已存在的输出文件（默认跳过） |
| `--cover` | 额外导出封面图片（`<音频名>.cover.jpg`） |
| `--no-tag` | 不写入歌曲信息与封面标签 |
| `--dir 目录` | 播放器扫描目录 |
| `--filter all\|ncm\|audio` | 播放器初始筛选 |
| `--cover-mode auto\|kitty\|iterm\|sixel\|blocks\|none` | 强制封面渲染方式 |
| `--volume 0.0~1.0` | 初始音量 |
| `--screenshot PNG --size 120x36 [--hd]` | 生成界面截图（调试/预览用） |

输出命名规则：`歌手 - 歌名.扩展名`（多歌手用 ` & ` 连接），扩展名根据解密后的音频头判断，
不轻信元数据里的 `format` 字段。目标文件已存在时默认跳过；同一次批量转换中撞名会自动加 ` (2)`。

## 播放器快捷键

| 按键 | 作用 |
| --- | --- |
| `↑` / `↓` | 选择列表项（`PgUp` / `PgDn` 翻页，`Home` / `End` 首尾） |
| `Enter` | 播放选中项（NCM 会先解密成音频） |
| `Space` | 播放 / 暂停（没有在播放时等价于播放选中项） |
| `←` / `→` | 后退 / 前进 5 秒 |
| `N` / `P` | 下一首 / 上一首 |
| `C` | 只转换选中项，不播放 |
| `A` | 转换列表里全部 NCM |
| `R` | 重新扫描目录 |
| `Y` | 显示 / 隐藏歌词 |
| `Tab` | 筛选：全部 → NCM → 音频 |
| `O` | 在资源管理器中打开所在目录 |
| `+` / `-` | 音量增减 5% |
| `Esc` / `Q` | 退出 |

> 小技巧：播放器运行时把文件拖进终端窗口，路径会被粘贴成输入，按 `Enter` 即可直接导入并解密。

## 终端封面协议

启动时按以下顺序自动判断，也可以用 `--cover-mode` 或环境变量 `NCM_COVER_MODE` 强制指定：

1. `KITTY_WINDOW_ID` / `TERM=xterm-kitty` / WezTerm / Ghostty → **kitty graphics protocol**
2. `TERM_PROGRAM=iTerm.app` → **iTerm2 内联图片**
3. `TERM` 含 `sixel`，或 Windows Terminal（`WT_SESSION`）且 DA1 查询报告支持 → **Sixel**
4. 其它终端 → **真彩半块字符画**（`▀`，24 位色，兼容性最好）

说明：

- Windows Terminal **1.22+** 原生支持 Sixel，高清封面可以直接显示；更老的版本会退化为字符画。
- WezTerm、kitty、iTerm2、Ghostty 可直接显示高清封面。
- 传统 `cmd.exe` 控制台（conhost）不支持图形协议，只能显示字符画，这是终端本身的限制。
- 不确定当前终端支持什么时，可以先跑一次截图或直接看效果；也可以手动试
  `python ncm2audio.py --cover-mode sixel` 等组合。

## 常见问题

**双击 bat 窗口一闪而过 / 提示找不到 Python？**
先去 <https://www.python.org/downloads/> 安装 Python，并勾选 “Add python.exe to PATH”，
然后双击 `安装依赖.bat` 重建环境。

**没有声音？**
播放后端优先使用 `pygame`（pygame-ce）。若没有安装或声卡不可用，会自动尝试 `ffplay`
（需要 ffmpeg 在 PATH 中）；都不行时仍可正常解密转换。执行 `--self-test` 与
`venv\Scripts\python.exe -c "import pygame;pygame.mixer.init();print('ok')"` 可自查。

**中文显示成方块/乱码？**
bat 里已执行 `chcp 65001`；若仍异常，请使用 Windows Terminal 或把终端字体换成微软雅黑/等宽字体。

**封面是马赛克？**
说明当前终端不支持图形协议，正在使用半块字符画。升级 Windows Terminal 到 1.22+，
或改用 WezTerm / kitty / iTerm2 即可显示高清封面。

**创建 venv 报 `WinError 5 拒绝访问`？**
某些安全软件会拦截虚拟环境创建，重试一次，或以管理员身份运行 `安装依赖.bat`。
脚本用 `--without-pip` 方式创建 venv 再安装 pip，可绕过部分 ensurepip 限制。

## 技术说明

NCM 容器结构（依据开源项目 ncmdump 系列的逆向结论）：

| 字段 | 长度 | 说明 |
| --- | --- | --- |
| 魔数 | 8 B | `CTENFDAM` |
| 占位 | 2 B | 跳过 |
| 密钥长度 | 4 B | 小端 |
| 密钥块 | n B | XOR `0x64` → AES-128-ECB(`hzHRAmso5kInbaxW`) → 去 PKCS#7 填充 → 去掉 17 字节 `neteasecloudmusic` |
| 元数据长度 | 4 B | 为 0 表示没有元数据 |
| 元数据块 | m B | XOR `0x63` → 去掉 22 字节 `163 key(Don't modify):` → Base64 → AES-128-ECB(`#14ljk_!\]&0U<'(`) → 去掉 6 字节 `music:` → JSON |
| CRC32 + 版本 | 5 B | 跳过 |
| 封面帧长度 / 封面长度 | 8 B | 两个小端 u32 |
| 封面数据 | k B | 原始 JPEG / PNG（不加密） |
| 封面填充 | `frame_len - k` | 必须跳过，否则音频密钥流会错位 |
| 音频数据 | 剩余 | 与 256 字节循环密钥流逐字节 XOR |

音频“加密”是 RC4 的 KSA 生成 256 字节置换后，按固定公式展开成 **256 字节循环密钥流**：

```text
对音频内偏移 p：
    j  = (p + 1) mod 256
    ks = S[(S[j] + S[(S[j] + j) mod 256]) mod 256]
    明文[p] = 密文[p] XOR ks
```

因此无需流式状态，可以任意分块并行处理；本工具用整型异或一次性处理 64KB 块。

## 测试

```bat
venv\Scripts\python.exe -m unittest discover -s tests -v
venv\Scripts\python.exe ncm2audio.py --self-test
```

测试覆盖：AES FIPS-197 / NIST 向量、PKCS#7、密钥流往返、容器解析（含封面帧填充）、
损坏文件、文件名清理、批量撞名、标签写入、Sixel 编码解码往返、界面渲染与截图。

## 致谢

NCM 格式的逆向结论来自开源社区的共同努力，本实现参考了：

- [anonymous5l/ncmdump](https://github.com/anonymous5l/ncmdump)（C++ 参考实现）
- [taurusxin/ncmdump](https://github.com/taurusxin/ncmdump)（C++ 实现）
- [nondanee/ncmdump](https://github.com/nondanee/ncmdump)
- [Johnserf-Seed/ncm2mp3](https://github.com/Johnserf-Seed/ncm2mp3)（格式文档）

## 免责声明

本工具仅用于把**自己账号已购买/已缓存**的音乐还原成通用音频格式，便于在其它设备上离线播放。
请勿用于传播或商业用途；由此产生的任何法律风险由使用者自行承担。
