# assglass：ASS 字幕背景高斯模糊与压制

`assglass` 根据 ASS 字幕的实际渲染位置，模糊标记字幕后方的视频，再将完整字幕烧入视频。背景处理和字幕烧录在同一次视频编码中完成，字幕字形本身保持清晰。

实现以 [`PLAN/ass_bgblur_technical_design_v1.10.md`](PLAN/ass_bgblur_technical_design_v1.10.md) 为基础：完整 libass track 分析、圆角矩形背景、原生 YUV420 权重和流式 FFmpeg 合成，并已加入 EventImages 逐事件导出与独立字幕组。**标记使用 Actor 字段（ASS 文件中的 `Name`），按可配置前缀匹配，默认 `bgblur`**；不使用 Effect 精确匹配。

## 使用前准备

需要 Python 3.8+、C++14 编译器、`pkg-config`、libass 开发文件，以及包含 `ass`、`gblur`、`maskedmerge` 滤镜和 `libx264` 编码器的 FFmpeg/ffprobe。Python helper 与 FFmpeg 需要使用同一共享 libass 和同一套字体。纯 Python 依赖不能代替这些系统依赖。

Ubuntu / Debian 的系统依赖：

```bash
sudo apt update
sudo apt install python3 python3-venv python3-dev build-essential pkg-config libass-dev ffmpeg \
  curl xz-utils patch libfreetype6-dev libfribidi-dev libharfbuzz-dev libfontconfig1-dev libx264-dev
```

macOS 的系统依赖：

```bash
xcode-select --install
brew install python pkg-config libass ffmpeg freetype fribidi harfbuzz x264
```

在项目目录安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
# 下载经 SHA-256 校验的固定版本源码；只安装到本项目 .tools
sh scripts/build_libass.sh
sh scripts/build_ffmpeg.sh
python -m pip install -e '.[test]'
assglass --help
```

安装会编译 C++ 原生核心；开发时也可用 `python native/build.py` 手动重建。若提示找不到 libass，先检查 `pkg-config --modversion libass`；若有多套 FFmpeg/libass，请先统一运行环境，再运行测试。更换 FFmpeg、libass 或字体后，应重新做集成验证。当前可检查动态依赖的 Linux/macOS 构建是运行范围；静态内置 libass 的下载版 FFmpeg 不能通过共享库一致性检查。

独立背景框需要项目扩展的 **libass 0.16.0 / EventImages ABI 1**。构建脚本应用仓库内的只读导出补丁，安装到 `.tools/libass-event-images`；FFmpeg 与原生核心随后链接同一个私有库。补丁在字幕碰撞调整完成后读取每个事件的图像，保留标准渲染输出，不修改系统 libass。若先安装了 Python 包、后来才构建扩展，请重新运行 `python -m pip install -e '.[test]'`。

默认 `backend=auto` 优先使用可用的 EventImages。没有扩展时可显式选择旧的 `--backend alpha --grouping merged`；请求独立分组时会提示安装扩展，绝不会自动把独立框退化为共同大框。

程序启动会测试 FFmpeg `maskedmerge` 的真实数值行为。本机原有 FFmpeg 5.0.1 在权重 255 时未精确输出模糊分支，会被拒绝，不能仅凭“装有 FFmpeg”跳过检查。项目可使用私有 FFmpeg 6.1.1，避免修改系统安装：

```bash
# Ubuntu / Debian 编译私有 FFmpeg 还需要这些工具
sudo apt install curl xz-utils libx264-dev

