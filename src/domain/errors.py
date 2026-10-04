"""领域错误类型。"""


class ContractError(ValueError):
    """事件信封/负载不符合契约。"""


class RuleViolation(ValueError):
    """命令违反领域规则（冻结、递补、重算边界等）。

    ``problems`` 保存全部违规原因，便于一次反馈给承办方。
    """

    def __init__(self, problems: str | list[str]):
        if isinstance(problems, str):
            problems = [problems]
        self.problems: list[str] = problems
        super().__init__("；".join(problems))
