# 内测指令门禁（NapCat / OneBot）

用配置中的内测 QQ 群成员身份保护指定 AstrBot 指令。该插件只做**前置放行/阻断**：不执行目标命令、不管理配额、也不判断目标插件是否成功。

## 机制

收到受保护命令后，本插件以高优先级检查调用者是否仍在任一 `beta_group_ids` 群中。查询使用 OneBot 11 `get_group_member_info`。未找到成员、Bot 不在资格群、接口失败、配置为空时均**拒绝放行**（fail closed），并调用 `event.stop_event()`。

默认静默拒绝，避免在群里产生文字。

## 配置示例

```json
{
  "enabled": true,
  "beta_group_ids": ["123456789"],
  "protected_commands": ["群分析", "group_analysis"],
  "membership_cache_seconds": 300,
  "deny_mode": "silent"
}
```

## 限制

- 第一版只面向 NapCat / OneBot (`aiocqhttp`)；不支持 QQ 官方机器人跨群成员资格查询。
- Bot 必须在至少一个资格群中；否则所有受保护命令都会被拒绝。
- 成员资格最多在 `membership_cache_seconds` 后更新；设为 `0` 可实时检查，但会增加 OneBot API 调用。
- 多 Bot 场景中，插件只使用**触发该消息的平台实例**；无法唯一识别实例时拒绝放行，不会错误选用别的 Bot。
