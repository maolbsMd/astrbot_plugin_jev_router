# astrbot_plugin_jev_router

基于 **TypeSafe Jev** 的多代理意图路由插件（放行式）—— 仅覆写本轮的模型 / 人设 / 工具白名单，AstrBot 主流程、会话历史归档与记忆注入全程与原生完全一致。

## ✨ 特性

- **意图自动路由**：每条消息进入时，调用 Jev SystemOne 接口判定最匹配的子代理
- **放行式架构**：不拦截、不接管主流程，只通过 `event.set_extra` 与 `on_llm_request` 钩子覆写本轮请求参数，记忆插件、历史归档均感知到统一会话上下文
- **动态模型切换**：命中代理后通过 `selected_provider` 无缝切换本轮 LLM Provider
- **人设追加模式**：子代理的 persona / system_prompt 以**追加**方式拼在原 system_prompt 之后，绝不清空原有人设与记忆注入
- **工具白名单**：支持按子代理限制可用插件工具（原生 `select_plugin_set` 多选器），留空或包含 `*` 即全量放行
- **上下文感知判定**：可选将近期对话摘要与上一轮命中的代理名喂给 Jev，提升「改一下」「继续」这类续聊输入的路由准确率
- **API 地址自动补全**：填域名、`/v1` 或完整 endpoint 均可，自动补全为 `/v1/systemone`
- **优雅降级**：Jev 请求失败、置信度低于阈值或未命中代理时，本轮自动回落默认主流程

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
| `api_url` | Jev 请求地址，支持反代/转发网关，自动补全路径 | `https://api.typesafe.ai/v1/systemone` |
| `jev_model` | Jev 判定模型版本 | `jev-latest` |
| `min_confidence` | 最低置信度（0~1），低于此值不切换代理 | `0.55` |
| `share_context_with_jev` | 判定时附带近期对话摘要 | `true` |
| `agents` | 子代理定义列表（见下） | `[]` |

### 子代理（agents）字段

| 字段 | 说明 |
| --- | --- |
| `name` | 代理唯一标识，作为 Jev choice 的选项名，如 `coder` / `math_tutor` |
| `desc` | 意图描述，喂给 Jev 判定，**各代理之间越互斥越准** |
| `persona_id` | 绑定 AstrBot 已有人设（留空沿用当前会话人设） |
| `provider_id` | 绑定模型 Provider（留空沿用当前会话模型） |
| `system_prompt` | 该代理专属追加指令（追加在人设与记忆之后生效） |
| `tools` | 可用插件白名单，默认 `["*"]` 全量放行 |

## 🔀 工作流程

```
用户消息
   │
   ▼
[on_message_route]  组装 state（近期对话 + 上轮代理 + 当前输入）
   │                调用 Jev SystemOne 判定意图
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
进入 AstrBot 原生 LLM 请求流程（记忆注入、历史归档照常）
```

## 📝 注意事项

- Jev 官方协议要求 `choice` 类型问题必须携带 `criteria` 字典映射，本插件已自动由 `name -> desc` 构建
- 插件的 `on_llm_request` 钩子使用 `-100` 低优先级，确保在记忆插件注入**之后**再追加人设，互不覆盖
- `_last_agent` 仅作为判定提示，不做会话粘性，每轮独立判定

## 👥 作者

maolbsMd & 牧奈

## 📄 License

MIT
