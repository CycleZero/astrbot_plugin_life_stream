# 生活流 · Life Stream

让 Bot **按自己的日程过日子**，并在合适的时间主动开口。

它读取 [`astrbot_plugin_life_scheduler`](https://github.com/muyouzhi6/astrbot_plugin_life_scheduler) 生成的当日日程（时间线），
到点后用当前人格生成一条很短的消息，发到你指定的会话。

不是「定时打卡」，是「她在过今天」：

- 日程写 `19:00-19:40 晚饭，白米饭为主`，她可能 19:12 才想起来说一句「刚扒了口饭 白米饭yyds」
- 不是每个时间点都汇报——按概率跳过，剩下那些才是真的
- 23:30 之后她睡了，不会吵你

## 依赖

| 依赖 | 说明 |
|---|---|
| AstrBot | `>=4.16,<5` |
| `astrbot_plugin_life_scheduler` | **必需**，提供当日日程。没装时插件会安静降级（不报错、不发消息） |
| 一个可用的对话模型 | 用于生成消息，建议已在 AstrBot 里配好 |

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/CycleZero/astrbot_plugin_life_stream
```

重启 AstrBot，或在 WebUI 插件页点「重载插件」。

## 快速开始

1. 确保 `astrbot_plugin_life_scheduler` 已经装好，并且今天的日程已生成（默认每天 07:00 生成）
2. 在你要接收消息的会话里发：

```
/生活流 绑定
```

3. 先看看效果：

```
/生活流 现在
```

她会立刻按当前时段生成一条并发送，同时把生成内容回给你看。

4. 满意就放着，之后她会在日程时间点自己开口。

## 命令

| 命令 | 说明 |
|---|---|
| `/生活流` | 查看状态（开关、目标会话、今日已发、今日日程摘要、接下来的时间点） |
| `/生活流 绑定` | 把当前会话加进目标列表 |
| `/生活流 解绑` | 从目标列表移除当前会话 |
| `/生活流 现在` | 立即按当前时段生成一条并发送（调试用） |
| `/生活流 重置` | 清空今天的触发记录 |
| `/生活流 开启` / `/生活流 关闭` | 开关 |

所有命令仅管理员可用。

## 配置

在 WebUI 插件配置页可以调整：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `target_umos` | `[]` | 目标会话列表（unified_msg_origin） |
| `quiet_hours` | `23:30-08:00` | 免打扰时段，支持跨零点 |
| `daily_limit` | `6` | 每日最多主动条数 |
| `send_probability` | `0.6` | 每个时间点的发送概率 |
| `max_delay_minutes` | `25` | 时间点后的最大随机延迟 |
| `window_minutes` | `10` | 触发窗口 |
| `check_interval_seconds` | `60` | 检查间隔 |
| `provider_id` | 空 | 指定生成用的模型，留空用默认对话模型 |
| `llm_system_prompt` | 内置 | 生成消息的系统提示词（会自动拼接当前默认人格） |
| `message_prompt` | 内置 | 提示词模板，可用 `{time}` `{activity}` `{date}` |

## 它是怎么判断的

```
每 60 秒检查一次
  ↓
今天还没有被触发的时间点里，找「开始时间 + 随机延迟 ≤ 现在 ≤ 开始 + 窗口 + 延迟」的
  ↓
按 send_probability 决定这次说不说
  ↓
取当前默认人格 + 日程内容 → 让模型生成一句 ≤30 字的话
  ↓
发送到 target_umos，并记进 state（重启不重复）
```

随机延迟和跳过都用 `日期 + 时间点 + 内容` 做哈希种子，**同一天内结果稳定**，
不会因为反复检查而抖动；换一天自然换一套。

## 数据

状态保存在 AstrBot 的 `data` 目录下：`life_stream_state.json`（每天各时间点的触发与发送计数）。
删掉它等于重置所有记录。

## 常见问题

**Q：装好了但一直不说话？**
按顺序检查：
1. `/生活流` 看「今日日程」是不是「未获取到」——是的话说明 `life_scheduler` 今天还没生成
2. 看「目标会话」是不是 0 个——是的话先 `/生活流 绑定`
3. 看「今日已发」是否已经到上限
4. 看当前时间是不是在免打扰时段

**Q：一天只发了一两条，是不是坏了？**
不是。`send_probability` 默认 0.6，日程里 8 个时间点大约会发 4~5 条；再加上免打扰和上限，实际更少。想更活跃就把概率调到 0.9、上限调到 10。

**Q：能不能顺便发 QQ 空间？**
本插件只管聊天消息。空间说说用 [`astrbot_plugin_qzone_ultra`](https://github.com/diaomin66/astrbot_plugin_qzone_ultra) 的 life_publish，它读的是同一份日程。

## 灵感来源

- 日程结构与 `get_life_context()` 接口来自 [astrbot_plugin_life_scheduler](https://github.com/muyouzhi6/astrbot_plugin_life_scheduler)（木有知）
- 「主动消息 + 免打扰 + 上下文感知」的产品思路参考 [astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat)（DBJD-CR）
- 「按日程发生活说说」的联动方式参考 [astrbot_plugin_qzone_ultra](https://github.com/diaomin66/astrbot_plugin_qzone_ultra)（diaomin66）

以上项目均为独立实现，本插件未复制其代码。

## License

MIT