# macOS 用户安装 x264：brew install x264
# 下载有 SHA-256 校验的官方源码，编译到本项目 .tools/ffmpeg-6.1.1
sh scripts/build_ffmpeg.sh
```

程序优先使用项目 `.tools/ffmpeg-6.1.1` 中的可执行文件；目录不存在时使用 PATH 中的工具。`--ffmpeg` / `--ffprobe` 始终可以显式指定。私有构建只保留本项目需要的功能，输入容器限 MP4 / MOV；要使用完整发行版请通过这两个参数选择，并通过运行时检查。`.tools` 不纳入 Git，迁移机器后需要重新编译，不要跨系统复制二进制。

## 标记字幕

在 Aegisub 中打开 Actor / 演员列，将需要背景模糊的 Dialogue 行写为 `bgblur`。标准 ASS 记录中，它是 Style 后面的 `Name` 字段：

```ass
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,bgblur,0,0,0,,这行字幕有模糊背景
Dialogue: 0,0:00:03.00,0:00:05.00,Default,旁白,0,0,0,,这行正常显示，但不添加背景模糊
```

前缀匹配区分大小写：`bgblur`、`bgblur_角色A`、`bgblurred` 都是标记；`notbgblur` 和 `BGBlur` 不是。`Comment` 行不是目标。Effect 原有内容保留，正文 Text 中的逗号也保留。

Actor 开头的空格不会自动去除，因此 ` bgblur` 不匹配。参数块必须紧跟配置的前缀；例如 `bgblur_角色A{strength=0.5}` 仍仅表示一个带后缀的标记名，使用默认参数。需要逐行参数时使用下面的标准写法。

可以在 Actor 标记后设置逐行外观参数：

```text
bgblur{feather=8;strength=0.65;padding_x=40;padding_y=24;radius=20}
bgblur;feather=8;strength=0.65
bgblur{threshold=0.5;padding_x=28;padding_y=16}
```

参数之间使用**分号**。Actor 不是带引号的 CSV 字段，逗号会打乱 ASS 字段，不能写 `bgblur{feather=8,strength=0.65}`。

如果需要保留原 Actor 内容，也可以用 sidecar 选择行：

```bash
# 列出从 0 开始的 Dialogue 序号，Comment 不计入序号
python scripts/make_sidecar.py subtitle.ass --list

# 按上一步看到的序号选择事件，不改原 ASS
python scripts/make_sidecar.py subtitle.ass --index 0 --index 2 -o selected.json
assglass input.mp4 subtitle.ass -o output.mp4 --sidecar selected.json
```

sidecar 绑定完整 ASS 的 SHA-256；编辑、重排或重新保存 ASS 后，需要重新生成 sidecar。Actor 与 sidecar 同时选择某行只计算一次，显式参数冲突会报错。

## 独立背景框与多层字幕分组

默认 `grouping=per-event`：每条标记 Dialogue 都有独立背景框。同时出现的两句话分别求范围、添加外扩和羽化，最后按像素最大值合并遮罩；中间的空白不会被一个共同矩形填满，重叠处也不会反复叠加模糊。

同一句字幕由正文、白描边、黑描边等多行构成时，在这些行的 Actor 中使用相同的 `group`：

```text
bgblur{group=comment}   # “说不定这段就被剪掉了”
bgblur{group=why}       # “为啥——！”正文层
bgblur{group=why}       # “为啥——！”白描边层
bgblur{group=why}       # “为啥——！”黑描边层
```

填写 Actor 时只填写左侧标记，不包含示例注释。组名区分大小写，可含中英文、数字、下划线、点和连字符，长度1～128。未写 `group` 的每一行独立；`bgblur_why` 仍只是前缀匹配，不会隐式创建组。`group` 也可放进 sidecar 每条事件的 `overrides`，但不能作为全局 mask 默认值。

同组的当前活动事件先合并字形与描边图像，再求一个框，因此多层样式不会丢失外沿。不同组可同时使用不同的 padding、feather、strength、threshold 等参数；同组同时活动的行必须具有相同的有效参数，冲突会在编码前指出。时间不重叠的行可以复用组名并采用不同参数。

```bash
# 默认独立框；必要时显式选择后端和分组
assglass input.mp4 subtitle.ass -o output.mp4 --backend event-images --grouping per-event

# 切换旧项目的共同大框配置为独立框
assglass input.mp4 subtitle.ass -o output.mp4 --config project.yaml --grouping per-event

