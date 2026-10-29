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
from ..core.models import Artifact, Capability, Level, RoleCard, Semantics, Task
from ..control.policy import (  # noqa: PLC2701 - 同包内复用判定细节，避免两处各写一份
    MAX_FREETEXT_BYTES,
    PolicyEngine,
    _extract_fields,
    _get_field,
)

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
        ① 逐条校验 kind 与声明自洽（防"自称 freetext 却绕过清洗"）
        ② 逐条规范化（剥离自由文本的注入外层）
        ③ 交给策略引擎做完整校验（等级、字段白名单、长度）
        ④ 强制把 ``semantics`` 改写为 ``data``

        任一步失败即抛 :class:`PolicyDenied`，**不返回部分结果**——
        部分成功会让调用方误以为拿到了完整数据。
        """
        cap = self._policy._card(node_id).capability(task.type)  # noqa: SLF001
        if cap is None:
            raise PolicyDenied(
                RuleId.WL_TYPE_NOT_REGISTERED,
                f"{node_id} has no capability {task.type}",
            )

        for art in artifacts:
            check_kind_matches_declaration(cap, art)

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
        """规范化单个产物。委托给模块级函数，与节点侧边界共用同一实现。"""
        return sanitize_artifact(cap, art)

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


# ---------------------------------------------------------------------------
# 自由文本规范化（两层边界共用同一实现）
# ---------------------------------------------------------------------------

def sanitize_artifact(cap: Capability, art: Artifact) -> Artifact:
    """规范化一个产物里声明的自由文本字段。

    * 自由文本字段：剥控制字符、剥指令性前缀、按字节截断
    * 其余字段：原样透传（结构化字段由白名单控制，不需要内容处理）

    提到模块级是为了让**节点侧与 coordinator 侧用同一份逻辑**。
    两份实现迟早会分叉，而分叉的那一侧就是绕过口。
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
        new_value = ResponseBoundary.sanitize_freetext(value)
        if new_value != value:
            cleaned[fname] = new_value
            touched = True

    if not touched:
        return art

    # 记录"这段内容被处理过"——接收方据此知道它不可原样信任
    prov = dict(art.provenance)
    prov["sanitized"] = True
    return replace(art, data=cleaned, provenance=prov)


def check_kind_matches_declaration(cap: Capability, art: Artifact) -> None:
    """校验产物的 ``kind`` 与能力的声明是否自洽。不符即拒。

    ⭐ **为什么这条不能省**

    ``kind="freetext"`` 是一个**语义承诺**：意思是"这里面是人类自然语言，
    我按自由文本处理过"（剥控制字符、剥指令性前缀、截断）。

    但清洗的实际依据是 ``cap.freetext_fields``——按**字段名**判断。
    于是存在一条绕过路径：

    ```
    能力声明 response_fields=["echo","level"]，freetext_fields=[]   ← 没声明自由文本
    节点回传 kind="freetext", data={"echo": 250KB 的注入文本}       ← 却自称 freetext
    ```

    结果：``sanitize_artifact`` 看到 ``freetext_fields`` 为空，**直接原样返回**。
    注入文本未经任何处理就通过了边界，而 ``kind`` 标签让接收方以为
    它已经被处理过。**这比不标更危险——它给了一个错误的安心感。**

    修法选"拒绝"而不是"自动清洗"：声明与实现不符是一个**应当被发现**
    的错误。静默把它洗干净，等于把这个不一致藏起来——
    而本项目的贯穿原则是"声明即承诺"（角色卡、应答等级都按这个执行）。

    同理，``kind="reference"`` 也不该被当成内容传递的通道：
    引用形态的 payload 是坐标，如果能力连 ``response_fields`` 都没声明，
    说明这个类型根本没打算返回引用。
    """
    if art.kind == "freetext" and not cap.freetext_fields:
        raise PolicyDenied(
            RuleId.WL_FREETEXT_NOT_ALLOWED,
            f"artifact kind=freetext but {cap.type} declares no freetext_fields",
        )
    if art.kind == "reference" and not cap.response_fields:
        # ⚠ **这一条是冗余防御，不是必需。**
        #   没有 response_fields 时，后面的字段白名单本来就会拒掉任何字段。
        #   留着它的价值只有一个：错误码更准确（指向"没声明字段"
        #   而不是"这个字段不在白名单里"）。
        #   写清楚是因为——把冗余防御说成关键防线，会让人误判真实风险位置。
        raise PolicyDenied(
            RuleId.WL_FIELD_NOT_ALLOWED,
            f"artifact kind=reference but {cap.type} declares no response_fields",
        )


# ---------------------------------------------------------------------------
# 节点侧边界
# ---------------------------------------------------------------------------

