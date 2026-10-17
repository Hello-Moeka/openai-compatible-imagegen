# OpenAI-compatible ImageGen Skill

面向 Codex 的自定义图片 API Skill：使用可配置的 OpenAI-compatible Images API 执行生图与编辑，结合 GPT Image 提示词参考，默认按图像质量优先选择模型。

本项目是社区项目，不是 OpenAI 官方产品。API 服务、账户、模型权限及费用由使用者自行配置。

## 功能

- 文生图、参考图编辑、人物换背景、多图参考和 mask 局部编辑。
- 按任务默认查阅一份提示词指南，连续修改复用已读资料。
- 自动选模结合 `/models` 与能力矩阵；也可明确指定任意模型 ID。
- 请求前检查输入图片、mask、输出目录及参数；请求后检查返回数量和图片可解码性。
- 支持 Base64、data URL 和远程图片 URL；保存图片与不含提示词、凭据的 manifest。
- 透明背景编辑会验证实际 alpha；脚本不自动重试或在失败后静默换模型。

## 安装到 Codex

需要 Python 3.10+、Git，以及可访问目标图片 API 的网络环境。依赖为 Pillow。

下面示例安装到默认全局目录；若设置了 `CODEX_HOME`，请将目标目录改为 `$CODEX_HOME/skills/openai-compatible-imagegen`。目标目录应不存在，已有安装请先检查本地修改与配置。

PowerShell：

```powershell
git clone https://github.com/Hello-Moeka/openai-compatible-imagegen.git "$HOME/.codex/skills/openai-compatible-imagegen"
Set-Location "$HOME/.codex/skills/openai-compatible-imagegen"
python -m pip install -r requirements.txt
Copy-Item .env.example .env.local
```

macOS / Linux：

```bash
git clone https://github.com/Hello-Moeka/openai-compatible-imagegen.git ~/.codex/skills/openai-compatible-imagegen
cd ~/.codex/skills/openai-compatible-imagegen
python3 -m pip install -r requirements.txt
cp .env.example .env.local
```

按本机 Python 环境需要使用虚拟环境。编辑安装目录的 `.env.local`，填入自己的 API root 和 Key：

```dotenv
IMAGE_API_BASE_URL=https://your-provider.example/v1
IMAGE_API_KEY=
IMAGE_MODEL=auto
```

`IMAGE_API_KEY` 留空只是示例，调用前必须填写。脚本从**脚本所在项目根目录**读取 `.env.local`，不从任意当前工作目录读取；同名进程环境变量优先。不要将真实 Key 提交到仓库或粘贴到 Issue。

安装后可在 Codex 中调用 `$openai-compatible-imagegen`。在需要绝对路径的会话中，使用安装目录下的脚本路径；输出目录建议指向当前任务工作区。

## 使用

以下命令在安装目录执行，运行时会使用真实 API 并可能产生费用。

文生图：

```bash
python scripts/image_api.py --prompt "一只橘猫坐在阳光下的窗边，温暖自然光" --output-dir outputs/cat-001
python scripts/inspect_output.py outputs/cat-001
```

人物参考与换背景：

```bash
python scripts/image_api.py --input person.png --image-role "人物身份参考，保留面部、发型与服装" --prompt "保留人物特征和姿势，将背景改为傍晚海边，光线自然协调" --output-dir outputs/edit-001
```

mask 编辑与批量：

```bash
python scripts/image_api.py --input source.png --mask mask.png --prompt "只修改透明遮罩区域，增加一只白色陶瓷杯，保留其余内容" --n 2 --output-dir outputs/mask-001
```

透明背景编辑：

```bash
python scripts/image_api.py --input product.png --prompt "保留产品外形与标签，移除背景" --background transparent --format png --output-dir outputs/cutout-001
```

仅检查请求、不发网络请求：

```bash
python scripts/image_api.py --prompt "测试提示词" --dry-run
```

`--check-config` 检查本地配置；`--check-connection` 只请求 `/models`，不代表生图或编辑已验证。`--help` 列出完整参数。每次使用新的或空的输出目录。

