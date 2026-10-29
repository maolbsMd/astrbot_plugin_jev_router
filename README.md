# astrbot_plugin_jev_router

基于 **TypeSafe Jev** 的多代理意图路由插件（放行式）—— 仅覆写本轮的模型 / 人设 / 工具白名单，AstrBot 主流程、会话历史归档与记忆注入全程与原生完全一致。

## ✨ 特性

- **意图自动路由**：只有**需要 bot 回复**的消息才调用 Jev（私聊全判；群聊仅 @bot / 引用 bot / 唤醒前缀）
- **放行式架构**：不拦截、不接管主流程，只通过 `event.set_extra` 与 `on_llm_request` 钩子覆写本轮请求参数，记忆插件、历史归档均感知到统一会话上下文
- **动态模型切换 + 失败兜底**：命中代理后通过 `selected_provider` 切到本轮模型；**首选调用失败（429/超时/5xx）时按 `fallback_provider_ids` 顺序重试**，成功则用兜底结果替换错误回复
- **人设追加模式**：子代理的 persona / system_prompt 以**追加**方式拼在原 system_prompt 之后，绝不清空原有人设与记忆注入
- **工具白名单（fail-closed）**：按插件名或精确工具名限制可用工具；`*` 才全放行，留空表示**不给任何工具**
- **上下文感知判定**：可选把近期对话原文与上一轮命中的代理名喂给 Jev，提升「改一下」「继续」这类续聊输入的路由准确率（默认关闭，见「隐私」）
- **API 地址自动补全**：填域名、`/v1` 或完整 endpoint 均可，自动补全为 `/v1/systemone`
- **优雅降级**：Jev 请求失败、置信度低于阈值或未命中代理时，本轮自动回落默认主流程；连续失败会熔断 60 秒

## 📦 安装

将本仓库克隆或解压到 AstrBot 的插件目录：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/maolbsMd/astrbot_plugin_jev_router.git
```

随后在 AstrBot WebUI 的插件管理页启用并配置。

## ⚙️ 配置项

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `typesafe_api_key` | TypeSafe (Jev) API Key，在 api.typesafe.ai 获取 | 空 |
| `api_url` | Jev 请求地址，支持反代/转发网关，自动补全路径；非本机地址的 `http://` 会被强制升级为 https | `https://api.typesafe.ai/v1/systemone` |
| `allow_insecure_http` | 允许明文 http（仅内网网关且网络可信时开启） | `false` |
| `jev_model` | Jev 判定模型版本 | `jev-latest` |
| `jev_timeout` | 单次判定超时（秒，0.5~10） | `3.0` |
| `min_confidence` | 最低置信度（0~1），低于此值不切换代理 | `0.55` |
| `share_context_with_jev` | 判定时是否附带**近期对话原文**（会发送到 `api_url` 指向的第三方服务） | `false` |
| `skip_commands` | 以 `/` 开头的指令消息不送判定 | `true` |
| `only_wake_in_group` | 群聊只有 @bot / 引用 bot / 唤醒前缀的消息才判定；私聊始终判定 | `true` |
| `history_rounds` | 附带多少轮历史对话；`-1` 表示不限，但最多取 50 轮 | `2` |
| `agents` | 子代理定义列表（见下） | `[]` |

### 子代理（agents）字段

| 字段 | 说明 |
| --- | --- |
| `name` | 代理唯一标识，作为 Jev choice 的选项名，如 `coder` / `math_tutor`；重名只保留第一个 |
| `desc` | 意图描述，喂给 Jev 判定，**各代理之间越互斥越准** |
| `persona_id` | 绑定 AstrBot 已有人设（留空沿用当前会话人设） |
| `provider_id` | 绑定模型 Provider（留空沿用当前会话模型） |
| `fallback_provider_ids` | 兜底模型列表：首选**调用失败**时按顺序重试；首选不存在/被删除时也会顶上；全不可用则沿用当前会话模型 |
| `system_prompt` | 该代理专属追加指令（追加在人设与记忆之后生效） |
| `tools` | 可用插件白名单：`*` = 全部放行；勾选插件 = 只保留该插件注册的工具；留空 = 不给任何工具 |

> `tools` 同时接受精确工具名（如 `web_search`）和插件名（如 `astrbot_plugin_search`）。
> 白名单是 **fail-closed** 的：没有归属信息、无法解析到所属插件的工具（MCP、subagent 等）在白名单模式下会被一并裁掉。

## 🔀 工作流程

