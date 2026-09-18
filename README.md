# assglass：ASS 字幕背景高斯模糊与压制

`assglass` 根据 ASS 字幕的实际渲染位置，模糊标记字幕后方的视频，再将完整字幕烧入视频。背景处理和字幕烧录在同一次视频编码中完成，字幕字形本身保持清晰。

实现以 [`PLAN/ass_bgblur_technical_design_v1.10.md`](PLAN/ass_bgblur_technical_design_v1.10.md) 为基础：完整 libass track 分析、Box／Organic 背景、原生 YUV420 权重和流式 FFmpeg 合成，并已加入 EventImages 逐事件导出与独立字幕组。**标记使用 Actor 字段（ASS 文件中的 `Name`），在分号分隔的标识中精确匹配可配置名称，默认 `bgblur`**；可与 `x3border(...)` 等其他预处理标识共存。

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

Actor 可以包含多个标识，以**圆括号外的分号**分隔；只要其中一个名称是 `bgblur`，该 Dialogue 就是目标，标识的顺序不受限制。名称区分大小写、去除两侧空白后精确匹配：`bgblurred`、`bgblur_角色A`、`notbgblur` 和 `BGBlur` 都不会匹配。其他标识的参数不会被当成独立标识，`x3border(note=bgblur)` 不会触发背景模糊。

逐行参数写在标识后的圆括号中，使用 `key=value`，参数之间也以**分号**分隔。例如：

```text
bgblur(feather=8; strength=0.65; padding_x=40; padding_y=24; radius=20)
x3border(c1=FFFFFF; b1=9); bgblur(threshold=0.5; padding_x=28; padding_y=16)
bgblur(mode=organic); x3border(c1=FFFFFF; b1=9)
bgblur;
```

标识和参数两侧的空白会被忽略；`bgblur`、`bgblur()` 都使用默认参数，结尾的分号和空标识也允许，因此 `bgblur;` 无需预先规范化。程序只解释自己的标识，保留 `x3border` 等其他内容，但不会替你执行对应的预处理。`Comment` 行不是目标；Effect 原有内容和正文 Text 中的逗号均保留。

同一 Actor 中可以重复 `bgblur`：`bgblur(feather=8); bgblur(strength=0.65)` 合并为一组逐行参数。跨多个标识重复同一参数的相同值也允许，给出不同值时会报错；单个参数块内仍不允许重复参数名（包括别名）。Actor 不是带引号的 CSV 字段，逗号会打乱 ASS 字段，不能写 `bgblur(feather=8,strength=0.65)`。

自定义名称仍使用 `--marker-prefix glass` 或配置项 `marker_prefix: glass`。选项名保留兼容，但含义改为精确匹配一个标识，例如 `x3border(...); glass(strength=0.65)`。

