import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import register, Star, Context
from astrbot.api import logger
from astrbot.core.agent.message import Message

# event extra keys，避免与其他插件冲突
_K_AGENT = "jev_router_agent_name"      # 本轮命中的代理名
_K_CONF = "jev_router_confidence"       # 本轮判定置信度
_K_PROVIDER_DONE = "jev_router_pid_set"  # provider extra 是否已写入

DEFAULT_JEV_URL = "https://api.typesafe.ai/v1/systemone"


@register("astrbot_plugin_jev_router", "Amadeus & 相合伞",
          "基于 TypeSafe Jev 的意图路由（放行式）：仅覆写本轮模型/人设/工具，"
          "主流程、会话历史与记忆注入归档全程与原生一致", "1.2.2")
class JevRouterPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        # unified_msg_origin -> 上一轮命中的代理名，仅作为 Jev 判定提示，不做粘性
        self._last_agent: dict[str, str] = {}

    async def initialize(self):
        """插件激活时初始化资源"""
        pass

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def terminate(self):
        if self.session and not self.session.closed:
            await self.session.close()

    # ---------- 配置解析 ----------

    def _get_agents(self) -> list[dict]:
        agents = self.config.get("agents", []) or []
        res = []
        for item in agents:
            if not isinstance(item, dict):
                continue
            # template_list 的条目可能直接是 dict，也可能是 {"agent": {...}}
            if "agent" in item and isinstance(item["agent"], dict):
                a = item["agent"]
            else:
                a = item
            if a.get("name"):
                res.append(a)
        return res

    def _resolve_api_url(self) -> str:
        """解析并自动补全 Jev API 地址，兼容填 base_url 或完整 endpoint 的情况"""
        raw = (self.config.get("api_url") or DEFAULT_JEV_URL).strip().rstrip("/")
        if not raw:
            return DEFAULT_JEV_URL
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        # 如果只填了域名 (如 https://api.typesafe.ai) 或 /v1，自动补全 /v1/systemone
        if raw.endswith("/v1"):
            return raw + "/systemone"
        if "/v1/" not in raw and not raw.endswith("/systemone"):
            return raw + "/v1/systemone"
        return raw

    def _resolve_persona_prompt(self, persona_id: str) -> str:
        """按 persona_id 取人设提示词，兼容新版 persona_manager 与旧版配置列表"""
        try:
            pm = getattr(self.context, "persona_manager", None)
            personas = getattr(pm, "personas", None)
            if isinstance(personas, dict) and persona_id in personas:
                p = personas[persona_id]
                prompt = getattr(p, "prompt", None) or getattr(p, "system_prompt", None)
                if prompt:
                    return str(prompt)
            if isinstance(personas, list):
                for p in personas:
                    pid = getattr(p, "persona_id", None) or getattr(p, "name", None)
                    if pid == persona_id:
                        prompt = getattr(p, "prompt", None) or getattr(p, "system_prompt", None)
                        if prompt:
                            return str(prompt)
        except Exception as e:
            logger.debug(f"[JevRouter] persona_manager 取提示词失败: {e}")
        try:
            cfg = self.context.get_config()
            for p in cfg.get("persona", []) or []:
                if isinstance(p, dict) and (p.get("name") == persona_id or p.get("persona_id") == persona_id):
                    return str(p.get("prompt", ""))
        except Exception as e:
            logger.debug(f"[JevRouter] 配置人设取提示词失败: {e}")
        return ""

    # ---------- Jev 意图判定 ----------

    async def _query_jev(self, state_text: str, agents: list[dict]) -> tuple[str, float]:
        api_key = (self.config.get("typesafe_api_key") or "").strip()
        if not api_key or not agents:
            return "", 0.0

        # Jev 官方 SystemOne 协议：choice 必须要有 criteria 字典映射
        criteria = {}
        for a in agents:
            name = a.get("name", "").strip()
            desc = a.get("desc", "").strip()
            if name:
                criteria[name] = desc if desc else name

        if not criteria:
            return "", 0.0

        url = self._resolve_api_url()
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": (self.config.get("jev_model") or "jev-latest").strip(),
            "state": state_text,
            "questions": {
                "agent": {
                    "type": "choice",
                    "instructions": "根据用户当前输入和近期对话，选择最匹配的处理的子代理。",
                    "criteria": criteria
                }
            },
        }
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(total=6)
            async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"[JevRouter] Jev 返回非200 ({resp.status}): {body[:200]}")
                    return "", 0.0
                data = await resp.json()
                ans = data.get("answers", {}).get("agent", {})
                if isinstance(ans, str):
                    return ans, 1.0
                choice = ans.get("choice", "") or ans.get("value", "") or ""
                conf = ans.get("confidence", ans.get("score", ans.get("probability", 1.0)))
                try:
                    conf = float(conf)
                except (TypeError, ValueError):
                    conf = 1.0
                return str(choice), conf
        except Exception as e:
            logger.warning(f"[JevRouter] 请求 Jev 判定失败: {e}")
            return "", 0.0

    async def _build_state(self, event: AstrMessageEvent, user_msg: str) -> str:
        """组装喂给 Jev 的 state：近期对话 + 上一轮代理提示 + 当前输入"""
        parts = []
        if self.config.get("share_context_with_jev", True):
            try:
                umo = event.unified_msg_origin
                cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                conv = await self.context.conversation_manager.get_conversation(umo, cid)
                if conv and conv.history:
                    tail = [Message.model_validate(m) for m in conv.history[-4:]]
                    lines = [f"{c.role}: {c.content[:80]}" for c in tail if getattr(c, "content", None)]
                    if lines:
                        parts.append("近期对话:\n" + "\n".join(lines))
            except Exception as e:
                logger.debug(f"[JevRouter] 读取近期对话失败(忽略): {e}")

        last = self._last_agent.get(event.unified_msg_origin)
        if last:
            parts.append(f"上一轮由代理[{last}]回答")

        parts.append(f"用户当前输入: {user_msg}")
        return "\n\n".join(parts)

    # ---------- 第一阶段：消息入口处判定 + 指定模型 ----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message_route(self, event: AstrMessageEvent):
        user_msg = (event.message_str or "").strip()
        if not user_msg:
            return

        agents = self._get_agents()
        if not agents:
            return

        state = await self._build_state(event, user_msg)
        choice, conf = await self._query_jev(state, agents)

        min_conf = float(self.config.get("min_confidence", 0.55) or 0.55)
        if not choice or conf < min_conf:
            self._last_agent.pop(event.unified_msg_origin, None)
            return

        target = next((a for a in agents if a["name"] == choice), None)
        if not target:
            return

        event.set_extra(_K_AGENT, choice)
        event.set_extra(_K_CONF, conf)
        self._last_agent[event.unified_msg_origin] = choice

        # 指定模型：主流程 _select_provider 会读取 selected_provider extra
        provider_id = (target.get("provider_id") or "").strip()
        if provider_id:
            event.set_extra("selected_provider", provider_id)
            event.set_extra(_K_PROVIDER_DONE, True)
        logger.info(f"[JevRouter] 命中意图路由 -> [{choice}] (置信度={conf:.2f}, 模型={provider_id or '沿用'})")

    # ---------- 第二阶段：LLM 请求前覆写人设与工具（低优先级，在记忆注入之后追加） ----------

    @filter.on_llm_request(priority=-100)
    async def on_llm_request_override(self, event: AstrMessageEvent, req):
        agent_name = event.get_extra(_K_AGENT)
        if not agent_name:
            return
        target = next((a for a in self._get_agents() if a["name"] == agent_name), None)
        if not target:
            return

        # 1) 人设：追加模式，绝不清空原有 system_prompt（保住 persona 与记忆注入）
        chosen_persona = (target.get("persona_id") or "").strip()
        persona_prompt = self._resolve_persona_prompt(chosen_persona) if chosen_persona else ""
        extra_prompt = (target.get("system_prompt") or "").strip()

        blocks = []
        if persona_prompt:
            blocks.append(f"# Sub-Agent Persona [{agent_name}]\n{persona_prompt}")
        if extra_prompt:
            blocks.append(f"# Sub-Agent Instructions\n{extra_prompt}")
        if blocks:
            req.system_prompt = f"{req.system_prompt or ''}\n\n" + "\n\n".join(blocks)

        # 2) 工具白名单：支持逗号字符串或 select_plugin_set 返回的列表
        raw_tools = target.get("tools")
        # 未配置、留空或未包含工具字段时，默认全选放行
        if raw_tools is None:
            raw_tools = ["*"]

        allow_set = set()
        if isinstance(raw_tools, list):
            for t in raw_tools:
                if isinstance(t, str) and t.strip():
                    allow_set.add(t.strip())
        elif isinstance(raw_tools, str) and raw_tools.strip():
            allow_set = {t.strip() for t in raw_tools.split(",") if t.strip()}

        # 包含 "*" 或为空集合时，默认放行所有插件
        if allow_set and "*" not in allow_set and getattr(req, "func_tool", None) is not None:
            ts = req.func_tool
            tools = getattr(ts, "tools", None)
            if isinstance(tools, list):
                kept = []
                for tool in tools:
                    t_name = getattr(tool, "name", "")
                    p_id = getattr(tool, "plugin_id", "")
                    if t_name in allow_set or p_id in allow_set or any(p in t_name for p in allow_set):
                        kept.append(tool)
                try:
                    ts.tools = kept
                except Exception as e:
                    logger.warning(f"[JevRouter] 工具白名单写入失败: {e}")
        logger.debug(f"[JevRouter] 已覆写本轮人设/工具: agent={agent_name}")
