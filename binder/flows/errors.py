"""绑号流程的业务异常。"""

from __future__ import annotations

# 上报进度时用于识别"账号信息有误"的前缀。
# 界面靠这个前缀给账号打上"信息有误"标记，所以两边必须一致。
ACCOUNT_INFO_ERROR_PREFIX = "账号信息有误"


class AccountInfoError(RuntimeError):
    """账号资料本身不正确（账密或 2FA 码错），重试不会变好。

    这类失败和普通的运行时失败要区别对待：流程立刻停下来，界面把该
    账号标记为"信息有误"并写回 CSV，后续轮次自动跳过它。

    标记只进不出：程序可以加标记，但永远不会自动取消 —— 清标记必须
    由人工核对账号资料后手动操作，否则一次网络抖动的误判就会把"待
    核对"悄悄变成"没问题"。
    """

    def __init__(self, field: str, detail: str = "") -> None:
        self.field = field
        self.detail = detail
        super().__init__(self.describe())

    def describe(self) -> str:
        if self.detail:
            return f"{self.status}（{self.detail}）"
        return self.status

    @property
    def status(self) -> str:
        """上报给界面的进度文案，同时是自动标记的触发条件。"""
        return f"{ACCOUNT_INFO_ERROR_PREFIX}：{self.field}"