# 仅在确实需要整组一个大矩形时使用旧模式
assglass input.mp4 subtitle.ass -o output.mp4 --allow-merged-box
```

`--allow-merged-box` 是旧模式开关，会选择 `grouping=merged`；所有同时出现的标记必须参数相同。含显式 `group` 标记的字幕不能进入 merged 模式，避免静默丢失分组含义。

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `padding_x` | 28 | 阈值筛选后的字形范围左右各扩张多少像素 |
| `padding_y` | 16 | 阈值筛选后的字形范围上下各扩张多少像素 |
| `radius` / `corner_radius` | 24 | 矩形圆角半径 |
| `feather` / `feather_sigma` | 12 | 背景权重边缘的高斯 sigma |
| `strength` | 1.0 | 0～1；0 关闭该背景，1 使用完整模糊权重 |
| `threshold` / `opacity_threshold` | 0.5 | 仅有效不透明度严格大于此值的像素参与字形范围计算 |
| `include` | `character+outline` | 用字形与描边计算范围，可添加 `shadow` |

外扩、圆角和 sigma 使用 1080p 输出像素单位；`strength` 和 `threshold` 是 0～1 的比例，不随分辨率缩放。`feather` 控制背景边缘过渡，`strength` 控制原图与模糊图的混合比例。视频高斯模糊的 `blur_sigma` 是任务级参数，默认 40；不能放在逐行标记中。未写出的参数继承当前任务默认值，每行独立解析，不继承上一行。

`threshold` 用于排除 ASS `\blur` 等效果产生的低透明度外沿。程序先从 libass 渲染结果计算每个像素的有效不透明度 `coverage / 255 × (255 − ASS alpha) / 255`，只用严格大于阈值的像素求包围框，再添加 padding、圆角和柔和羽化。字形、描边等选中图像之间仍取最大值，不累加重叠图层的透明度。阈值只影响背景范围，不改写字幕外观，也不截断背景最终的羽化。

默认阈值为 0.5，同时将左右／上下外扩恢复到 28／16 像素，为实际字形留出余量。`threshold=0` 恢复旧的所有非零像素范围；`threshold=1` 不会选中任何像素。如果某帧所有目标像素的不透明度都不超过阈值（例如淡入淡出的低透明阶段），该帧不生成背景框；需要保留这些阶段时可降低阈值。

## 压制视频

```bash
# 默认：背景处理 + 完整原 ASS 烧录
assglass input.mp4 subtitle.ass -o output.mp4

# 只处理背景，不新增硬字幕
assglass input.mp4 subtitle.ass -o background.mp4 --no-burn-subtitles

# 自定义 Actor 前缀
assglass input.mp4 subtitle.ass -o output.mp4 --marker-prefix glass

# 调整视频模糊强度和默认背景外观
assglass input.mp4 subtitle.ass -o output.mp4 \
  --blur-sigma 20 --default feather=8 --default strength=0.75

# 全局调整识别字形范围的阈值；也可在 Actor 中逐行设置 threshold
assglass input.mp4 subtitle.ass -o output.mp4 --default threshold=0.5

# 较快的编码设置；默认 preset 是 veryslow
assglass input.mp4 subtitle.ass -o output.mp4 --preset fast --crf 20

# 显式加载同一字体目录
assglass input.mp4 subtitle.ass -o output.mp4 --fonts-dir ./fonts