```
用户消息
   │
   ▼
需要 bot 回复？（私聊 / 群聊被 @、被引用、唤醒前缀）
   │ 否                          │ 是
   ▼                             ▼
走默认主流程              [on_message_route]  跳过 / 开头指令、熔断期内直接放行
                                 │                组装 state（近期对话 + 上轮代理 + 当前输入）
                                 │                调用 Jev SystemOne 判定意图（限并发）
                                 ▼
                          命中代理且置信度 ≥ 阈值？
                                 │ 是                          │ 否
                                 ▼                             ▼
                          写入 event extra:              走默认主流程
                            selected_provider（切模型）
                            jev_router_agent_name
                                 │
                                 ▼
                          [on_llm_request]  追加子代理 persona / system_prompt
                                            按白名单裁剪 func_tool.tools
                                 │
                                 ▼
                          进入 AstrBot 原生 LLM 请求流程
                                 │
                                 ▼
                          provider 调用失败（role=err）？
                                 │ 是                          │ 否
                                 ▼                             ▼
                          [on_llm_response] 按 fallback 顺序重试    正常返回
                          成功则用兜底结果替换错误回复
```

## 🔒 隐私与安全

- **对话出站**：`share_context_with_jev` 默认**关闭**，开启后发送的是近期对话**原文**（不是摘要）；群聊里 `conv.history` 是整群共享的，可能包含其他人的消息与工具结果，请确认接收方可信
- **API Key**：`typesafe_api_key` 会以 `Authorization: Bearer` 发往 `api_url`；非本机地址的 `http://` 会被强制改成 https（除非显式开启 `allow_insecure_http`），避免明文传输
- **工具边界**：白名单裁剪的是本轮 `func_tool`，不会影响其他请求；如果开启了 AstrBot 的 `computer_use_runtime: local`，`local_execute_shell` / `local_python` / 文件读写等内建工具也会被白名单一并裁剪
- **成本与可用性**：只对需要回复的消息判定，每条消息最多一次 Jev 调用，并发受限（默认 4），连续失败 3 次熔断 60 秒，超长输入按 6000 字符截断
- **失败兜底**：`fallback_provider_ids` 只在首选**实际调用失败**（或首选不存在）时生效；兜底回复不带工具调用，只保证用户拿到回答

## 📝 注意事项

- Jev 官方协议要求 `choice` 类型问题必须携带 `criteria` 字典映射，本插件已自动由 `name -> desc` 构建
- 插件的 `on_llm_request` 钩子使用 `-100` 低优先级，确保在记忆插件注入**之后**再追加人设，互不覆盖
- `_last_agent` 仅作为判定提示，不做会话粘性，每轮独立判定（最多缓存 500 个会话）
- 同一个模型人设 prompt 等保持一致，所以缓存命中率对每个模型并没有影响，但要避免短时间使用太多不同代理

## 📋 更新日志

### 1.4.0

- 群聊只判定需要回复的消息（@bot / 引用 bot / 唤醒前缀），私聊照旧全判；新增 `only_wake_in_group`，默认开启
- 兜底语义改为「首选**调用失败**后重试」：新增 `on_llm_response` 钩子，按 `fallback_provider_ids` 顺序重试；成功则替换错误回复并修正本轮历史
- 首选不存在/被删除时的预检回退保留，作为兜底链的第一层
- `provider_request` 缺失或未命中代理时不触发兜底，避免影响其他插件的错误处理

### 1.3.0

- 修复工具白名单：改为按「精确工具名 / 工具所属插件名」匹配（此前用不存在的 `plugin_id` 字段，勾选插件会误删全部工具；子串匹配又会误放行）
- 工具白名单改为 fail-closed：`*` 才全放行，留空表示不给工具
- 新增限流与熔断：并发上限、连续失败熔断、`jev_timeout` 可配
- 以 `/` 开头的指令不再送 Jev 判定（`skip_commands`）
- `share_context_with_jev` 默认改为 `false`，文案更正为「原文」
- `history_rounds=-1` 限制为最多 50 轮；state 超长截断
- `min_confidence` / `jev_timeout` 解析加保护，NaN 与百分制置信度归一化
- `ClientSession` 创建加锁，避免并发首条消息重复建连接池泄漏
- `api_url` 非本机的明文 http 强制升级 https
- 移除对 `astrbot.core.agent.message` 的依赖，历史改为直接读 dict
- 版本号改为 semver；图标改为 `logo.png`；补充 `requirements.txt`；`astrbot_version` 改为 `>=4.5.0`

### 1.2.3

- 新增 `fallback_provider_ids`：首选 Provider 失效时按顺序回退，全不可用则沿用会话模型

## 👥 作者

maolbsMd & 牧奈

## 📄 License

MIT