**旧写法迁移：**将 `bgblur{feather=8;strength=0.65}` 和 `bgblur;feather=8;strength=0.65` 改为 `bgblur(feather=8;strength=0.65)`。括号外的内容是独立标识，不再作为 `bgblur` 的参数；旧的 `bgblur_角色A` 等前缀后缀写法也需改为独立的 `bgblur` 标识，需要分组时使用 `bgblur(group=角色A)`。

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
bgblur(group=comment)   # “说不定这段就被剪掉了”
bgblur(group=why)       # “为啥——！”正文层
bgblur(group=why)       # “为啥——！”白描边层
bgblur(group=why)       # “为啥——！”黑描边层
```

填写 Actor 时只填写左侧标记，不包含示例注释。组名区分大小写，可含中英文、数字、下划线、点和连字符，长度1～128。未写 `group` 的每一行独立；`bgblur_why` 不会匹配标记或隐式创建组。`group` 也可放进 sidecar 每条事件的 `overrides`，但不能作为全局 mask 默认值。

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
| `mode` | `box` | `box` 圆角矩形，或 `organic` 跟随字形的不规则背景 |
| `padding_x` | 28 | 阈值筛选后的字形范围左右各扩张多少像素 |
| `padding_y` | 16 | 阈值筛选后的字形范围上下各扩张多少像素 |
| `radius` / `corner_radius` | 24 | 矩形圆角半径 |
| `feather` / `feather_sigma` | Box 12／Organic 16 | 背景权重边缘的高斯 sigma |
| `strength` | 1.0 | 0～1；0 关闭该背景，1 使用完整模糊权重 |
| `threshold` / `opacity_threshold` | 0.5 | 仅有效不透明度严格大于此值的像素参与字形范围计算 |
| `include` | `character+outline` | 用字形与描边计算范围，可添加 `shadow` |

外扩、圆角和 sigma 使用 1080p 输出像素单位；`strength` 和 `threshold` 是 0～1 的比例，不随分辨率缩放。`feather` 控制背景边缘过渡，`strength` 控制原图与模糊图的混合比例。视频高斯模糊的 `blur_sigma` 是任务级参数，默认 40；不能放在逐行标记中。未写出的参数继承当前任务默认值，每行独立解析，不继承上一行。

`threshold` 用于排除 ASS `\blur` 等效果产生的低透明度外沿。程序先从 libass 渲染结果计算每个像素的有效不透明度 `coverage / 255 × (255 − ASS alpha) / 255`，只保留严格大于阈值的像素。Box 从这些像素求包围框，再添加 padding、圆角和柔和羽化；Organic 按下节处理保留下来的字形。字形、描边等选中图像之间取最大值，不累加重叠图层的透明度。阈值不改写字幕外观，也不截断背景最终的羽化。

默认阈值为 0.5，同时将左右／上下外扩恢复到 28／16 像素，为实际字形留出余量。`threshold=0` 恢复旧的所有非零像素范围；`threshold=1` 不会选中任何像素。如果某帧所有目标像素的不透明度都不超过阈值（例如淡入淡出的低透明阶段），该帧不生成背景框；需要保留这些阶段时可降低阈值。

## Organic 不规则背景

Organic 适合旋转文字、长短不齐的多行文字和分散的字形。它沿字形外扩，再用闭运算填补小孔和狭窄间隙，最后羽化边缘；较大的空白仍会保留，不会自动变成一个大矩形。

```bash
# 全局使用 Organic；原 ASS 无需修改
assglass input.mp4 subtitle.ass -o organic.mp4 --default mode=organic

# 调整外扩和字间连接程度
assglass input.mp4 subtitle.ass -o organic.mp4 \
  --default mode=organic --default expand_x=48 --default expand_y=36 --default close=48