# 仅做启动检查，不压制视频
assglass input.mp4 subtitle.ass -o output.mp4 --check-only
```

也可将 `assglass` 替换为 `python -m assglass`。输出容器支持 MP4 / MOV，以保留已支持 CFR 帧率的精确时间戳。已有输出默认不会覆盖；确认需要替换时添加 `--overwrite`。输出先写临时文件，完整性检查通过后才发布到目标路径。

无标记行时，默认仍烧录全部原字幕。`--no-burn-subtitles` 下不新增字幕，但已有硬字幕仍在原视频中；它不会自动封装 ASS 软字幕轨。关闭烧录后再用其他工具烧字幕，会再次编码；希望只编码一次时使用默认模式。

默认编码器为 `libx264`，使用 CRF 18、`veryslow`、level 4.2、closed GOP、AQ mode 3 / strength 0.8、deblock `-1:-1`、me_range 32、subq 10，以及 `b:v=10M`、`maxrate=20M`、`bufsize=10M`。这是 CRF + VBV 组合，不表示平均码率固定为 10 Mbps。无模糊区间仍会经历视频编码；默认输出不是无损复制。

音频默认复制，需与输出容器兼容；需要转换时可用 `--audio-codec aac`，不要音频时用 `--audio-codec none`。`--ffmpeg` 和 `--ffprobe` 可以指定可执行文件路径。

## 项目配置与诊断

配置接受 JSON 或 YAML。例如：

```yaml
marker_prefix: bgblur
selection:
  backend: auto
  grouping: per-event
defaults:
  blur_sigma: 40
  feather_sigma: 12
  strength: 1.0
  opacity_threshold: 0.5
  padding_x: 28
  padding_y: 16
  corner_radius: 24
output:
  burn_subtitles: true
encoder:
  options:
    crf: 18
    preset: veryslow
```

```bash
assglass input.mp4 subtitle.ass -o output.mp4 --config examples/config.yaml
```

任务默认值按“内置值 → 项目配置 → CLI 默认值”覆盖，逐行显式参数再覆盖背景外观默认值。`--no-burn-subtitles` 和 `--burn-subtitles` 可以覆盖配置中的布尔值，两个开关不能同时使用。常用编码参数可直接用 CLI，其余已允许的参数使用 `--encoder KEY=VALUE`；参数不允许改动处理流水线的帧率或滤镜。

使用 `--manifest render.json` 指定机器可读运行记录，其中包含输入身份、工具环境、有效配置、时间与帧数、filtergraph 和权重摘要，方便排查和复现。报错时保留完整错误信息和 manifest（若已生成）。

`--debug-mask weights.raw` 可保存实际发送的三平面原始权重。它是混合权重，不能当普通 YUV 图像观看：Y 是 1920×1080，U/V 各 960×540，每帧 3,110,400 字节，三个平面在没有目标时都为 0。默认生产路径只流式传输权重，不落盘整段 mask；debug 文件有容量上限，长片请谨慎启用。

## 第一版支持范围

输入要求为 H.264、1920×1080、逐行扫描、方形像素、8 位 `yuv420p`、BT.709 / tv range、`chroma_location=left`，以及从 0 开始的精确 **60/1 或 60000/1001（约 59.94）fps CFR**。后者为本地实际素材新增的独立 profile，保留原始帧率，不转换为 60 fps。程序会检查实际显示帧时间序列，文件标称帧率不足以通过。

其他分辨率、帧率、VFR、HDR、10 位、不同色度位置、非方形像素、旋转或缺失关键色彩标签的输入会被拒绝。程序不会自动补帧、改色彩标签或猜测视频格式。ASS 文件应为严格 UTF-8，可带文件头 BOM。

当前实现包括 `event-images` 与旧的 `alpha` 选择后端，形状为 `box` 圆角矩形。`organic` 不规则形状、背景强度连续跟随字幕透明度、隐藏字形几何恢复等仍未实现。

EventImages 渲染完整原始 ASS，所有标记与未标记事件继续参与自动碰撞、特效和逐帧历史；只在渲染之后按真实事件身份提取目标图像。支持多组同时活动的独立背景。源 ASS 的 Dialogue 索引还会与 libass 的冻结事件数组核对，不能安全对应时会报错，不会猜测归属。

完全透明或有效不透明度不超过 `opacity_threshold` 的像素不会贡献背景几何。达到阈值要求的部分按几何生成指定强度的背景，背景强度不再乘以字幕的透明度。背景从 libass 已裁剪且通过阈值的字形范围向外扩张，因此 padding 和羽化可能越过原 `clip` 边界。

EventImages 不再改写任何字幕透明度，因此完整 ASS 中未标记的 Effect=fx、卡拉 OK、复杂 transform 等可交给 libass 原样渲染，不受旧 Alpha 隐藏规则限制。旧 `alpha` 后端仍只支持受限的 alpha/reset 规范化；无法保证隐藏或布局保持的组合会在编码前报错。无论使用哪种后端，都不修改原 ASS 文件。

## 常见问题

**字体位置不对或出现方框：**安装原稿所用字体，或向 helper 与 FFmpeg 提供同一字体目录。Aegisub 使用的 renderer 和字体环境可能不同；最终位置以当前导出环境为准。

**有背景框，但看不到毛玻璃：**原 ASS 的 `BorderStyle=3`、不透明 drawing 或其他底板可能盖住模糊结果。程序保留原样式；需要在 ASS 中调整底板透明度或删除不需要的底板。

**多行同时出现时报错：**独立框需要 EventImages 扩展。若已启用它，检查是否把不同外观参数的重叠行写进了同一 `group`；不同组可以采用不同参数。旧 Alpha 模式仍需要在接受共同大框的前提下显式启用 `--allow-merged-box`。

**提示 helper 与 FFmpeg 使用不同 libass：**在构建私有 libass 后重新运行 `sh scripts/build_ffmpeg.sh`，再重建原生核心／重新安装 Python 包。程序会验证真实库路径、版本和文件摘要；不要通过跳过检查混用系统 FFmpeg 与私有 helper。

**编码较慢：**全画面模糊和默认 `veryslow` 编码都有开销。可先用 `--preset fast` 对短片验证效果，再决定正式输出设置。输入码率较低并不意味着模糊或编码计算量很低。

## 开发与测试

```bash
python -m pytest -q

