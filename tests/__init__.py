# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017

import os as _os

#: 测试期间**绝不**碰真实的 `data_dir/helper.lock`。
#: 坑（自己踩的）：`HelperServer.start()` 会写单实例锁，而默认路径是用户的真实数据目录 ——
#: 边跑测试边开着助手时，测试会把助手的锁覆盖掉、退出时再删掉，等于把护栏偷偷拆了。
#: 锁本身的读写由 tests/test_instance.py 在临时目录里单独测。
_os.environ.setdefault("EASYSUB_NO_LOCK", "1")