```

也可以仅在需要的行的 Actor 中写入：

```text
bgblur(mode=organic)
bgblur(mode=organic;expand_x=48;expand_y=36;close=48;feather=16)
bgblur(group=why;mode=organic;close=10)
```

| Organic 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `expand_x` / `expand_y` | 48 / 36 | 椭圆外扩的水平／垂直半径；默认核为 97×73 |
| `close` | 48 | 矩形闭运算半径；默认核为 97×97，做一次膨胀再一次腐蚀 |
| `feather` | 16 | 边缘 Gaussian sigma，有限支持半径为 `ceil(3 × sigma)` |
| `strength` / `threshold` | 1.0 / 0.5 | 沿用当前背景强度和字形筛选阈值 |

半径使用 1080p 像素单位。`expand_x=0;expand_y=0` 关闭外扩，`close=0` 关闭闭运算，`feather=0` 关闭羽化；只把一个外扩半径设为 0，会沿另一个轴扩张。`padding_x`、`padding_y`、`radius` 仅适用于 Box，不能在 Organic 行中指定。全局 defaults 可以同时保存两种模式的几何参数，切换模式时仅使用适用项；公共参数仍按项目、CLI、逐行覆盖的优先级继承。Box 与 Organic 可以在不同组中同时使用。

Organic 默认使用**椭圆外扩 `expand_x=48;expand_y=36`、矩形闭运算 `close=48;feather=16`**。`expand_x/y` 控制主体外扩，`close` 填平凹口和邻近间隙，`feather` 控制软边；`close=48` 并不表示四周额外外扩 48 像素。矩形 closing 由四遍一维单调队列扫描完成，固定工作区下复杂度为 `O(宽×高)`；较小半径还能缩小所需处理边距。默认值按每行最终模式选择；只写 `mode=box` 会恢复 Box 的外扩 28 / 16 和 feather 12。项目、CLI、Actor 中的显式数值仍优先。

当前 Organic 算法版本为 `organic-ellipse-expand-rect-close-v2`，外扩保持椭圆核，闭运算改为矩形核。旧配置中显式写出的 `close=96` 等数值会保留，但将采用矩形核；旧圆盘核的外观不能仅靠恢复同一个 close 数值复现。

每个独立组先合并本组的字形覆盖，再单独完成外扩、闭运算、羽化和强度缩放，最后在组之间取最大权重。相同 `group` 的多层字幕会共同生成轮廓；不同组之间不做闭运算连接。若本来的外扩范围相交，最终背景仍可能相连。旧 Alpha 后端须显式使用 `--backend alpha --grouping merged`，它会把所有当前目标视作同一组，要求参数一致，并可能连接相邻目标。

算法保留通过阈值的 `coverage / 255` 灰度，不把字形二值化，也不再乘字幕 opacity。`threshold=0` 可保留所有非零可见覆盖，包括弱笔画和淡薄外沿。没有额外的自动填洞或连通域清理。所有算子使用零延拓，保留完整处理边距后才裁到画面；外扩核使用椭圆内（含边界）的整数像素中心，closing 核使用完整的轴对齐矩形。运行记录的 `mask_geometry` 会写出算法版本、实际核尺寸、羽化范围、数据类型与边界策略。

Organic 在 C++ 中执行，不需要安装 OpenCV；源码运行会自动重建原生核心，安装版需要重新安装 Python 包。它比 Box 增加了字形形态学运算，较大外扩和 closing 会增加处理时间与内存。

## 空帧快速路径与静态复用

程序先按完整字幕历史做一次轻量活动扫描。没有标记、完全透明、阈值筛选后没有字形、`strength=0` 或可证明完全离屏的帧，会跳过遮罩构建，并让 FFmpeg 旁路背景 Gaussian 和混合运算。连续空帧共享一个全零权重缓冲，不再逐像素生成权重。视频帧、PTS、声音和原字幕烧录仍完整保留；“跳过”指跳过背景处理，并非删除视频帧。

默认逐组缓存上一帧的字幕图像与遮罩。分组/事件身份、配置、画面尺寸、渲染环境和图像位置、颜色、全部覆盖率数据一致时复用结果。摘要仅用于快速筛选，之后由 C++ 精确比较像素；动画或参数变化会重新计算。缓存用于字幕遮罩，背景视频仍取当前帧，适合字幕不动而背景继续变化的片段。

合并后的最终遮罩也会用于复用上一帧的 **Y/U/V 混合权重**。C++ 精确比较遮罩位置与 float32 内容；相同存储直接命中，相同内容的重新合并结果也能命中。命中后不再运行整帧亮度量化和色度采样。每帧仍使用自己的时间戳，静止字幕下的视频背景正常更新。遮罩、尺寸或采样配置变化会重新生成权重。

遮罩缓存与权重缓存各有默认 **16 MiB** 上限，均计入同一个默认 **64 MiB** 原生内存总预算。权重缓存仅保留最近一次的最终遮罩与权重，不积累整段视频。容量不足时不保留结果；工作帧需要更多内存时会释放可选缓存再重试。降低总预算也会限制缓存上限。项目配置可调整或关闭复用：

```yaml
transport:
  max_in_flight_bytes: 67108864
  mask_cache_bytes: 16777216   # 0 关闭跨帧遮罩缓存，便于对照测试
  weight_cache_bytes: 16777216 # 0 关闭跨帧权重缓存（包括全零权重）
  mask_hash: crc32            # 默认快速校验；可选 sha256 兼容旧结果
