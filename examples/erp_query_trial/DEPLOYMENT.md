# 同一 Wi-Fi 内访问 DB-GPT

当前主机 Wi-Fi IPv4 地址为 `192.168.3.11`，访问地址：

```text
http://192.168.3.11:5670
```

这是内网试用部署，所有访问者使用当前共享权限；不具备正式员工账号及权限隔离。
仅在可信的 Wi-Fi 中试用，不设置路由器端口转发，不对公网发布。

## 放行访问

确保 DB-GPT 已启动，然后在管理员 PowerShell 中运行：

```powershell
Set-Location 'D:\code\DB-ERP'
& .\examples\erp_query_trial\Enable-WifiAccess.ps1
```

脚本只允许当前 WLAN IPv4 子网访问本项目监听进程的 TCP 5670 端口。
它不会关闭防火墙，不会修改 Windows 网络类别。
`-WhatIf` 可预演，不创建规则。执行结果保存在 `.cache/lan-access-status.json`。

其他电脑连接同一 Wi-Fi 后，在浏览器中打开上述地址，选择数据库 `erp_trial_readonly`。
通过第二台电脑实际打开页面并查看数据源，才算完成跨电脑验收。
如果同一 Wi-Fi 仍不能互访，检查路由器是否启用了访客网络或客户端隔离。

## 主机要求

主机要保持开机且 DB-GPT 服务持续运行；休眠、关机或停止服务后，其他电脑无法访问。
Wi-Fi 地址由 DHCP 分配，可能变化；可在路由器中为主机保留地址。
如果主机 IP 变化，重新运行脚本并使用它返回的新 URL。
本次不安装开机自动启动或系统服务。

## 撤销访问

在管理员 PowerShell 中仅禁用本次规则：

```powershell
Disable-NetFirewallRule -Name 'DBGPT-WiFi-Trial-5670'
```

ERP 数据源使用 SQLite `mode=ro`，修改数据库会被拒绝。
自然语言查询仍会调用配置的第三方模型 API；是否外发公司业务数据需另行确认。
