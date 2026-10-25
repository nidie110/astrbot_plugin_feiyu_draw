import asyncio
import json
import re
import uuid
from pathlib import Path

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parent))
_sys.modules.pop("_feiyu_kit", None)  # 顶层模块名，重载插件时先清缓存再导，避免用到旧字节
import _feiyu_kit as kit  # noqa: E402

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_feiyu_draw"

# 机器相关的路径和密钥都不写进代码：走插件配置（_conf_schema.json）或 plugin_data/config.json。
DEFAULT_CONFIG = {
    # ComfyUI 服务地址
    "comfyui_url": "http://127.0.0.1:8188",
    # 当前使用的工作流 json：绝对路径，或相对于 workflow_dir 的文件名
    "workflow": "",
    # 工作流目录（/工作流 指令在这里列文件、切换）
    "workflow_dir": "",
    "negative": "lowres, bad anatomy, bad hands, watermark, text, blurry, worst quality",
    "timeout_seconds": 600,
    # 画图前先停 IndexTTS 腾显存，画完再拉起来（两个都吃显存时再开）
    "vram_switch": True,
    "tts_dir": "",
    "tts_python": "",
    # 中文提示词先翻成英文（走 OpenAI 兼容接口，key 留空则不翻译）
    "translate": True,
    "api_base": "https://api.deepseek.com",
    "api_key": "",
    "translate_model": "deepseek-chat",
}

# 「画一只猫」「帮我画个…」「生成一张…」「来张图」
DRAW_RE = re.compile(
    r"^\s*(?:帮我|给我|替我|请)?\s*(?:画|绘)(?:一?(?:张|个|幅|只|条|位))?\s*[：:，,]?\s*(.+)$"
    r"|^\s*(?:生成|来)\s*(?:一?(?:张|个|幅))\s*(?:图|图片|插画)?\s*[：:，,]?\s*(.+)$",
    re.S,
)


