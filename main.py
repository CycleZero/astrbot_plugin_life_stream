# -*- coding: utf-8 -*-
"""生活流 Life Stream —— 让 Bot 按自己的日程"过日子"并主动开口。

它读取 `astrbot_plugin_life_scheduler` 生成的当日日程（时间线），在合适的
时间点用当前人格生成一条很短的主动消息，发到配置好的会话里。

设计要点：
* 不是每个时间点都发 —— 按概率跳过，避免像打卡机器人；
* 时间点后随机延迟，像"她过一会儿才想起来跟你说"；
* 免打扰时段 + 每日上限，防止刷屏；
* 已触发的时间点持久化，重启不会重复；
* 找不到 life_scheduler 时安静降级，不影响 AstrBot 启动。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

# "19:00-19:40 晚饭，白米饭为主" / "23:30 睡觉" 这类日程行
SLOT_RE = re.compile(
    r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*"
    r"(?:[-–—~～至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*)?"
    r"(.*)$"
)

DEFAULT_SYSTEM_PROMPT = (
    "你正在用手机 QQ 和人聊天。下面会给出一条你此刻正在做的事，"
    "请用你自己的口吻发一条很短的消息。\n"
    "要求：只输出消息本身，一到两句，不超过 30 字；"
    "不要解释、不要引号、不要 Markdown、不要写「摘要」或「补充」。"
)

DEFAULT_MESSAGE_PROMPT = (
    "现在是 {time}，你正在：{activity}\n"
    "请按上面这条状态，给对方发一条很短的消息。"
)


class LifeStreamPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.state_file = self.data_dir / "life_stream_state.json"
        self._loop_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop(), name="life_stream_loop")
        logger.info(
            "[生活流] 已启动：enabled=%s 目标会话=%d 每日上限=%s",
            self._cfg("enabled", True),
            len(self._cfg("target_umos", []) or []),
            self._cfg("daily_limit", 6),
        )

    async def terminate(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._loop_task = None
        logger.info("[生活流] 已停止")

    # ------------------------------------------------------------------ 配置/状态
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001 - 配置对象异常时不影响主流程
            return default
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return bool(self._cfg(key, default))

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _load_state(self) -> dict:
        if not self.state_file.exists():
            return {}
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[生活流] 读取状态失败：%s", exc)
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
            tmp.replace(self.state_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[生活流] 写入状态失败：%s", exc)

    def _today_state(self, state: dict, today: str) -> dict:
        day = state.get(today)
        if not isinstance(day, dict):
            day = {}
            state[today] = day
        day.setdefault("fired", [])
        day.setdefault("sent", 0)
        return day

    # ------------------------------------------------------------------ 日程来源
    def _life_plugin(self) -> Any | None:
        for name in (
            "astrbot_plugin_life_scheduler",
            "life_scheduler",
            "LifeSchedulerPlugin",
        ):
            try:
                metadata = self.context.get_registered_star(name)
            except Exception:  # noqa: BLE001
                continue
            if metadata is None:
                continue
            for attr in ("star_cls", "star", "instance", "plugin", "plugin_instance"):
                instance = getattr(metadata, attr, None)
                if instance is not None and hasattr(instance, "get_life_context"):
                    return instance
        return None

    async def _fetch_schedule_text(self) -> str:
        """取当日日程文本；没有 life_scheduler 或没有日程时返回空串。"""
        plugin = self._life_plugin()
        if plugin is None:
            return ""
        try:
            data = plugin.get_life_context()
            if asyncio.iscoroutine(data):
                data = await data
        except Exception as exc:  # noqa: BLE001
            logger.debug("[生活流] 取日程失败：%s", exc)
            return ""
        if not isinstance(data, dict):
            return ""

        schedule = str(data.get("schedule") or "").strip()
        if schedule:
            return schedule

        timeline = data.get("timeline")
        if isinstance(timeline, list):
            lines = []
            for item in timeline:
                if isinstance(item, dict):
                    when = str(item.get("time") or item.get("at") or "").strip()
                    what = str(item.get("content") or item.get("activity") or "").strip()
                    if what:
                        lines.append(f"{when} {what}".strip())
                elif item:
                    lines.append(str(item))
            return "\n".join(lines)
        return ""

    @staticmethod
    def parse_slots(schedule_text: str) -> list[dict]:
        slots: list[dict] = []
        for raw_line in (schedule_text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = SLOT_RE.match(line)
            if not match:
                continue
            sh, sm, eh, em, activity = match.groups()
            try:
                start = dtime(int(sh), int(sm))
            except ValueError:
                continue
            end = None
            if eh is not None and em is not None:
                try:
                    end = dtime(int(eh), int(em))
                except ValueError:
                    end = None
            activity = (activity or "").strip(" -—:：\t")
            if not activity:
                continue
            slots.append(
                {
                    "id": start.strftime("%H:%M"),
                    "start": start,
                    "end": end,
                    "activity": activity,
                }
            )
        slots.sort(key=lambda item: item["start"])
        return slots

    # ------------------------------------------------------------------ 时间工具
    @staticmethod
    def _parse_hhmm(text: str) -> dtime | None:
        match = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$", text or "")
        if not match:
            return None
        try:
            return dtime(int(match.group(1)), int(match.group(2)))
        except ValueError:
            return None

    def _in_quiet_hours(self, now: dtime) -> bool:
        span = str(self._cfg("quiet_hours", "23:30-08:00") or "").strip()
        if not span or "-" not in span:
            return False
        left, _, right = span.partition("-")
        start = self._parse_hhmm(left)
        end = self._parse_hhmm(right)
        if start is None or end is None:
            return False
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # 跨零点

    @staticmethod
    def _pick(seed: str, modulo: int) -> int:
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % max(1, modulo)

    # ------------------------------------------------------------------ 主循环
    async def _loop(self) -> None:
        interval = max(15, self._cfg_int("check_interval_seconds", 60))
        # 启动错峰，避免和别的插件同时打 LLM
        await asyncio.sleep(min(20, interval))
        while not self._stop.is_set():
            try:
                if self._cfg_bool("enabled", True):
                    await self._tick()
            except Exception as exc:  # noqa: BLE001 - 循环不能因单次异常退出
                logger.warning("[生活流] 本轮检查失败：%s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        targets = [str(item) for item in (self._cfg("target_umos", []) or []) if item]
        if not targets:
            return

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        state = self._load_state()
        day = self._today_state(state, today)

        if day["sent"] >= max(1, self._cfg_int("daily_limit", 6)):
            return
        if self._in_quiet_hours(now.time()):
            return

        schedule_text = await self._fetch_schedule_text()
        if not schedule_text:
            return
        slots = self.parse_slots(schedule_text)
        if not slots:
            return

        window = max(1, self._cfg_int("window_minutes", 10))
        max_delay = max(0, self._cfg_int("max_delay_minutes", 25))
        probability = min(1.0, max(0.0, self._cfg_float("send_probability", 0.6)))
        changed = False

        for slot in slots:
            key = f"{slot['id']}"
            if key in day["fired"]:
                continue
            seed = f"{today}|{slot['id']}|{slot['activity']}"
            delay = self._pick(seed + "|delay", max_delay + 1)
            start_dt = now.replace(
                hour=slot["start"].hour, minute=slot["start"].minute, second=0, microsecond=0
            )
            due = start_dt.timestamp() + delay * 60
            if now.timestamp() < due or now.timestamp() > start_dt.timestamp() + (window + max_delay) * 60:
                continue

            day["fired"].append(key)
            changed = True

            if self._pick(seed + "|skip", 100) >= int(probability * 100):
                logger.debug("[生活流] 按概率跳过 %s %s", slot["id"], slot["activity"])
                continue

            text = await self._compose_message(slot, now)
            if not text:
                continue
            sent = await self._broadcast(text, targets)
            if sent:
                day["sent"] += 1
            if day["sent"] >= max(1, self._cfg_int("daily_limit", 6)):
                break

        if changed:
            self._save_state(state)

    # ------------------------------------------------------------------ 生成/发送
    async def _compose_message(self, slot: dict, now: datetime) -> str:
        provider = None
        provider_id = str(self._cfg("provider_id", "") or "").strip()
        try:
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            provider = provider or self.context.get_using_provider()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[生活流] 选择模型失败：%s", exc)
            return ""
        if provider is None:
            logger.warning("[生活流] 没有可用的对话模型，跳过本次")
            return ""

        persona_prompt = ""
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
            persona_prompt = str(getattr(persona, "system_prompt", "") or "").strip()
        except Exception:  # noqa: BLE001 - 取不到人格就用空
            persona_prompt = ""

        system_prompt = str(self._cfg("llm_system_prompt", DEFAULT_SYSTEM_PROMPT))
        if persona_prompt:
            system_prompt = f"{persona_prompt}\n\n{system_prompt}"

        template = str(self._cfg("message_prompt", DEFAULT_MESSAGE_PROMPT))
        try:
            prompt = template.format(
                time=slot["id"], activity=slot["activity"], date=now.strftime("%Y-%m-%d")
            )
        except Exception:  # noqa: BLE001 - 模板写错时不至于崩
            prompt = f"现在是 {slot['id']}，你正在：{slot['activity']}。"

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                session_id=f"life_stream_{now.strftime('%Y%m%d')}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[生活流] 生成消息失败：%s", exc)
            return ""

        text = str(getattr(resp, "completion_text", "") or "").strip()
        return self._clean_text(text)

    @staticmethod
    def _clean_text(text: str) -> str:
        text = (text or "").strip()
        for prefix in ("消息：", "消息:", "回复：", "回复:"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
        text = text.strip("「」\"'“”")
        text = re.sub(r"\s*\n+\s*", " ", text).strip()
        if len(text) > 120:
            text = text[:120].rstrip() + "…"
        return text

    async def _broadcast(self, text: str, targets: list[str]) -> int:
        try:
            from astrbot.api.event import MessageChain
        except Exception:  # noqa: BLE001
            logger.error("[生活流] 无法导入 MessageChain，发送中止")
            return 0
        sent = 0
        for umo in targets:
            try:
                ok = await self.context.send_message(umo, MessageChain().message(text))
                if ok is False:
                    logger.warning("[生活流] 目标会话不可达：%s", umo)
                else:
                    sent += 1
                    logger.info("[生活流] 已发送到 %s：%s", umo, text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[生活流] 发送到 %s 失败：%s", umo, exc)
        return sent

    # ------------------------------------------------------------------ 命令
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生活流", alias={"life_stream"})
    async def cmd_life_stream(self, event: AstrMessageEvent):
        """查看/控制生活流。用法：生活流 [状态|绑定|解绑|现在|重置|开启|关闭]"""
        raw = str(getattr(event, "message_str", "") or "").strip()
        arg = raw.split(maxsplit=1)[1].strip() if " " in raw else ""
        action = arg.split(maxsplit=1)[0].lower() if arg else "状态"

        if action in ("绑定", "bind"):
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            targets = [str(item) for item in (self._cfg("target_umos", []) or []) if item]
            if not umo:
                yield event.plain_result("拿不到当前会话标识，绑定失败。")
                return
            if umo in targets:
                yield event.plain_result(f"这个会话已经在列表里了：{umo}")
                return
            targets.append(umo)
            self._update_config("target_umos", targets)
            yield event.plain_result(f"已绑定当前会话：{umo}\n现在共 {len(targets)} 个目标。")
            return

        if action in ("解绑", "unbind"):
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            targets = [str(item) for item in (self._cfg("target_umos", []) or []) if item]
            if umo in targets:
                targets.remove(umo)
                self._update_config("target_umos", targets)
                yield event.plain_result(f"已解绑：{umo}")
            else:
                yield event.plain_result("当前会话不在目标列表里。")
            return

        if action in ("开启", "on", "关闭", "off"):
            enabled = action in ("开启", "on")
            self._update_config("enabled", enabled)
            yield event.plain_result(f"生活流已{'开启' if enabled else '关闭'}。")
            return

        if action in ("重置", "reset"):
            today = datetime.now().strftime("%Y-%m-%d")
            state = self._load_state()
            state[today] = {"fired": [], "sent": 0}
            self._save_state(state)
            yield event.plain_result("今天的触发记录已清空。")
            return

        if action in ("现在", "test", "试一条"):
            schedule_text = await self._fetch_schedule_text()
            slots = self.parse_slots(schedule_text)
            now = datetime.now()
            slot = None
            for item in slots:
                if item["start"] <= now.time():
                    slot = item
            if slot is None and slots:
                slot = slots[0]
            if slot is None:
                yield event.plain_result("没拿到今天的日程（life_scheduler 未安装，或今天还没生成）。")
                return
            text = await self._compose_message(slot, now)
            if not text:
                yield event.plain_result("生成失败：没有可用模型，或模型返回为空。")
                return
            targets = [str(item) for item in (self._cfg("target_umos", []) or []) if item]
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if umo and umo not in targets:
                targets = [umo]
            sent = await self._broadcast(text, targets) if targets else 0
            yield event.plain_result(
                f"当前时段：{slot['id']} {slot['activity']}\n生成内容：{text}\n已发送 {sent} 个会话。"
            )
            return

        # 默认：状态
        schedule_text = await self._fetch_schedule_text()
        slots = self.parse_slots(schedule_text)
        today = datetime.now().strftime("%Y-%m-%d")
        day = self._today_state(self._load_state(), today)
        targets = [str(item) for item in (self._cfg("target_umos", []) or []) if item]
        lines = [
            "📋 生活流状态",
            f"开关：{'开启' if self._cfg_bool('enabled', True) else '关闭'}",
            f"目标会话：{len(targets)} 个",
            f"今日已发：{day['sent']} / {self._cfg_int('daily_limit', 6)}",
            f"今日已触发时间点：{', '.join(day['fired']) or '无'}",
            f"免打扰：{self._cfg('quiet_hours', '23:30-08:00')}",
            f"发送概率：{self._cfg_float('send_probability', 0.6):.0%}",
            f"今日日程：{'已获取，%d 个时间点' % len(slots) if slots else '未获取到'}",
        ]
        if slots:
            lines.append("即将到来：")
            now = datetime.now().time()
            upcoming = [slot for slot in slots if slot["start"] >= now][:3]
            if not upcoming:
                lines.append("  今天剩下的都过完了")
            for slot in upcoming:
                lines.append(f"  {slot['id']} {slot['activity'][:24]}")
        lines.append("命令：生活流 [状态|绑定|解绑|现在|重置|开启|关闭]")
        yield event.plain_result("\n".join(lines))

    def _update_config(self, key: str, value: Any) -> None:
        try:
            self.config[key] = value
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[生活流] 保存配置 %s 失败：%s", key, exc)
