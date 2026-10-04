"""事件信封校验（向后兼容入口）。

完整契约见 :mod:`src.domain.contract`；本模块保留旧函数名
``validate_event``，供早期资料与测试继续使用。
"""

from src.domain.contract import validate_envelope as validate_event

__all__ = ["validate_event"]
