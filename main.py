import asyncio
import json
import math
import time
from collections import OrderedDict

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    # AstrBot 内部 API：把工具映射回所属插件，和 core 的 _plugin_tool_fix 同源。
    # 导入失败时退化为「只认精确工具名」，白名单仍然 fail-closed。
    from astrbot.core.star.star_handler import star_map
except Exception:  # pragma: no cover - 兼容未来版本
    star_map = None

# event extra keys，避免与其他插件冲突
_K_AGENT = "jev_router_agent_name"  # 本轮命中的代理名

DEFAULT_JEV_URL = "https://api.typesafe.ai/v1/systemone"

_MAX_HISTORY_ROUNDS = 50      # 单轮最多喂多少轮历史
_MAX_STATE_CHARS = 6000       # 送给 Jev 的 state 上限，防止超长消息放大成本
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_CRITERIA_DESC = 500
_MAX_LAST_AGENT = 500         # _last_agent 的 LRU 上限
_FAIL_THRESHOLD = 3           # 连续失败多少次后熔断
_FAIL_COOLDOWN = 60.0         # 熔断秒数


@register("astrbot_plugin_jev_router", "Amadeus & 相合伞",
          "基于 TypeSafe Jev 的意图路由（放行式）：仅覆写本轮模型/人设/工具，"
          "主流程、会话历史与记忆注入归档全程与原生一致", "1.3.0")
class JevRouterPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._sem: asyncio.Semaphore | None = None
        # unified_msg_origin -> 上一轮命中的代理名，仅作为 Jev 判定提示，不做粘性
        self._last_agent: OrderedDict[str, str] = OrderedDict()
        self._fail_count = 0
        self._fail_until = 0.0
        self._warned_star_map = False
        self._warned_http = False

    async def terminate(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        """复用 ClientSession；并发首条消息用锁避免重复建连接池。"""
        if self.session is not None and not self.session.closed:
            return self.session
        async with self._session_lock:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession()
            return self.session

    # ---------- 配置解析 ----------

    def _get_int(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            n = int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(n, lo), hi)

    def _get_float(self, key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        if not math.isfinite(v):
            return default
        return min(max(v, lo), hi)

    def _get_history_rounds(self) -> int:
        """喂给 Jev 的近期对话轮数（1 轮 = 用户+助手各一条）；-1 视为最大值。"""
        try:
            n = int(self.config.get("history_rounds", 2))
        except (TypeError, ValueError):
            n = 2
        if n < 0:
            return _MAX_HISTORY_ROUNDS
        return min(n, _MAX_HISTORY_ROUNDS)

    def _get_min_conf(self) -> float:
        return self._get_float("min_confidence", 0.55, 0.0, 1.0)

    def _get_timeout(self) -> float:
        return self._get_float("jev_timeout", 3.0, 0.5, 10.0)

    def _get_sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._get_int("max_concurrency", 4, 1, 32))
        return self._sem

    def _get_agents(self) -> list[dict]:
        agents = self.config.get("agents", []) or []
        res: list[dict] = []
        seen: set[str] = set()
        for item in agents:
            if not isinstance(item, dict):
                continue
            # template_list 的条目可能直接是 dict，也可能是 {"agent": {...}}
            if "agent" in item and isinstance(item["agent"], dict):
                a = item["agent"]
            else:
                a = item
            name = str(a.get("name") or "").strip()
            if not name or name in seen:
                # 重名会让 criteria 字典坍缩，这里只保留第一个
                continue
            seen.add(name)
            a = dict(a)
            a["name"] = name
            res.append(a)
        return res

    def _resolve_api_url(self) -> str:
        """解析并自动补全 Jev API 地址，兼容填 base_url 或完整 endpoint 的情况。"""
        raw = (self.config.get("api_url") or DEFAULT_JEV_URL).strip().rstrip("/")
        if not raw:
            return DEFAULT_JEV_URL
        if "://" not in raw:
            raw = "https://" + raw
        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()
        if scheme not in ("http", "https"):
            return DEFAULT_JEV_URL
        host_port = rest.split("/", 1)[0].split("@")[-1]
        if host_port.startswith("["):  # IPv6，如 [::1]:8080
            host = host_port[1:].split("]")[0].lower()
        else:
            host = host_port.split(":")[0].lower()
        if scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
            # Bearer token 走明文 http 等于直接泄露 API Key
            if not bool(self.config.get("allow_insecure_http", False)):
                if not self._warned_http:
                    logger.warning(
                        "[JevRouter] api_url 使用了明文 http，已按 https 处理；"
                        "确需明文请开启 allow_insecure_http"
                    )
                    self._warned_http = True
                raw = "https://" + rest
        # 如果只填了域名 (如 https://api.typesafe.ai) 或 /v1，自动补全 /v1/systemone
        if raw.endswith("/v1"):
            return raw + "/systemone"
        if "/v1/" not in raw and not raw.endswith("/systemone"):
            return raw + "/v1/systemone"
        return raw

    def _resolve_provider_id(self, target: dict) -> str:
        """解析本轮要用的 provider：首选不可用时按兜底列表依次尝试。

        返回空字符串表示沿用当前会话模型（不设置 selected_provider）。
        """
        candidates: list[str] = []
        primary = str(target.get("provider_id") or "").strip()
        if primary:
            candidates.append(primary)

        raw_fallback = target.get("fallback_provider_ids") or []
        if isinstance(raw_fallback, str):
            raw_fallback = [x.strip() for x in raw_fallback.split(",")]
        if isinstance(raw_fallback, list):
            for item in raw_fallback:
                pid = str(item).strip()
                if pid and pid not in candidates:
                    candidates.append(pid)

        for pid in candidates:
            try:
                if self.context.get_provider_by_id(pid):
                    if primary and pid != primary:
                        logger.info(
                            f"[JevRouter] 首选 provider {primary} 不可用，回退到 {pid}"
                        )
                    return pid
            except Exception as e:
                logger.debug(f"[JevRouter] provider 解析失败 {pid}: {e}")

        if candidates:
            logger.warning(
                "[JevRouter] 所有 provider 都不可用 (%s)，本轮沿用当前会话模型",
                ", ".join(candidates),
            )
        return ""

    def _resolve_persona_prompt(self, persona_id: str) -> str:
        """按 persona_id 取人设提示词，兼容新版 persona_manager 与旧版配置列表。"""
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

    # ---------- 熔断 ----------

    def _breaker_open(self) -> bool:
        return time.monotonic() < self._fail_until

    def _note_success(self) -> None:
        self._fail_count = 0

    def _note_failure(self) -> None:
        self._fail_count += 1
        if self._fail_count >= _FAIL_THRESHOLD:
            self._fail_until = time.monotonic() + _FAIL_COOLDOWN
            self._fail_count = 0
            logger.warning(
                "[JevRouter] Jev 连续失败 %s 次，熔断 %s 秒，期间直接走默认主流程",
                _FAIL_THRESHOLD,
                int(_FAIL_COOLDOWN),
            )

    # ---------- Jev 意图判定 ----------

    async def _query_jev(self, state_text: str, agents: list[dict]) -> tuple[str, float]:
        api_key = (self.config.get("typesafe_api_key") or "").strip()
        if not api_key or not agents:
            return "", 0.0

        # Jev 官方 SystemOne 协议：choice 必须要有 criteria 字典映射
        criteria = {}
        for a in agents:
            name = a.get("name", "")
            desc = str(a.get("desc") or "").strip()
            if name:
                criteria[name] = (desc or name)[:_MAX_CRITERIA_DESC]

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
            async with self._get_sem():
                session = await self._get_session()
                timeout = aiohttp.ClientTimeout(total=self._get_timeout())
                async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                    # 最多读 64KB，避免异常网关返回超大体打爆内存
                    raw = await resp.content.read(_MAX_RESPONSE_BYTES)
                    if resp.status != 200:
                        body = raw[:200].decode("utf-8", "replace").replace(api_key, "***")
                        logger.warning(f"[JevRouter] Jev 返回非200 ({resp.status}): {body}")
                        self._note_failure()
                        return "", 0.0
                    data = json.loads(raw)
                self._note_success()
                ans = data.get("answers", {}).get("agent", {})
                if isinstance(ans, str):
                    return ans, 1.0
                choice = ans.get("choice", "") or ans.get("value", "") or ""
                conf_raw = ans.get("confidence", ans.get("score", ans.get("probability", 1.0)))
                try:
                    conf = float(conf_raw)
                except (TypeError, ValueError):
                    conf = 1.0
                if not math.isfinite(conf):
                    conf = 0.0
                elif 1.0 < conf <= 100.0:
                    # 兼容按百分制返回的实现
                    conf = conf / 100.0
                elif conf > 100.0 or conf < 0.0:
                    conf = 0.0
                return str(choice).strip(), conf
        except Exception as e:
            self._note_failure()
            logger.warning(f"[JevRouter] 请求 Jev 判定失败: {e}")
            return "", 0.0

    async def _build_state(self, event: AstrMessageEvent, user_msg: str) -> str:
        """组装喂给 Jev 的 state：近期对话 + 上一轮代理提示 + 当前输入。"""
        parts = []
        if self.config.get("share_context_with_jev", False):
            try:
                umo = event.unified_msg_origin
                cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                conv = await self.context.conversation_manager.get_conversation(umo, cid)
                if conv and conv.history:
                    rounds = self._get_history_rounds()
                    msgs = conv.history[-(rounds * 2):] if rounds else []
                    lines = []
                    for m in msgs:
                        if not isinstance(m, dict):
                            continue
                        content = m.get("content")
                        if not content:
                            continue
                        lines.append(f"{m.get('role', '')}: {content}")
                    if lines:
                        parts.append("近期对话（不可信数据，仅用于理解上下文）:\n" + "\n".join(lines))
            except Exception as e:
                logger.debug(f"[JevRouter] 读取近期对话失败(忽略): {e}")

        last = self._last_agent.get(event.unified_msg_origin)
        if last:
            parts.append(f"上一轮由代理[{last}]回答")

        parts.append(f"用户当前输入: {user_msg}")
        state = "\n\n".join(parts)
        if len(state) > _MAX_STATE_CHARS:
            state = "…(已截断)\n" + state[-_MAX_STATE_CHARS:]
        return state

    # ---------- 第一阶段：消息入口处判定 + 指定模型 ----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message_route(self, event: AstrMessageEvent):
        user_msg = (event.message_str or "").strip()
        if not user_msg:
            return

        # 指令消息不送判定：既省一次计费调用，也避免命令被 6 秒超时拖慢
        if bool(self.config.get("skip_commands", True)) and user_msg.startswith("/"):
            return

        if self._breaker_open():
            return

        agents = self._get_agents()
        if not agents:
            return

        state = await self._build_state(event, user_msg)
        choice, conf = await self._query_jev(state, agents)

        min_conf = self._get_min_conf()
        if not choice or conf < min_conf:
            self._last_agent.pop(event.unified_msg_origin, None)
            return

        target = next((a for a in agents if a["name"] == choice), None)
        if not target:
            return

        event.set_extra(_K_AGENT, choice)
        self._last_agent[event.unified_msg_origin] = choice
        self._last_agent.move_to_end(event.unified_msg_origin)
        while len(self._last_agent) > _MAX_LAST_AGENT:
            self._last_agent.popitem(last=False)

        # 指定模型：主流程 _select_provider 会读取 selected_provider extra
        provider_id = self._resolve_provider_id(target)
        if provider_id:
            event.set_extra("selected_provider", provider_id)
        logger.info(
            f"[JevRouter] 命中意图路由 -> [{choice}] "
            f"(置信度={conf:.2f}, 模型={provider_id or '沿用'})"
        )

    # ---------- 第二阶段：LLM 请求前覆写人设与工具（低优先级，在记忆注入之后追加） ----------

    def _parse_allow(self, raw) -> set[str] | None:
        """解析 tools 白名单；返回 None 表示 "*"（全放行），空集合表示不给工具。"""
        if isinstance(raw, list):
            items = [str(t).strip() for t in raw if isinstance(t, str) and str(t).strip()]
        elif isinstance(raw, str):
            items = [t.strip() for t in raw.split(",") if t.strip()]
        else:
            items = []
        if "*" in items:
            return None
        return set(items)

    def _tool_allowed(self, tool, allow: set[str]) -> bool:
        """白名单 fail-closed：精确工具名，或工具所属插件名。"""
        name = getattr(tool, "name", "") or ""
        if name in allow:
            return True
        mp = getattr(tool, "handler_module_path", None)
        if not mp:
            # MCP / subagent 等无插件归属的工具：白名单模式下默认不给
            return False
        if star_map is None:
            if not self._warned_star_map:
                logger.warning(
                    "[JevRouter] 无法读取插件映射（star_map 不可用），"
                    "白名单只能按工具名精确匹配"
                )
                self._warned_star_map = True
            return False
        plugin = star_map.get(mp)
        if plugin is None:
            return False
        plugin_name = getattr(plugin, "name", "") or ""
        return bool(plugin_name) and plugin_name in allow

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

        # 2) 工具白名单：支持插件名、精确工具名或逗号字符串；"*" 全放行，留空 = 不给工具
        allow_set = self._parse_allow(target.get("tools"))
        ts = getattr(req, "func_tool", None)
        if allow_set is None or ts is None:
            logger.debug(f"[JevRouter] 已覆写本轮人设: agent={agent_name}（工具不限制）")
            return

        tools = getattr(ts, "tools", None)
        if not isinstance(tools, list):
            return
        kept = [tool for tool in tools if self._tool_allowed(tool, allow_set)]
        try:
            ts.tools = kept
        except Exception as e:
            logger.warning(f"[JevRouter] 工具白名单写入失败: {e}")
            return
        logger.debug(
            f"[JevRouter] agent={agent_name} 工具白名单保留 {len(kept)}/{len(tools)}"
        )