class LocalBoundary:
    """**节点侧**应答边界 —— 数据离开节点之前先裁剪。

    为什么需要第二道边界
    --------------------
    :class:`ResponseBoundary` 跑在协调节点（coordinator）上。但本系统的核心威胁模型
    是"**coordinator 被攻破**"——那时 coordinator 上的任何检查都形同虚设，因为攻击者可以
    直接改代码跳过它。

    所以真正能保护数据的是**数据离开节点之前**的那一次裁剪。
    本类就做这件事：它不依赖任何中心侧配置，只用节点**自己声明的角色卡**。

    ⭐ 两层的分工（不重复，也不冲突）
    ---------------------------------
    ==============  ==================================  ==============================
    层              回答的问题                          需要的输入
    ==============  ==================================  ==============================
    节点侧（本类）  **我能承诺什么**                    自己的角色卡
    coordinator 侧           **你能得到什么**                    域 × 类型矩阵 + 全部角色卡
    ==============  ==================================  ==============================

    节点侧检查不出"域矩阵只允许 metadata"——那需要知道对方是谁，
    而节点不该知道全网拓扑。它只管一件事：**不产出超出自己声明的东西**。

    这正是"声明即承诺"的执行点：能力注册时声明 level=summary，
    实现就必须真的只产出 summary。**在数据离开节点前就裁剪，
    而不是让 coordinator 事后拦。**

    上限怎么算
    ----------
    ``ceiling = min(角色卡的数据等级上限, 该能力声明的等级)``

    coordinator 侧的上限是 ``min(矩阵上限, 节点上限, 能力等级)``，比这里更严格。
    **两级取最小，最终结果与单级三者取最小完全等价**，
    所以节点侧不会放宽任何东西——它只是把裁剪提前了。
    """

    def __init__(self, card: RoleCard | None, *,
                 levels: Mapping[str, Level] | None = None) -> None:
        """
        :param card: 节点自己的角色卡。为 ``None`` 时进入**降级模式**：
            只能用 ``levels`` 检查产出等级，无法检查字段白名单
            （因为不知道声明了哪些字段）。降级模式会由 SDK 记一条警告。
        :param levels: 任务类型 → 产出等级。降级模式下唯一可用的约束。
        """
        self._card = card
        self._levels = dict(levels or {})

    @property
    def degraded(self) -> bool:
        """是否处于降级模式（没有角色卡，只有等级约束）。"""
        return self._card is None

    def check(self, task: Task, artifact: Artifact) -> Artifact:
        """校验并裁剪一条产物。返回**可以直接发送**的产物。

        失败抛 :class:`PolicyDenied`，且**绝不发送部分结果**。
        """
        cap = self._capability(task.type)
        ceiling = self._ceiling(task.type, cap)

        # -- ① 等级 --
        if artifact.level > ceiling:
            raise PolicyDenied(
                RuleId.WL_LEVEL_EXCEEDED,
                f"local boundary: artifact level={artifact.level} "
                f"ceiling={ceiling}",
            )

        # -- ② kind 与声明自洽（两层用同一个函数，防止两边判断分叉） --
        if cap is not None:
            check_kind_matches_declaration(cap, artifact)

        # -- ③ 自由文本清洗（在字段检查之前做，因为清洗会改值不改键） --
        if cap is not None:
            artifact = sanitize_artifact(cap, artifact)

        # -- ④ 字段白名单（降级模式下无从判断，跳过） --
        if cap is not None:
            allowed = set(cap.response_fields)
            stray = _extract_fields(artifact.data) - allowed
            if stray:
                # 只报数量不报字段名 —— 与 coordinator 侧一致，字段名本身可能带信息
                raise PolicyDenied(
                    RuleId.WL_FIELD_NOT_ALLOWED,
                    f"local boundary: {len(stray)} field(s) outside allow-list",
                )

            # -- ⑤ 自由文本长度 --
            for fname in cap.freetext_fields:
                value = _get_field(artifact.data, fname)
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise PolicyDenied(
                        RuleId.WL_FREETEXT_NOT_ALLOWED,
                        f"local boundary: {fname} must be a string",
                    )
                size = len(value.encode("utf-8"))
                if size > MAX_FREETEXT_BYTES:
                    raise PolicyDenied(
                        RuleId.WL_FREETEXT_NOT_ALLOWED,
                        f"local boundary: {fname} size={size}",
                    )

        # -- ⑥ 语义：产出永远是数据 --
        prov = dict(artifact.provenance)
        prov.setdefault("local_boundary", "passed")
        return replace(artifact, provenance=prov)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _capability(self, task_type: str) -> Capability | None:
        if self._card is None:
            return None
        cap = self._card.capability(task_type)
        if cap is None:
            # 自己没声明这个能力却在产出 —— 说明实现在做角色卡没承认的事
            raise PolicyDenied(
                RuleId.WL_TYPE_NOT_REGISTERED,
                f"local boundary: {task_type} not declared in role card",
            )
        return cap

    def _ceiling(self, task_type: str, cap: Capability | None) -> Level:
        """本次产出的等级上限。

        有角色卡：``min(卡片数据等级上限, 能力声明等级)``。
        降级模式：只能取 ``levels`` 里为该类型声明的等级；
        连它也没有时退到最保守的 ``metadata`` —— **未知一律最严**。
        """
        if cap is not None and self._card is not None:
            # ⚠ 原来是 min(constraints.max_response_level, cap.level)。
            #   constraints 已删（Step 5c），节点侧的等级只剩 self_limits。
            return min(self._card.effective_max_level(), cap.level)
        return self._levels.get(task_type, Level.METADATA)


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


__all__ = ["LocalBoundary", "ResponseBoundary"]
