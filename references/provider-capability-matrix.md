# Provider 能力矩阵

本矩阵记录当前配置的 OpenAI-compatible provider 的真实运行行为，不根据
`GET /models` 的模型列表推断能力。当前检查于 2026-10-18 完成；编辑和
mask 测试使用一张 1254x1254 PNG 输入图，以及一张同尺寸的 RGBA PNG mask。

## 官方契约边界

OpenAI 官方文档将 `gpt-image-2.5-flare` 和 `gpt-image-2.5-sunburst` 定义为
使用 Image API `generations` 和 `edits` 端点的图像模型。图像生成指南说明了
multipart mask 编辑、`background=transparent`、透明输出需要 PNG/WebP，以及
使用 `n` 请求多张图片。这些是 API 契约层面的说明；下表记录的是本 provider
的实际兼容性结果。

官方参考资料：

- [Flare 模型文档](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare)
- [Sunburst 模型文档](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst)
- [图像生成指南](https://developers.openai.com/api/docs/guides/image-generation)
- [Python edit 参考](https://developers.openai.com/api/reference/python/resources/images/methods/edit)

## 端点与参数检查

| 能力 | `gpt-image-2` | `gpt-image-2.5-flare` | `gpt-image-2.5-sunburst` | 证据 |
|---|---:|---:|---:|---|
| 文生图，`n=1` | 已验证 | 已验证 | 已验证 | 每个模型各执行一次 `POST /images/generations`，请求成功且图片可解码 |
| 参考图编辑，`n=1` | 已验证 | 已验证 | 已验证 | 每个模型各执行一次 `POST /images/edits`，请求成功且图片可解码 |
| Mask 编辑，`n=1` | 已验证 | 已验证 | 已验证 | 使用同尺寸 PNG mask；Sunburst 前两次遇到 provider 503 过载，第三次成功 |
| 透明背景生成 | 不可用 | 不可用 | 不可用 | 请求 `background=transparent` 和 PNG；三个模型都返回无 alpha 的 RGB 图片 |
| 透明背景编辑 | 已验证 | 已验证 | 已验证 | 同一输入图请求透明编辑；三个模型均返回带 alpha 的 RGBA，alpha 极值为 `(0, 255)` |
| 文生图批量，`n=2` | 已验证 | 已验证 | 已验证 | 每个模型都准确返回两张可解码图片 |
| 参考图编辑批量，`n=2` | 已验证 | **是；复测返回 2 张，曾有一次返回 4 张** | 已验证 | 最近一次 Flare 复测请求 2 张并保存、解码 2 张；此前一次请求 2 张却返回 4 张，保留异常记录 |
| Mask 编辑批量，`n=2` | 已验证 | 已验证 | 已验证 | 每个模型都准确返回两张图片 |

2026-10-18 对 Flare 的普通参考图编辑批量和 mask 编辑批量各复测一次，均请求两张并
返回两张可解码图片，manifest 检查通过。普通编辑此前曾返回四张，最新复测已成功，
因此能力标记为“是”并允许参与自动选择；历史数量异常仍作为 provider 稳定性风险保留。
脚本不会静默重试，也不会截断 provider 返回的多余图片。

## 响应传输形式

| 端点 | `gpt-image-2` | `gpt-image-2.5-flare` | `gpt-image-2.5-sunburst` |
|---|---|---|---|
| 文生图响应 | `url` | `b64_json` | `b64_json` |
| 编辑响应 | `url` | `b64_json` | `b64_json` |

客户端支持这两种响应形式，并把 `response_type` 写入每个 manifest 输出项。
本次检查中 provider 没有让 Image 2 返回 `b64_json`，它的实时响应均为 URL；
客户端已验证对该 URL 的下载、重定向限制和认证头隔离。

## 模型发现结果

经过认证的 `GET /models` 返回：

- `gpt-image-2`
- `gpt-image-2.5-flare`
- `gpt-image-2.5-sunburst`
- `nano-banana-2`

`nano-banana-2` 暂不进入本 Skill 的自动矩阵，因为本项目尚未验证它的文生图、
编辑、mask、透明背景、批量和响应形式能力。

## 自动选择标记

脚本当前使用以下已验证组合：

| 模型 | 文生图 | 参考图编辑 | Mask | 透明背景生成 | 透明背景编辑 | 文生图批量 | 编辑批量 | Mask 批量 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `gpt-image-2` | 是 | 是 | 是 | 否 | 是 | 是 | 是 | 是 |
| `gpt-image-2.5-flare` | 是 | 是 | 是 | 否 | 是 | 是 | 是 | 是 |
| `gpt-image-2.5-sunburst` | 是 | 是 | 是 | 否 | 是 | 是 | 是 | 是 |

## 模型质量与自动优先级

截至 2026-10-18，OpenAI Docs 将 Sunburst 定位为图像生成和编辑能力最强、适合高质量与精确编辑的型号；Flare 是面向速度的较小型号，图像质量与 GPT Image 2 相当。官方建议先用 Sunburst 建立质量基线，再用相同提示词和输入评估 Flare，只有在质量仍达标且延迟改善时才切换。官方没有发布这三者面向本 provider 的统一质量分数或完整延迟基准。

因此自动模式默认按质量优先：`gpt-image-2.5-sunburst` → `gpt-image-2.5-flare` → `gpt-image-2`。Flare 与 GPT Image 2 的官方质量定位相当时，以 Flare 的速度定位作为平手次序；显式 `--priority speed` 则将 Flare 放在首位。该排序是官方定性定位加确定性选择规则，不代表对 `api-top.com` 路由质量或延迟的独立基准测试。

官方来源：[图像提示与模型选择](https://developers.openai.com/api/docs/guides/image-prompting)、[Sunburst 模型页](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst)、[Flare 模型页](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare)、[GPT Image 2 模型页](https://developers.openai.com/api/docs/models/gpt-image-2)。

透明背景生成的“否”表示 provider 的实际输出没有 alpha，并不表示 provider
拒绝了这个参数。因此自动模式会在付费请求前拒绝透明背景生成。显式指定模型
仍可以进行探测，但客户端会拒绝没有实际透明像素的返回图片。