# 指定另一套 FFmpeg，运行实际短片流水线检查
ASSGLASS_TEST_FFMPEG=/path/to/ffmpeg \
ASSGLASS_TEST_FFPROBE=/path/to/ffprobe \
  python -m pytest -q tests/test_pipeline.py

# 对本机已安装的多套工具链逐一检查
sh scripts/test_matrix.sh /path/to/ffmpeg6/bin /path/to/ffmpeg8/bin
```

测试包含配置与 ASS 解析、原生 mask 数值、透明度筛选、逐事件归属、独立／命名组、碰撞历史一致性、回调失败与所有权、视频格式和时间检查，以及依赖真实 FFmpeg/libass 的短片集成测试。集成测试生成合成 1080p60 素材，不需要下载视频。

本次本地验证使用 macOS、FFmpeg 6.1.1、私有共享 libass 0.16.0（EventImages ABI 1）和 x264 core 164。最近一次完整测试结果为 **149 passed**（无跳过），包含 60000/1001 fps 的真实压制与精确时间戳验收，以及 opacity_threshold 的严格边界、颜色透明度、图层合并和真实 libass 模糊外沿回归。12 帧真实压制检查已覆盖：两种烧录模式的 mask / ledger 一致、标记起止与透明切换零帧偏差、未标记字幕正常烧入且不泄漏背景、三平面零 mask、精确输出时间戳，以及从输出 x264 SEI 核对完整默认编码参数。另验证了生产滤镜在编码前的全零/全一权重端点逐字节正确、日志失败不会导致 pipe 死锁、wheel 独立安装，以及 AAC 默认复制的 10 个音频包哈希一致。本机 FFmpeg 5.0.1 未通过权重端点自检，已作为拒绝环境处理。

FFmpeg 5～9 是设计兼容目标，不代表任意发行构建均已认证。本地验证结果只适用于记录的工具链；Ubuntu 版本矩阵、真实长片资源压力和全部字体组合需要在目标环境继续测试。短片通过不等于已完成一小时视频压力验收，也不代表承诺实时处理速度。

示例 ASS 位于 [`examples/demo.ass`](examples/demo.ass)。完整设计、数值规则和后续扩展边界见设计文档。
