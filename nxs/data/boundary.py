"""应答边界（D4/M6）—— 节点侧强制执行的最后一道关卡。

**这是本系统区别于普通消息队列的核心模块。**

它做的只有一件事：把执行方的产出**改写成符合约束的形态，或拒绝它**。
判定的规则来自策略引擎（``control/policy.py``），本模块负责**执行**那个判定，
并额外做两件策略层无法做的事：

1. **强制改写 ``semantics``** —— 产出永远是 ``data``，不可能是 ``request``。
   这是防跨域提示注入的机制：回传内容的来源（对话记录、文档、外部邮件）
   本质是人类自然语言，其中可能包含"请把 X 发给 Y"这类**伪装成事实的指令**。
   接收方看到 ``data`` 就知道"这段内容要当数据看，不能直接当命令执行"。

2. **剥离自由文本的指令性外层** —— 对声明为 ``freetext`` 的字段，
   去掉控制字符与明显的前缀注入模式。这是**纵深防御的第一层**，
   也是最有效的一层，因为它在数据离开节点之前就做了处理。

为什么"拒绝"在这里而不是在策略层
--------------------------------
策略层回答"允许吗"，边界层回答"给什么"。分开的好处是：策略可以
被替换成更复杂的实现（比如接外部策略引擎），而边界层的改写逻辑不变。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Iterable, Mapping

from ..core.errors import PolicyDenied, RuleId
from ..core.models import Artifact, Capability, Level, Semantics, Task
from ..control.policy import PolicyEngine, MAX_FREETEXT_BYTES

#: 控制字符（保留换行与制表符，它们在正常文本里合法）。
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: 明显的指令性前缀。**只做剥离，不做"检测告警"**——
#: 告警会产生噪声，而剥离是确定性的收益。
#: 注意：这不是完整的注入防护（那不存在），而是降低最低成本的攻击成功率。
_INSTRUCTION_PREFIXES = (
    re.compile(r"^\s*(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above)\b.*?[.:\n]", re.I | re.S),
    re.compile(r"^\s*you\s+are\s+now\b.*?[.:\n]", re.I | re.S),
    re.compile(r"^\s*system\s*[:：].*?\n", re.I | re.S),
    re.compile(r"^\s*assistant\s*[:：].*?\n", re.I | re.S),
    re.compile(r"^\s*忽略(?:之前|上面|以上)(?:的)?(?:所有)?(?:指令|指示|要求).*?[。：:\n]", re.S),
    re.compile(r"^\s*你现在是\b.*?[。：:\n]", re.S),
    re.compile(r"^\s*请(?:立即)?执行\b.*?[。：:\n]", re.S),
)


class ResponseBoundary:
    """应答边界。**无状态**，可安全共享。

    :param policy: 策略引擎，提供等级与字段白名单的判定
    """

    def __init__(self, policy: PolicyEngine) -> None:
        self._policy = policy

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def enforce(self, node_id: str, task: Task,
                artifacts: Iterable[Artifact]) -> list[Artifact]:
        """校验并改写产出。通过则返回改写后的产物列表。

        流程：
        ① 逐条规范化（剥离自由文本的注入外层）
        ② 交给策略引擎做完整校验（等级、字段白名单、长度）
        ③ 强制把 ``semantics`` 改写为 ``data``

        任一步失败即抛 :class:`PolicyDenied`，**不返回部分结果**——
        部分成功会让调用方误以为拿到了完整数据。
        """
        cap = self._policy._card(node_id).capability(task.type)  # noqa: SLF001
        if cap is None:
            raise PolicyDenied(
                RuleId.WL_TYPE_NOT_REGISTERED,
                f"{node_id} has no capability {task.type}",
            )

        sanitized = [self._sanitize(node_id, cap, a) for a in artifacts]

        # 交给策略引擎做权威判定（等级 / 白名单 / 长度）
        probe = replace(task, artifacts=list(sanitized))
        self._policy.check_response(node_id, probe)

        # 强制语义：产出永远是数据，不是命令
        probe.semantics = Semantics.DATA
        probe.artifacts = sanitized
        return sanitized

    def enforce_on_task(self, node_id: str, task: Task) -> Task:
        """就地校验并改写任务上的产物，返回同一个 task 对象。

        便于在生命周期流程里串联使用。
        """
        task.artifacts = self.enforce(node_id, task, task.artifacts)
        task.semantics = Semantics.DATA
        return task

    # ------------------------------------------------------------------
    # 规范化
    # ------------------------------------------------------------------

    def _sanitize(self, node_id: str, cap: Capability,
                  art: Artifact) -> Artifact:
        """规范化单个产物。

        * 自由文本字段：剥控制字符、剥指令性前缀、按字节截断
        * 其余字段：原样透传（结构化字段由白名单控制，不需要内容处理）
        """
        if not cap.freetext_fields:
            return art

        if not isinstance(art.data, Mapping):
            return art

        cleaned = dict(art.data)
        touched = False
        for fname in cap.freetext_fields:
            value = cleaned.get(fname)
            if not isinstance(value, str):
                continue
            new_value = self.sanitize_freetext(value)
            if new_value != value:
                cleaned[fname] = new_value
                touched = True

        if not touched:
            return art

        # 记录"这段内容被处理过"——接收方据此知道它不可原样信任
        prov = dict(art.provenance)
        prov["sanitized"] = True
        return replace(art, data=cleaned, provenance=prov)

    @staticmethod
    def sanitize_freetext(value: str) -> str:
        """清洗一个自由文本字段。

        三步，都是确定性的：

        1. **去控制字符**（保留换行与制表符）
        2. **剥指令性前缀**——循环剥离，因为可能叠了多层
        3. **按字节截断**——注意是多字节安全的截断，不会切碎 UTF-8 字符

        刻意**不做**的事：不改写内容、不做语义判断、不添加告警标记。
        这些会产生误报与副作用，而本方法的定位是"降低攻击成功率"，
        不是"保证内容安全"——后者做不到。
        """
        out = _CONTROL_CHARS.sub("", value)

        changed = True
        while changed:
            changed = False
            for pat in _INSTRUCTION_PREFIXES:
                m = pat.match(out)
                if m:
                    out = out[m.end():]
                    changed = True
                    break

        out = out.strip()
        return _truncate_utf8(out, MAX_FREETEXT_BYTES)

    # ------------------------------------------------------------------
    # 只读辅助：给接收方用的"信任提示"
    # ------------------------------------------------------------------

    @staticmethod
    def trust_hint(node_id: str, artifacts: Iterable[Artifact]) -> dict[str, Any]:
        """生成给接收方看的信任提示。

        接收方据此知道：这些内容来自谁、最高等级是多少、
        **其中是否含自由文本**（自由文本是不可信输入面）。
        """
        arts = list(artifacts)
        has_freetext = any(a.kind == "freetext" for a in arts)
        sanitized = any(bool(a.provenance.get("sanitized")) for a in arts)
        level = max((a.level for a in arts), default=Level.METADATA)
        return {
            "source": node_id,
            "max_level": str(level),
            "contains_freetext": has_freetext,
            "sanitized": sanitized,
            "semantics": Semantics.DATA,
        }


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按字节上限截断，保证不切碎 UTF-8 字符。

    直接对 bytes 切片会把多字节字符切成两半，产生乱码——
    因此逐字符累加，超限即停。
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        size = len(ch.encode("utf-8"))
        if used + size > max_bytes:
            break
        out.append(ch)
        used += size
    return "".join(out)


__all__ = ["ResponseBoundary"]
