# WUST CampusNet Keeper

武科大校园网 Windows 自动认证工具，处理 **Wi-Fi 仍然连接、但网页认证过期** 的情况。它以学校门户的认证状态接口为准；不会因为某个外网检测站点暂时不通就重新提交账号密码，也不会重连网卡。

[下载最新 Windows 版本](https://github.com/zerosignal666/WUST-CampusNet-Keeper/releases/latest)

## 使用

1. 在 Windows 10/11 下载并双击 Release 中的 `WUST-CampusNet-Keeper.exe`。首次运行填入学号、认证密码。
2. 连接校园 Wi-Fi 后点击“自动获取”，程序会从学校门户的 `/api/config` 读取当前 `nasId`。如果获取失败，在浏览器打开 `http://59.68.177.9/`，将最终认证页地址粘贴到“网关 ID 或页面地址”一栏，再点击保存。程序只保存其中的 `nasId` 数字；也可以直接填写该数字。不要分享包含学号、IP 等信息的完整地址。
3. 按需勾选“登录 Windows 后自动启动”，点击“保存并开始监控”。以后启动时会自动读取配置并监控。
4. “立即检测并尝试认证”会马上查询门户状态；若已下线，将使用保存的学号和密码尝试登录。门户已在线时不会重复登录。关闭窗口会停止本次监控；开机启动设置仍会保留，取消勾选并保存即可关闭。

配置与轮转日志位于 `%LOCALAPPDATA%\WUSTWifiKeeper`。密码由 Windows DPAPI 加密，绑定当前 Windows 用户；配置文件复制到其他电脑或账户无法直接使用。日志不会记录密码。

## 判定与重试

- 每 20 秒读取学校的 `/api/account/status`。门户显示在线时，会核对当前账号是否与配置一致；若外网不通，会提示出口或运营商问题，不重复登录。
- 门户连续两次显示未认证后，从 `/api/r/{nasId}` 确认当前设备属于校园内网，再提交登录。登录接口返回 `code=0`、返回的在线学号与所填学号一致，且门户状态接口再次确认在线后，才显示“门户确认认证成功”。外网是否恢复会单独显示。
- 学校接口返回 `code=1`（认证失败）或 `code=2`（需要验证码）时暂停自动重试。其他失败按 30、60、120、300 秒逐步延长重试间隔。门户不可达或无法确认校园内网身份时，不发送密码。
- 认证前重新尝试从校园网门户读取 `nasId`，以适应不同校区；读取失败时使用已保存的值。校园网地址会绕过系统 HTTP 代理直连。

## 注意

学校页面和登录接口目前使用 **HTTP**。DPAPI 只保护电脑上的配置文件，无法加密发往学校服务器的认证请求；请仅在可信的校园网络使用，并向学校咨询是否提供 HTTPS 接口。

本版没有托盘功能，也不会自动重连物理 Wi-Fi；它专门处理网页认证过期。已有使用者在手动下线后验证了正确账号可通过“立即检测并尝试认证”恢复登录；自然过期后的长期运行仍需继续观察。学校接口的 `code=1` 表示认证失败，但程序暂时无法进一步可靠区分密码错误、欠费等具体原因。旧版配置会自动读取，先前保存的 Wi-Fi 名称会被忽略。

**保存配置不等于验证密码。** 如果门户已显示账号在线，程序不会为了测试新输入的密码而主动登出；界面会说明当前账号是否匹配，以及密码尚未重新验证。等认证自然过期后，程序才会提交并核对这些凭据。

## 判定依据

- [武科大认证页](http://59.68.177.9/tpl/wust_yys/login_account.html)的脚本使用 `/api/account/status` 判断在线状态，登录接口的 `code=0/1/2` 分别对应成功、失败、需要验证码。
- [微软的 Windows Wi-Fi 权限说明](https://learn.microsoft.com/en-us/windows/win32/nativewifi/wi-fi-access-location-changes)说明读取当前 Wi-Fi 连接的接口可能因位置权限返回访问拒绝，因此本程序不再把 SSID 读取结果作为认证前提。
- [微软 NCSI 说明](https://learn.microsoft.com/en-us/windows-server/networking/ncsi/ncsi-frequently-asked-questions)说明单个外网探针失败可能是门户、代理或网络环境造成，不能单独判定学校认证已过期。

## 源码运行与构建

需要 Windows 和 Python 3.10 或更新版本；运行程序本身无需第三方 Python 包。

```powershell
pyw -3 wifi_keeper.pyw
```

打包需要 PyInstaller：

```powershell
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name WUST-CampusNet-Keeper wifi_keeper.pyw
```

离线测试：

```powershell
py -3 -m unittest -v test_keeper_core.py
```

## 致谢与许可

感谢 [Zyakusen/WUST_wifi_keeper](https://github.com/Zyakusen/WUST_wifi_keeper) 提供最初的项目启发。本项目依据武科大门户接口重新实现了认证状态判断、登录结果核对和 Windows 端交互。

代码采用 [MIT License](LICENSE)。
