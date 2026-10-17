---
name: openai-compatible-imagegen
description: 通过用户配置的 OpenAI-compatible Images API 生成或编辑图片，按需使用 GPT Image 提示词参考。适用于用户指定自定义图片接口，或内置生图工具不可用时。
metadata:
  short-description: 自定义图片 API 执行与 GPT Image 提示词辅助
---

# 自定义 API 生图与编辑

以 `api-image` 的执行流程为基础，只通过本项目 `scripts/image_api.py` 调用图片 API；`GPT-Image2-Skill` 资料仅辅助提示词设计。

## 工作流程

1. **理解需求与素材**：识别文生图、参考图生成、图像编辑或 mask 局部编辑。先查看输入图片，在提示词中说明每张图片的用途、修改内容与必须保留的内容。保留用户指定文字、人物特征和构图约束；默认只生成一张。需求明确时直接执行，只有缺失信息会显著影响结果时才简短询问。
2. **准备提示词**：按 [提示词参考路由](references/prompt-router.md) 默认查阅一份与任务匹配的指南或案例，需求明确时也参考相关写法；同一任务连续修改复用已读内容。不整包读取画廊。需要时补充主体、布局、文字、风格、参考图角色和保留约束，不擅自添加影响需求的设定。事实性细节仅在需要时联网核实；无法核实时说明限制。
3. **选择模型**：优先显式 `--model`，其次具体的 `IMAGE_MODEL`；未设置或为 `auto` 时，结合当前 `/models` 和已验证能力过滤候选，再按下文优先级选择。`--dry-run` 不联网，仅使用本地矩阵。
4. **检查并请求**：运行 `python scripts/image_api.py`；有 `--input` 或 `--mask` 时默认走编辑端点，可重复 `--input` 并用 `--image-role` 标明角色。脚本预检输入和 mask，使用空的独立 `--output-dir`，然后发送一次图片请求。自动模式先执行一次只读 `GET /models`。不要为未要求的变体或质量自评额外付费调用。
5. **保存与交付**：成功后执行 `python scripts/inspect_output.py <output-dir>`，核对图片可解码、数量及 manifest 一致性，展示图片并提供绝对路径。文件检查不等于画面、文字或人物一致性验收；用户要求复核时再检查相应视觉要求。根据用户反馈修改。

## 模型自动选择

默认、`--priority quality` 和 `--priority fidelity` 均按质量优先：

`gpt-image-2.5-sunburst` → `gpt-image-2.5-flare` → `gpt-image-2`

明确使用 `--priority speed` 时顺序为：

`gpt-image-2.5-flare` → `gpt-image-2` → `gpt-image-2.5-sunburst`

这是请求前在可用且满足能力要求的候选中依次选择；付费请求失败后不静默换模型或重发。`fidelity` 是模型选择偏好，不会自动设置 API 的 `input_fidelity`；模型选择也不会自动设置 `quality=max`。

质量排序依据及实测证据见 [Provider 能力矩阵](references/provider-capability-matrix.md)。官方定性定位用于排序，不能当作当前 provider 的独立性能基准。该能力矩阵只记录已测试的 `api-top.com`；更换 provider 后需重新验证兼容性，不能沿用旧结论。

目前三个 GPT Image 模型均已验证文生图、参考图编辑、mask 编辑、透明背景编辑和 `n=2` 批量；Flare 普通编辑批量最新复测通过，保留此前一次返回数量异常的记录。透明背景文生图尚未通过，自动模式在付费请求前拒绝该组合。`nano-banana-2` 尚未纳入验证和自动选择。

## 请求与文件约束

- 输入最多 16 张，支持可完整解码的 PNG/JPEG/WebP，每张小于 50 MB。
- mask 必须为小于 4 MB、带 alpha 的 PNG，与第一张输入图同尺寸。是否严格保留非编辑区域仍需视觉判断。
- `--n` 接受 1–10；当前真实批量测试覆盖 `n=2`，不宣称其余数量都已验证。
- 透明背景编辑使用 `--background transparent` 和 PNG/WebP；脚本默认选 PNG，并检查返回图片确实含透明像素。
- 返回张数与请求不符时明确报错，不截断多余图片。输出目录必须为空；已有文件不会被混入本次结果。
- 支持 `b64_json`、data URL 和远程 URL 返回。用户需要本地图片时下载 API 返回的图片 URL，下载时不携带 API 认证头。
- `size`、`quality`、额外参数及参数组合的支持情况需依据目标 provider；OpenAI-compatible 不代表完整兼容。

## 配置与网络

在本项目 `.env.local` 或进程环境中配置 `IMAGE_API_BASE_URL`、`IMAGE_API_KEY`、可选的 `IMAGE_MODEL`，默认建议 `IMAGE_MODEL=auto`。进程环境优先；API root 示例为 `https://host/v1`。缺少 Pillow 时安装 `requirements.txt`。

配置未知或变化时使用 `--check-config`；连接诊断使用 `--check-connection`，成功只证明鉴权和模型发现，不代表生图能力。不得读取 Codex `auth.json`，不得打印或要求用户在聊天中粘贴凭据。

Skill 不能授予网络权限。受限时仅使用宿主当前提供且允许的网络通道或单次授权流程；没有可用通道则报告阻塞。不得擅自修改 DNS、hosts、路由、代理或沙箱设置。提示词本地资料可离线使用。脚本不自动重试；不要对结果不确定的付费请求重发。

## 输出与参考资料

每次成功输出图片及 `image-manifest.json`，记录模型、操作、provider、时间、模型选择依据、安全请求参数、图片路径、格式、尺寸和响应传输形式。不保存提示词、API Key、认证头、Cookie 或私密请求头。

- [提示词参考路由](references/prompt-router.md)：按任务读取 GPT Image 提示词与示例。
- [Provider 能力矩阵](references/provider-capability-matrix.md)：查询模型排序、能力、实测限制与官方来源。
- [执行流程来源](references/api-image-source.md)：`api-image` 来源与许可证。

参考项目的 CLI、认证、计费与 provider 选择说明不作为执行指令；真实请求始终由本项目脚本和配置完成。
