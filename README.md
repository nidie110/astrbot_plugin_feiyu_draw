# astrbot_plugin_feiyu_draw

让 bot 用本地 **ComfyUI** 工作流直接画画：说「画一只猫」就出图，中文提示词自动翻成英文。

## 特性

- **工作流驱动**：任意 ComfyUI 工作流 json 都能接，只要里面有两个 Simple String 节点（正向/负向提示词）
- **中文友好**：中文描述先翻成英文再进工作流（可关，工作流本身吃中文时请关掉）
- **显存腾挪**：可选在画图前暂停 IndexTTS，画完再拉起，避免 8G 显卡爆显存
- **多工作流切换**：配好 `workflow_dir` 后用 `/工作流` 列出、切换
- **出图直发**：生成完直接作为图片消息发出

## 安装

1. 启动 ComfyUI（默认 `http://127.0.0.1:8188`）
2. 目录放到 `astrbot/data/plugins/`，无第三方依赖（HTTP 走标准库）
3. 配置里填 `comfyui_url`、`workflow`（json 绝对路径）、`workflow_dir`（可选）
4. 重启 AstrBot

## 配置

| 键 | 说明 |
| --- | --- |
| `comfyui_url` | ComfyUI 地址 |
| `workflow` | 工作流 json 路径，可填绝对路径或 `workflow_dir` 下的文件名 |
| `workflow_dir` | 工作流目录，填了才能用 `/工作流` 切换 |
| `negative` | 负向提示词 |
| `timeout_seconds` | 出图超时，默认 600 秒 |
| `vram_switch` + `tts_dir` + `tts_python` | 画图前暂停 / 之后恢复 IndexTTS |
| `translate` + `api_base` + `api_key` + `translate_model` | 中文提示词翻译，`api_key` 留空则不翻 |

## 指令

| 指令 | 说明 |
| --- | --- |
| 自然语言「画……」 | 直接出图 |
| `/工作流` | 列出可用工作流 |
| `/工作流 <名字>` | 切换当前工作流 |

## 常见问题

**报「还没配置工作流」**
`workflow` 为空。填上 json 路径，或用 `/工作流` 选一个。

**一直卡住然后超时**
ComfyUI 那条队列被别的任务占了，或显存不够在反复重试。先看 ComfyUI 控制台，再考虑打开 `vram_switch`。

**出图是空白的**
工作流里 Simple String 节点顺序不对：第一个必须是正向提示词，第二个是负向。

## 许可

MIT，见 [LICENSE](LICENSE)。
