"""测试包初始化：把测试环境钉死，不受开发者本机 `.env` 影响。

为什么需要：`.env` 里一旦配了 `DEEPSEEK_API_KEY`，`provider=auto` 就会选中远程模型，
于是"期望离线模型回复"的用例（SSE 事件顺序、三后端契约）会随本机配置飘。
测试必须自己对环境负责——默认强制离线模型，远程相关用例在自己的用例里显式覆盖。
"""

import os

# 除非用例自己覆盖，否则一律走离线模型：
# - 不花钱、不受网络影响、回复确定；
# - 需要真模型的用例（tests/test_remote_model.py）会 patch 自己的 env。
os.environ["PROVIDER"] = "heuristic"
