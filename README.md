# XAUUSD 多周期预警助手

双击 `dist/XAUUSD预警助手-v1.0.0.exe` 运行。程序只读取本机 MetaTrader 5 中 `XAUUSD` 的已收盘K线，周期可选择 `M5`、`M15` 或 `H1`，一次只使用所选周期，不混合周期。

## 使用方法

1. 打开 MT5 并登录交易账户，确认“市场报价”中存在准确名称为 `XAUUSD` 的品种。
2. 双击 EXE，填写 SMTP 配置。邮箱通常需要填写“授权码”，不是网页登录密码。
3. 选择行情周期 M5、M15 或 H1，先点“发送测试邮件”，成功后点“开始监控”。窗口保持打开即可。
4. MT5 无法自动发现时，在界面中填写 `terminal64.exe` 的完整路径。

常见配置：QQ 邮箱使用 `smtp.qq.com`、端口 `465`、`SSL`；163 邮箱使用 `smtp.163.com`、端口 `465`、`SSL`。具体设置以邮箱服务商当前说明为准。

配置、去重状态和日志保存在 `%APPDATA%\XAUUSDAlert`。SMTP 密码/授权码通过 Windows DPAPI 加密，只能由当前 Windows 用户解密。每个周期首次启用时分别从该周期最新一根已收盘 K 线建立基准，不补发历史信号；之后重启会延续检查，单波段最多发送一次。

程序会根据实时 tick 自动识别常见的 MT5 经纪商服务器时区偏移，将邮件中的波段和整理段时间统一换算成北京时间。

## 固定策略口径

- 枢轴确认周期 `N=2`，固定不可调。本K线 low 严格低于左右各2根的 low 时为 Pivot Low；high 严格高于左右各2根的 high 时为 Pivot High。
- 按时间排序的相邻 Pivot Low → Pivot High 构成候选上涨波段；相邻 Pivot High → Pivot Low 构成候选下跌波段。
- 界面可调整三项过滤参数：波段包含K线数 `K≥5`；上涨 `(max-min)/min≥R`、下跌 `(max-min)/max≥R`，默认 `R=0.004`；内部最大回撤或反弹不超过总幅度的 `M` 倍，默认 `M=0.5`。
- 上涨内部回撤按“运行中历史最高 high 到其后 low”的最大跌幅计算；下跌内部反弹按“运行中历史最低 low 到其后 high”的最大升幅计算。
- 终点枢轴右侧第2根 K线收盘时正式确认波段；右侧第3根为8根整理段的第1根。第8根整理K线收盘后立即校验，无突破触发条件。
- 上涨波段的 0.382 价格为 `max - (max-min)*0.382`；整理段任一 low 低于该价格则多头结构作废。
- 下跌波段的 0.382 价格为 `min + (max-min)*0.382`；整理段任一 high 高于该价格则空头结构作废。
- 多头止盈为 `max - (max-min)*0.382 + (max-min)`。
- 空头止盈为 `min + (max-min)*0.382 - (max-min)`，向波段低点下方延伸一个完整波段幅度。

程序只负责信号识别和邮件提醒，不会自动下单。

## 源码验证与构建

推荐使用 64 位 Python 3.12。首次构建可执行：

```powershell
.\build.ps1
```

脚本会安装 `requirements.txt` 中锁定的依赖、运行策略测试，然后使用 `XAUUSD预警助手.spec` 生成 `dist/XAUUSD预警助手-v1.0.0.exe`。也可指定 Python 可执行文件：

```powershell
.\build.ps1 -PythonExecutable "C:\Path\To\Python312\python.exe"
```

## 安全说明

仓库和 EXE 不内置邮箱账号、密码或授权码。SMTP 授权码只在运行时由用户输入，并在 `%APPDATA%\XAUUSDAlert` 中使用 Windows DPAPI 加密保存。`.gitignore` 会排除 `.env`、私钥、日志、运行时配置及回测产物。
