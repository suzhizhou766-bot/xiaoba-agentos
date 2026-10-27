# Lightroom预设精选候选库

**后续更新（2026-10-18）：用户要求的分类安装已完成。LR中现有45个XMP、8个“小八”分组，旧格式5个已由LR原生转换。** 详见 `安装与分类说明.md`、`LR安装记录.json`；分类成品包为 `小八_LR分类预设_8组45个XMP.zip`。下文“未安装”属于采集阶段的历史记录，实际安装状态以这条更新和安装回执为准；预设效果仍未逐项试用。

采集日期：2026-10-18。用户要求检索全网并抓取优秀LR预设。本轮结合中英文搜索核对13处来源，实际取得2个官方/作者原始包，共45个不同SHA256的预设：40个XMP、5个LRTEMPLATE。

## 已抓取的内容

| 来源与系列 | 文件数 | 格式 | 用途 |
|---|---:|---|---|
| Adobe · Architecture | 18 | XMP | 建筑与城市风格；包含褪色、薄雾、低饱和等不同取向 |
| Adobe · Basic | 9 | 6 XMP / 3 LRTEMPLATE | 黑白、城市电影感、风景 |
| Adobe · Colour | 3 | XMP | 主体强调、强光色彩协调、日出色调 |
| Adobe · Portrait | 3 | XMP | 冷调、暖调、胶片感人像 |
| Adobe · Night | 4 | XMP | 城市夜景、自然夜景、电影光线 |
| Adobe · Nature / Street / Pets | 6 | XMP | 绿植、街头、动物 |
| Presets Heaven · PH Bloggers 1 | 2 | LRTEMPLATE | 历史作者样本，RAW/JPEG分别提供 |

公开来源：[Adobe官方合集](https://www.adobe.com/products/photoshop-lightroom/lightroom-presets.html)、[Presets Heaven作者样本](https://www.presetsheaven.com/presets/lightroom-presets-for-bloggers/)。Adobe是本包的官方分发入口，不将包内所有摄影师作品都归为Adobe自行创作。

## 先试哪些

本轮“精选”是按来源、题材和可检查的参数预选，没有在Lightroom中加载实际照片验证美观或相机兼容。下表是试用顺序建议，来自文件静态检查；不是已证明适合所有照片的排名。

| 方向 | 预设 | 建议与实测参数提示 |
|---|---|---|
| 会所建筑的主体与亮暗 | emphasise-objects | 从这类主体强调思路开始比较；文件会写入Exposure2012 +1.00，不能忽略原片曝光 |
| 材质与颜色分离 | Vibrance and Contrast | 自然饱和度+35、饱和度+15、去朦胧+5；同时抬黑+35，需检查是否重新变灰 |
| 石材、木纹的质地实验 | Earth Tones and Texture | 清晰度+35、去朦胧+40，效果较强，重点看边缘与材质是否过硬 |
| 暖调自然人像 | WarmPreset2021 | 对比+20、黑色-36；检查肤色、原有光线及暗部层次 |
| 绿植、自然场景 | Luke-Madeiran-Foliage | 题材匹配的自然候选，须在实际叶色与光线下比较 |
| 城市夜景 | 02-city-lights | 夜景候选，检查招牌、灯光和暗部，不能按名称认定保留所有高光 |

依照小八STYLE-012，本组会所第二版已采用的经验是保留材料本色、高光反射与明暗层次。建筑系列中的Faded、Washed、Haze类并不天然符合这一目标，先保留作风格比较；不要将18个建筑预设整组自动套用。

## 文件与检查

- `预设文件/XMP/`：40个原始XMP；`LR_XMP_导入包.zip`：仅含这40个XMP，便于按软件支持的流程导入。
- `预设文件/旧版LRTEMPLATE/`：5个历史格式，保留原内容；没有自动转换或执行。现代Lightroom能否直接使用须按具体版本验证。
- `逐项清单.md`：45个预设的编号、分类、路径和参数注意项。
- `预设目录.json`：逐项SHA256、文件格式、参数摘要及检查状态。
- `原始下载包/`、`下载记录.json`：作者原包、公开下载来源、下载方式与哈希。
- `来源与获取条件.json`：13处来源的获取条件与未完成项。

已完成ZIP的CRC、解包路径、SHA256去重、40个XMP的XML和Normal预设类型检查、5个LRTEMPLATE的UTF-8/Develop静态检查。全部原文件保持字节内容，没有重写作者参数。参数提示是有限规则扫描，不是完整的效果预测。没有启动或安装到Lightroom，没有套用实片，也未记为用户已采用风格。

[Adobe导入说明](https://helpx.adobe.com/lightroom/desktop/kb/faq-install-presets-profiles.html)列出的Lightroom桌面入口为“File → Import Profiles & Presets”；具体Lightroom Classic、桌面云版、手机版流程分别核对，不能混用版本说明。个别预设引用相机配置文件，实际显示与效果仍受原片和版本影响。

## 其他值得继续看的来源

| 来源 | 方向 | 本轮状态 |
|---|---|---|
| [ON1及合作作者](https://www.on1.com/free/lightroom-presets/) | 风景、胶片、肖像、自适应 | 官方列出11包；表单需要个人信息与同意条款，未代填 |
| [RNI All Films 4 Demo](https://reallyniceimages.com/products/rni-all-films-4-free-demo.html) | 胶片模拟 | 只记录官方Demo获取入口，未完成Gumroad领取 |
| [Signature Edits Clean & Classic](https://shop.signatureedits.com/clean-classic-freebie) | 清透经典样本 | 官方样本入口已核实，未完成领取 |
| [FixThePhoto室内系列](https://fixthephoto.com/free-interior-design-lightroom-presets) | 室内与房地产 | 已核实下载页要求姓名/邮箱，未提交 |
| [Photon Collective Quantum](https://photoncollective.com/the-quantum-collection-free-presets-for-adobe-lightroom-and-camera-raw) | 胶片与电影感 | 页面有免费声明，但旧获取组件未提供本轮可用文件 |
| [PresetLove Clean](https://presetlove.com/presets/clean/) | 清透自然 | 下载链返回网页，未取得预设；不把网页冒充文件 |
| [Presetpro](https://www.presetpro.com/free-lightroom-presets/) | 风景、胶片、城市 | 作者页可读，下载子站403，未取得文件 |
| [FilterGrade](https://filtergrade.com/free-lightroom-presets/) | 摄影师风格 | 当前访问受限，未取得文件 |
| [Mastin Labs Portra Original](https://mastinlabs.com/pages/portra-original) | 暖中性胶片、人像 | 付费产品，仅记录官方入口 |
| [DVLOP](https://dvlop.com/shop/weddings) | 婚礼摄影师与胶片系列 | 付费产品，仅记录官方入口 |
| [Goodlight Pack 1](https://goodlightpresets.com/pack-1-color) | 色彩人像 | 付费产品，仅记录官方入口 |

以上名单是后续比较对象，不代表每个系列都适合用户，也没有购买或领取未取得的文件。已下载包的依据为原站公开免费链接；未将公开下载推定为可转售或公开再分发许可，本库只作本地整理，具体使用条件保留原站入口。
