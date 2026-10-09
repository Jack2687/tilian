# Windows 上 UPI 登录请求的 CA 加载失败

用户反馈任务 `10c06a3131c44d46` 在校验登录会话时提示 `upi_proxy_network_error`，但代理检测正常。

只读检查该任务的 50 条任务项：全部已被用户取消，47 条保留了底层错误，错误码全部为 `curl: (77)`，正文为 `error adding trust anchors from locations`。CAfile 指向包含中文目录的项目虚拟环境中的 `certifi/cacert.pem`。这证明该批保存的异常是本地 CA 加载失败，不能据此要求用户更换代理。

文件实际存在且包含 121 张证书。上次重启进程时设置了 `PYTHONUTF8=1`，当前安装的 `curl_cffi 0.16.3` 对 Windows 文件路径使用 `locale.getpreferredencoding(False)` 编码，该设置使其返回 UTF-8，而原生 TLS 库的文件读取仍需要 Windows ANSI 编码。中文 CA 路径由此无法加载。

已在相同 `PYTHONUTF8=1` 环境中用同一台回环 HTTPS 服务、同一份测试 CA 复现：原始 Session 报 `SSLError / curl 77`，使用修复后的 Session 返回 HTTP 200。默认未开启 UTF-8 模式时，原始中文路径能被加载，因此只在默认终端验证会漏掉实际 Worker 的运行条件。

当前库虽提供 `CAINFO_BLOB` 常量，Python `Curl.setopt` 尚未实现其赋值，因此采用原信任包的 ASCII 路径副本。

修复内容：

- 新建 UPI Session 时先采用请求库已经选定的 CA 配置，保留 `REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE` 与默认信任源的既有优先级。
- Windows 下路径含非 ASCII 字符时，Python 读取原 CA 内容并复制到进程专用临时目录，再将该路径设置为 `Session.verify`。副本与原文件字节一致，按内容哈希复用，写入使用临时文件替换。临时目录随进程正常退出清理。
- 主请求客户端和登录会话刷新共用这个 Session 创建方法。没有关闭证书链或主机名验证，也没有修改代理、账号或支付参数。
- curl 77 明确显示本地证书配置错误，CLI 返回 `upi_tls_configuration_error / retryable=false`；Worker 将其视为终态。刷新登录态遇到该错误也会保留错误，不再静默吞掉。

验证包含受控本地 HTTPS 服务：可信 CA 成功，未知 CA 和主机名不匹配仍拒绝。以实际触发问题的 `PYTHONUTF8=1` 环境运行 99 项引擎测试通过，22 项后端测试通过，共 121 项；其中包含显式开启 UTF-8 的子进程握手回归。Worker 已重载，同时移除上次重启人为添加的 `PYTHONUTF8` 覆盖。

没有重放该任务或创建支付订单。本次处理登录阶段的 CA 加载问题，和此前审批通过后的 Stripe 支付设置拒绝属于不同阶段。
