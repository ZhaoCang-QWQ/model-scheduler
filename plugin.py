"""峰谷时段省钱调度插件：按「星期+时段」自动切换回复模型。"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from pydantic import field_validator

SUPPORTED_CONFIG_VERSION = "0.3.0"
PLUGIN_TAG = "[峰谷省钱]"


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="是否启用插件（总开关；关闭=完全不干预模型选择）", json_schema_extra={"label": "插件总开关"})
    config_version: str = Field(default=SUPPORTED_CONFIG_VERSION, description="配置版本（勿改）", json_schema_extra={"hidden": True, "disabled": True})


class TimeRuleConfig(PluginConfigBase):
    """单条「时段 → 模型」规则。"""

    __ui_label__ = "规则"

    @field_validator("timezone_offset", mode="before")
    @classmethod
    def _normalize_timezone_offset(cls, v: Any) -> float:
        """配置页该字段以文本框输入：空串/非数字一律回退默认 8.0。

        注意：不得返回 None —— TOML 不支持 null，配置写回会报错。
        """
        if v is None or isinstance(v, bool):
            return 8.0
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return 8.0
            try:
                return float(s)
            except ValueError:
                return 8.0
        return 8.0

    enabled: bool = Field(default=True, description="是否启用本条规则", json_schema_extra={"label": "启用"})
    note: str = Field(default="", description="备注名（如：DeepSeek 错峰半价）", json_schema_extra={"label": "备注", "placeholder": "DeepSeek 错峰半价"})
    start: str = Field(default="00:30", description="开始时间 HH:MM（含）", json_schema_extra={"label": "开始时间", "placeholder": "00:30"})
    end: str = Field(default="08:30", description="结束时间 HH:MM（不含）；开始>结束视为跨午夜区间（如 22:00~06:00）；开始==结束视为全天生效", json_schema_extra={"label": "结束时间", "placeholder": "08:30"})
    model_name: str = Field(default="", description="该时段使用的模型名（必须与麦麦模型配置中的模型 name 完全一致；留空=本条规则无效）", json_schema_extra={"label": "模型名", "placeholder": "如 deepseek-v4-flash", "hint": "须先在麦麦模型配置页定义过该模型"})
    task_names: List[str] = Field(default_factory=lambda: ["replyer"], description="生效任务（本插件作用于回复生成链路，仅 replyer 有效，填写其他任务名不会生效）", json_schema_extra={"label": "生效任务", "hint": "仅 replyer 有效；官方钩子不支持给 planner 等其他任务指定模型"})
    days_of_week: List[str] = Field(default_factory=list, description="本规则在星期几生效：留空=每天；weekday=周一~周五；weekend=周六~周日；也可逐个填 mon/tue/wed/thu/fri/sat/sun（或中文 周一~周日）。时区按本规则时区计算", json_schema_extra={"label": "生效星期", "hint": "留空=每天；weekday=工作日；weekend=周末；mon~sun 或 周一~周日"})
    use_global_timezone: bool = Field(default=True, description="是否跟随「通用」页的全局时区偏移（默认开启）。关闭后本条规则改用下方 timezone_offset，适配不同厂商错峰时段按不同时区公布的情况", json_schema_extra={"label": "跟随全局时区"})
    timezone_offset: float = Field(default=8.0, ge=-12.0, le=14.0, description="本规则的时区偏移（小时，支持半小时区如 5.5）。仅当上方「跟随全局时区」关闭时生效", json_schema_extra={"label": "时区偏移（小时）", "hint": "8=北京时间；0=UTC；支持小数如 5.5"})


class RulesSectionConfig(PluginConfigBase):
    __ui_label__ = "时段规则"
    __ui_icon__ = "list"
    __ui_order__ = 1
    rules: List[TimeRuleConfig] = Field(
        default_factory=lambda: [
            TimeRuleConfig(
                note="工作日早高峰 09:00-12:00（DeepSeek 标准价时段 → 切备用模型，请填模型名）",
                start="09:00", end="12:00", days_of_week=["weekday"],
            ),
            TimeRuleConfig(
                note="工作日下午高峰 14:00-18:00（DeepSeek 标准价时段 → 切备用模型，请填模型名）",
                start="14:00", end="18:00", days_of_week=["weekday"],
            ),
        ],
        description="规则列表：可添加任意多条「时间区间→模型」规则，命中时段即用该模型；多条命中时取最上面一条。空闲时段无需写规则——插件零干预，走麦麦原模型（如 DeepSeek 半价）",
        json_schema_extra={"label": "规则列表"},
    )


class GeneralSectionConfig(PluginConfigBase):
    __ui_label__ = "通用"
    __ui_icon__ = "settings"
    __ui_order__ = 2
    timezone_offset: float = Field(default=8, ge=-12.0, le=14.0, description="UTC 偏移小时数（8=北京时间；支持小数如 5.5）。未单独设置时区的规则按此偏移判断时段", json_schema_extra={"label": "时区偏移（小时）"})
    default_model_name: str = Field(default="", description="兜底模型：所有规则都不命中时使用的模型（留空=不干预，走麦麦原模型策略）", json_schema_extra={"label": "兜底模型名", "hint": "留空=不命中时不干预"})
    log_every_switch: bool = Field(default=True, description="每次实际切换模型时打 info 日志", json_schema_extra={"label": "记录切换日志"})


class ModelSchedulerRootConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig, json_schema_extra={"label": "插件"})
    rules: RulesSectionConfig = Field(default_factory=RulesSectionConfig, json_schema_extra={"label": "时段规则"})
    general: GeneralSectionConfig = Field(default_factory=GeneralSectionConfig, json_schema_extra={"label": "通用"})


class ModelSchedulerPlugin(MaiBotPlugin):
    """按时段自动切换回复模型（通用「时段→模型」调度器）。"""

    config_model = ModelSchedulerRootConfig

    # ---------------- 辅助方法 ----------------

    def _cfg(self, section: str, key: str, default: Any = None) -> Any:
        """安全读取配置：getattr 链，任何异常返回 default。"""
        try:
            value = getattr(self.config, section, None)
            if value is None:
                return default
            value = getattr(value, key, default)
            return default if value is None else value
        except Exception:
            return default

    def _parse_hhmm(self, s: Any) -> Optional[int]:
        """把 "HH:MM" 解析成当日分钟数（0~1439）；非法返回 None。"""
        if not isinstance(s, str):
            return None
        s = s.strip()
        if not s or ":" not in s:
            return None
        parts = s.split(":", 1)
        if not (parts[0].isdigit() and parts[1].isdigit()):
            return None
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    @staticmethod
    def _in_window(now_minutes: int, start_m: int, end_m: int) -> bool:
        """区间判断：start==end 全天；start<end [start, end)；start>end 跨午夜环绕。"""
        if start_m == end_m:
            return True
        if start_m < end_m:
            return start_m <= now_minutes < end_m
        return now_minutes >= start_m or now_minutes < end_m

    def _global_offset(self) -> float:
        """读取「通用」页的全局时区偏移，非法或超范围时回退 8。"""
        try:
            value = float(self._cfg("general", "timezone_offset", 8))
        except (TypeError, ValueError):
            return 8.0
        return value if -12.0 <= value <= 14.0 else 8.0

    @staticmethod
    def _minutes_in_offset(utc_now: datetime, offset: float) -> int:
        """把当前 UTC 时刻换算到指定偏移时区的当日分钟数。"""
        local_now = utc_now + timedelta(hours=offset)
        return local_now.hour * 60 + local_now.minute

    _WEEKDAY_ALIASES = {
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6,
        "星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3, "星期五": 4, "星期六": 5, "星期日": 6,
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    }

    @classmethod
    def _parse_days(cls, specs: Any) -> Optional[set]:
        """解析生效星期为集合（0=周一..6=周日）；留空/全非法 → None（=每天，不过滤）。"""
        if not specs:
            return None
        if isinstance(specs, str):
            specs = [specs]
        days: set = set()
        for raw in specs:
            s = str(raw).strip().lower()
            if not s:
                continue
            if s in ("weekday", "workday"):
                days.update(range(5))
            elif s == "weekend":
                days.update((5, 6))
            else:
                key = next((k for k in cls._WEEKDAY_ALIASES if k.lower() == s), None)
                if key is not None:
                    days.add(cls._WEEKDAY_ALIASES[key])
        return days or None

    def _match_rule(self, **kwargs: Any) -> Optional[str]:
        """核心匹配：返回应使用的模型名，不干预时返回 None。"""
        # 总开关
        if not self._cfg("plugin", "enabled", True):
            return None

        # 规则列表
        rules_section = getattr(self.config, "rules", None)
        rules = getattr(rules_section, "rules", None) if rules_section is not None else None
        if not rules:
            rules = []

        # 任务名
        raw_task = kwargs.get("task_name")
        task_name = str(raw_task).strip().lower() if raw_task is not None else ""

        utc_now = datetime.now(timezone.utc)
        global_offset = self._global_offset()

        for rule in rules:
            try:
                if not getattr(rule, "enabled", True):
                    continue
                model_name = str(getattr(rule, "model_name", "") or "").strip()
                if not model_name:
                    continue

                # 任务匹配：kwargs 无 task_name 时视为 "replyer"；task_names 留空=不匹配任何任务
                rule_tasks_raw = getattr(rule, "task_names", None) or []
                rule_tasks = [str(t).strip().lower() for t in rule_tasks_raw if str(t).strip()]
                effective_task = task_name if task_name else "replyer"
                if not rule_tasks or effective_task not in rule_tasks:
                    continue

                # 规则时区：默认跟随全局；仅当规则显式关闭「跟随全局时区」时用规则自身偏移
                if getattr(rule, "use_global_timezone", True):
                    rule_offset = global_offset
                else:
                    raw_rule_offset = getattr(rule, "timezone_offset", None)
                    try:
                        rule_offset = float(raw_rule_offset) if raw_rule_offset is not None else global_offset
                    except (TypeError, ValueError):
                        rule_offset = global_offset
                    if not (-12.0 <= rule_offset <= 14.0):
                        rule_offset = global_offset

                # 按规则时区换算本地时间（星期与时窗都基于它，避免跨时区差一天）
                local_now = utc_now + timedelta(hours=rule_offset)

                # 星期过滤：days_of_week 留空=每天
                allowed_days = self._parse_days(getattr(rule, "days_of_week", None) or [])
                if allowed_days is not None and local_now.weekday() not in allowed_days:
                    continue

                # 时间窗
                start_m = self._parse_hhmm(getattr(rule, "start", None))
                end_m = self._parse_hhmm(getattr(rule, "end", None))
                if start_m is None or end_m is None:
                    note = getattr(rule, "note", "") or ""
                    self.ctx.logger.debug("%s 规则时间解析失败，已跳过（note=%s, start=%s, end=%s）", PLUGIN_TAG, note, getattr(rule, "start", None), getattr(rule, "end", None))
                    continue

                now_minutes = local_now.hour * 60 + local_now.minute
                if self._in_window(now_minutes, start_m, end_m):
                    return model_name
            except Exception:
                self.ctx.logger.debug("%s 单条规则处理异常，已跳过", PLUGIN_TAG, exc_info=True)
                continue

        # 兜底模型：对所有任务生效，无任务限制
        default_model = str(self._cfg("general", "default_model_name", "") or "").strip()
        if default_model:
            return default_model

        return None

    @staticmethod
    def _is_retry_attempt(kwargs: Dict[str, Any]) -> bool:
        """重试轮判定：attempt>1 或 retry_count>0（兼容两种编号约定）。"""
        attempt = kwargs.get("attempt")
        retry_count = kwargs.get("retry_count")
        try:
            if attempt is not None and int(attempt) > 1:
                return True
            if retry_count is not None and int(retry_count) > 0:
                return True
        except (TypeError, ValueError):
            return False
        return False

    # ---------------- 钩子 ----------------

    @HookHandler(
        "maisaka.replyer.before_request",
        mode=HookMode.BLOCKING,
        name="model_scheduler_switch",
        description="按时段规则切换回复模型",
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def _on_replyer_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        try:
            if self._is_retry_attempt(kwargs):
                # 重试/重生成轮不干预：官方文档未定义宿主重试轮的模型回退策略，
                # 保守起见交还宿主处理，避免反复指定可能刚失败的模型
                self.ctx.logger.debug("%s 重试轮放行 (attempt=%s, retry_count=%s)", PLUGIN_TAG, kwargs.get("attempt"), kwargs.get("retry_count"))
                return {"action": "continue"}
            target = self._match_rule(**kwargs)
            if not target:
                return {"action": "continue"}
            if str(kwargs.get("model_name") or "").strip() == target:
                return {"action": "continue"}  # 已是指定模型
            if self._cfg("general", "log_every_switch", True):
                self.ctx.logger.info("%s 时段命中 → 指定模型 %s (task=%s)", PLUGIN_TAG, target, kwargs.get("task_name"))
            modified = dict(kwargs)  # 完整替换语义：先整体复制
            modified["model_name"] = target
            return {"action": "continue", "modified_kwargs": modified}
        except Exception:
            self.ctx.logger.exception("%s 处理钩子时发生异常，本次不干预", PLUGIN_TAG)
            return {"action": "continue"}

    # ---------------- 生命周期 ----------------

    async def on_load(self) -> None:
        rules: List[Any] = []
        valid_rules = 0
        try:
            rules_section = getattr(self.config, "rules", None)
            rules = list(getattr(rules_section, "rules", None) or [])
            valid_rules = sum(
                1 for r in rules if getattr(r, "enabled", True) and str(getattr(r, "model_name", "") or "").strip()
            )
        except Exception:
            rules, valid_rules = [], 0
        try:
            offset = float(self._cfg("general", "timezone_offset", 8))
        except (TypeError, ValueError):
            offset = 8.0
        default_model = str(self._cfg("general", "default_model_name", "") or "").strip()
        self.ctx.logger.info(
            "%s 插件已加载 | 时区: UTC+%g | 有效规则: %d/%d | 兜底模型: %s",
            PLUGIN_TAG, offset, valid_rules, len(rules), default_model or "无",
        )
        if self._cfg("plugin", "enabled", True) and valid_rules == 0 and not default_model:
            self.ctx.logger.warning(
                "%s 已启用但没有任何生效规则，模型选择将不受影响，请检查规则配置", PLUGIN_TAG
            )
        # 官方钩子仅在回复链路触发，规则里填的其他任务名不会生效
        other_tasks = sorted({
            str(t).strip().lower()
            for r in rules for t in (getattr(r, "task_names", None) or [])
            if str(t).strip().lower() and str(t).strip().lower() != "replyer"
        })
        if other_tasks:
            self.ctx.logger.warning(
                "%s 规则中的生效任务 [%s] 不会生效：本插件只作用于 replyer（回复生成），官方钩子不支持为其他任务指定模型",
                PLUGIN_TAG, ", ".join(other_tasks),
            )

    async def on_unload(self) -> None:
        self.ctx.logger.info("%s 插件已卸载", PLUGIN_TAG)

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        self.ctx.logger.info("%s 配置已更新 (scope=%s, version=%s)", PLUGIN_TAG, scope, version)


def create_plugin() -> MaiBotPlugin:
    return ModelSchedulerPlugin()