class FeiyuDraw(Star):
    """大肥鱼·画画：说「画一只猫」就调 ComfyUI 工作流直接出图。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.plugin_cfg = dict(config or {})
        self.data_dir = Path("data/plugin_data") / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.data_dir / "config.json"
        self.cfg = self._load_cfg()

    def _load_cfg(self) -> dict:
        cfg = kit.merge_config(DEFAULT_CONFIG, self.plugin_cfg, self.cfg_path)
        if not self.cfg_path.exists():
            kit.save_json(self.cfg_path, DEFAULT_CONFIG, keep=0)
        return cfg

    # ---------- ComfyUI ----------

    def _build_prompt(self, text: str) -> dict:
        wf_path = str(self.cfg.get("workflow") or "")
        if not wf_path:
            raise RuntimeError("还没配置工作流：先在插件配置里填 workflow，或用 /工作流 选一个")
        wf = json.loads(Path(wf_path).read_text(encoding="utf-8"))
        strings = [k for k, v in wf.items() if v.get("class_type") == "Simple String"]
        strings.sort(key=lambda x: int(x) if str(x).isdigit() else 0)
        if not strings:
            raise RuntimeError("工作流里没有 Simple String 节点")
        wf[strings[0]]["inputs"]["string"] = text
        if len(strings) > 1:
            wf[strings[1]]["inputs"]["string"] = self.cfg.get("negative", "")
        return wf

    # ---------- 显存腾挪：画图前关掉 IndexTTS，画完再拉起来 ----------

    def _port_open(self, port: int) -> bool:
        return kit.port_open(port)

    def _stop_index_tts(self) -> None:
        import subprocess, time
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="ignore", timeout=30).stdout
        except Exception as e:
            logger.warning(f"[feiyu_draw] 查端口失败: {e}")
            return
        pids = set()
        for line in out.splitlines():
            if ":7860" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pids.add(parts[-1])
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=20)
                logger.info(f"[feiyu_draw] 已停 IndexTTS (PID {pid})，腾显存")
            except Exception as e:
                logger.warning(f"[feiyu_draw] 停 IndexTTS 失败 {pid}: {e}")
        for _ in range(30):
            if not self._port_open(7860):
                break
            time.sleep(0.5)

    def _start_index_tts(self) -> None:
        import subprocess, time
        if self._port_open(7860):
            return
        py = self.cfg.get("tts_python", "")
        d = self.cfg.get("tts_dir", "")
        if not py or not d:
            return
        try:
            subprocess.Popen(
                [py, "webui.py", "--port", "7860", "--host", "127.0.0.1", "--model_dir", "checkpoints"],
                cwd=d,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x00000008,  # DETACHED_PROCESS
            )
            logger.info("[feiyu_draw] 正在重新拉起 IndexTTS")
            for _ in range(180):
                if self._port_open(7860):
                    logger.info("[feiyu_draw] IndexTTS 已恢复")
                    return
                time.sleep(1)
        except Exception as e:
            logger.error(f"[feiyu_draw] 重启 IndexTTS 失败: {e}")

    def _generate_with_vram(self, prompt: str) -> str:
        import threading
        switched = bool(self.cfg.get("vram_switch", True))
        if switched:
            self._stop_index_tts()
        try:
            return self._generate(prompt)
        finally:
            if switched:
                threading.Thread(target=self._start_index_tts, daemon=True).start()

    def _generate(self, prompt: str) -> str:
        import time
        import urllib.request

        base = self.cfg["comfyui_url"].rstrip("/")
        logger.info(f"[feiyu_draw] 开始生成: {prompt[:40]}")
        prompt = self._to_english(prompt)
        wf = self._build_prompt(prompt)

        req = urllib.request.Request(
            base + "/prompt",
            data=json.dumps({"prompt": wf, "client_id": "astrbot-feiyu-draw"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        pid = json.loads(opener.open(req, timeout=60).read())["prompt_id"]

        deadline = time.time() + int(self.cfg.get("timeout_seconds", 180))
        images = []
        while time.time() < deadline:
            h = json.loads(opener.open(f"{base}/history/{pid}", timeout=30).read())
            if pid in h:
                for node in h[pid].get("outputs", {}).values():
                    images.extend(node.get("images", []))
                break
            time.sleep(2)
        if not images:
            raise RuntimeError("ComfyUI 没有返回图片")

        img = images[0]
        url = f"{base}/view?filename={img['filename']}&subfolder={img.get('subfolder','')}&type={img.get('type','output')}"
        out = self.data_dir / "images" / f"d_{uuid.uuid4().hex[:8]}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        with opener.open(url, timeout=60) as r, open(out, "wb") as f:
            f.write(r.read())
        return str(out)

    def _workflow_uses_zimage(self) -> bool:
        try:
            txt = Path(self.cfg["workflow"]).read_text(encoding="utf-8").lower()
            return "z_image" in txt or "zimage" in txt
        except Exception:
            return False

    def _to_english(self, text: str) -> str:
        """SD1.5 的文本编码器只认英文，中文提示词先翻译成英文 tag。"""
        if not self.cfg.get("translate", True):
            return text
        if self._workflow_uses_zimage():
            return text
        if not re.search(r"[\u4e00-\u9fff]", text):
            return text
        if not self.cfg.get("api_key"):
            logger.info("[feiyu_draw] 没配 api_key，跳过翻译")
            return text
        import urllib.request

        payload = {
            "model": self.cfg.get("translate_model", "deepseek-chat"),
            "messages": [
                {"role": "system", "content": "Translate the user's image description into concise English danbooru-style tags, comma separated. Output tags only, no explanation."},
                {"role": "user", "content": text},
            ],
            "temperature": 0.3,
        }
        req = urllib.request.Request(
            self.cfg.get("api_base", "https://api.deepseek.com").rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.cfg.get("api_key", ""),
            },
        )
        logger.info("[feiyu_draw] 翻译请求发出")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            r = json.loads(opener.open(req, timeout=15).read())
            out = r["choices"][0]["message"]["content"].strip()
            logger.info(f"[feiyu_draw] 提示词翻译: {text} -> {out[:80]}")
            return out or text
        except Exception as e:
            logger.warning(f"[feiyu_draw] 翻译失败，直接用原文: {e}")
            return text

    # ---------- 工作流管理 ----------

    @filter.command("工作流")
    async def cmd_workflow(self, event: AstrMessageEvent):
        """查看/切换工作流：/工作流 或 /工作流 文件名"""
        raw = (event.message_str or "").strip()
        parts = raw.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        wdir = Path(self.cfg.get("workflow_dir", ""))
        if not wdir.exists():
            yield event.plain_result(f"工作流目录不存在：{wdir}")
            return
        files = sorted([f.name for f in wdir.glob("*.json")])
        if not files:
            yield event.plain_result("目录里没有工作流 json")
            return

        active = Path(self.cfg.get("workflow", "")).name
        if not arg:
            lines = [f"{'▶' if f == active else '　'} {f[:-5]}" for f in files]
            yield event.plain_result("可用工作流：\n" + "\n".join(lines) + "\n\n切换：/工作流 名字")
            return

        match = None
        for f in files:
            if f == arg or f[:-5] == arg:
                match = f
                break
        if match is None:
            low = arg.lower()
            for f in files:
                if low in f.lower():
                    match = f
                    break
        if match is None:
            yield event.plain_result("没找到这个工作流，发 /工作流 看列表")
            return

        self.cfg["workflow"] = str(wdir / match)
        self.cfg_path.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        extra = "（Z-Image，中文直接用，不翻译）" if self._workflow_uses_zimage() else "（会先把中文翻成英文 tag）"
        yield event.plain_result(f"已切换：{match[:-5]} {extra}")

    # ---------- 触发 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_draw(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if text.startswith("/"):
            return
        if text.startswith("画着") or text.endswith("着呢…"):
            return
        m = DRAW_RE.match(text)
        if not m:
            return
        payload = (m.group(1) or m.group(2) or "").strip(" ：:，,。")
        if len(payload) < 1:
            return
        # 过滤掉"画画"这种没有具体内容的
        if payload in ("画", "画图", "图"):
            return

        event.stop_event()
        logger.info(f"[feiyu_draw] 触发画图: {payload[:50]}")
        # 用 event.send 直接发进度，避免被别的插件截断 result 链
        try:
            await event.send(MessageChain([Comp.Plain("画着呢…")]))
        except Exception as e:
            logger.warning(f"[feiyu_draw] 进度消息发送失败: {e}")
        try:
            path = await asyncio.to_thread(self._generate_with_vram, payload)
        except Exception as e:
            logger.error(f"[feiyu_draw] 生成失败: {e}")
            yield event.plain_result(f"没画出来：{type(e).__name__}")
            return
        img = Comp.Image.fromFileSystem(path)
        # 先留一句文字进对话历史，让“我画过这张图”被记住
        yield event.plain_result(f"画好了：{payload[:40]}")
        try:
            await event.send(MessageChain([img]))
            logger.info("[feiyu_draw] 图片已直接发送")
        except Exception as e:
            logger.error(f"[feiyu_draw] 直接发送失败，回落到 result 链: {e}")
            yield event.chain_result([img])