```

权重流诊断摘要默认使用 **CRC32**。缓存命中的帧复用单帧校验码，通过 CRC 合并得到完整原始字节流的校验值；重复帧、帧顺序和字节数仍全部计入。CRC32 不参与缓存命中判断，缓存使用精确内容比较。ASS、字体、sidecar 等低频身份摘要仍使用 SHA-256。

manifest 通过 `mask_checksum.algorithm` 和 `mask_checksum.value` 明确标注校验算法，同时记录帧数与字节数。需要与旧的 SHA-256 记录比较时，添加 `--mask-hash sha256`；此时也会输出兼容字段 `mask_sha256`，但必须扫描每一帧全部权重字节。默认 CRC32 模式不再输出 `mask_sha256`，读取 manifest 的外部脚本应使用 `mask_checksum`。

空帧快速路径需要 FFmpeg 的 `sendcmd` 和 `gblur`/`maskedmerge` 时间线支持。使用旧版项目私有 FFmpeg 时，运行 `sh scripts/build_ffmpeg.sh` 更新；不必重新编译 libass。程序会检查所需能力，缺失时给出明确提示。

manifest 的 `activity` 记录模糊/旁路帧数及扫描耗时，`optimization.mask_cache` 与 `optimization.weight_cache` 分别记录两类缓存命中、构建和内存回收次数及占用峰值。`optimization.checksum` 记录实际扫描的字节数和复用帧数，`timings.stages_seconds.checksum` 单独记录校验耗时。形态学、Gaussian 和权重热路径已经使用 C++；后续算法优化的文献、精确性限制和建议见 [Organic 性能调研](docs/organic-performance-research.md)。

此前矩形 `close=24;expand_x=28;expand_y=16` 的本机完整流程短基准（并非当前 `close=48;expand_x=48;expand_y=36` 默认值）：从原始 H.264 视频按完整 GOP 复制 1980 帧（33.033 秒，1080p59.94），使用 Organic、默认缓存和 CRC32、CRF 18，包含字幕烧录、完整输入预检和输出验收。两种编码设置串行测量：

| 编码设置 | 33.033 秒样片总耗时 | 同类负载的 30 分钟线性估算 |
| --- | ---: | ---: |
| demo 的 `veryfast;subq=5;me_range=16` | 89.7 秒 | 约 1 小时 21 分钟 |
| 默认 `veryslow;subq=10;me_range=32` | 221.2 秒 | 约 3 小时 21 分钟 |

样片有 466/1980 帧需要背景模糊（23.5%），实际构建 45 个组遮罩，1933 帧复用最终权重；两次运行的权重流 CRC32 均为 `1b15f419`。veryfast 测量中遮罩构建累计 10.0 秒、权重转换 0.81 秒、权重校验 0.11 秒。这些累计阶段时间与并行编码重叠，不能简单相加为总耗时。30 分钟估算要求相似的视频复杂度、字幕密度和动画程度；启动固定开销也被线性放大，因此只用于粗估。若只使用当前 ASS（标记均在开头约 28 秒内），后续无标记部分的背景处理会旁路，不能套用相同的遮罩密度。原始测量记录保存在本地 `local_test/preview/perf/rect24_pipeline_projection.json`。

上一轮优化的本地基准：同一段 1770 帧（29.53 秒）1080p59.94 素材，使用相同 Organic `close=96;feather=16` 和 veryfast 编码设置，正式导出从 1612.2 秒降至 214.2 秒，约快 7.5 倍。其中 1304 帧旁路，515 次组遮罩请求命中 470 次缓存，实际构建 45 次；遮罩阶段从 1507.7 秒降至 130.4 秒。整段实际发送的权重 SHA-256 相同。缓存峰值 6.92 MiB，应用原生总峰值 15.12 MiB。此结果包含预扫描和输出验收，尚未包含后续的非空权重缓存、CRC32 与矩形 closing；只代表本机这个样例，每帧都变化的字幕会有较低缓存命中率。

本次独立短基准（同机，1080p YUV420p，每帧 3,110,400 字节）：标准库 SHA-256 扫描中位耗时约 6.88 ms，CRC32 约 0.156 ms，分别取 5 轮、每轮 100 帧；权重转换在静态遮罩命中时从约 16.30 ms 降至 0.144 ms，测量前预热 5 次、测量 50 次，交替使用两份内容相同但存储不同的遮罩，包含精确比较成本。遮罩 ROI 为 `(0,556,1920,1080)`，缓存占用 7,134,720 字节。这些是单步骤测量，不包含上游遮罩生成、视频编解码和读写，不能换算成整段视频的加速倍数。

## 压制视频

```bash
# 默认：背景处理 + 完整原 ASS 烧录
assglass input.mp4 subtitle.ass -o output.mp4

# 只处理背景，不新增硬字幕
assglass input.mp4 subtitle.ass -o background.mp4 --no-burn-subtitles

# 自定义 Actor 标识名称（精确匹配，选项名保留兼容）
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
  # 省略 feather_sigma 时按模式选择默认值：Box 12，Organic 16。
  # feather_sigma: 12
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

当前实现包括 `event-images` 与旧的 `alpha` 选择后端，以及 `box` 圆角矩形和 `organic` 不规则形状。背景强度连续跟随字幕透明度、隐藏字形几何恢复等仍未实现。

EventImages 渲染完整原始 ASS，所有标记与未标记事件继续参与自动碰撞、特效和逐帧历史；只在渲染之后按真实事件身份提取目标图像。支持多组同时活动的独立背景。源 ASS 的 Dialogue 索引还会与 libass 的冻结事件数组核对，不能安全对应时会报错，不会猜测归属。

完全透明或有效不透明度不超过 `opacity_threshold` 的像素不会贡献背景几何。达到阈值要求的部分按几何生成指定强度的背景，背景强度不再乘以字幕的透明度。背景从 libass 已裁剪且通过阈值的字形范围向外扩张，因此 padding 和羽化可能越过原 `clip` 边界。