## 模型选择与兼容性

模型指定优先级：`--model` → 具体的 `IMAGE_MODEL` → 自动选择。

默认、`--priority quality` 和 `--priority fidelity`：

```text
gpt-image-2.5-sunburst → gpt-image-2.5-flare → gpt-image-2
```

显式 `--priority speed`：

```text
gpt-image-2.5-flare → gpt-image-2 → gpt-image-2.5-sunburst
```

这是候选模型满足能力要求时的请求前选择顺序。`fidelity` 不自动设置 `input_fidelity`，模型选择也不会自动设置 `quality=max`。

当前自动能力矩阵基于 **2026-10-18 对 api-top.com 的开发者实测**，不是对所有兼容接口的保证，也不是独立质量排行榜。客户端未按 provider 自动维护多套矩阵；更换服务商后，应先显式指定 `--model` 验证并维护对应能力标记，再使用自动模式。自动模式需要 provider 支持 `/models`。

已知边界：

- 透明背景文生图未通过当前 provider 测试，自动模式拒绝此组合；透明背景编辑已通过。
- Flare 普通编辑批量最新 `n=2` 复测通过，但历史上曾请求 2 张返回 4 张；数量不符时脚本报错。
- `--n` 接受 1–10，真实批量测试覆盖 `n=2`。多图输入上限是客户端约束，不表示全部数量组合均已实测。
- 服务商曾忽略请求尺寸并返回其他尺寸；manifest 记录实际尺寸，不保证精确分辨率或画布比例。
- `nano-banana-2` 不在自动候选中；其他模型可通过显式 `--model` 调用，能力由接口决定。
- mask 是生成编辑的引导，不能保证未编辑区域逐像素不变；身份、文案、细节质量需要实际审阅。
- Skill 不能授予网络权限。网络受限时只使用宿主允许的通道，不修改本机网络设置。

详细记录与官方来源见 [能力矩阵](references/provider-capability-matrix.md)。原始测试图片、manifest 和本地配置不随仓库发布，矩阵是开发者测试摘要。

## 输出与项目结构

成功输出图片与 `image-manifest.json`。manifest 记录模型、provider、时间、操作、选择依据、安全参数、输入文件名，以及实际图片路径、格式、尺寸和响应类型；不记录提示词、API Key 或认证头。输出仍可能包含敏感的文件名与路径，分享前按实际内容检查。

```text
SKILL.md                         Codex 入口与完整工作流
scripts/image_api.py             API 执行与预检
scripts/inspect_output.py        图片及 manifest 检查
references/prompt-router.md      提示词资料路由
references/provider-capability-matrix.md  实测摘要与模型选择依据
references/gpt-image/            提示词指南与案例资料
tests/test_image_api.py          本地模拟服务测试
.env.example                    无凭据配置模板
```

## 开发与验证

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m py_compile scripts/image_api.py scripts/inspect_output.py
```

测试使用临时图片和本地 HTTP 服务，不读取真实 `.env.local`，不需要 API Key，不调用付费图片服务。GitHub Actions 在 Windows 与 Linux 上运行测试。API 返回成功与文件检查通过不等于最终图像质量验收。

## 来源与许可证

本项目自身代码与维护的工作流采用 [GNU AGPL-3.0](LICENSE)（`AGPL-3.0-only`）。改编或随附的上游资料保留原有 MIT 许可与署名：

- 执行流程参考 [yc-duan/api-image](https://github.com/yc-duan/api-image)，版本与边界见 [来源说明](references/api-image-source.md)，许可见 [api-image-LICENSE](references/api-image-LICENSE)。本项目不调用其 CLI。
- 提示词参考改编自 [wuyoscar/GPT-Image2-Skill](https://github.com/wuyoscar/GPT-Image2-Skill)，版本见 [提示词路由](references/prompt-router.md)，许可见 [参考库 LICENSE](references/gpt-image/LICENSE)。

上游示例中的第三方品牌、人物和远程图片仍受各自来源约束；本项目许可证不替代第三方素材授权。