EventImages 不再改写任何字幕透明度，因此完整 ASS 中未标记的 Effect=fx、卡拉 OK、复杂 transform 等可交给 libass 原样渲染，不受旧 Alpha 隐藏规则限制。旧 `alpha` 后端仍只支持受限的 alpha/reset 规范化；无法保证隐藏或布局保持的组合会在编码前报错。无论使用哪种后端，都不修改原 ASS 文件。

Alpha 的 `alpha-v1.1` 校验支持无参数 `\c`、`\1c`～`\4c`：保留原标签，由 libass 恢复当前 Style 的对应颜色，不改变透明度。`fx`、`karaoke` 等 libass 不识别的 Effect 值也原样保留，不再因字段非空而拒绝；正文标签仍独立检查。Alpha 暂不接受未标记行的实际 `Banner;…`、`Scroll up;…`、`Scroll down;…` 滚动效果，这类输入可使用 EventImages 后端。

## 常见问题

**字体位置不对或出现方框：**安装原稿所用字体，或向 helper 与 FFmpeg 提供同一字体目录。Aegisub 使用的 renderer 和字体环境可能不同；最终位置以当前导出环境为准。

**有背景框，但看不到毛玻璃：**原 ASS 的 `BorderStyle=3`、不透明 drawing 或其他底板可能盖住模糊结果。程序保留原样式；需要在 ASS 中调整底板透明度或删除不需要的底板。

**多行同时出现时报错：**独立框需要 EventImages 扩展。若已启用它，检查是否把不同外观参数的重叠行写进了同一 `group`；不同组可以采用不同参数。旧 Alpha 模式仍需要在接受共同大框的前提下显式启用 `--allow-merged-box`。

**提示 helper 与 FFmpeg 使用不同 libass：**在构建私有 libass 后重新运行 `sh scripts/build_ffmpeg.sh`，再重建原生核心／重新安装 Python 包。程序会验证真实库路径、版本和文件摘要；不要通过跳过检查混用系统 FFmpeg 与私有 helper。

**处理较慢：**先查看 manifest 的阶段耗时及缓存命中率。大半径 Organic 闭运算在动态字幕上仍可能较慢；编码耗时较高时可先用 `--preset fast` 对短片验证效果。输入码率较低并不意味着模糊或编码计算量很低。

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

测试包含配置与 ASS 解析、Box／Organic 原生 mask 数值、透明度筛选、逐事件归属、独立／命名组、碰撞历史一致性、回调失败与所有权、视频格式和时间检查，以及依赖真实 FFmpeg/libass 的短片集成测试。Organic 另有独立全画布参考比对，覆盖灰度膨胀与闭运算、贴边与帧外字形、零半径、Gaussian 支持边界、预算不足回收、不同组不跨组闭合，以及与 Box 混用；两种形状均验证字幕烧录开关不改变 mask 和精确时序。集成测试生成合成 1080p60 素材，不需要下载视频。

本次本地验证使用 macOS、FFmpeg 6.1.1、私有共享 libass 0.16.0（EventImages ABI 1）和 x264 core 164。早期完整回归结果为 **302 passed**（无跳过），包含 60000/1001 fps 的真实压制与精确时间戳验收，以及 opacity_threshold 的严格边界、颜色透明度、图层合并和真实 libass 模糊外沿回归。12 帧真实压制检查已覆盖：两种烧录模式的 mask / ledger 一致、标记起止与透明切换零帧偏差、未标记字幕正常烧入且不泄漏背景、三平面零 mask、精确输出时间戳，以及从输出 x264 SEI 核对完整默认编码参数。新增覆盖空帧旁路的首帧/逐帧切换、多线程与遮罩输入延迟、sendcmd 失败检测、缓存开关整段权重一致、精确像素比较、强制摘要碰撞、事件/参数失效和低预算驱逐重试。另验证了生产滤镜在编码前的全零/全一权重端点逐字节正确、日志失败不会导致 pipe 死锁；此前还验证了 wheel 独立安装及 AAC 默认复制的 10 个音频包哈希一致。本机 FFmpeg 5.0.1 未通过权重端点自检，已作为拒绝环境处理。

FFmpeg 5～9 是设计兼容目标，不代表任意发行构建均已认证。本地验证结果只适用于记录的工具链；Ubuntu 版本矩阵、真实长片资源压力和全部字体组合需要在目标环境继续测试。短片通过不等于已完成一小时视频压力验收，也不代表承诺实时处理速度。

示例 ASS 位于 [`examples/demo.ass`](examples/demo.ass)。完整设计、数值规则和后续扩展边界见设计文档。
